"""Text-first chat bridge for the landing-page build composer.

WHY THIS EXISTS (read before changing anything):
The original chat path went through `gemini_bridge.run_session` against
the Gemini *Live* API (cascade audio-first model). Live worked for the
voice flow but was structurally wrong for a text chat product:

  • Live ends its stream after every model turn, forcing the server to
    transparently reconnect. User messages sent during that 200-500 ms
    reconnect window got dropped.
  • The cascade Live model picks tools loosely — observed multiple times
    that an answer like "Pawsome Tails" to the business-name question
    triggered `note_build_facts(agent_name=…)` instead of the intended
    `record_template_answer(question_id="business_name", value=…)`.
  • Live frequently returns silent tool-call-only turns (no audio, no
    transcript) — meaning the operator's chat shows nothing while the
    server silently fires a tool.
  • `response_modalities=["TEXT"]` is rejected by the Live cascade
    model — we were forced to AUDIO mode where the server synthesises
    voice we throw away client-side. Waste + latency.

This bridge replaces the Live path *only* for chat mode (mode=text on
the WS query string). It uses the regular streaming API
(`client.aio.chats.create` + `send_message_stream`) against
`gemini-2.5-flash`, with **manual** function calling so we can:

  • Run our existing tool handlers (select_build_template,
    record_template_answer, note_build_facts, save_agent) and emit
    the same client-side events (`transcript`, `turn_complete`,
    `template_question`, `agent_saved`, `build_complete`).
  • Keep the conversation history server-side across turns instead of
    constantly reconnecting.
  • Stream tokens to the client so chat bubbles appear progressively.
  • Loop the function-call ↔ function-response cycle within a single
    user turn until the model is fully done.

The WS protocol is IDENTICAL to gemini_bridge — the client's
LandingChatView consumes the same events. No frontend changes.

Voice mode is unchanged — `/ws/session` without `mode=text` still
routes to `gemini_bridge.run_session`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re as _re
import time as _time_mod
from typing import Any, Optional

from fastapi import WebSocket, WebSocketDisconnect
from google.genai import types

from . import db
from . import build_state as _bs
from . import build_templates as _bt
from . import gemini_bridge as _gb  # reuse: prompt builder, tool decls, _client, _Handoff
from . import silent_defaults

log = logging.getLogger("eva.chat")

# Non-Live chat model. Cheap, fast, supports function calling reliably
# and (unlike the cascade Live model) treats tool choices deterministically.
CHAT_MODEL = "gemini-2.5-flash"

# "Best" model for the catch-all (Any-industry) path — used to compose a
# bespoke agent for any use case imaginable. Falls back to CHAT_MODEL if the
# pro model isn't available to this key. Override via env.
CATCHALL_MODEL = os.environ.get("GEMINI_CATCHALL_MODEL", "gemini-2.5-pro")


async def extract_wizard_answers(
    template: dict[str, Any],
    text: str,
    *,
    locale: str = "en-IN",
    timeout_s: float = 8.0,
) -> dict[str, Any]:
    """One-shot LLM extraction: given a resolved template + the operator's
    free-text description (from the landing prompt box), pull out as many
    template answers as the text confidently supports, so the form wizard
    opens PRE-FILLED.

    Returns a {question_id: value} dict where values are already validated /
    coerced (lists joined to comma-strings so the wizard's text inputs render
    them cleanly; bools stay bool). Confidently-unanswerable questions are
    omitted — the operator fills those in the wizard."""
    text = (text or "").strip()
    if not text:
        return {}
    questions = template.get("questions") or []
    if not questions:
        return {}

    qlines: list[str] = []
    for q in questions:
        line = f'- "{q.get("id")}" ({q.get("type")}): {q.get("prompt") or q.get("label") or q.get("id")}'
        if q.get("options"):
            line += f'  [value must be exactly one of: {", ".join(map(str, q["options"]))}]'
        qlines.append(line)

    system = (
        "You pre-fill a business-setup form from the owner's free-text description. "
        "Extract ONLY fields the text clearly states or strongly implies — never guess "
        "or invent. Output STRICT JSON: an object mapping question_id to value. Rules: "
        "enum fields → exactly one of the listed options; bool fields → true/false; "
        "list fields → a comma-separated string; text/phone/email → a short string. "
        "OMIT any field you cannot answer with confidence. Do not include commentary."
    )
    prompt = (
        "QUESTIONS:\n" + "\n".join(qlines)
        + "\n\nBUSINESS DESCRIPTION:\n" + text
        + "\n\nReturn the JSON object now:"
    )

    try:
        client = _gb._client()
    except Exception as e:  # noqa: BLE001
        log.warning("extract_wizard_answers: client init failed: %s", e)
        return {}

    config = types.GenerateContentConfig(
        system_instruction=system,
        response_mime_type="application/json",
        temperature=0.0,
    )

    async def _call() -> Optional[str]:
        resp = await client.aio.models.generate_content(
            model=CHAT_MODEL, contents=prompt, config=config,
        )
        return getattr(resp, "text", None)

    try:
        raw = await asyncio.wait_for(_call(), timeout=timeout_s)
    except asyncio.TimeoutError:
        log.warning("extract_wizard_answers: timeout after %.1fs", timeout_s)
        return {}
    except Exception as e:  # noqa: BLE001
        log.warning("extract_wizard_answers: model call failed: %s", e)
        return {}
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        # Defensive: strip code fences / stray prose around the JSON.
        import re as _re
        m = _re.search(r"\{.*\}", raw, _re.DOTALL)
        if not m:
            return {}
        try:
            data = json.loads(m.group(0))
        except Exception:  # noqa: BLE001
            return {}
    if not isinstance(data, dict):
        return {}

    qby = {q.get("id"): q for q in questions}
    out: dict[str, Any] = {}
    for qid, val in data.items():
        q = qby.get(qid)
        if not q:
            continue
        if val is None or (isinstance(val, str) and not val.strip()):
            continue
        cleaned, err = _bt.validate_answer(q, val)
        if err:
            continue
        # Lists → comma string so the wizard text input shows them cleanly.
        if isinstance(cleaned, list):
            cleaned = ", ".join(str(x) for x in cleaned)
        out[qid] = cleaned
    log.info("extract_wizard_answers: extracted %d/%d fields", len(out), len(questions))
    return out


# ─── Catch-all (Any-industry) — best-model bespoke builds ───────────────────
#
# When the operator picks "Any industry" and describes a use case we have no
# template for, we don't fall back to a bland generic form. Instead the best
# model (1) designs a tailored question set for THAT use case + pre-fills what
# the description states, and (2) at save time composes a full, production-grade
# agent (persona / greeting / system prompt / small talk) for it. This is how
# the wizard handles "any use case imaginable".

_JSON_TYPES = {"text", "text_list", "enum", "bool", "phone", "email"}
_ALLOWED_CONNECTORS = [
    "calendar_check", "calendar_book", "sms_send",
    "knowledge_base_search", "http_webhook", "order_status",
]


def _clean_info_groups(raw: Any) -> Optional[list[dict[str, Any]]]:
    """Coerce a model-generated Additional Info schema into the canonical
    {id, label, emoji, desc, info_only} shape (matching info_schemas._g).
    Returns None if nothing usable, so callers fall back to sector groups."""
    if not isinstance(raw, list) or not raw:
        return None
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for g in raw:
        if not isinstance(g, dict):
            continue
        gid = str(g.get("id") or "").strip().lower().replace(" ", "_")
        if not gid or gid in seen:
            continue
        label = str(g.get("label") or gid.replace("_", " ").title()).strip()
        emoji = str(g.get("emoji") or "📋").strip()[:4] or "📋"
        desc = str(g.get("desc") or "").strip()
        out.append({
            "id": gid, "label": label, "emoji": emoji, "desc": desc,
            "info_only": bool(g.get("info_only")),
        })
        seen.add(gid)
        if len(out) >= 8:
            break
    return out or None


def _parse_json_blob(raw: Optional[str]) -> Any:
    """Parse a model's JSON output, tolerating code fences / stray prose."""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        m = _re.search(r"[\{\[].*[\}\]]", raw, _re.DOTALL)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except Exception:  # noqa: BLE001
            return None


async def _best_generate(
    system: str, prompt: str, *, timeout: float = 28.0, temperature: float = 0.4,
) -> tuple[Optional[str], Optional[str]]:
    """Generate JSON with the best available model, falling back to the fast
    model if the pro model isn't reachable. Returns (text, model_used)."""
    try:
        client = _gb._client()
    except Exception as e:  # noqa: BLE001
        log.warning("_best_generate: client init failed: %s", e)
        return None, None
    cfg = types.GenerateContentConfig(
        system_instruction=system,
        response_mime_type="application/json",
        temperature=temperature,
    )
    # Pro first (quality), flash second (availability/speed). De-dupe if equal.
    models = [CATCHALL_MODEL] + ([CHAT_MODEL] if CHAT_MODEL != CATCHALL_MODEL else [])
    for model in models:
        try:
            resp = await asyncio.wait_for(
                client.aio.models.generate_content(model=model, contents=prompt, config=cfg),
                timeout=timeout,
            )
            txt = getattr(resp, "text", None)
            if txt:
                return txt, model
        except Exception as e:  # noqa: BLE001
            log.warning("_best_generate(%s) failed: %s", model, e)
            continue
    return None, None


async def generate_dynamic_template(use_case: str, *, locale: str = "en-IN") -> Optional[dict[str, Any]]:
    """Design a tailored onboarding form for an arbitrary use case AND pre-fill
    the answers the description already states. Returns a wizard-payload-shaped
    dict with `dynamic: true` + `use_case`, or None on failure (caller falls
    back to the static _generic template)."""
    use_case = (use_case or "").strip()
    if not use_case:
        return None
    system = (
        "You design onboarding forms for phone-AI agents for ANY business or use "
        "case. Given a short description, output STRICT JSON describing a tailored "
        "setup form AND pre-filling answers the description clearly states.\n"
        "JSON shape: an object with keys: sector (one lowercase word category), "
        "agent_role (short role label, e.g. 'Yoga studio receptionist'), intro (one "
        "friendly sentence), persona (one-sentence agent persona), questions (array), "
        "and prefill (object of question_id -> value).\n"
        "Each question object has: id (snake_case), label (short), prompt (short "
        "friendly question), type (one of text, text_list, enum, bool, phone, email), "
        "required (bool), hint (optional example string), options (array of "
        "lowercase_snake_case strings, ENUM ONLY), suggestions (array of short names, "
        "AGENT_NAME ONLY).\n"
        "RULES: 6-9 questions. The FIRST question MUST have id 'business_name' (type "
        "text, required). The LAST MUST have id 'agent_name' (type text, required, "
        "with 4-5 short first-name suggestions fitting the locale). The middle "
        "questions must be SPECIFIC and genuinely useful for THIS use case (what they "
        "offer, how they book or triage, hours, pricing approach, transfer number, "
        "etc.). enum options are lowercase_snake_case (2-5 of them). ids are unique "
        "snake_case. In prefill, include ONLY values the description clearly states "
        "(enum -> one of its options, bool -> true/false, list -> comma string); omit "
        "anything unknown. Output JSON only, no commentary."
    )
    prompt = f"LOCALE: {locale}\nUSE CASE DESCRIPTION:\n{use_case}\n\nReturn the JSON now:"
    raw, model = await _best_generate(system, prompt, timeout=28.0, temperature=0.5)
    data = _parse_json_blob(raw)
    if not isinstance(data, dict):
        log.warning("generate_dynamic_template: no usable JSON")
        return None
    raw_qs = data.get("questions")
    if not isinstance(raw_qs, list) or not raw_qs:
        return None

    questions: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for q in raw_qs:
        if not isinstance(q, dict):
            continue
        qid = str(q.get("id") or "").strip().lower().replace(" ", "_")
        if not qid or qid in seen_ids:
            continue
        qtype = str(q.get("type") or "text").strip().lower()
        if qtype not in _JSON_TYPES:
            qtype = "text"
        opts = q.get("options") if qtype == "enum" else None
        if qtype == "enum" and not (isinstance(opts, list) and opts):
            qtype = "text"  # enum without options is useless → free text
            opts = None
        slot = "agent_name" if qid == "agent_name" else f"variables.{qid}"
        sugs = q.get("suggestions") if qid == "agent_name" and isinstance(q.get("suggestions"), list) else None
        questions.append({
            "id": qid,
            "label": str(q.get("label") or qid.replace("_", " ").title()),
            "prompt": str(q.get("prompt") or ""),
            "type": qtype,
            "required": bool(q.get("required")),
            "hint": q.get("hint"),
            "options": [str(o) for o in opts] if opts else None,
            "suggestions": [str(s) for s in sugs][:5] if sugs else None,
            "default": None,
            "slot": slot,
        })
        seen_ids.add(qid)

    # Guarantee the two anchor questions exist.
    if "business_name" not in seen_ids:
        questions.insert(0, {"id": "business_name", "label": "Business name",
                             "prompt": "What's it called?", "type": "text", "required": True,
                             "hint": None, "options": None, "suggestions": None,
                             "default": None, "slot": "variables.business_name"})
        seen_ids.add("business_name")
    if "agent_name" not in seen_ids:
        questions.append({"id": "agent_name", "label": "Agent name",
                          "prompt": "What should we call your agent?", "type": "text",
                          "required": True, "hint": None, "options": None,
                          "suggestions": ["Riya", "Maya", "Aria", "Nova", "Sam"],
                          "default": None, "slot": "agent_name"})
        seen_ids.add("agent_name")
    else:
        # Ensure the agent_name field always offers quick-pick names.
        for q in questions:
            if q["id"] == "agent_name" and not q.get("suggestions"):
                q["suggestions"] = ["Riya", "Maya", "Aria", "Nova", "Sam"]

    # Validate the model's prefill against the (cleaned) questions.
    prefill_in = data.get("prefill") if isinstance(data.get("prefill"), dict) else {}
    qby = {q["id"]: q for q in questions}
    prefill: dict[str, Any] = {}
    for qid, val in prefill_in.items():
        q = qby.get(qid)
        if not q or val is None or (isinstance(val, str) and not val.strip()):
            continue
        cleaned, err = _bt.validate_answer(q, val)
        if err:
            continue
        if isinstance(cleaned, list):
            cleaned = ", ".join(str(x) for x in cleaned)
        prefill[qid] = cleaned

    import hashlib as _hl
    tid = "dynamic." + _hl.sha1(use_case.encode("utf-8")).hexdigest()[:10]
    log.info("generate_dynamic_template: %d questions, %d prefilled (model=%s)",
             len(questions), len(prefill), model)
    return {
        "id": tid,
        "dynamic": True,
        "use_case": use_case,
        "sector": str(data.get("sector") or "generic").strip().lower() or "generic",
        "agent_role": str(data.get("agent_role") or "Custom agent"),
        "intro": str(data.get("intro") or "Got it — a few quick questions and your agent is ready."),
        "persona": str(data.get("persona") or ""),
        "questions": questions,
        "prefill": prefill,
    }


async def compose_dynamic_agent(
    use_case: str, answers: dict[str, Any], *, locale: str = "en-IN", sector_hint: str = "generic",
) -> dict[str, Any]:
    """Compose a complete, bespoke save_agent payload for an arbitrary use case
    using the operator's answers. The best model writes the persona, greeting,
    and system prompt; silent_defaults backfills the rest downstream."""
    answers = answers or {}
    agent_name = str(answers.get("agent_name") or "").strip() or "Aria"
    business = str(answers.get("business_name") or "the business").strip()
    facts = {k: v for k, v in answers.items() if k not in ("agent_name",) and v not in (None, "")}

    system = (
        "You configure production-grade phone-AI agents for ANY business. Given a "
        "use case + the operator's answers, output STRICT JSON for a complete agent. "
        "Keys: sector (one lowercase word), gender, voice, persona (2-3 sentences: "
        "who the agent is + tone), greeting (the EXACT first line spoken on "
        "answering a call — one warm sentence that names the business), "
        "system_prompt (a thorough, well-structured operating brief: who the agent "
        "is, what the business does using the facts, how to handle the common call "
        "types for THIS use case step by step, tone, and GUARDRAILS — never invent "
        "prices/availability/dates (offer a callback instead), never take card "
        "payments on the call, hand off to a human when unsure), small_talk (3-4 "
        "short on-brand lines), outcomes (array of 4-6 OBJECTS, see below), "
        "connectors (subset of " + ", ".join(_ALLOWED_CONNECTORS) + "), info_groups, "
        "extra_info_prefill, purpose, and guardrails.\n"
        # ── gender + voice: drive pronouns + TTS picker. Tightly coupled. ──
        "gender: one of 'female', 'male', or 'neutral'. Pick the one that matches "
        "the agent_name and the use case (e.g. 'Rohan' / 'Vikram' / 'Arjun' are "
        "male; 'Priya' / 'Aria' / 'Maya' / 'Anika' are female; if the operator "
        "picked a name that doesn't read clearly gendered, return 'neutral').\n"
        "voice: one of 'Aoede', 'Leda', 'Kore', 'Zephyr' (female-sounding), or "
        "'Charon', 'Fenrir', 'Puck', 'Orus' (male-sounding). MUST match the gender "
        "you picked. Default to 'Aoede' for female, 'Charon' for male, 'Kore' for "
        "neutral. Pick a more specific voice when the use case suggests one ('Leda' "
        "for soft / clinical; 'Charon' for deep / formal; 'Puck' for upbeat / "
        "kid-facing; 'Orus' for measured / corporate).\n"
        # ── purpose: drives the runtime CORE PURPOSE / Mission: block. ──
        "purpose: an object { summary, actions }. summary is one sentence in the "
        "operator's voice describing why this agent exists ('Qualify wedding-photo "
        "leads and book pre-shoot consultations.'). actions is an ordered list of "
        "1-3 verbs from this fixed vocabulary: callback_request, appointment_booking, "
        "quote_request, inquiry_capture, complaint_intake, order_status, "
        "support_ticket, emergency_routing. Pick the ones that match the use case.\n"
        # ── outcomes objects: drives the runtime [kind] tag on each outcome. ──
        "Each outcome is { id (snake_case slug), label (short Title Case), kind "
        "(one of: success, qualified, info, failure) }. The kind tells the analytics "
        "engine which calls count as wins. Examples: { id: 'consultation_booked', "
        "label: 'Consultation booked', kind: 'success' }; { id: 'callback_requested', "
        "label: 'Callback requested', kind: 'qualified' }; { id: 'info_only', "
        "label: 'Information given', kind: 'info' }; { id: 'voicemail', label: "
        "'Voicemail', kind: 'failure' }.\n"
        # ── guardrails: short bullet rules surfaced as a separate block at runtime. ──
        "guardrails is an array of 3-6 short rule strings the agent must follow — "
        "use-case-specific (e.g. for dog-walking: 'Never confirm a walk without the "
        "dog's name and the pickup address.'). Don't restate the universal safety "
        "floor (no card numbers, no medical/legal advice) — those are auto-applied.\n"
        # ── info_groups + extra_info_prefill (unchanged). ──
        "info_groups is an array of 4-6 'Additional Info' field groups the OPERATOR "
        "will later fill with reference knowledge the agent answers callers from — "
        "tailored to THIS use case (e.g. for a wedding photographer: Packages & "
        "Pricing, Shoot Types, Coverage Area, Deposit & Cancellation, FAQs). Each "
        "group: { id (snake_case), label (Title Case), emoji (1 char), desc (one "
        "short line of what to put there), info_only (true for reference-only groups "
        "that don't drive an action) }.\n"
        "extra_info_prefill is an object mapping some of those group ids to a short "
        "text seeded from the facts you were given (so the operator starts ahead); "
        "omit groups you can't seed.\n"
        "Write naturally and specifically for the use case — no placeholders, no "
        "{{braces}}. Bake the facts into the text. No commentary."
    )
    prompt = (
        f"LOCALE: {locale}\nUSE CASE: {use_case}\nAGENT NAME: {agent_name}\n"
        f"BUSINESS NAME: {business}\nANSWERS (facts to use): {json.dumps(facts, default=str)}\n\n"
        "Return the JSON now:"
    )
    raw, model = await _best_generate(system, prompt, timeout=32.0, temperature=0.55)
    data = _parse_json_blob(raw)
    if not isinstance(data, dict):
        data = {}

    conns = [c for c in (data.get("connectors") or []) if c in _ALLOWED_CONNECTORS]
    if not conns:
        conns = ["calendar_check", "calendar_book", "sms_send", "knowledge_base_search"]

    # Outcomes — the new shape is a list of objects with id+label+kind so the
    # runtime prompt can show [kind] tags for catch-all agents. We still
    # accept the legacy string-only shape (older builds, or models that
    # ignored the upgrade) and synthesise sensible defaults.
    raw_outcomes = data.get("outcomes") or []
    outcomes: list[str] = []
    outcome_catalogue: list[dict[str, Any]] = []
    seen_oids: set[str] = set()
    for o in raw_outcomes:
        if isinstance(o, dict):
            oid = str(o.get("id") or "").strip().lower().replace(" ", "_")
            if not oid or oid in seen_oids:
                continue
            kind = str(o.get("kind") or "").strip().lower()
            if kind not in {"success", "qualified", "info", "failure"}:
                kind = "info"
            label = str(o.get("label") or oid.replace("_", " ").title()).strip()
            outcomes.append(oid)
            outcome_catalogue.append({"id": oid, "label": label, "kind": kind, "description": ""})
            seen_oids.add(oid)
        elif isinstance(o, str):
            oid = o.strip().lower().replace(" ", "_")
            if not oid or oid in seen_oids:
                continue
            outcomes.append(oid)
            # Heuristic kind from the slug — better than nothing.
            kind = (
                "success" if any(k in oid for k in ("booked", "confirmed", "resolved", "scheduled", "sold"))
                else "qualified" if any(k in oid for k in ("lead", "callback", "qualified", "interest"))
                else "failure" if any(k in oid for k in ("voicemail", "no_interest", "lost", "abandoned"))
                else "info"
            )
            label = oid.replace("_", " ").title()
            outcome_catalogue.append({"id": oid, "label": label, "kind": kind, "description": ""})
            seen_oids.add(oid)
    if not outcomes:
        outcomes = ["info_given", "lead_captured", "booking_made", "callback_requested", "voicemail"]
        outcome_catalogue = [
            {"id": "info_given",         "label": "Information given",  "kind": "info",      "description": ""},
            {"id": "lead_captured",      "label": "Lead captured",      "kind": "qualified", "description": ""},
            {"id": "booking_made",       "label": "Booking made",       "kind": "success",   "description": ""},
            {"id": "callback_requested", "label": "Callback requested", "kind": "qualified", "description": ""},
            {"id": "voicemail",          "label": "Voicemail",          "kind": "failure",   "description": ""},
        ]

    small_talk = [str(s) for s in (data.get("small_talk") or []) if str(s).strip()][:5]

    # Purpose — drives the runtime CORE PURPOSE block + the ⭐ primary
    # outcomes on the Call-outcomes page. Pre-185 catch-all agents had
    # purpose=None, so their runtime CORE PURPOSE read "(Not configured)".
    _VALID_PURPOSE_ACTIONS = {
        "callback_request", "appointment_booking", "quote_request",
        "inquiry_capture", "complaint_intake", "order_status",
        "support_ticket", "emergency_routing",
    }
    raw_purpose = data.get("purpose") if isinstance(data.get("purpose"), dict) else {}
    purpose_summary = str(raw_purpose.get("summary") or "").strip()[:240]
    purpose_actions_raw = raw_purpose.get("actions") if isinstance(raw_purpose.get("actions"), list) else []
    purpose_actions: list[str] = []
    for a in purpose_actions_raw:
        if not isinstance(a, str):
            continue
        slug = a.strip().lower().replace(" ", "_").replace("-", "_")
        if slug in _VALID_PURPOSE_ACTIONS and slug not in purpose_actions:
            purpose_actions.append(slug)
    if not purpose_summary:
        purpose_summary = f"Handle calls for {business} — answer questions, capture leads, and book the relevant action."
    if not purpose_actions:
        # Last-resort guess based on the outcome kinds we already resolved.
        if any(c["kind"] == "success" for c in outcome_catalogue):
            purpose_actions = ["appointment_booking"]
        else:
            purpose_actions = ["inquiry_capture"]
    purpose = {"summary": purpose_summary, "actions": purpose_actions}

    # Guardrails — short bullet rules surfaced as a dedicated runtime block
    # (separate from the prose guardrails baked into system_prompt). The
    # universal safety floor (no card numbers, no medical/legal advice) is
    # always layered on top — we only need use-case-specific rules here.
    raw_rails = data.get("guardrails") if isinstance(data.get("guardrails"), list) else []
    guardrails_list: list[str] = []
    seen_rails: set[str] = set()
    for r in raw_rails:
        if not isinstance(r, str):
            continue
        t = r.strip().lstrip("-•*").strip()
        if not t or t.lower() in seen_rails:
            continue
        guardrails_list.append(t[:240])
        seen_rails.add(t.lower())
        if len(guardrails_list) >= 6:
            break
    if not guardrails_list:
        # Always-safe fallbacks so a catch-all agent ships with SOMETHING in
        # the runtime guardrails block instead of "(none specified)".
        guardrails_list = [
            "Never invent prices, availability, or dates — offer a callback if unsure.",
            "Never take card or payment details on the phone.",
            "Hand off to a human if the caller asks, sounds upset, or you can't help.",
        ]

    # Per-agent Additional Info schema (tailored to the use case). Cleaned to
    # the same {id,label,emoji,desc,info_only} shape the dashboard + call-prompt
    # consume. None when the model gave nothing usable → sector fallback.
    info_groups = _clean_info_groups(data.get("info_groups"))
    extra_prefill_in = data.get("extra_info_prefill") if isinstance(data.get("extra_info_prefill"), dict) else {}
    extra_info: dict[str, Any] = {}
    if info_groups:
        valid_ids = {g["id"] for g in info_groups}
        for gid, text in extra_prefill_in.items():
            if gid in valid_ids and isinstance(text, (str, int, float)) and str(text).strip():
                extra_info[gid] = str(text).strip()

    # Gender + matching voice (build 187). Tightly coupled so the agent
    # the operator sees on the dashboard, the pronouns Eva uses in her
    # build chatter, and the voice the caller hears all agree. Default
    # to female + Aoede only if the model omits both — that's the
    # historical behaviour, so existing wizard paths keep working.
    _FEMALE_VOICES = {"Aoede", "Leda", "Kore", "Zephyr"}
    _MALE_VOICES   = {"Charon", "Fenrir", "Puck", "Orus"}
    _ALL_VOICES    = _FEMALE_VOICES | _MALE_VOICES
    gender_raw = str(data.get("gender") or "").strip().lower()
    if gender_raw not in ("female", "male", "neutral"):
        gender_raw = "female"
    voice_raw = str(data.get("voice") or "").strip()
    if voice_raw not in _ALL_VOICES:
        voice_raw = ""
    # Repair mismatches — if the model picked a voice that contradicts
    # its own gender, snap to the canonical default for the gender. We
    # trust gender over voice because gender drives more downstream
    # surfaces (UI pronouns, prompt hint, future selects).
    if voice_raw:
        in_female_set = voice_raw in _FEMALE_VOICES
        in_male_set   = voice_raw in _MALE_VOICES
        if gender_raw == "female" and not in_female_set:
            voice_raw = ""
        elif gender_raw == "male" and not in_male_set:
            voice_raw = ""
    if not voice_raw:
        voice_raw = {"female": "Aoede", "male": "Charon", "neutral": "Kore"}[gender_raw]

    # Stash the kind-labelled outcome catalogue on `variables` under a
    # reserved key so `_format_outcomes_with_kinds_for_prompt` can pick
    # it up at runtime for catch-all agents (whose sector isn't in any
    # pre-baked call_outcomes catalogue). Keyed with a leading underscore
    # so it's clearly system metadata, not operator-edited content.
    base_variables = {k: v for k, v in answers.items() if k != "agent_name"}
    base_variables["_outcome_catalogue"] = outcome_catalogue
    # Gender is stored under `variables.gender` (no DB migration needed —
    # variables is JSONB). The frontend pronouns() helper + the runtime
    # prompt's gender-hint line both read from here.
    base_variables["gender"] = gender_raw

    args: dict[str, Any] = {
        "sector": str(data.get("sector") or sector_hint or "generic").strip().lower() or "generic",
        "locale": locale,
        "name": agent_name,
        "voice": voice_raw,
        "persona": str(data.get("persona") or "").strip(),
        "greeting": str(data.get("greeting") or "").strip(),
        "system_prompt": str(data.get("system_prompt") or "").strip(),
        "small_talk": small_talk,
        "outcomes": outcomes,
        "connectors": conns,
        "guardrails": guardrails_list,
        "policy": {"dos": {"sms_recap": True, "language_match": True},
                   "donts": {"no_price_promise": True, "no_phone_payment": True}},
        "purpose": purpose,
        "variables": base_variables,
        "info_groups": info_groups,   # None → sector fallback
        "extra_info": extra_info,
    }
    # Safety net: if the model gave us nothing usable, synthesise a minimal but
    # valid agent so the build still completes.
    if not args["greeting"]:
        args["greeting"] = f"Hi, this is {agent_name} from {business} — how can I help?"
    if not args["system_prompt"]:
        args["system_prompt"] = (
            f"You are {agent_name}, the phone assistant for {business}. "
            f"Context: {use_case}. Be warm, concise, and helpful. Never invent "
            "prices, availability, or dates — offer a callback if unsure. Never take "
            "card payments on the call. Hand off to a human when you can't help."
        )
    if not args["persona"]:
        args["persona"] = f"Warm, helpful assistant for {business}."
    log.info("compose_dynamic_agent: composed (model=%s, sector=%s, gender=%s, voice=%s, info_groups=%s)",
             model, args["sector"], gender_raw, voice_raw, len(info_groups) if info_groups else 0)
    return args


async def regenerate_info_groups(agent: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Redesign an existing agent's Additional Info schema to match its CURRENT
    purpose (persona + system prompt + facts). Carries the operator's already-
    filled notes into the new sections so nothing is lost. Returns
    {info_groups, extra_info} or None if the model gave nothing usable (caller
    keeps the existing schema)."""
    name = str(agent.get("name") or "the agent")
    sector = str(agent.get("sector") or "generic")
    persona = str(agent.get("persona") or "")
    sysp = str(agent.get("system_prompt") or "")[:1400]
    variables = agent.get("variables") if isinstance(agent.get("variables"), dict) else {}
    existing = {
        k: v for k, v in (agent.get("extra_info") or {}).items()
        if isinstance(v, str) and v.strip()
    }

    system = (
        "You redesign the 'Additional Info' reference sections for a phone-AI "
        "agent so they match its CURRENT purpose. Output STRICT JSON with two "
        "keys: info_groups and extra_info_prefill.\n"
        "info_groups: an array of 4-6 sections the operator fills with reference "
        "knowledge the agent answers callers from, tailored to this agent. Each: "
        "{ id (snake_case), label (Title Case), emoji (1 char), desc (one short "
        "line), info_only (true for reference-only sections) }.\n"
        "extra_info_prefill: an object mapping new section ids to text. CRITICAL: "
        "carry the operator's EXISTING NOTES into the matching new sections so "
        "nothing they wrote is lost; you may reorganise/rephrase lightly to fit. "
        "Omit sections you have nothing for.\n"
        "No placeholders, no {{braces}}, no commentary."
    )
    prompt = (
        f"AGENT: {name}\nSECTOR: {sector}\nPERSONA: {persona}\n"
        f"PURPOSE / OPERATING BRIEF:\n{sysp}\n"
        f"KNOWN FACTS: {json.dumps(variables, default=str)}\n"
        f"EXISTING NOTES (carry these into the new sections): {json.dumps(existing, default=str)}\n\n"
        "Return the JSON now:"
    )
    raw, model = await _best_generate(system, prompt, timeout=32.0, temperature=0.5)
    data = _parse_json_blob(raw)
    if not isinstance(data, dict):
        return None
    info_groups = _clean_info_groups(data.get("info_groups"))
    if not info_groups:
        return None
    valid_ids = {g["id"] for g in info_groups}
    extra: dict[str, Any] = {}
    # 1) Preserve existing notes for any id that survived (exact, no rewrite).
    for k, v in existing.items():
        if k in valid_ids:
            extra[k] = v
    # 2) Fold the model's carry-over prefill into the (renamed) new sections.
    prefill = data.get("extra_info_prefill") if isinstance(data.get("extra_info_prefill"), dict) else {}
    for gid, text in prefill.items():
        if gid in valid_ids and isinstance(text, (str, int, float)) and str(text).strip():
            extra.setdefault(gid, str(text).strip())
    log.info("regenerate_info_groups: %d sections (model=%s, carried %d notes)",
             len(info_groups), model, len(extra))
    return {"info_groups": info_groups, "extra_info": extra}


async def run_chat_session(
    ws: WebSocket,
    *,
    client_locale: str = "en-US",
    client_tz: str = "UTC",
    tweaks: Optional[dict[str, Any]] = None,
    user_id: Optional[int] = None,
    sid: Optional[str] = None,
    industry: Optional[str] = None,
) -> None:
    """Top-level chat session. One WS = one conversation. Persistent
    history across user turns within the WS. No reconnects.

    `industry` is the optional landing-page preset — set when the
    operator picked an industry from the homepage dropdown or arrived via
    a `/for-<industry>` deep-link. When present we lock that industry's
    template up front (skipping triage) so Eva opens the deterministic
    interview immediately. Falls through to normal triage if the industry
    has no template."""
    client = _gb._client()

    # Sid handling: same shape as gemini_bridge so a chat session can
    # share state with any subsequent voice session keyed by the same
    # sessionStorage value on the client.
    if not sid:
        import uuid as _uuid
        sid = f"ws-{_uuid.uuid4().hex}"
        log.info("run_chat_session: no client sid; minted synthetic sid=%s", sid[:18])
    build_sid: str = sid

    tools = [types.Tool(function_declarations=[
        _gb._save_agent_decl(),
        _gb._note_build_facts_decl(),
        _gb._select_build_template_decl(),
        _gb._record_template_answer_decl(),
    ])]

    # BuildMonitor — same state machine the voice path uses. Restore
    # any previously-captured slots so a reconnecting chat sees the
    # template / answers already collected.
    build_monitor = _bs.BuildMonitor(
        sid=build_sid,
        started_at_monotonic=_time_mod.monotonic(),
    )
    try:
        row = await db.get_build_session(user_id=user_id, sid=build_sid)
        if row:
            build_monitor.update_slots(row)
    except Exception as e:  # noqa: BLE001
        log.warning("init build_session read failed: %s", e)

    # ── Industry preset (landing-page dropdown / /for-<industry>) ──
    # Lock the industry's template BEFORE the first turn so Eva skips
    # triage entirely. We only preset when nothing more specific was
    # already captured (a reconnect that already nailed a city template
    # wins). `match_by_industry` is locale-tolerant — it falls back to our
    # en-IN coverage when the browser locale has no variant — so the
    # preset reliably locks an industry even outside India.
    preset_template: Optional[dict[str, Any]] = None
    if industry and not build_monitor.template_id:
        try:
            cand = _bt.match_by_industry(industry, locale=client_locale)
        except Exception as e:  # noqa: BLE001
            log.warning("industry preset match failed: %s", e)
            cand = None
        if cand and cand.get("id") and cand["id"] != "_generic":
            tid = cand["id"]
            try:
                await db.set_build_template(user_id=user_id, sid=build_sid, template_id=tid)
            except Exception as e:  # noqa: BLE001
                log.warning("preset set_build_template failed: %s", e)
            build_monitor.template_id = tid
            preset_template = cand
            log.warning(
                "chat[%s]: INDUSTRY PRESET → template locked %s (industry=%s locale=%s)",
                build_sid[:18], tid, industry, client_locale,
            )

    # System prompt with text_only=True so the prompt's opener block
    # picks the chat-mode "DO NOT greet, get on with it" branch. When an
    # industry was preset, append a block telling Eva the template is
    # already locked + what the first interview question is, so she never
    # calls select_build_template and opens straight into the interview.
    system_prompt = await _gb._builder_system_prompt(
        client_locale=client_locale, client_tz=client_tz, user_id=user_id,
        text_only=True,
    )
    # Two augmentations, in priority order:
    #  (A) RESUMING — a template is locked AND has answers already (the
    #      form→chat / voice→chat handoff, or a reconnect). Tell Eva exactly
    #      what's already captured so she NEVER re-asks it, and point her at
    #      the next unanswered question.
    #  (B) INDUSTRY PRESET — a template is locked but no answers yet (landing
    #      dropdown / /for-<industry>). Skip triage, open at question 1.
    active_tpl = _bt.get_template(build_monitor.template_id) if build_monitor.template_id else None
    answers_now = build_monitor.template_answers or {}
    answered_qs = [
        q for q in ((active_tpl or {}).get("questions") or [])
        if q.get("id") in answers_now and answers_now.get(q["id"]) not in (None, "")
    ] if active_tpl else []
    if active_tpl and answered_qs:
        try:
            next_q = _bt.next_unanswered_question(active_tpl, answers_now)
        except Exception:  # noqa: BLE001
            next_q = None
        facets = active_tpl.get("facets") or {}
        ind_label = str(facets.get("industry") or industry or "").replace("_", " ").strip() or "business"
        lines = []
        for q in answered_qs:
            v = answers_now.get(q["id"])
            if isinstance(v, list):
                v = ", ".join(str(x) for x in v)
            elif isinstance(v, bool):
                v = "yes" if v else "no"
            lines.append(f"  - {q.get('prompt') or q.get('id')} → {v}")
        cont = (
            f'Continue from the NEXT unanswered question: "{next_q["prompt"]}". Ask just '
            "that, in one short line. As they answer, call record_template_answer.\n"
            if next_q else
            "Every question is already answered. Give a one-line wrap-up offer "
            "(\"she's ready — want a quick hello?\") and on yes call save_agent.\n"
        )
        system_prompt += (
            "\n\nRESUMING A BUILD (form → chat handoff)\n"
            f"The {ind_label} template ({active_tpl['id']}) is LOCKED and the operator "
            "ALREADY answered these in the form — treat them as FINAL and NEVER ask "
            "them again:\n" + "\n".join(lines) + "\n"
            "Do NOT greet, do NOT re-introduce yourself, do NOT call "
            "select_build_template. " + cont
        )
    elif preset_template is not None:
        try:
            first_q = _bt.next_unanswered_question(
                preset_template, build_monitor.template_answers,
            )
        except Exception:  # noqa: BLE001
            first_q = None
        facets = preset_template.get("facets") or {}
        ind_label = str(facets.get("industry") or industry or "").replace("_", " ")
        first_prompt = (first_q or {}).get("prompt") or ""
        system_prompt += (
            "\n\nINDUSTRY PRESET (landing page)\n"
            f"The operator picked the {ind_label} industry before this chat began, "
            f"so the {preset_template['id']} interview template is ALREADY LOCKED. "
            "Do NOT call select_build_template — the template is set. Open with at "
            "most a one-line warm acknowledgement (no triage, no 'what kind of "
            "business?') and go straight into the interview. As the operator answers, "
            "call record_template_answer for each question, in order. If their first "
            "message already answers some questions, record those first, then ask the "
            "next unanswered one.\n"
            + (f'First question: "{first_prompt}"\n' if first_prompt else "")
        )

    handoff = _gb._Handoff()

    # ── small WS-send wrapper (silently swallows close-races) ──
    async def _send_json(payload: dict[str, Any]) -> None:
        try:
            # default=str is LOAD-BEARING: the saved-agent payload (from
            # db.create_agent → SELECT *) carries `created_at` as a
            # Python datetime. Plain json.dumps raises TypeError on it,
            # the bare except swallows it, and the `agent_saved` event
            # is silently never sent — which is exactly why the reveal
            # never fired. default=str coerces datetimes → ISO strings.
            await ws.send_text(json.dumps(payload, default=str))
        except Exception:  # noqa: BLE001
            pass

    # ── Card emitter (same protocol as gemini_bridge) ──
    async def emit_template_question_card() -> None:
        if not build_monitor.template_id:
            return
        try:
            template = _bt.get_template(build_monitor.template_id)
            if not template:
                return
            next_q = _bt.next_unanswered_question(
                template, build_monitor.template_answers,
            )
            answered, total = build_monitor.template_progress()
            if next_q is None:
                await _send_json({
                    "type": "template_complete",
                    "template_id": build_monitor.template_id,
                    "progress": {"answered": total, "total": total},
                })
                return
            primary = None
            sugs = next_q.get("suggestions") or []
            if sugs:
                primary, _alts = _bs._pick_suggestion(list(sugs), build_monitor.sid)
            await _send_json({
                "type": "template_question",
                "template_id": build_monitor.template_id,
                "question": {
                    "id": next_q["id"],
                    "prompt": next_q["prompt"],
                    "type": next_q["type"],
                    "required": bool(next_q.get("required")),
                    "hint": next_q.get("hint"),
                    "options": next_q.get("options"),
                    "suggestions": sugs or None,
                    "primary_suggestion": primary,
                    "progress": {
                        "answered": answered, "total": total,
                        "number": answered + 1,
                    },
                },
            })
        except Exception as e:  # noqa: BLE001
            log.warning("emit template_question failed: %s", e)

    # ── Tool handlers (closures over per-session state) ──
    # These are the minimum-viable versions of what lives in
    # gemini_bridge. The chat path is template-driven on the happy
    # path, so on_save_agent doesn't need the elaborate extras
    # backfill machinery — compose_save_args from the template gives
    # us a fully-substituted payload.

    async def on_save_agent(args: dict[str, Any]) -> dict[str, Any]:
        # Dedupe: a model turn that fires save_agent twice in the same
        # chat is unlikely (we're single-threaded per turn), but
        # protect anyway.
        if handoff.exit_after_save and handoff.saved_agent is not None:
            sa = handoff.saved_agent
            return {"ok": True, "agent_id": sa.get("id"), "name": sa.get("name"),
                    "note": "already committed"}
        # If template is locked, the template's compose_save_args is
        # the source of truth — replace whatever the model sent.
        if build_monitor.template_id:
            tpl = _bt.get_template(build_monitor.template_id)
            if tpl is not None:
                composed = _bt.compose_save_args(tpl, build_monitor.template_answers or {})
                log.info("on_save_agent: composing from template %s (%d answers)",
                         build_monitor.template_id, len(build_monitor.template_answers or {}))
                args = composed
        # Silent defaults fill any remaining gaps (voice, ambience,
        # outcomes, small_talk by sector).
        try:
            args = silent_defaults.merge_into_save_args(args)
        except Exception as e:  # noqa: BLE001
            log.warning("silent_defaults merge failed: %s", e)
        # Pre-fill Additional Info from captured facts (services/offers)
        # so the operator lands on a partially-filled page, not a blank
        # one. Operator-supplied extra_info (none at build time) wins.
        try:
            from . import info_schemas
            pre = info_schemas.prefill_extra_info(args.get("sector"), args.get("variables") or {})
            if pre:
                args["extra_info"] = {**pre, **(args.get("extra_info") or {})}
        except Exception as e:  # noqa: BLE001
            log.warning("prefill_extra_info failed: %s", e)
        try:
            owner_id = user_id if user_id is not None else (await db.get_founder())["id"]
            saved = await db.create_agent(args, user_id=owner_id)
        except Exception as e:
            log.exception("on_save_agent db.create_agent failed: %s", e)
            return {"ok": False, "error": str(e)}
        log.info("chat on_save_agent: saved id=%s name=%s", saved.get("id"), saved.get("name"))
        try:
            await db.mark_build_committed(user_id=user_id, sid=build_sid, agent_id=saved["id"])
        except Exception as e:  # noqa: BLE001
            log.warning("mark_build_committed failed: %s", e)
        try:
            await db.seed_helper_memory(user_id=owner_id, agent_id=saved["id"], agent=saved)
        except Exception as e:  # noqa: BLE001
            log.warning("seed_helper_memory(chat) failed: %s", e)
        handoff.exit_after_save = True
        handoff.saved_agent = saved
        return {"ok": True, "agent_id": saved["id"], "name": saved["name"]}

    async def on_select_build_template(args: dict[str, Any]) -> dict[str, Any]:
        # Repeat-call guard — identical to gemini_bridge. Once a
        # named template is locked, refuse re-triage and tell the
        # model to call record_template_answer instead.
        if build_monitor.template_id and build_monitor.template_id != "_generic":
            locked = build_monitor.template_id
            tpl = _bt.get_template(locked)
            pending = _bt.next_unanswered_question(
                tpl, build_monitor.template_answers,
            ) if tpl else None
            return {
                "ok": False,
                "error": (
                    f"Template {locked!r} is already locked. DO NOT call "
                    f"select_build_template again. The operator's last "
                    f"message is the answer to NEXT QUESTION — call "
                    f"record_template_answer instead."
                ),
                "locked_template_id": locked,
                "next_question_id": (pending or {}).get("id"),
                "next_question_prompt": (pending or {}).get("prompt"),
            }
        try:
            template = _bt.find_best_match(
                industry=args.get("industry") or None,
                sub_industry=args.get("sub_industry") or None,
                locale=args.get("locale") or None,
                country=args.get("country") or None,
                city=args.get("city") or None,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("select_build_template lookup failed: %s", e)
            template = None
        if not template:
            return {"ok": True, "found": False}
        tid = template["id"]
        try:
            await db.set_build_template(user_id=user_id, sid=build_sid, template_id=tid)
        except Exception as e:  # noqa: BLE001
            log.warning("set_build_template DB write failed: %s", e)
        build_monitor.template_id = tid
        log.warning("chat[%s]: TEMPLATE LOCKED %s (%d questions)",
                    build_sid[:18], tid, len(template.get("questions") or []))
        first_q = _bt.next_unanswered_question(template, build_monitor.template_answers)
        primary = None
        if first_q and first_q.get("suggestions"):
            primary, _alts = _bs._pick_suggestion(list(first_q["suggestions"]), build_sid)
        # Emit card so the client renders it WITHOUT waiting for the
        # model's text turn to finish.
        await emit_template_question_card()
        return {
            "ok": True, "found": True, "template_id": tid,
            "intro": template.get("intro") or "",
            "total_questions": len(template.get("questions") or []),
            "next_question": {
                "id": first_q["id"], "prompt": first_q["prompt"],
                "type": first_q["type"], "options": first_q.get("options"),
                "required": bool(first_q.get("required")),
                "propose_name": primary,
            } if first_q else None,
        }

    async def on_record_template_answer(args: dict[str, Any]) -> dict[str, Any]:
        tid = build_monitor.template_id
        if not tid:
            return {"ok": False, "error": "no template locked — call select_build_template first"}
        template = _bt.get_template(tid)
        if not template:
            return {"ok": False, "error": f"template {tid!r} no longer in registry"}
        qid = (args.get("question_id") or "").strip()
        q = _bt.question_by_id(template, qid)
        if not q:
            return {"ok": False, "error": f"unknown question_id {qid!r} for template {tid!r}"}
        value, err = _bt.validate_answer(q, args.get("value"))
        if err:
            await _send_json({
                "type": "template_question_error",
                "question_id": qid, "error": err,
                "retry_prompt": q.get("prompt"),
            })
            return {
                "ok": False, "retry_prompt": q.get("prompt"),
                "error": err, "options": q.get("options"),
                "hint": q.get("hint"),
            }
        try:
            await db.record_template_answer(
                user_id=user_id, sid=build_sid, question_id=qid, value=value,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("record_template_answer DB write failed: %s", e)
        build_monitor.template_answers[qid] = value
        log.info("chat[%s]: answer q=%s value=%r (%d/%d)",
                 build_sid[:18], qid, value,
                 len(build_monitor.template_answers),
                 len(template.get("questions") or []))
        next_q = _bt.next_unanswered_question(template, build_monitor.template_answers)
        # Push the next card immediately.
        await emit_template_question_card()
        if next_q is None:
            return {
                "ok": True, "done": True,
                "answered": len(build_monitor.template_answers),
                "total": len(template.get("questions") or []),
                "next_action": "All template questions answered. Make a one-line wrap-up offer; on yes, fire save_agent.",
            }
        return {
            "ok": True, "done": False,
            "answered": len(build_monitor.template_answers),
            "total": len(template.get("questions") or []),
            "next_question": {
                "id": next_q["id"], "prompt": next_q["prompt"],
                "type": next_q["type"], "options": next_q.get("options"),
                "required": bool(next_q.get("required")),
                "hint": next_q.get("hint"),
            },
        }

    async def on_note_build_facts(args: dict[str, Any]) -> dict[str, Any]:
        typed_keys = {"sector_kind", "business_name", "primary_job", "agent_name"}
        extras: dict[str, Any] = {}
        for k, v in (args or {}).items():
            if k in typed_keys: continue
            if v is None or v == "": continue
            extras[k] = v
        try:
            row = await db.merge_build_facts(
                user_id=user_id, sid=build_sid,
                sector_kind=args.get("sector_kind"),
                business_name=args.get("business_name"),
                primary_job=args.get("primary_job"),
                agent_name=args.get("agent_name"),
                extras=extras or None,
            )
            # Auto-template-from-extractor: if note_build_facts just
            # gave us enough signal to lock or upgrade a template, do
            # it server-side without waiting for Eva to re-call
            # select_build_template.
            try:
                await _gb._auto_template_from_extractor(
                    user_id=user_id, sid=build_sid,
                    build_row=row, build_monitor=build_monitor,
                )
            except Exception as e:  # noqa: BLE001
                log.warning("auto_template after note_build_facts failed: %s", e)
            await emit_template_question_card()
            return {
                "ok": True,
                "facts": {k: row.get(k) for k in
                          ("sector_kind", "business_name", "primary_job", "agent_name")},
                "extras_keys": sorted((row.get("extras") or {}).keys()),
            }
        except Exception as e:  # noqa: BLE001
            log.warning("note_build_facts failed: %s", e)
            return {"ok": True, "warning": "persistence-deferred"}

    # ── Skip handler (client → server, bypasses model entirely) ──
    async def handle_template_skip(question_id: str) -> None:
        if not build_monitor.template_id: return
        if not question_id: return
        template = _bt.get_template(build_monitor.template_id)
        if not template: return
        q = _bt.question_by_id(template, question_id)
        if not q or q.get("required"): return
        try:
            await db.record_template_answer(
                user_id=user_id, sid=build_sid, question_id=question_id, value=None,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("template_skip DB write failed: %s", e)
        build_monitor.template_answers[question_id] = None
        log.info("chat[%s]: SKIPPED q=%s", build_sid[:18], question_id)
        await emit_template_question_card()

    HANDLERS = {
        "save_agent":             on_save_agent,
        "select_build_template":  on_select_build_template,
        "record_template_answer": on_record_template_answer,
        "note_build_facts":       on_note_build_facts,
    }

    # ── Chat session ──
    # automatic_function_calling disabled — we run the tool loop
    # manually because we need to:
    #   1. emit `template_question` cards as a side effect of tool calls
    #   2. apply the repeat-call guard before the model recurses
    #   3. let on_save_agent's side effects (handoff flags) terminate
    #      the outer loop deterministically
    config = types.GenerateContentConfig(
        system_instruction=system_prompt,
        tools=tools,
        temperature=0.4,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )
    try:
        chat = client.aio.chats.create(model=CHAT_MODEL, config=config)
    except Exception as e:
        log.exception("chats.create failed: %s", e)
        await _send_json({"type": "error", "message": f"Couldn't open chat: {e}"})
        return

    await _send_json({"type": "ready", "model": CHAT_MODEL, "kind": "builder"})
    # If we restored a template from a previous session, push its
    # current card immediately on connect.
    await emit_template_question_card()

    # Rolling conversation history for the extractor window (last N
    # turns). The chat object holds its own full history for the model;
    # this is a lightweight {role,text} mirror the extractor consumes.
    convo_turns: list[dict[str, str]] = []

    # ── Main loop ──
    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                log.info("chat[%s]: client disconnect", build_sid[:18])
                break
            if "text" not in msg or msg["text"] is None:
                # Binary frames not used in text chat.
                continue
            try:
                data = json.loads(msg["text"])
            except json.JSONDecodeError:
                continue
            kind = data.get("type")
            if kind == "stop":
                break
            if kind == "template_skip":
                qid = str(data.get("question_id") or "")
                await handle_template_skip(qid)
                # The skip handler is server-side only — Eva (the chat
                # model) didn't see it. If we don't tell her, she'll
                # wait silently for the next user message even though
                # the BUILD STATE has advanced. Nudge her with a
                # synthetic user message so she takes her next turn:
                # either ask the next question (if more remain) or
                # do the wrap-up offer (if interview is complete).
                if build_monitor.template_id:
                    try:
                        tpl = _bt.get_template(build_monitor.template_id)
                        next_q = _bt.next_unanswered_question(
                            tpl, build_monitor.template_answers,
                        ) if tpl else None
                    except Exception:  # noqa: BLE001
                        next_q = None
                    nudge = (
                        f"[SYSTEM NOTICE: Operator clicked Skip on optional question "
                        f"'{qid}'. " + (
                            "All template questions are now complete. Make the "
                            "ONE-LINE wrap-up offer ('she's ready — want a quick "
                            "hello?') and on operator confirm, fire save_agent. "
                            "Do NOT acknowledge this notice."
                            if next_q is None else
                            f"Move on to the NEXT QUESTION (id={next_q['id']}, "
                            f"prompt='{next_q['prompt']}'). Read it briefly. Do "
                            f"NOT acknowledge this notice."
                        ) + "]"
                    )
                    try:
                        await _run_model_turn(
                            chat=chat, user_text=nudge,
                            handlers=HANDLERS, send_json=_send_json,
                            build_monitor=build_monitor,
                        )
                    except Exception as e:
                        log.exception("post-skip nudge turn failed: %s", e)
                # If save_agent fired from the nudge turn, the
                # handoff-check below picks it up.
                if handoff.exit_after_save and handoff.saved_agent is not None:
                    log.info("chat[%s]: agent saved (post-skip), emitting reveal", build_sid[:18])
                    await _send_json({"type": "agent_saved", "agent": handoff.saved_agent})
                    await _send_json({"type": "build_complete"})
                    break
                continue
            if kind != "text" or not data.get("text"):
                continue
            user_text = str(data["text"]).strip()
            if not user_text:
                continue

            # #3 Transcript persistence — record the operator's turn
            # durably BEFORE the model turn, so a mid-build WS drop can
            # be reconstructed + the BuildRecovery banner has content.
            convo_turns.append({"role": "user", "text": user_text})
            try:
                await db.append_transcript_turn(
                    user_id=user_id, sid=build_sid, role="user", text=user_text,
                )
            except Exception as e:  # noqa: BLE001
                log.warning("append_transcript_turn(user) failed: %s", e)

            # Run one user turn through the chat — may involve
            # multiple round-trips for function calls.
            try:
                eva_text = await _run_model_turn(
                    chat=chat, user_text=user_text,
                    handlers=HANDLERS, send_json=_send_json,
                    build_monitor=build_monitor,
                )
            except Exception as e:
                log.exception("model turn failed: %s", e)
                await _send_json({"type": "error", "message": str(e)[:200]})
                break

            # Persist Eva's turn too.
            if eva_text and eva_text.strip():
                convo_turns.append({"role": "model", "text": eva_text.strip()})
                try:
                    await db.append_transcript_turn(
                        user_id=user_id, sid=build_sid, role="model", text=eva_text.strip(),
                    )
                except Exception as e:  # noqa: BLE001
                    log.warning("append_transcript_turn(model) failed: %s", e)

            # If save_agent fired during this turn, emit the reveal
            # events IMMEDIATELY and close the loop. This check is BEFORE
            # the extractor backstop on purpose: once the agent is
            # saved, the extractor has nothing useful left to do, and
            # running it first added a visible ~2s lag between the
            # operator's "yes" and the reveal animation.
            if handoff.exit_after_save and handoff.saved_agent is not None:
                log.info("chat[%s]: agent saved, emitting reveal", build_sid[:18])
                await _send_json({"type": "agent_saved", "agent": handoff.saved_agent})
                await _send_json({"type": "build_complete"})
                break

            # #2 Eavesdropping extractor backstop — pull structured
            # slots out of what the operator actually said, in case the
            # model fumbled a record_template_answer. Runs the same
            # extractor the voice path uses, then auto-promotes /
            # syncs any template state it surfaced. Skipped entirely
            # once the build is committed (handled above).
            try:
                from . import extractor as _ex
                await _ex.run_extraction_pass(
                    user_id=user_id, sid=build_sid,
                    transcript_turns=convo_turns[-12:],
                )
                fresh = await db.get_build_session(user_id=user_id, sid=build_sid)
                if fresh:
                    build_monitor.update_slots(fresh)
                    await _gb._auto_template_from_extractor(
                        user_id=user_id, sid=build_sid,
                        build_row=fresh, build_monitor=build_monitor,
                    )
                    await emit_template_question_card()
            except Exception as e:  # noqa: BLE001
                log.warning("extractor backstop failed: %s", e)
    except WebSocketDisconnect:
        log.info("chat[%s]: WebSocketDisconnect", build_sid[:18])
    except Exception as e:  # noqa: BLE001
        log.exception("chat[%s]: loop crashed: %s", build_sid[:18], e)


async def _run_model_turn(
    *,
    chat,
    user_text: str,
    handlers: dict[str, Any],
    send_json,
    build_monitor,
) -> str:
    """Send one user message, stream the model's response, handle any
    function calls inline (loop until none remain), then emit
    turn_complete. Hard cap on tool-call iterations to prevent runaway
    cycles where the model keeps calling tools without producing text.

    Returns the model's accumulated text for this turn (so the caller
    can persist it to the transcript + feed the extractor)."""
    # Initial send: user text wrapped as a single Part.
    next_message: Any = user_text
    full_text: str = ""
    for iteration in range(10):  # hard cap
        try:
            stream = await chat.send_message_stream(next_message)
        except Exception as e:
            log.exception("chat.send_message_stream failed (iter %s): %s", iteration, e)
            raise
        function_calls: list[Any] = []
        async for chunk in stream:
            if not getattr(chunk, "candidates", None):
                continue
            for cand in chunk.candidates:
                content = getattr(cand, "content", None)
                if not content or not getattr(content, "parts", None):
                    continue
                for part in content.parts:
                    txt = getattr(part, "text", None)
                    if txt:
                        full_text += txt
                        # Stream the text chunk to the client as the
                        # SAME `transcript role:model` event the
                        # LandingChatView already consumes.
                        await send_json({"type": "transcript", "role": "model", "text": txt})
                    fc = getattr(part, "function_call", None)
                    if fc and getattr(fc, "name", None):
                        function_calls.append(fc)
        if not function_calls:
            # Model finished without calling any more tools — turn done.
            await send_json({"type": "turn_complete"})
            return full_text
        # Execute every tool call this iteration produced, build
        # function-response parts to feed back into the chat.
        responses: list[Any] = []
        for fc in function_calls:
            name = fc.name
            args = dict(fc.args) if fc.args else {}
            log.info("chat tool_call %s args=%s", name, json.dumps(args, default=str)[:300])
            handler = handlers.get(name)
            if handler is None:
                result: dict[str, Any] = {"ok": False, "error": f"unknown tool {name!r}"}
            else:
                try:
                    result = await handler(args)
                except Exception as e:
                    log.exception("handler %s raised: %s", name, e)
                    result = {"ok": False, "error": str(e)}
            responses.append(types.Part(
                function_response=types.FunctionResponse(name=name, response=result),
            ))
        next_message = responses  # next iteration feeds the function responses back
    # Hit the iteration cap — log and exit cleanly so the loop doesn't
    # spin forever.
    log.warning("chat turn hit 10-iteration cap without resolving — emitting turn_complete")
    await send_json({"type": "turn_complete"})
    return full_text
