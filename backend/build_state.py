"""Server-side enforcement of the Eva-build spec.

The LLM (Gemini Live cascade) is unreliable across several spec-relevant
axes — parallel completion streams, brand-name stylization, primer-
without-commit, drift past the 90s wrap target, skipping side-effect
tool calls. We've tightened prompts repeatedly; the model still does
these things some fraction of the time. The conclusion of the audit
(see chat history) is that we stop relying on the model to follow the
flow and instead WATCH the flow server-side, then take over when the
model fails to advance.

This module is the watchful state machine. It does NOT speak to the
operator and does NOT generate audio — it observes transcript-level
events from the bridge and decides:

  • What build PHASE we're in (OPEN / GATHER / NAME / OFFER / COMMITTING
    / PRIMER / DONE)
  • Whether a forced commit is warranted RIGHT NOW
  • What "next action" the prompt should hint at

The bridge ([gemini_bridge.py]) instantiates one BuildMonitor per builder
WS session, calls `observe_user_turn` / `observe_model_turn` from the
existing on_turn_complete_hook, and consults `should_force_save` after
each model turn. If true, the bridge force-fires save_agent with args
composed from the build_session row.

Detection heuristics are intentionally simple regex/keyword matches —
the goal is high recall on YES-tokens and OFFER patterns, not nuance.
False positives can only force a save that ALSO ships sensible
defaults, which is exactly what we want anyway.
"""
from __future__ import annotations

import enum
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional


log = logging.getLogger("eva.build_state")


class Phase(str, enum.Enum):
    """Discrete phases of the Eva-build flow. Stored as strings (for
    log readability + serialisation); progress is one-way (no
    backsliding even on reconnect — once we've heard the agent name,
    we don't pretend we haven't)."""
    OPEN        = "open"        # before any operator audio arrives
    GATHER      = "gather"      # operator describing the business
    NAME        = "name"        # Eva proposing the agent name
    OFFER       = "offer"       # wrap-up offer made; awaiting yes
    COMMITTING  = "committing"  # save_agent in flight (or about to be)
    PRIMER      = "primer"      # dashboard primer being delivered
    DONE        = "done"        # reveal card up; pump exiting


# Phase ordering for "never go backwards". The bridge uses this to
# guard transitions if a reconnect-replay confuses things.
_PHASE_ORDER = {p: i for i, p in enumerate([
    Phase.OPEN, Phase.GATHER, Phase.NAME, Phase.OFFER,
    Phase.COMMITTING, Phase.PRIMER, Phase.DONE,
])}


# Required slots to legitimately enter each phase. The state machine
# uses these to BLOCK a too-early transition and to MOTIVATE the
# state-block prompt ("you need primary_job next").
_PHASE_REQUIRED_SLOTS: dict[Phase, list[str]] = {
    Phase.OPEN:        [],
    Phase.GATHER:      [],
    Phase.NAME:        ["sector_kind", "primary_job"],
    Phase.OFFER:       ["sector_kind", "primary_job", "agent_name"],
    Phase.COMMITTING:  ["sector_kind", "agent_name"],  # business_name optional
    Phase.PRIMER:      [],
    Phase.DONE:        [],
}


# Affirmative-token detection. The cascade Live model's transcription
# normalizes spoken yes-equivalents reasonably consistently — we
# pattern-match against the lowered, punctuation-stripped string.
# We cover English variants + the high-signal Hindi/Hinglish tokens
# common in en-IN locale (haan, ji, theek hai, accha, kar do). False
# positives are tolerable: this only TRIGGERS a save when Eva has
# also offered + slots are present.
_AFFIRMATIVE_PATTERNS = [
    re.compile(r'\b(yes|yeah|yep|yup|yup\.|sure|okay|ok|alright|kindly|please|absolutely|sounds good|sounds great|do it|go ahead|let\'s|lets do|that works|works for me|right|correct|exactly)\b'),
    re.compile(r'\b(haan|han|ji|theek hai|thik hai|accha|achha|kar do|haan kar do|chalega)\b'),
]


# Eva's wrap-up offer detection. The prompt specifies the offer
# beat as one of: "she's ready", "want a quick hello", "shall I hand
# you over", "want to give her a quick test", etc. We match on the
# distinctive phrasing — these strings shouldn't appear earlier in a
# build, so a match is a strong signal we've entered OFFER phase.
_OFFER_PATTERNS = [
    re.compile(r"want a quick hello", re.IGNORECASE),
    re.compile(r"hand you over", re.IGNORECASE),
    re.compile(r"give her a quick test", re.IGNORECASE),
    re.compile(r"give her a try", re.IGNORECASE),
    re.compile(r"\bshe(?:'s| is) (?:all )?(?:ready|set)", re.IGNORECASE),
    re.compile(r"shall i (?:hand|put|connect)", re.IGNORECASE),
]


# Dashboard-primer detection. If Eva starts the primer beat, we
# should already be COMMITTING. If she starts it WITHOUT save_agent
# having fired, that's spec violation E from the audit — server
# takes over.
_PRIMER_PATTERNS = [
    re.compile(r"before i let you go", re.IGNORECASE),
    re.compile(r"when you (?:land in|open|see) (?:the |her )?dashboard", re.IGNORECASE),
    re.compile(r"in (?:the |her )?dashboard", re.IGNORECASE),
    re.compile(r"overview is (?:the |her )?home", re.IGNORECASE),
]


def is_affirmative(text: str) -> bool:
    """True if the operator's utterance contains a yes-equivalent.
    Punctuation-stripped, lowercased compare. Empty / None → False."""
    if not text:
        return False
    norm = text.strip().lower()
    # Trailing punctuation can attach to keywords ("yeah." / "ok!").
    norm = re.sub(r"[.,!?;:]+", " ", norm)
    return any(p.search(norm) for p in _AFFIRMATIVE_PATTERNS)


def looks_like_offer(text: str) -> bool:
    """True if Eva's utterance contains a wrap-up offer pattern."""
    if not text:
        return False
    return any(p.search(text) for p in _OFFER_PATTERNS)


def looks_like_primer(text: str) -> bool:
    """True if Eva's utterance is delivering the dashboard primer."""
    if not text:
        return False
    return any(p.search(text) for p in _PRIMER_PATTERNS)


def _has_all(slots: dict[str, Any], keys: list[str]) -> bool:
    """All listed slot keys present + non-empty."""
    return all((slots.get(k) or "").strip() if isinstance(slots.get(k), str)
               else bool(slots.get(k))
               for k in keys)


def _pick_suggestion(suggestions: list[str], sid: str) -> tuple[str, list[str]]:
    """Pick ONE suggestion as the primary proposal (seeded by build_sid)
    and return the rest as alternates.

    Same sid → same primary across reconnects (operator hears a
    consistent name even if the WS bounces). Different sids → different
    primaries (no two operators get suggested the same name back-to-back,
    which is what made every agent end up named Maya). Plain hash-mod —
    we don't need cryptographic randomness here.
    """
    if not suggestions:
        return "", []
    if len(suggestions) == 1:
        return suggestions[0], []
    # Stable, deterministic seed: hash the sid (a uuid hex) → int → mod.
    # `hash(sid)` would change per-process due to PYTHONHASHSEED; use a
    # plain sum-of-codepoints for stability.
    seed = sum(ord(c) for c in (sid or "fallback"))
    idx = seed % len(suggestions)
    primary = suggestions[idx]
    alternates = [s for i, s in enumerate(suggestions) if i != idx]
    return primary, alternates


@dataclass
class BuildMonitor:
    """Per-WS-session observer. The bridge calls observe_* on every
    relevant event; the bridge consults `should_force_save` after
    each model turn_complete.

    NOT thread-safe in the strict sense; relies on the bridge running
    a single asyncio task for the receive pump. Mutations are
    sequential."""
    sid: str
    started_at_monotonic: float = 0.0   # set externally on first turn
    phase: Phase = Phase.OPEN
    slots: dict[str, Any] = field(default_factory=dict)  # mirror of build_session row
    user_turns: int = 0
    model_turns: int = 0

    # Trip-wires recorded by turn-index so the force-commit logic
    # knows how long ago each happened. NOTE: `user_affirmed_at_model_turn`
    # stores the MODEL turn count at the time the user affirmed, NOT
    # the user turn count — this lets us check "did at least one model
    # turn pass after the yes?" by comparing against the current
    # model_turns counter.
    eva_offered_at_turn: Optional[int] = None
    user_affirmed_at_model_turn: Optional[int] = None
    save_agent_fired_at_turn: Optional[int] = None
    primer_started_at_turn: Optional[int] = None

    # Set by the bridge when it has decided to force-commit (so we
    # don't double-fire if the watchdog and the post-yes path both
    # decide to force on the same turn).
    force_commit_armed: bool = False

    # ─── Template-driven interview state (Phase: deterministic Eva) ──
    # Set when triage resolves a (industry × locale × city) tuple to
    # a YAML template from backend/build_templates/. When non-None,
    # Eva runs the deterministic interview flow: ask the template's
    # questions in order, record answers via record_template_answer,
    # compose save_agent from the template at the end.
    #
    # When None (no template matched), the existing probabilistic
    # flow runs unchanged. Both paths coexist; templates take over
    # only when triage hits a known cell.
    template_id: Optional[str] = None
    template_answers: dict[str, Any] = field(default_factory=dict)
    # Last question id we sent to Eva — used so on a reconnect we
    # know which question to repeat if the operator's answer didn't
    # make it through.
    template_last_question_id: Optional[str] = None

    # ── observers (called from on_turn_complete_hook) ────────────

    def observe_user_turn(self, text: str) -> None:
        """Record one operator utterance ending. Updates affirmative
        trip-wire if Eva had previously made the offer."""
        self.user_turns += 1
        if is_affirmative(text) and self.eva_offered_at_turn is not None:
            # Only count this as a build-commit "yes" if it follows
            # Eva's offer — random "yeah" earlier in the conversation
            # shouldn't force a save.
            # Stamp the CURRENT model_turns so should_force_save can
            # check "has a model turn elapsed since the yes?".
            self.user_affirmed_at_model_turn = self.model_turns
            log.info(
                "build_state[%s]: affirmative detected after offer (user_turn=%d, model_turn=%d)",
                self.sid[:18], self.user_turns, self.model_turns,
            )

    def observe_model_turn(self, text: str, save_agent_called: bool) -> None:
        """Record one Eva utterance ending. Detects offer / primer
        patterns and the save_agent trip-wire."""
        self.model_turns += 1
        if save_agent_called and self.save_agent_fired_at_turn is None:
            self.save_agent_fired_at_turn = self.model_turns
            log.info(
                "build_state[%s]: save_agent fired by model (turn %d)",
                self.sid[:18], self.model_turns,
            )
        if self.eva_offered_at_turn is None and looks_like_offer(text):
            self.eva_offered_at_turn = self.model_turns
            log.info(
                "build_state[%s]: offer pattern detected (turn %d)",
                self.sid[:18], self.model_turns,
            )
        if self.primer_started_at_turn is None and looks_like_primer(text):
            self.primer_started_at_turn = self.model_turns
            log.info(
                "build_state[%s]: primer pattern detected (turn %d)",
                self.sid[:18], self.model_turns,
            )

    def update_slots(self, build_session_row: Optional[dict[str, Any]]) -> None:
        """Pull the latest slot state from build_sessions (where Eva's
        note_build_facts AND the extractor write). Called by the
        bridge after each refresh so phase decisions reflect the
        durable truth, not just what the monitor has seen."""
        if not build_session_row:
            return
        extras = build_session_row.get("extras") if isinstance(build_session_row.get("extras"), dict) else {}
        merged = {
            "sector_kind":   build_session_row.get("sector_kind"),
            "business_name": build_session_row.get("business_name"),
            "primary_job":   build_session_row.get("primary_job"),
            "agent_name":    build_session_row.get("agent_name"),
            **(extras or {}),
        }
        # Only OVERWRITE — never blank a slot. Once filled, stays filled.
        for k, v in merged.items():
            if v:
                self.slots[k] = v

        # Template state — picked up from the durable row so a Gemini
        # drop / WS-level reconnect can resume the interview at the
        # right question. Like slots, never blank an already-set
        # template_id (operator can't un-pick an industry mid-flow
        # without an explicit reset).
        tid = build_session_row.get("template_id")
        if tid and not self.template_id:
            self.template_id = tid
        ta = build_session_row.get("template_answers")
        if isinstance(ta, dict) and ta:
            # Merge — answers are append-only within a build session.
            for qid, val in ta.items():
                self.template_answers[qid] = val

    # ── phase decision ────────────────────────────────────────────

    def recompute_phase(self) -> Phase:
        """Derive the current phase from the slots + trip-wires.
        Monotonic: never goes backwards even if the LLM "forgets" or
        a transcript-replay confuses things."""
        current_idx = _PHASE_ORDER[self.phase]
        candidate = self.phase

        # OPEN → GATHER on first user turn.
        if current_idx <= _PHASE_ORDER[Phase.OPEN] and self.user_turns >= 1:
            candidate = Phase.GATHER

        # GATHER → NAME when sector + job are captured.
        if _PHASE_ORDER[candidate] <= _PHASE_ORDER[Phase.GATHER] and _has_all(
            self.slots, _PHASE_REQUIRED_SLOTS[Phase.NAME]
        ):
            candidate = Phase.NAME

        # NAME → OFFER when agent_name is captured OR Eva makes the offer.
        if _PHASE_ORDER[candidate] <= _PHASE_ORDER[Phase.NAME]:
            if self.eva_offered_at_turn is not None:
                candidate = Phase.OFFER
            elif _has_all(self.slots, _PHASE_REQUIRED_SLOTS[Phase.OFFER]):
                candidate = Phase.OFFER

        # OFFER → COMMITTING when user affirms.
        if _PHASE_ORDER[candidate] == _PHASE_ORDER[Phase.OFFER] and self.user_affirmed_at_model_turn is not None:
            candidate = Phase.COMMITTING

        # COMMITTING → PRIMER once save_agent has fired.
        if _PHASE_ORDER[candidate] == _PHASE_ORDER[Phase.COMMITTING] and self.save_agent_fired_at_turn is not None:
            candidate = Phase.PRIMER

        # Monotonic check — never roll backwards.
        if _PHASE_ORDER[candidate] >= current_idx:
            if candidate != self.phase:
                log.info(
                    "build_state[%s]: phase %s → %s (slots=%s, offer@%s, yes@%s, save@%s)",
                    self.sid[:18], self.phase.value, candidate.value,
                    sorted(k for k, v in self.slots.items() if v),
                    self.eva_offered_at_turn, self.user_affirmed_at_model_turn,
                    self.save_agent_fired_at_turn,
                )
            self.phase = candidate
        return self.phase

    # ── enforcement decisions ─────────────────────────────────────

    def should_force_save(
        self, *, now_monotonic: float, watchdog_deadline_s: float = 120.0,
    ) -> tuple[bool, str]:
        """Should the bridge force-fire save_agent right now?

        Returns (yes, reason_for_log).

        Two trigger paths:

          A) Operator affirmed AFTER the offer, AND Eva hasn't called
             save_agent within ONE model turn since the affirmation.
             This is the common case: Eva slips into the primer
             without committing first.

          B) Wall-clock >= watchdog_deadline_s AND we have at least
             the minimum slots to compose a useful save_agent call.
             This is the catastrophe-prevention path for stalled
             builds.

        Returns (False, "") if no condition met OR if force_commit_armed
        is already set (caller has already decided to commit).
        """
        if self.force_commit_armed:
            return False, ""
        if self.save_agent_fired_at_turn is not None:
            return False, ""

        # Path A — operator said yes, Eva didn't follow through.
        # `user_affirmed_at_model_turn` captures model_turns AT the
        # affirmation. So model_turns > that means Eva has had at
        # least one model turn since then to call save_agent. If she
        # didn't, we take over.
        if self.user_affirmed_at_model_turn is not None:
            if self.model_turns > self.user_affirmed_at_model_turn:
                if _has_all(self.slots, _PHASE_REQUIRED_SLOTS[Phase.COMMITTING]):
                    return True, "operator affirmed but save_agent never fired"
                # Slots not minimally complete — log but don't force.
                return False, ""

        # Path B — wall-clock past the deadline.
        if self.started_at_monotonic > 0 and now_monotonic > 0:
            elapsed = now_monotonic - self.started_at_monotonic
            if elapsed >= watchdog_deadline_s and _has_all(self.slots, _PHASE_REQUIRED_SLOTS[Phase.COMMITTING]):
                return True, f"watchdog deadline reached at {elapsed:.0f}s"

        return False, ""

    # ── prompt-block rendering ────────────────────────────────────

    # ── template-driven helpers ───────────────────────────────────

    def template_progress(self) -> tuple[int, int]:
        """Return (answered_count, total_questions) for the current
        template. (0, 0) if no template is active."""
        if not self.template_id:
            return (0, 0)
        try:
            from . import build_templates as _bt
            t = _bt.get_template(self.template_id)
            if not t:
                return (0, 0)
            total = len(t.get("questions") or [])
            answered = sum(1 for q in t.get("questions") or []
                           if q.get("id") in self.template_answers)
            return (answered, total)
        except Exception:  # noqa: BLE001
            return (0, 0)

    def render_state_block(self) -> str:
        """A compact, structured view of the current build state for
        Eva's system prompt. Replaces ambiguity with a literal status
        line: 'You're in phase X, slot Y needs to be captured, save_agent
        is/isn't yet allowed, next action is Z.' Combined with the
        existing facts block, gives Eva a deterministic decision
        surface."""
        next_action = {
            Phase.OPEN:       "Greet ONCE and ask what kind of business this is.",
            Phase.GATHER:     "Capture sector_kind and primary_job in the next turn. If the operator names a business, capture that LITERALLY (no respelling) via note_build_facts.",
            Phase.NAME:       "Propose an agent_name + a greeting line in one breath. Save the name via note_build_facts the moment you propose it.",
            Phase.OFFER:      "Make ONE wrap-up offer: 'she's ready — want a quick hello?'. When the operator confirms, fire save_agent IMMEDIATELY in the same turn.",
            Phase.COMMITTING: "Fire save_agent NOW. Then deliver the dashboard primer. Do NOT ask for more confirmation.",
            Phase.PRIMER:     "Deliver the 10-15s dashboard primer, then STOP. The reveal card will appear on its own.",
            Phase.DONE:       "Conversation is closing. Say one short closing line and stop.",
        }.get(self.phase, "")

        save_allowed = self.phase in (Phase.OFFER, Phase.COMMITTING, Phase.PRIMER)
        save_required = self.phase == Phase.COMMITTING

        filled = sorted(k for k, v in self.slots.items() if v)
        required_next = [
            s for s in _PHASE_REQUIRED_SLOTS.get(
                Phase(_PHASE_ORDER_REVERSE[min(_PHASE_ORDER[self.phase] + 1, _PHASE_ORDER[Phase.DONE])]),
                [],
            )
            if not self.slots.get(s)
        ]

        save_line = (
            "REQUIRED THIS TURN" if save_required
            else "ALLOWED" if save_allowed
            else "skip until "
                 + ", ".join(required_next) if required_next
                 else "skip until you have a sector + agent name"
        )

        # If a template is active, the deterministic interview takes
        # over from probabilistic next_action prose. Eva's prompt now
        # says "ask the literal question below; record the answer via
        # record_template_answer; ask the next." This is the load-bearing
        # block — once present, Eva should NOT improvise.
        template_block = ""
        if self.template_id:
            try:
                from . import build_templates as _bt
                t = _bt.get_template(self.template_id)
                if t:
                    next_q = _bt.next_unanswered_question(t, self.template_answers)
                    answered, total = self.template_progress()
                    if next_q:
                        opts_line = ""
                        if next_q.get("type") == "enum" and next_q.get("options"):
                            opts_line = f"\n  Options        : {', '.join(str(o) for o in next_q['options'])}"
                        hint_line = ""
                        if next_q.get("hint"):
                            hint_line = f"\n  Hint           : {next_q['hint']}"
                        # Suggestion rendering: pick ONE primary suggestion
                        # (seeded by build_sid so the same operator gets
                        # the same name on every reconnect, but different
                        # operators get different names). Show alternates
                        # as a separate line. This is the fix for the
                        # "every agent becomes Maya" bug — the model
                        # always picked the first item in a flat list.
                        sug_line = ""
                        propose_line = ""
                        primary = None
                        sugs = next_q.get("suggestions") or []
                        if sugs:
                            primary, alternates = _pick_suggestion(sugs, self.sid)
                            propose_line = f"\n  PROPOSE NAME   : {primary}"
                            if alternates:
                                sug_line = f"\n  Alternates     : {', '.join(alternates)}"
                        # Per-question response guidance. For the
                        # agent_name question specifically we want Eva
                        # to PROPOSE the seeded name (not list all
                        # suggestions and let the model anchor on
                        # whichever appears first). Other questions get
                        # the generic guidance.
                        if next_q.get("id") == "agent_name" and primary:
                            respond_block = (
                                "  How to respond : Propose the PROPOSE NAME above —\n"
                                f"                   'I'll call her {primary} — work?'. Do NOT\n"
                                "                   list the alternates aloud. Only fall back\n"
                                "                   to one of them if the operator rejects this\n"
                                f"                   name. When confirmed (yes / sure / haan),\n"
                                "                   call record_template_answer with\n"
                                f"                   question_id=agent_name and value=\"{primary}\"\n"
                                "                   (or whatever the operator counter-proposed).\n"
                            )
                        else:
                            respond_block = (
                                "  How to respond : Read the NEXT QUESTION verbatim (or a near-\n"
                                "                   paraphrase). When the operator answers,\n"
                                "                   call record_template_answer with\n"
                                "                   question_id=" + next_q['id'] + " and the value.\n"
                                "                   Then check this block again — the next\n"
                                "                   question will appear.\n"
                            )
                        template_block = (
                            "---------------------------------------------------------\n"
                            f"  TEMPLATE       : {self.template_id}\n"
                            f"  Progress       : {answered} of {total} questions answered\n"
                            f"  NEXT QUESTION  : {next_q['prompt']}\n"
                            f"  Question id    : {next_q['id']}\n"
                            f"  Answer type    : {next_q['type']}\n"
                            f"  Required       : {bool(next_q.get('required'))}{opts_line}{hint_line}{propose_line}{sug_line}\n"
                            f"{respond_block}"
                        )
                    else:
                        template_block = (
                            "---------------------------------------------------------\n"
                            f"  TEMPLATE       : {self.template_id}\n"
                            f"  Progress       : ALL {total} questions answered ✓\n"
                            "  How to respond : Make the wrap-up offer in one short line\n"
                            "                   ('she's ready — want a quick hello?'); the\n"
                            "                   operator's yes → save_agent immediately.\n"
                        )
            except Exception:  # noqa: BLE001
                pass

        return (
            "=========================================================\n"
            "BUILD STATE (server-authoritative; do not contradict)\n"
            "---------------------------------------------------------\n"
            f"  Phase           : {self.phase.value.upper()}\n"
            f"  Filled slots    : {', '.join(filled) if filled else '(none yet)'}\n"
            f"  Needed next     : {', '.join(required_next) if required_next else '(none)'}\n"
            f"  save_agent      : {save_line}\n"
            f"  Next action     : {next_action}\n"
            f"{template_block}"
            "\n"
            "Rules baked in here override prose elsewhere in the prompt:\n"
            "  • Wait until your needed-next slots are captured before\n"
            "    calling save_agent. (The server WILL accept a premature\n"
            "    call and fill gaps with sector defaults — but that\n"
            "    ships a half-built agent the operator has to fix in\n"
            "    the dashboard. Don't.)\n"
            "  • If save_agent is REQUIRED THIS TURN: call it. If you\n"
            "    don't, the SERVER will fire it on your behalf with the\n"
            "    facts already collected. You don't get to skip.\n"
            "  • Filled slots are FROZEN ground truth. Never re-ask.\n"
            "  • If the operator corrects a filled slot, call\n"
            "    note_build_facts with the correction and move on.\n"
            "  • Calling save_agent twice in one build is wasted — the\n"
            "    server deduplicates. If you've already saved this\n"
            "    session, deliver the dashboard primer and stop.\n"
            "=========================================================\n\n"
        )


# Reverse mapping used by render_state_block.
_PHASE_ORDER_REVERSE = {i: p for p, i in _PHASE_ORDER.items()}


# ─── force-commit helper (used by 3 paths) ───────────────────────────────
#
# Path A: in-session enforcement (`run_session` calls `on_save_agent` with
#         forced args when the operator affirmed but Eva didn't follow
#         through, or the 120s watchdog tripped).
# Path B: WS-close auto-commit (operator closed the tab / hung up after
#         giving enough info but before save_agent fired).
# Path C: REST /api/build-sessions/<sid>/finalize (operator clicked the
#         fallback button on the dashboard recovery banner).
#
# Paths B and C run OUTSIDE the run_session closure (no `handoff`, no
# `on_save_agent`, no WS to send events on) — they need a standalone
# helper that does the same composition + commit logic. This function
# is that helper. Path A still goes through `on_save_agent` directly
# (so the handoff + reveal flow works); the args-composition logic
# below is the standalone equivalent for B and C.


_KNOWN_SECTORS: set[str] = set()


def _ensure_sector_set_loaded() -> set[str]:
    """Lazy-load the SECTORS enum from presets. Inlined to avoid a
    top-level import cycle (presets is already imported by
    gemini_bridge, and build_state is imported BY gemini_bridge — so
    a top-level `from .presets import SECTORS` would be a fine import
    today, but we lazy-load to keep this module's dep surface tiny)."""
    global _KNOWN_SECTORS
    if not _KNOWN_SECTORS:
        from . import presets
        _KNOWN_SECTORS = {s["id"] for s in presets.SECTORS}
    return _KNOWN_SECTORS


def _normalize_sector(raw: Optional[str]) -> str:
    """Map a free-text sector_kind ("dental clinic", "homeopathic
    pharmacy", "automotive showroom") to a canonical SECTORS enum id.
    Falls back to 'generic' for unmatched inputs."""
    if not raw:
        return "generic"
    sectors = _ensure_sector_set_loaded()
    s = raw.strip().lower()
    if s in sectors:
        return s
    # Token-overlap matching — first sector whose id appears as a token
    # in the operator's phrasing wins. "homeopathic pharmacy" lacks an
    # exact match → 'generic'; "dental clinic" matches 'dental'.
    tokens = set(re.split(r"[\s/&_-]+", s))
    for sid in sectors:
        if sid in tokens or sid.replace("_", " ") in s:
            return sid
    return "generic"


def _compose_minimal_args_from_row(row: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Build a minimal save_agent args dict from the build_session row.
    Returns None if the row lacks the minimum (sector_kind +
    agent_name). All the rest of the dashboard fields get filled by
    `silent_defaults.merge_into_save_args` downstream + the per-slot
    backfill in `on_save_agent`'s helper. The function intentionally
    DOES NOT fabricate a business_name when the operator didn't say
    one — leaves it blank so the dashboard prompts for it later."""
    if not row:
        return None
    sector_raw = row.get("sector_kind")
    agent_name = row.get("agent_name")
    if not (sector_raw and agent_name):
        return None

    extras = row.get("extras") if isinstance(row.get("extras"), dict) else {}

    business_name = (row.get("business_name") or "").strip()
    primary_job = (row.get("primary_job") or "").strip()

    # Compose a starter system_prompt from what we know. Sector defaults
    # + persona overlays at runtime will round this out. Intentionally
    # short — the model's full agent prompt is layered on at call time
    # by `_agent_system_prompt`.
    sp_who = (
        f"You are {agent_name}, the receptionist for {business_name}."
        if business_name
        else f"You are {agent_name}, the receptionist."
    )
    sp_job = (
        f"Most callers want to {primary_job}."
        if primary_job
        else "Help callers with whatever they're calling about."
    )
    sp_close = (
        "Be warm, acknowledge before acting, confirm critical details, "
        "and close with 'anything else I can help with?'."
    )
    sp_lit = (
        f" Use the business's name as written: '{business_name}' — "
        "never re-spell it." if business_name else ""
    )
    system_prompt = " ".join([sp_who, sp_job, sp_lit.strip(), sp_close]).strip()

    # Greeting: prefer extractor's hint, else compose from name+brand.
    greeting_hint = (extras.get("greeting_hint") or "").strip() if isinstance(extras, dict) else ""
    if greeting_hint:
        greeting = greeting_hint
    elif business_name:
        greeting = f"Hello, this is {agent_name} at {business_name} — how can I help?"
    else:
        greeting = f"Hello, this is {agent_name} — how can I help?"

    # Persona: short one-liner if extractor didn't provide.
    persona_hint = (extras.get("persona_hint") or "").strip() if isinstance(extras, dict) else ""
    persona = persona_hint or f"Warm, efficient receptionist."

    # Locale: extractor hint > region default > en-US.
    locale = (extras.get("locale_hint") or "").strip() if isinstance(extras, dict) else ""
    if not locale:
        locale = "en-US"

    # Variables: fold every extracted slot into the standard keys.
    variables: dict[str, Any] = {}
    if business_name:
        variables["business_name"] = business_name
    if isinstance(extras, dict):
        for key_src, key_dst in (
            ("country", "country"),
            ("city", "city"),
            ("address", "address"),
            ("hours", "hours"),
            ("services", "services"),
            ("offers", "offers"),
            ("email", "email"),
            ("website", "website"),
            ("escalation_phone", "phone"),
            ("notification_phone", "notification_phone"),
            ("language", "languages"),
        ):
            v = extras.get(key_src)
            if isinstance(v, str) and v.strip():
                variables[key_dst] = v.strip()

    args: dict[str, Any] = {
        "name": agent_name,
        "sector": _normalize_sector(sector_raw),
        "locale": locale,
        # Voice is a Gemini-voice-id enum value. We leave it blank here
        # and let silent_defaults pick the region/sector default rather
        # than risk an invalid enum.
        "voice": "Aoede",
        "system_prompt": system_prompt,
        "greeting": greeting,
        "persona": persona,
        "variables": variables,
    }
    return args


async def force_commit_build_session(
    *, user_id: Optional[int], sid: str,
    require_minimum: bool = True,
) -> Optional[dict[str, Any]]:
    """Commit the in-progress build_session for `(user_id, sid)` into a
    real saved agent. Returns the saved agent dict, or None if the
    row doesn't exist, lacks minimum slots, or is already committed.

    Idempotent: if the row is already `status='committed'`, looks up
    the linked agent via `committed_agent_id` and returns it. Race-
    safe-ish: two concurrent finalize calls might both insert; the
    UNIQUE constraint on (org_id, slug) + create_agent's slug retry
    loop handles the collision by suffixing -2, -3.

    Used by the WS-close path and the REST /finalize endpoint.
    """
    from . import db, silent_defaults

    row = await db.get_build_session(user_id=user_id, sid=sid)
    if not row:
        # Either doesn't exist or already committed/abandoned. For
        # already-committed: caller can hit /state to find the
        # committed_agent_id.
        return None

    args = _compose_minimal_args_from_row(row)
    if not args and require_minimum:
        return None
    if not args:
        # Defaults-only fallback: create a placeholder agent with the
        # canonical "Agent" name + generic sector. Only enabled when
        # the caller explicitly opts out of the minimum check.
        args = {
            "name": "Agent",
            "sector": "generic",
            "locale": "en-US",
            "voice": "Aoede",
            "system_prompt": "Friendly receptionist.",
            "greeting": "Hello, how can I help?",
            "persona": "Warm receptionist.",
            "variables": {},
        }

    # Layer in silent defaults (per-sector outcomes, VAD, policy, etc.).
    args = silent_defaults.merge_into_save_args(args)

    try:
        saved = await db.create_agent(args, user_id=user_id)
    except Exception:  # noqa: BLE001
        log.exception("force_commit_build_session: create_agent failed sid=%s", sid[:18])
        return None

    try:
        await db.mark_build_committed(user_id=user_id, sid=sid, agent_id=saved["id"])
    except Exception as e:  # noqa: BLE001
        log.warning("force_commit_build_session: mark_committed failed: %s", e)

    log.info(
        "force_commit_build_session: committed sid=%s agent_id=%s name=%s",
        sid[:18], saved.get("id"), saved.get("name"),
    )
    return saved
