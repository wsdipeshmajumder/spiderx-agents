"""Eavesdropping fact extractor for the Eva build flow.

Eva's `note_build_facts` tool is opt-in — the model decides whether to
call it. In production transcripts we see Eva skip the tool when the
conversation is fluent, then re-ask the same fact two turns later after
a context-window blip. The fix is to stop relying on the LLM's tool-
calling discipline and instead extract facts SERVER-SIDE from the same
audio transcripts Gemini Live already streams back to us.

How it plugs in:

  gemini_bridge._pump_gemini_to_client (builder kind only) receives
  `input_transcription` events for the operator's spoken audio and
  `output_transcription` events for Eva's replies. On each user-side
  turn_complete the bridge fires `run_extraction_pass(...)` as a
  background asyncio task. The task:

    1. Bundles the most recent user turns + Eva's most recent reply into
       a short prompt.
    2. Calls a cheap text-only Gemini Flash model with a JSON output
       schema covering every slot we care about.
    3. Merges any new slot values into the build_sessions row via
       db.merge_build_facts. Existing values are preserved — the
       extractor never NULLs out a previously-captured slot.

  Failure is silent: a failed extraction logs a warning, increments
  nothing, and never blocks the audio pump.

Cost: each pass is ~500 input + ~150 output tokens against Flash. Over
a typical 4-turn build that's well under 0.01¢. We can run it on every
turn without budget worry.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Optional

from google import genai
from google.genai import types as genai_types

from . import db


log = logging.getLogger("eva.extractor")


# Text-only Gemini Flash model for the extractor. Cheap, fast, supports
# structured output. Override via env if you need a stable point release
# or a regional override. NOT the same model the Live session uses —
# Live needs Flash Live, this needs vanilla Flash.
EXTRACTOR_MODEL = os.environ.get("GEMINI_EXTRACTOR_MODEL", "gemini-2.5-flash")


# Cap on how many turns of context the extractor sees per pass. Smaller
# = cheaper + faster; larger = catches more cross-turn references
# ("that one you mentioned"). 8 is a sweet spot for typical builds that
# wrap in 4-6 turns total.
_EXTRACT_TURN_WINDOW = 8


# Wall-clock cap per extraction pass. The model call routinely takes
# 2-5s end-to-end, but tail latency can spike past 6s (we saw timeouts
# in real builds). Bumped to 12s with an env override so an operator
# in a slow region can dial up further. Failure to extract on time is
# silent — the next turn's pass picks up the same slot. Tail extraction
# never blocks the audio pump (it's already fire-and-forget).
EXTRACTOR_TIMEOUT_S = float(os.environ.get("GEMINI_EXTRACTOR_TIMEOUT_S", "12.0"))


# Schema for the extractor's structured output. Every field optional,
# every value a string (or string array). Returning a partial blob is
# the common case — extractor returns ONLY what was newly heard.
def _extraction_schema() -> genai_types.Schema:
    """Build the response_schema for the extractor's structured output.
    Mirrors the slot vocabulary in db_pg._BUILD_EXTRA_SCALARS /
    _BUILD_EXTRA_ARRAYS plus the four typed columns."""
    return genai_types.Schema(
        type=genai_types.Type.OBJECT,
        properties={
            # The 4 typed columns on build_sessions.
            "sector_kind":   genai_types.Schema(type=genai_types.Type.STRING),
            "business_name": genai_types.Schema(type=genai_types.Type.STRING),
            "primary_job":   genai_types.Schema(type=genai_types.Type.STRING),
            "agent_name":    genai_types.Schema(type=genai_types.Type.STRING),
            # Business profile slots — go into extras JSONB.
            "language":           genai_types.Schema(type=genai_types.Type.STRING),
            "country":            genai_types.Schema(type=genai_types.Type.STRING),
            "city":               genai_types.Schema(type=genai_types.Type.STRING),
            "address":            genai_types.Schema(type=genai_types.Type.STRING),
            "hours":              genai_types.Schema(type=genai_types.Type.STRING),
            "services":           genai_types.Schema(type=genai_types.Type.STRING),
            "offers":             genai_types.Schema(type=genai_types.Type.STRING),
            "email":              genai_types.Schema(type=genai_types.Type.STRING),
            "website":            genai_types.Schema(type=genai_types.Type.STRING),
            "escalation_phone":   genai_types.Schema(type=genai_types.Type.STRING),
            "notification_phone": genai_types.Schema(type=genai_types.Type.STRING),
            "locale_hint":        genai_types.Schema(type=genai_types.Type.STRING),
            "voice_hint":         genai_types.Schema(type=genai_types.Type.STRING),
            "ambience_hint":      genai_types.Schema(type=genai_types.Type.STRING),
            "persona_hint":       genai_types.Schema(type=genai_types.Type.STRING),
            "greeting_hint":      genai_types.Schema(type=genai_types.Type.STRING),
            "additional_jobs":    genai_types.Schema(
                type=genai_types.Type.ARRAY,
                items=genai_types.Schema(type=genai_types.Type.STRING),
            ),
            "mentioned_guardrails": genai_types.Schema(
                type=genai_types.Type.ARRAY,
                items=genai_types.Schema(type=genai_types.Type.STRING),
            ),
        },
    )


# Slot-vocabulary instruction. The model's biggest failure mode is
# inventing values the operator didn't actually say — so the prompt is
# heavy on "ONLY return X if you heard it explicitly". The opposite
# failure mode (extractor never returns anything) is fine — silent
# defaults pick up the slack at save_agent time.
_EXTRACTOR_SYSTEM_PROMPT = """You are a structured-output extractor running ALONGSIDE a voice agent builder.
A human operator is talking to "Eva" to set up a phone-AI agent for their business.
You receive the recent transcript of that conversation. Your job: pull factual slots OUT of what the OPERATOR said.

CRITICAL RULES — read every time:

  1. ONLY extract slots the operator EXPLICITLY stated. If the operator said "I run a dental clinic" → sector_kind="dental". If they didn't say a business name, do NOT invent one.
  2. NEVER fabricate. NEVER infer from sector — that's downstream's job. Don't guess hours, languages, country from context clues. Quote-or-skip.
  3. Return ONLY slots whose value is new or corrected in the most recent operator turn. If the operator already said the business name 3 turns ago, don't re-emit it — the server already has it. Emit only what's NEW.
  4. If nothing new this turn — return an empty object {}. That's the normal case; don't pad it.
  5. Eva is the OTHER speaker. NEVER extract from Eva's words. If Eva said "shall we call her Maya?", do NOT extract agent_name=Maya UNLESS the operator answered yes (or proposed Maya themselves).

Slot definitions (only emit when actually heard):

  • sector_kind        : the business type, in the operator's words ("dental", "homeopathic pharmacy", "automotive showroom").
  • business_name      : the actual brand — LITERAL transcription of what the operator SAID. If they said "smile and dental", emit "smile and dental" (NOT "Smyle N Dental", NOT "Smile & Dental", NOT any creative respelling). Do not capitalize differently. Do not add "Pvt Ltd" or "Clinic" or any suffix the operator didn't say. If they only described the business type without naming it ("a dental clinic", "a salon") — omit business_name entirely. NEVER invent or stylize a brand.
  • primary_job        : top 1-2 things callers do. "book and reschedule appointments". Short phrase.
  • agent_name         : the receptionist agent's name (Maya, Sofia). Only if confirmed by the operator.
  • language           : languages the agent should speak. "Hindi", "Hindi, English", "Bangla only".
  • country            : ISO-2 code. India="IN", US="US", UK="GB", Singapore="SG". Only if explicit.
  • city               : "Bangalore", "Mumbai", "London".
  • address            : full street/area address if mentioned.
  • hours              : human-readable. "Mon–Sat 9 AM – 9 PM, closed Sun" or "till 9 every day".
  • services           : free-text list, in the operator's words.
  • offers             : current promotions ("free first consultation").
  • email              : business email.
  • website            : URL.
  • escalation_phone   : caller-facing phone for "put me through to a human".
  • notification_phone : operator's own SMS line (often distinct from escalation_phone).
  • locale_hint        : BCP-47 locale if obvious — "en-IN", "hi-IN", "en-US", "en-GB", "en-SG".
  • voice_hint         : if the operator said "make her sound female / younger / more formal".
  • ambience_hint      : if mentioned ("she should sound like she's in a clinic").
  • persona_hint       : one-line persona descriptor if the operator described one.
  • greeting_hint      : if the operator dictated a specific greeting line.
  • additional_jobs    : array of OTHER things callers do beyond primary_job.
  • mentioned_guardrails : array of "always do X" / "never do X" rules the operator named.

Return JSON only — no commentary, no markdown, no preamble."""


# Single shared client. Reused across passes so we don't pay TCP handshake
# per turn. The Live-session client is in gemini_bridge; this one is a
# parallel instance configured the same way but never used for Live.
_client: Optional[genai.Client] = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY env var is not set")
        _client = genai.Client(api_key=api_key)
    return _client


def _format_transcript_for_extractor(turns: list[dict[str, Any]]) -> str:
    """Render the recent turn window for the extractor prompt. Each
    line tagged Operator:/Eva: so the model can attribute statements
    correctly. Caps each turn at 800 chars to keep the prompt tight."""
    lines: list[str] = []
    for t in turns[-_EXTRACT_TURN_WINDOW:]:
        role = (t.get("role") or "").lower()
        text = (t.get("text") or "").strip()
        if not text:
            continue
        speaker = "Operator" if role == "user" else "Eva"
        lines.append(f"{speaker}: {text[:800]}")
    return "\n".join(lines)


def _normalize_slots(raw: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split the extractor's flat dict into (typed_facts, extras_dict).
    Typed facts hit the 4 columns on build_sessions; the rest go into
    the extras JSONB. Empty values are dropped at this layer so the
    db layer doesn't need to re-check."""
    typed_keys = ("sector_kind", "business_name", "primary_job", "agent_name")
    typed: dict[str, Any] = {}
    extras: dict[str, Any] = {}
    for k, v in (raw or {}).items():
        if v is None:
            continue
        if isinstance(v, str):
            v = v.strip()
            if not v:
                continue
        elif isinstance(v, list):
            v = [x.strip() if isinstance(x, str) else x for x in v]
            v = [x for x in v if x]
            if not v:
                continue
        else:
            # Unexpected shape — skip rather than crash.
            continue
        if k in typed_keys:
            typed[k] = v
        else:
            extras[k] = v
    return typed, extras


async def run_extraction_pass(
    *,
    user_id: Optional[int],
    sid: str,
    transcript_turns: list[dict[str, Any]],
    timeout_s: float = EXTRACTOR_TIMEOUT_S,
) -> Optional[dict[str, Any]]:
    """Run one extraction pass against the most recent transcript window.
    Fire-and-forget from the caller — never raise. Returns the slot
    dict actually persisted (typed + extras merged), or None if nothing
    was extracted / persisted. Pure side-effect for the caller;
    return value is for tests/logging.

    Guardrails:
      • Skips if `sid` is empty or the window has no operator turns.
      • Bounded by `timeout_s` — extraction must NOT block the audio
        pipeline forever even if the Gemini text model is slow.
      • All exceptions caught + logged at WARN; never propagates.
    """
    if not sid:
        return None
    if not transcript_turns:
        return None
    user_turns = [t for t in transcript_turns if (t.get("role") or "").lower() == "user"]
    if not user_turns:
        # Nothing the operator said yet — extractor has nothing to do.
        return None

    rendered = _format_transcript_for_extractor(transcript_turns)
    if not rendered:
        return None

    try:
        client = _get_client()
    except Exception as e:  # noqa: BLE001
        log.warning("extractor: client init failed: %s", e)
        return None

    config = genai_types.GenerateContentConfig(
        system_instruction=_EXTRACTOR_SYSTEM_PROMPT,
        response_mime_type="application/json",
        response_schema=_extraction_schema(),
        # Slight temperature lets the model be flexible about phrasing
        # ("till 9" → "open till 9 PM") without hallucinating. The schema
        # is the structural guardrail.
        temperature=0.2,
    )

    async def _call_model() -> Optional[str]:
        # `client.aio.models.generate_content` is the async path; wrap
        # in asyncio.wait_for to bound runtime.
        resp = await client.aio.models.generate_content(
            model=EXTRACTOR_MODEL,
            contents=rendered,
            config=config,
        )
        return getattr(resp, "text", None)

    try:
        raw_text = await asyncio.wait_for(_call_model(), timeout=timeout_s)
    except asyncio.TimeoutError:
        log.warning("extractor: timeout after %.1fs (sid=%s)", timeout_s, sid[:18])
        return None
    except Exception as e:  # noqa: BLE001
        log.warning("extractor: model call failed: %s", e)
        return None

    if not raw_text:
        return None

    # The model is configured for JSON output but defensively parse —
    # mid-prompt drift sometimes produces leading whitespace or stray
    # backticks.
    try:
        raw_text = raw_text.strip()
        if raw_text.startswith("```"):
            raw_text = raw_text.strip("`")
            if raw_text.startswith("json"):
                raw_text = raw_text[4:]
        parsed = json.loads(raw_text)
        if not isinstance(parsed, dict):
            return None
    except (json.JSONDecodeError, ValueError) as e:
        log.warning("extractor: JSON parse failed: %s (raw=%s)", e, raw_text[:200])
        return None

    typed, extras = _normalize_slots(parsed)
    if not typed and not extras:
        # Nothing new — the model correctly said "this turn added nothing".
        return None

    try:
        merged = await db.merge_build_facts(
            user_id=user_id, sid=sid,
            sector_kind=typed.get("sector_kind"),
            business_name=typed.get("business_name"),
            primary_job=typed.get("primary_job"),
            agent_name=typed.get("agent_name"),
            extras=extras or None,
        )
        await db.bump_extraction_count(user_id=user_id, sid=sid)
        log.info(
            "extractor.persisted sid=%s typed=%s extras=%s",
            sid[:18], list(typed.keys()), list(extras.keys()),
        )
        return {"typed": typed, "extras": extras, "row": merged}
    except Exception as e:  # noqa: BLE001
        log.warning("extractor: persist failed: %s", e)
        return None
