"""Single WebSocket session that holds the entire end-to-end voice experience.

Flow
────
1. Browser connects to /ws/session.
2. We open a Live session with the Eva *builder* persona. Eva greets the
   user, knows the list of existing agents (so it can offer to call any of
   them by voice), and exposes two tools:
      • save_agent(name, sector, locale, voice, system_prompt, greeting, …)
      • select_agent(agent_id)
3. When either tool fires, we mark a handoff and wait for the current model
   turn to finish (so Eva's "let me put you through…" gets spoken).
4. We close the builder Live session and open a fresh one configured as the
   target agent. The browser's WS stays open the whole time — the user
   experiences a single uninterrupted conversation with a voice that changes.
5. The target agent's saved connectors are wired as Gemini function tools
   that dispatch to backend/connectors.py.

The relay itself: PCM16 16 kHz mic chunks (browser → server → Gemini) +
PCM16 24 kHz audio (Gemini → server → browser). Plain WebSocket binary frames.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import traceback
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import WebSocket, WebSocketDisconnect
from google import genai
from google.genai import types

from . import db
from .connectors import CONNECTOR_DECLS, build_tools as build_connector_tools, handle as handle_connector
from .presets import (
    CONNECTOR_TYPES,
    DEFAULT_VOICE as _PRESETS_DEFAULT_VOICE,
    GUARDRAIL_LIBRARY,
    LOCALES,
    SECTORS,
    SIP_PROVIDERS,
    VOICES,
)

log = logging.getLogger("eva.bridge")

DEFAULT_MODEL = os.environ.get("GEMINI_LIVE_MODEL", "gemini-3.1-flash-live-preview")
# Single model policy: Gemini 3.1 Flash Live (cascade Live).
#
# Every behaviour in this bridge — the NO_INTERRUPTION activity handling, the
# tightened LOW-sensitivity VAD, the reconnect preamble + transcript recap,
# the never-re-greet system prompt block — was tuned and verified against
# this model. The native-audio family ignores `realtime_input_config` and
# `language_code`, accepts a different set of generation params, and has its
# own drop pattern, so falling back to it would silently undo all of the
# above and re-introduce the bugs we just fixed.
#
# If you ever need to override (e.g. to test a stable point release), set
# GEMINI_LIVE_MODEL in the environment — this code path will still try
# fallbacks before giving up, but the fallback list is intentionally empty.
FALLBACK_MODELS = [
    "gemini-3.1-flash-live-preview",
]


def _client() -> genai.Client:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY env var is not set")
    return genai.Client(api_key=api_key)


# ───────────────────────── system prompts ────────────────────────────────


# ─────────────────────────── region inference ───────────────────────────


_TZ_REGION = {
    # India
    "Asia/Kolkata": "IN", "Asia/Calcutta": "IN",
    # UK / Ireland
    "Europe/London": "GB", "Europe/Dublin": "IE",
    # US (sample)
    "America/New_York": "US", "America/Chicago": "US", "America/Denver": "US",
    "America/Los_Angeles": "US", "America/Phoenix": "US",
    # Other anglosphere
    "Australia/Sydney": "AU", "Australia/Melbourne": "AU", "Australia/Brisbane": "AU",
    "Australia/Perth": "AU", "Pacific/Auckland": "NZ",
    "America/Toronto": "CA", "America/Vancouver": "CA",
    # Gulf
    "Asia/Dubai": "AE", "Asia/Riyadh": "SA", "Asia/Qatar": "QA",
    # SE Asia
    "Asia/Singapore": "SG", "Asia/Kuala_Lumpur": "MY", "Asia/Manila": "PH",
    "Asia/Jakarta": "ID", "Asia/Bangkok": "TH", "Asia/Ho_Chi_Minh": "VN",
    # East Asia
    "Asia/Tokyo": "JP", "Asia/Seoul": "KR", "Asia/Shanghai": "CN",
    "Asia/Hong_Kong": "HK", "Asia/Taipei": "TW",
    # Europe (selected)
    "Europe/Paris": "FR", "Europe/Berlin": "DE", "Europe/Madrid": "ES",
    "Europe/Rome": "IT", "Europe/Amsterdam": "NL",
    # LatAm
    "America/Mexico_City": "MX", "America/Sao_Paulo": "BR", "America/Bogota": "CO",
    "America/Buenos_Aires": "AR",
    # Africa
    "Africa/Lagos": "NG", "Africa/Johannesburg": "ZA", "Africa/Nairobi": "KE",
}


def _country_from(locale: str, tz: str) -> str:
    if tz in _TZ_REGION:
        return _TZ_REGION[tz]
    if locale and "-" in locale:
        return locale.split("-")[-1].upper()
    return "US"


# Re-export the platform-wide default from presets so this module can
# reference it locally. presets.DEFAULT_VOICE is the canonical source —
# changing it there flips the fallback for both Eva and db_pg in one go.
DEFAULT_VOICE = _PRESETS_DEFAULT_VOICE


def _english_variant(country: str) -> str:
    """Return the best English locale Gemini Live ships for this country.

    The Live API only ships en-IN / en-GB / en-US / en-AU. Non-English
    countries don't get their own English variant — we fall back to
    en-GB as the most neutral choice for non-anglophone operators
    (closer to how international business English actually sounds than
    en-US, which carries strong American idiom). Eva's speaking_note
    in `_REGION_PROFILES` carries the country flavour separately."""
    return {
        # Anglophone — Gemini-native variants
        "IN": "en-IN", "GB": "en-GB", "UK": "en-GB",
        "AU": "en-AU", "NZ": "en-AU",
        "CA": "en-US",  # Canadian English is closer to en-US than en-GB
        "IE": "en-GB", "ZA": "en-GB",
        "SG": "en-GB", "HK": "en-GB", "MY": "en-GB", "PH": "en-US",
        # Gulf — Indian-English is the lingua franca
        "AE": "en-IN", "SA": "en-IN", "QA": "en-IN", "KW": "en-IN", "BH": "en-IN", "OM": "en-IN",
        # Americas
        "US": "en-US", "MX": "en-US", "BR": "en-US", "AR": "en-US", "CO": "en-US", "CL": "en-US",
        # Continental Europe — en-GB is the neutral international default
        "DE": "en-GB", "FR": "en-GB", "ES": "en-GB", "IT": "en-GB", "NL": "en-GB",
        "PL": "en-GB", "PT": "en-GB", "SE": "en-GB", "DK": "en-GB",
        # East Asia (English as business language). en-GB is the neutral
        # international register — closer to how Japanese/Korean business
        # English actually sounds than the American midwestern of en-US.
        "JP": "en-GB", "CN": "en-GB", "KR": "en-GB", "TW": "en-GB",
        # SE Asia
        "ID": "en-GB", "TH": "en-GB", "VN": "en-GB",
        # Africa
        "NG": "en-GB", "KE": "en-GB",
    }.get(country, "en-GB")


_REGION_PROFILES = {
    "IN": {
        "speaking_note": "Indian English accent, warm and unhurried.",
        "default_agent_locale": "hi-IN",
        "default_second_languages": "Hindi as primary; offer Tamil, Bengali, Marathi, Kannada, Telugu when sector suggests south/east India.",
        "default_sip": "exotel",
        "default_currency": "INR",
        "default_voice": "Aoede",   # warm + clear; tested heavily for en-IN
        "naming_hint": "Indian first names — Maya, Priya, Anjali, Asha, Riya, Kabir, Rohan, Arjun.",
    },
    "GB": {
        "speaking_note": "British English, dry warmth.",
        "default_agent_locale": "en-GB",
        "default_second_languages": "primarily English; Polish or Urdu only if user mentions a multi-lingual catchment.",
        "default_sip": "twilio",
        "default_currency": "GBP",
        "default_voice": "Leda",     # measured, professional
        "naming_hint": "British/European first names — Emily, Olivia, Aoife, Saoirse, James, Oliver.",
    },
    "US": {
        "speaking_note": "American English, friendly midwestern register.",
        "default_agent_locale": "en-US",
        "default_second_languages": "primarily English; offer Spanish if sector typically multilingual.",
        "default_sip": "twilio",
        "default_currency": "USD",
        "default_voice": "Aoede",
        "naming_hint": "American first names — Maya, Olivia, Ava, Sofia, Daniel, Ethan, Jordan.",
    },
    "SG": {
        "speaking_note": "Singaporean English, crisp and friendly with occasional Singlish warmth.",
        "default_agent_locale": "en-GB",  # Live API doesn't ship en-SG; en-GB is closest.
        "default_second_languages": "Mandarin / Malay / Tamil if business hints at multicultural catchment.",
        "default_sip": "twilio",
        "default_currency": "SGD",
        "default_voice": "Leda",
        "naming_hint": "Mixed — Mei, Hui, Aisha, Aravind, Wei, Daniel, Priya, Jia.",
    },
    "AU": {
        "speaking_note": "Australian English, relaxed and brisk.",
        "default_agent_locale": "en-AU",
        "default_second_languages": "primarily English.",
        "default_sip": "twilio",
        "default_currency": "AUD",
        "default_voice": "Aoede",
        "naming_hint": "Australian first names — Charlotte, Mia, Jack, Oliver.",
    },
    "AE": {
        "speaking_note": "Indian-English accent (very common in the UAE/Gulf), warm and professional.",
        "default_agent_locale": "en-IN",
        "default_second_languages": "Hindi for retail/services; Arabic if user mentions Emirati customers.",
        "default_sip": "twilio",
        "default_currency": "AED",
        "default_voice": "Aoede",
        "naming_hint": "Mixed — Maya, Aisha, Karan, Mohammed, Sara.",
    },
    # ── Continental Europe ──────────────────────────────────────────
    # Non-anglophone profiles. Operators here run their businesses in
    # local languages but international callers hit them in English, so
    # we use English as the agent's primary locale with neutral en-GB.
    "DE": {
        "speaking_note": "Neutral international English with a clear, direct German register. Concise sentences, no fluff.",
        "default_agent_locale": "en-GB",
        "default_second_languages": "German as primary if local catchment; English for international callers.",
        "default_sip": "twilio",
        "default_currency": "EUR",
        "default_voice": "Leda",
        "naming_hint": "German first names — Anna, Lena, Lukas, Max, Sophie, Felix.",
    },
    "FR": {
        "speaking_note": "Neutral international English with a French-influenced register. Polite, slightly formal.",
        "default_agent_locale": "en-GB",
        "default_second_languages": "French as primary for local; English for international.",
        "default_sip": "twilio",
        "default_currency": "EUR",
        "default_voice": "Leda",
        "naming_hint": "French first names — Camille, Sophie, Léa, Lucas, Hugo, Léo.",
    },
    "ES": {
        "speaking_note": "Neutral international English with a warm Spanish register.",
        "default_agent_locale": "en-GB",
        "default_second_languages": "Spanish as primary for local; English for international.",
        "default_sip": "twilio",
        "default_currency": "EUR",
        "default_voice": "Leda",
        "naming_hint": "Spanish first names — María, Lucía, Sofía, Mateo, Lucas, Diego.",
    },
    # ── Americas (non-US) ──────────────────────────────────────────
    "BR": {
        "speaking_note": "Warm, melodic register; Brazilian Portuguese flavour. Friendly + expressive.",
        "default_agent_locale": "en-US",
        "default_second_languages": "Portuguese as primary; English for international callers.",
        "default_sip": "twilio",
        "default_currency": "BRL",
        "default_voice": "Aoede",
        "naming_hint": "Brazilian first names — Ana, Maria, Beatriz, Pedro, Lucas, Gabriel.",
    },
    "MX": {
        "speaking_note": "Friendly, warm, slightly formal — Mexican Spanish register.",
        "default_agent_locale": "en-US",
        "default_second_languages": "Spanish as primary; English for cross-border callers.",
        "default_sip": "twilio",
        "default_currency": "MXN",
        "default_voice": "Aoede",
        "naming_hint": "Mexican first names — María, Sofía, Valentina, Mateo, Santiago, Diego.",
    },
    "CA": {
        "speaking_note": "Canadian English — polite, even-keeled, mild American register.",
        "default_agent_locale": "en-US",
        "default_second_languages": "English primary; French in Québec.",
        "default_sip": "twilio",
        "default_currency": "CAD",
        "default_voice": "Aoede",
        "naming_hint": "Canadian first names — Olivia, Emma, Ava, Liam, Noah, Owen.",
    },
    # ── East Asia ──────────────────────────────────────────────────
    "JP": {
        "speaking_note": "Polite, measured English with Japanese register sensibility. Confirm carefully, never rush.",
        "default_agent_locale": "en-GB",
        "default_second_languages": "Japanese as primary; English for international.",
        "default_sip": "twilio",
        "default_currency": "JPY",
        "default_voice": "Leda",
        "naming_hint": "Japanese first names — Yuki, Sakura, Hina, Haruto, Yuto, Sora.",
    },
    # ── Anglophone-business stubs ──────────────────────────────────
    # These countries don't have full local-language profiles yet but
    # `_english_variant` already maps them to en-GB or en-US. Without a
    # region profile they'd fall through to the US profile and emit
    # American currency / SIP / naming hints into Eva's brief. Stubs
    # keep the brief at least directionally correct until full profiles
    # land. Promote to full status as we gain users in each market.
    "NL": {
        "speaking_note": "Direct, dry, efficient English. Dutch operators expect no fluff.",
        "default_agent_locale": "en-GB", "default_voice": "Leda",
        "default_second_languages": "Dutch as primary; English for international.",
        "default_sip": "twilio", "default_currency": "EUR",
        "naming_hint": "Dutch first names — Anna, Sophie, Lotte, Lucas, Daan, Sem.",
    },
    "IT": {
        "speaking_note": "Warm, expressive English with Italian cadence.",
        "default_agent_locale": "en-GB", "default_voice": "Leda",
        "default_second_languages": "Italian as primary; English for international.",
        "default_sip": "twilio", "default_currency": "EUR",
        "naming_hint": "Italian first names — Sofia, Giulia, Aurora, Leonardo, Francesco, Lorenzo.",
    },
    "KR": {
        "speaking_note": "Polite, measured English with Korean register sensibility.",
        "default_agent_locale": "en-GB", "default_voice": "Leda",
        "default_second_languages": "Korean as primary; English for international.",
        "default_sip": "twilio", "default_currency": "KRW",
        "naming_hint": "Korean first names — Seo-yeon, Ji-woo, Min-jun, Do-yoon, Joon-ho.",
    },
    "ZA": {
        "speaking_note": "South African English — friendly, slightly formal.",
        "default_agent_locale": "en-GB", "default_voice": "Leda",
        "default_second_languages": "English primary; Afrikaans / Zulu / Xhosa per region.",
        "default_sip": "twilio", "default_currency": "ZAR",
        "naming_hint": "Mixed — Olivia, Lerato, Naledi, Daniel, Bandile, Sipho.",
    },
    "NZ": {
        "speaking_note": "New Zealand English — relaxed, friendly.",
        "default_agent_locale": "en-AU", "default_voice": "Aoede",
        "default_second_languages": "English primary.",
        "default_sip": "twilio", "default_currency": "NZD",
        "naming_hint": "Kiwi first names — Charlotte, Mia, Olivia, Jack, Oliver, Hunter.",
    },
    "IE": {
        "speaking_note": "Irish English — warm, lilting, conversational.",
        "default_agent_locale": "en-GB", "default_voice": "Leda",
        "default_second_languages": "English primary.",
        "default_sip": "twilio", "default_currency": "EUR",
        "naming_hint": "Irish first names — Saoirse, Aoife, Niamh, Conor, Cian, Oisín.",
    },
}


def _region_hint(locale: str, tz: str) -> dict[str, str]:
    country = _country_from(locale, tz)
    profile = _REGION_PROFILES.get(country, _REGION_PROFILES["US"])
    # `default_voice` is read by Eva's prompt as the region-appropriate
    # voice suggestion. Falls back to the platform-wide DEFAULT_VOICE
    # constant if a future region forgets to set one.
    default_voice = profile.get("default_voice", DEFAULT_VOICE)
    defaults_text = (
        f"    - Country (inferred): {country}\n"
        f"    - Default agent locale to suggest: {profile['default_agent_locale']}\n"
        f"    - Default voice for this region: {default_voice}\n"
        f"    - Second-language guidance: {profile['default_second_languages']}\n"
        f"    - SIP provider default: {profile['default_sip']}\n"
        f"    - Currency: {profile['default_currency']}\n"
        f"    - Name suggestions: {profile['naming_hint']}"
    )
    return {
        "country": country,
        "region": country,
        "speaking_note": profile["speaking_note"],
        "defaults_text": defaults_text,
    }


# ─────────────────────────── agents brief ───────────────────────────────


async def _agents_brief(user_id: Optional[int] = None) -> str:
    # Scoped to the current user post-Phase-A multi-tenancy. Fall back to the
    # founder if no user_id was threaded down (keeps unauthenticated dev
    # flows from breaking the builder prompt).
    uid = user_id if user_id is not None else (await db.get_founder())["id"]
    rows = await db.list_agents(uid)
    if not rows:
        return "There are no saved agents yet."
    parts = []
    for r in rows[:12]:
        parts.append(f"  #{r['id']} — {r['name']} ({r.get('sector') or '?'}, {r.get('locale') or '?'})")
    return "Existing saved agents (the user can choose to call any of them by voice):\n" + "\n".join(parts)


def _format_build_facts_block(row: Optional[dict[str, Any]]) -> str:
    """Render the build_session row as a Facts-already-collected block
    that gets prepended to Eva's system prompt on every (re)connect.
    The whole point is that Eva CANNOT structurally re-ask any fact
    that appears in this block — it's authoritative and overrides any
    "ask the user" rule lower in the prompt.

    Renders both the four typed-column facts AND every soft slot the
    server-side extractor has captured from the operator's transcript
    (language, country, hours, services, etc.) — so Eva never re-asks
    "what language?" because the extractor heard the user say "Hindi"
    three turns ago.

    Returns an empty string when there are no facts yet (fresh build);
    the caller can safely concat unconditionally."""
    if not row:
        return ""
    typed_pairs = [
        ("SECTOR_KIND",   row.get("sector_kind")),
        ("BUSINESS_NAME", row.get("business_name")),
        ("PRIMARY_JOB",   row.get("primary_job")),
        ("AGENT_NAME",    row.get("agent_name")),
    ]
    # Render every soft slot the extractor (or Eva) has persisted.
    # Order matches the dashboard's Business profile + Persona pages.
    # Defensive: a pre-fix corrupted row may have stored extras as a
    # JSONB array of stringified blobs (see merge_build_facts comment).
    # Treat anything not-a-dict as empty so the prompt block renders
    # cleanly instead of crashing.
    extras = row.get("extras")
    if not isinstance(extras, dict):
        extras = {}
    soft_pairs = [
        ("LANGUAGE",            extras.get("language")),
        ("COUNTRY",             extras.get("country")),
        ("CITY",                extras.get("city")),
        ("ADDRESS",             extras.get("address")),
        ("HOURS",               extras.get("hours")),
        ("SERVICES",            extras.get("services")),
        ("OFFERS",              extras.get("offers")),
        ("EMAIL",               extras.get("email")),
        ("WEBSITE",             extras.get("website")),
        ("ESCALATION_PHONE",    extras.get("escalation_phone")),
        ("NOTIFICATION_PHONE",  extras.get("notification_phone")),
        ("LOCALE_HINT",         extras.get("locale_hint")),
        ("VOICE_HINT",          extras.get("voice_hint")),
        ("AMBIENCE_HINT",       extras.get("ambience_hint")),
        ("PERSONA_HINT",        extras.get("persona_hint")),
        ("GREETING_HINT",       extras.get("greeting_hint")),
    ]
    additional_jobs = extras.get("additional_jobs") or []
    mentioned_guardrails = extras.get("mentioned_guardrails") or []

    known_typed = [(k, v) for k, v in typed_pairs if v]
    known_soft = [(k, v) for k, v in soft_pairs if v]
    if not known_typed and not known_soft and not additional_jobs and not mentioned_guardrails:
        return ""

    lines: list[str] = []
    if known_typed:
        lines.append("  -- Core --")
        for k, v in known_typed:
            lines.append(f"  • {k:<20} : {v}")
    if known_soft:
        lines.append("  -- Business profile / Persona --")
        for k, v in known_soft:
            lines.append(f"  • {k:<20} : {v}")
    if additional_jobs:
        joined = ", ".join(str(x) for x in additional_jobs[:6])
        lines.append(f"  • ADDITIONAL_JOBS     : {joined}")
    if mentioned_guardrails:
        joined = "; ".join(str(x) for x in mentioned_guardrails[:6])
        lines.append(f"  • GUARDRAILS_HEARD    : {joined}")
    body = "\n".join(lines)

    return (
        "=========================================================\n"
        "FACTS ALREADY COLLECTED — DO NOT RE-ASK THESE.\n"
        "---------------------------------------------------------\n"
        "These facts are PERSISTED in the database. They survive Gemini\n"
        "session drops AND WS-level reconnects. Some were captured by\n"
        "your own note_build_facts calls, others were heard directly\n"
        "from the operator by the server-side extractor.\n"
        "\n"
        "Treat EVERY line below as LOCKED ground truth:\n"
        "  • Do NOT ask the operator about it.\n"
        "  • Do NOT propose or confirm it ('was it BrightSmile?').\n"
        "  • Do NOT paraphrase it back as a check-in.\n"
        "  • If the operator actively CORRECTS a value, call\n"
        "    note_build_facts with the new value and continue silently.\n"
        "\n"
        f"{body}\n"
        "\n"
        "When composing save_agent, fold every soft slot above into the\n"
        "appropriate field (LANGUAGE → variables.languages, HOURS →\n"
        "variables.hours, GREETING_HINT → greeting, PERSONA_HINT →\n"
        "persona, etc.). The server backfills any you miss, but you\n"
        "should still include them so the system_prompt YOU write\n"
        "matches the data.\n"
        "=========================================================\n\n"
    )


async def _auto_template_from_extractor(
    *,
    user_id: Optional[int],
    sid: str,
    build_row: dict[str, Any],
    build_monitor,
) -> None:
    """Bridge the eavesdropping extractor (and note_build_facts) to the
    deterministic template flow.

    Two jobs, both safety-nets for "Eva never called the tool":

      (1) Auto-pick the template if `template_id` is unset and the row
          has enough signal (industry + maybe locale/city). Reuses
          `find_best_match`, then persists via `db.set_build_template`
          and updates `build_monitor.template_id` so the next BUILD STATE
          block carries the NEXT QUESTION line.

      (2) Sync any facts the extractor already captured into
          `template_answers` — business_name, hours, city, etc. — so
          `next_unanswered_question` skips past them. Without this,
          Eva would ask the operator their business name a second time
          right after the extractor heard them say it.

    Both jobs are idempotent and silently no-op when there's nothing
    new to write. Exceptions bubble up to the caller's try/except so a
    DB hiccup never breaks the build flow.
    """
    from . import build_templates as _bt

    # (1) Auto-pick the template if not already locked.
    if not build_monitor.template_id:
        facets = _bt.facets_from_build_row(build_row)
        if facets.get("industry"):
            try:
                match = _bt.find_best_match(
                    industry=facets.get("industry"),
                    sub_industry=facets.get("sub_industry"),
                    locale=facets.get("locale"),
                    country=facets.get("country"),
                    city=facets.get("city"),
                )
            except Exception as e:  # noqa: BLE001
                log.warning("auto_template: find_best_match failed: %s", e)
                match = None
            # Only auto-promote to a NAMED template — never auto-lock
            # the operator into the `_generic` fallback. _generic is
            # for "no real match found"; locking it would prevent a
            # later, better signal from upgrading the build.
            if match and match.get("id") and match["id"] != "_generic":
                tid = match["id"]
                try:
                    await db.set_build_template(
                        user_id=user_id, sid=sid, template_id=tid,
                    )
                except Exception as e:  # noqa: BLE001
                    log.warning("auto_template: set_build_template failed: %s", e)
                build_monitor.template_id = tid
                log.warning(
                    "build_state[%s]: AUTO-TEMPLATE LOCKED %s from extractor facets (industry=%s sub=%s locale=%s city=%s)",
                    sid[:18], tid,
                    facets.get("industry"), facets.get("sub_industry"),
                    facets.get("locale"), facets.get("city"),
                )

    # (2) Sync extractor-captured slots into template_answers. Runs
    # regardless of whether the lock happened just now or earlier — new
    # extractor passes keep adding facts (hours, phone, services) that
    # match template questions Eva otherwise has to re-ask.
    if not build_monitor.template_id:
        return
    template = _bt.get_template(build_monitor.template_id)
    if not template:
        return
    captured = _bt.extracted_answers_from_build_row(template, build_row)
    if not captured:
        return
    already = build_monitor.template_answers or {}
    synced = 0
    for qid, val in captured.items():
        if qid in already:
            continue
        # Run the captured value through the per-question validator so
        # we never write malformed data (an "enum" question with an
        # off-list value would be a worse bug than just re-asking).
        q = _bt.question_by_id(template, qid)
        if not q:
            continue
        try:
            normalized, err = _bt.validate_answer(q, val)
        except Exception as e:  # noqa: BLE001
            log.warning("auto_template: validate %s=%r raised %s — skipping", qid, val, e)
            continue
        if err is not None:
            # Extracted value didn't pass validation — leave for Eva to
            # re-ask explicitly (she might mishear, the extractor might
            # mishear, but at least one of them gets a clean pass).
            log.info("auto_template: skip %s=%r (validation: %s)", qid, val, err)
            continue
        try:
            await db.record_template_answer(
                user_id=user_id, sid=sid, question_id=qid, value=normalized,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("auto_template: record_template_answer(%s) failed: %s", qid, e)
            continue
        build_monitor.template_answers[qid] = normalized
        synced += 1
    if synced:
        log.warning(
            "build_state[%s]: AUTO-SYNC %d extractor answer(s) into template_answers (template=%s)",
            sid[:18], synced, build_monitor.template_id,
        )


async def _builder_system_prompt(
    *,
    client_locale: str = "en-US",
    client_tz: str = "UTC",
    user_id: Optional[int] = None,
    text_only: bool = False,
) -> str:
    # `text_only=True` is the chat-mode flag from the WS query string
    # (mode=text). In chat mode we:
    #  - DROP the SAVED AGENTS context entirely — operator can't pick
    #    an existing agent via chat anyway, and listing names like
    #    "Riya" gives the model raw material to hallucinate the old
    #    "Building new today, or want to call Riya?" opener. With the
    #    list gone, the model can't pull names that don't exist for
    #    this session.
    #  - REPLACE the two-case opener with a flat "DO NOT greet, the
    #    operator already typed their intent" directive. Chat sessions
    #    are always initiated by an explicit prompt submission.
    agents_brief = await _agents_brief(user_id) if not text_only else (
        "(chat mode — operator's saved agents intentionally hidden so "
        "your opener can't accidentally offer to call one. If they want "
        "to call an existing agent they'll use the dashboard nav, not "
        "this build chat.)"
    )
    sectors = ", ".join(s["id"] for s in SECTORS)
    locales = ", ".join(l["id"] for l in LOCALES)
    voices = ", ".join(v["id"] for v in VOICES)
    connectors = ", ".join(c["id"] for c in CONNECTOR_TYPES)
    region = _region_hint(client_locale, client_tz)
    # The OPENER block differs between chat (text_only) and voice mode.
    # Built as a variable so the f-string template doesn't have to host
    # multi-line ternaries with backslashes (which Python f-strings
    # don't allow).
    if text_only:
        opener_block = (
            "CHAT MODE — operator has already typed an intent prompt. NEVER greet. "
            "NEVER say 'Hi, I'm Eva'. NEVER mention calling an existing agent. The "
            "intent is ALWAYS to build a new agent — get on with it.\n"
            "\n"
            "  Your first response must be:\n"
            "    (a) a 2-3 word acknowledgement ('Got it.'), AND\n"
            "    (b) immediately call select_build_template with whatever triage signal\n"
            "        you have, OR ask ONE short triage question if you genuinely need it.\n"
            "\n"
            "  FORBIDDEN first-turn phrases (these are bugs from prior versions):\n"
            "    • 'Hi, I'm Eva'\n"
            "    • 'Building new today, or want to call <name>?'\n"
            "    • 'Would you like to call <existing agent>?'\n"
            "    • Any introduction of yourself\n"
            "    • Any offer to call an existing saved agent\n"
            "\n"
            "  The operator clicked or typed into a chat composer that already said\n"
            "  'Build with Eva' — they did NOT come here to be greeted or offered an\n"
            "  existing agent. Skip straight to substance."
        )
    else:
        opener_block = (
            "The operator may enter this session in TWO ways. Detect which and act accordingly:\n"
            "\n"
            "  CASE A — Empty / pleasantry first turn (the operator said nothing substantive\n"
            "  yet, OR you receive a bracketed `[The caller is on the line…]` directive):\n"
            "    Say EXACTLY ONE opener and STOP. No additional content this turn.\n"
            "\n"
            "      \"Hi, I'm Eva. What kind of agent shall we build today?\"\n"
            "\n"
            "  CASE B — Substantive first turn (operator opened with something like\n"
            "  \"Car dealership — callers ask about models…\" or \"I run a dental clinic\n"
            "  in Bangalore\"):\n"
            "    DO NOT GREET. The operator already told you what they want. Skip the\n"
            "    \"Hi, I'm Eva\" line entirely. Your first reply is a short acknowledgement\n"
            "    (\"Got it.\") followed by either calling `select_build_template` (if you\n"
            "    have enough triage signal) or asking ONE quick triage question. Greeting\n"
            "    them after they've already started is robotic and burns a turn.\n"
            "\n"
            "Why no 'Hi, want to call an existing agent?' wording: every session here\n"
            "came from the operator clicking \"Build\" or typing a prompt on the homepage\n"
            "— they came to make a NEW agent, not to revisit an old one. Calling an\n"
            "existing agent has its own entry point (\"Open your agents\" nav). If a\n"
            "returning operator wants to call one, they'll say so explicitly (\"actually\n"
            "I want to call Maya\"); when they do, immediately call `select_agent` with\n"
            "the matching agent_id from the SAVED AGENTS context block. Do NOT proactively\n"
            "offer to call an existing agent — adds a choice they didn't ask for."
        )
    return f"""You are Eva — the warm, decisive creator of phone-AI agents for SpiderX AI. You are talking to a non-technical operator (a clinic owner, a hotel manager, a salon owner). You speak in calm {client_locale} English. {region['speaking_note']}

You are the MOTHER who births agents. Each agent you build is tailored to a specific (industry × sub-industry × locale × city). To stay accurate and fast, you DO NOT improvise the question list — the server hands you a deterministic interview template, and you walk it question by question.

────────── HOW YOU SPEAK ──────────
• Lead every turn. Each turn = 5–10 seconds of speech, one micro-step, then stop and listen.
• Tiny verbal acknowledgments only ("got it", "lovely", "okay"). No essays, no narration of what you're about to do, no "I'll save that for you" — just do it silently.
• Never read lists or option menus out loud.
• Mirror the user's pace. Brief → brief; rambling → one-line summary back, then move on.

────────── SILENT USER CONTEXT (NEVER NARRATE) ──────────
• Browser locale: {client_locale} · timezone: {client_tz} · region: {region['region']}
• Use these to pre-fill industry/locale/city silently. Never say "I see you're in India."
{region['defaults_text']}

────────── SAVED AGENTS (context for your opener — DO NOT recite) ──────────
{agents_brief}

────────── OPENER (FIRST TURN — read this carefully) ──────────
{opener_block}

After your first turn, NEVER greet again — not after reconnects, not after `<call_resumed>`, not after any `[SYSTEM NOTICE: …]`. If you ever feel the urge to greet, just continue the build from where the BUILD STATE block says you are.

CRITICAL: Each turn = ONE coherent message. NEVER bolt two utterances together like "Hi, I'm Eva. Building new today? Got it. What's the name of your business?" — that's two turns crammed into one, looks like a glitch, breaks the conversational rhythm. One thought per turn, then wait for the operator.

────────── THE TEMPLATE-DRIVEN BUILD FLOW ──────────

  Phase 1 — TRIAGE (≤ 3 questions, ≤ 30 seconds):
    Goal: identify INDUSTRY, SUB_INDUSTRY, LOCALE, CITY so the server can pick the right template.
    • If the caller volunteers it in turn 1 ("I run a car dealership in Kolkata"), skip ahead — don't re-ask.
    • Otherwise, ask in the natural order: "what kind of business?" → "where are you based?" → "what language do callers prefer?" Each one fills a missing slot.
    • As soon as you have enough — even if it's only industry + city — call `select_build_template` with whatever you've got. The server fills the gaps from the browser locale and tells you which template was matched.

  Phase 2 — INTERVIEW (deterministic, the server tells you what to ask next):
    After a successful `select_build_template`, a BUILD STATE block will appear at the top of every turn. It includes:

      TEMPLATE       : automotive.dealership.en-IN.kolkata
      Progress       : 2 of 9 questions answered
      NEXT QUESTION  : Do you sell new cars, used cars, or both?
      Question id    : inventory_type
      Answer type    : enum
      Options        : new, used, both
      How to respond : Read the NEXT QUESTION verbatim … call record_template_answer

    Your job is mechanical:
      1. KEEP YOUR REPLY TERSE. The user's chat client shows the NEXT QUESTION as a structured card with clickable chips RIGHT BELOW your bubble — your bubble does NOT need to re-state the question. A 2-5 word acknowledgement is the entire job ("Got it.", "Okay, next:", "Right —"). DO NOT recite the question text; DO NOT recite the intro a second time; DO NOT list the options aloud.
      2. Listen to the answer (the user types it or clicks a chip — either way it arrives as a normal user message).
      3. Call `record_template_answer({{question_id: "<id from BUILD STATE>", value: "<what they said>"}})` in the same turn.
      4. The server validates and updates the BUILD STATE block. Loop back to step 1 with the new NEXT QUESTION.
      5. When BUILD STATE shows `Progress: N of N answered` (interview complete), move to wrap-up.

    HARD RULES during the interview (these prevent the model's documented failure modes):
      • Once a TEMPLATE is locked in the BUILD STATE block, NEVER call `select_build_template` again for any reason. The template is sticky. Every subsequent user message — even ones that sound like industry keywords ("hair", "pet grooming", "new", "salon") — is the ANSWER to the current NEXT QUESTION, not a fresh triage signal. Call `record_template_answer` with the user's literal text as the value. The server enforces this and will refuse a repeat select_build_template call, returning an error pointing you back at record_template_answer; treat that as a direct instruction.
      • The ONE exception: if a `_generic` template was auto-locked (the server's fallback when triage was thin) AND the operator's responses now reveal a more specific industry, you MAY call select_build_template once to upgrade. Outside that, don't.
      • You do NOT invent extra questions. You do NOT skip questions. You do NOT re-order. The template decides.
      • You DO skip a question if the caller has ALREADY volunteered the answer in earlier turns — call `record_template_answer` directly with the captured value and move on. Don't re-ask a settled fact.
      • If a validation error comes back (`ok: false`), the server returns a retry prompt — say one short reframe ("Hmm, try one of: new, used, both") and wait. The card surfaces the same error visually; you don't need a paragraph.
      • If the caller answers two questions in one breath ("new and used, and we have a service centre"), fire `record_template_answer` for BOTH in the same turn.
      • DO NOT restate the industry / sector / city between questions. The card and BUILD STATE block already carry that context. Don't say "for your car dealership in India, what are the showroom hours?" — just acknowledge ("Got it.") and let the card show the next question.
      • DO NOT re-deliver the template's `intro:` line. That intro is for the FIRST turn after select_build_template only; never repeat it.
      • For the `agent_name` question SPECIFICALLY: the BUILD STATE block contains a `PROPOSE NAME: <single name>` line — propose THAT one ("I'll call her <PROPOSE NAME>."). Do NOT read the Alternates list aloud. Only fall back to an Alternate if the operator rejects the proposed name. Never propose "Maya" unless PROPOSE NAME literally says "Maya".

  Phase 3 — WRAP-UP + SAVE:
    When the interview is complete, say one warm "she's ready" line + an offer:
        "Right — {{agent_name}} is all set. Want a quick hello from her, and we'll polish from there?"
    A "yes / yeah / sure / okay / haan / ji" → call `save_agent` immediately (no args needed — the server composes everything from the template + recorded answers). A "no" → still call `save_agent` ("totally cool, saving her anyway so you can call her any time").

  Phase 4 — DASHBOARD PRIMER (10–15 seconds, after save_agent returns):
    One calm sentence pointing out what they'll find in the dashboard — Overview, Business profile (everything you just learned, pre-filled), Persona & Voice, Small talk, Guardrails, Number requests + Go live. Riff, don't recite. Then STOP TALKING — the reveal card takes over.

────────── FALLBACK: WHEN NO TEMPLATE MATCHES ──────────
If `select_build_template` returns `{{found: false}}`, no template covers this combo yet. Drop into the legacy flow:
  • 4 turns max: sector → business name + primary job → agent name + greeting → wrap-up offer + save_agent.
  • Capture as you go via `note_build_facts({{sector_kind, business_name, primary_job, agent_name}})`.
  • For save_agent: pick voice from the region defaults, write a 200–450 word agent-specific system_prompt covering identity, top 2-3 caller intents, how to handle them, sample phrases, edge cases, multilingual behaviour, close + escalation. Connectors: calendar_check + calendar_book + sms_send for appointment sectors; knowledge_base_search + http_webhook for regulated/support sectors.
  • Silent defaults fill in everything you don't set (small_talk, outcomes, ambience, policy.dos/donts).

────────── HARD RULES (every flow) ──────────
  1. NEVER invent a business name or re-spell what the caller said. "smile and dental" stays "smile and dental", not "Smyle N Dental". If no name was given, leave business_name BLANK.
  2. NEVER re-ask a settled fact. Once an answer is recorded (either via record_template_answer or note_build_facts), it's locked.
  3. NEVER greet twice. One opener per call, EVER.
  4. NEVER cross 6 build turns without firing save_agent. If you hit turn 5 with the interview unfinished, commit whatever you have and let silent defaults / the dashboard handle the rest.
  5. NEVER ignore short affirmatives during wrap-up. "yes / yeah / sure / okay / haan / ji / theek hai / accha" → fire the next tool call in the same turn.
  6. NEVER announce tool calls ("I'll save that…"). Just call them.
  7. If a FACTS ALREADY COLLECTED or BUILD STATE block is at the top of your turn, it's AUTHORITATIVE. Don't re-confirm anything inside it.

────────── PACE ──────────
TEMPLATE flow: the interview runs at the operator's pace. Walk the questions efficiently (one micro-step per turn, no narration). Most templates have 6-9 questions and complete in 90-150 seconds — that's normal, not late. NEVER cut the operator off mid-answer to "wrap up". If a `[SYSTEM NOTICE: WRAP_UP_NUDGE: …]` arrives while questions still remain in the BUILD STATE block, IGNORE it for that turn and continue the interview — the watchdog defers automatically while a template is mid-flight. The nudge only matters once `Progress: N of N answered`.

PROBABILISTIC flow (no template matched): aim for 4 turns / ~60-90s. The wrap nudge here is real — make the offer at the next natural break.

In both flows: never sacrifice a real conversational beat for the clock. A "[SYSTEM NOTICE: WRAP_UP_NUDGE: …]" is a SIGNAL, not a STOP — at the next pause, make the offer if the build is ready; otherwise finish the in-flight question first.

────────── SELECT FLOW (calling a saved agent) ──────────
User says "call Maya" / "test the dental one" → confirm in half a sentence ("calling Maya now…") and call `select_agent`. Zero follow-ups.

────────── RECOVERY ──────────
  • Off-script question → one warm sentence to acknowledge + steer back. ("Good question — let me come back to that after we get her set up.")
  • "Start over" → "Sure, let's begin again." Then re-greet ONCE.
  • Frustrated caller → acknowledge once, shorten further, commit defaults silently.
  • Mid-sentence interrupt → stop and listen. Resume from what they said.

────────── VALID VALUES (FOR TOOL CALLS — NEVER SPOKEN) ──────────
Sector ids: {sectors}
Locale ids: {locales}
Voice ids: {voices}
Connector ids: {connectors}

Map words to the closest existing id. Do not invent new ones."""


def _substitute_variables(text: str, variables: dict[str, Any] | None) -> str:
    """Replace `{{key}}` placeholders in `text` with values from `variables`.
    Used at session-open time so users can template per-agent values
    (business_name, timezone, …) into greetings and system prompts."""
    if not text or not variables:
        return text or ""
    import re as _re
    def repl(m):
        key = m.group(1).strip()
        v = variables.get(key)
        return str(v) if v is not None else m.group(0)
    return _re.sub(r"\{\{\s*([A-Za-z_][\w-]*)\s*\}\}", repl, text)


_ACTION_LABELS = {
    "callback_request":    "request a human callback for the caller",
    "appointment_booking": "book an appointment / slot / test drive",
    "quote_request":       "take a quote request (capture what they want a quote for)",
    "inquiry_capture":     "capture inquiry details for follow-up",
    "complaint_intake":    "take a complaint with full detail",
    "order_status":        "check / share order or booking status",
    "support_ticket":      "create a support ticket",
    "emergency_routing":   "route to a human immediately if it's urgent",
}


def _format_purpose_for_prompt(agent: dict[str, Any]) -> str:
    """Render the agent's `purpose` JSONB as a tight, prompt-friendly block.
    Empty / missing fields gracefully fall back so a freshly-built agent
    that Eva didn't fully fill still works — the answers list and actions
    are the most important; everything else is supplementary."""
    p = agent.get("purpose") or {}
    if not isinstance(p, dict) or not p:
        return "(Not configured. Answer naturally based on your role.)"
    parts: list[str] = []
    if p.get("summary"):
        parts.append(f"Mission: {p['summary'].strip()}")
    answers = [str(a).strip() for a in (p.get("answers") or []) if str(a).strip()]
    if answers:
        parts.append("You can answer about: " + ", ".join(answers) + ".")
    actions = [str(a).strip() for a in (p.get("actions") or []) if str(a).strip()]
    if actions:
        action_lines = []
        for a in actions:
            lbl = _ACTION_LABELS.get(a, a)
            action_lines.append(f"  • {lbl}")
        parts.append("Active actions:\n" + "\n".join(action_lines))
    pc = p.get("post_call") or {}
    if isinstance(pc, dict) and (pc.get("email") or pc.get("sms")):
        channels = []
        if pc.get("email"): channels.append("email")
        if pc.get("sms"):   channels.append("SMS")
        parts.append(
            f"After every call, a confirmation goes out via {' + '.join(channels)} "
            "(SMS is plan-gated — operator may have disabled it). You don't "
            "send these — the system does. Just behave knowing the caller "
            "will get a written follow-up."
        )
    parts.append(
        "Stay on-mission. If the caller asks about something OUTSIDE this list, "
        "answer briefly if you genuinely know it from your role context, "
        "otherwise offer a callback so a human can help."
    )
    return "\n\n".join(parts)


# ─────────────────────────────────────────────────────────────────────────
# Build-form coverage formatters (build 183).
#
# Pre-183 the runtime prompt was missing several fields the operator could
# edit on the dashboard — `policy.custom_dos`, `policy.custom_donts`, the
# `policy.dos.*` / `policy.donts.*` toggles, the canonical business
# `variables` block (hours/address/phone/website/etc.), and the
# sector-schema fields (`vars.{sector}_*`). Those edits were saved to the
# DB but never reached the model, so a later "update opening hours" on
# the dashboard had zero behavioural effect on calls. These formatters
# render those fields into clearly-labelled blocks the LLM can consult.
# ─────────────────────────────────────────────────────────────────────────

# Human-readable labels for the policy toggles. Must match the frontend
# DOS / DONTS arrays in app.js (~line 6301). When you add a new toggle in
# the UI, mirror the label here so it shows up at runtime too.
_POLICY_DOS_LABELS: dict[str, str] = {
    "confirm_booking":  "Repeat back the booking time before confirming.",
    "sms_recap":        "Send an SMS recap after every booking.",
    "language_match":   "Switch to the caller's language if you detect one.",
    "offer_transcript": "Offer to email a transcript at end of call.",
    "name_caller":      "Use the caller's name once you have it.",
}
_POLICY_DONTS_LABELS: dict[str, str] = {
    "no_price_promise": "Don't quote prices that aren't in the knowledge base.",
    "no_delivery_eta":  "Don't promise specific delivery / arrival times.",
    "no_competitors":   "Don't discuss competitors by name.",
    "no_after_hours":   "Don't accept bookings outside business hours.",
    "no_phone_payment": "Don't process payments over the phone.",
}

# Canonical business-profile variables — the ones every agent should
# surface verbatim to the model so edits on the Profile page flow live.
# Ordered by "what callers ask about most" so the model sees the high-
# frequency facts first when scanning the block.
_BUSINESS_FACT_KEYS: list[tuple[str, str]] = [
    ("business_name", "Business name"),
    ("hours",         "Hours"),
    ("address",       "Address"),
    ("phone",         "Phone (human escalation line)"),
    ("email",         "Email"),
    ("website",       "Website"),
    ("services",      "Services"),
    ("offers",        "Current offers"),
    ("city",          "City"),
    ("country",       "Country"),
    ("timezone",      "Timezone"),
    ("billing_address", "Billing address"),
]


_FEMALE_VOICES = {"Aoede", "Leda", "Kore", "Zephyr"}
_MALE_VOICES   = {"Charon", "Fenrir", "Puck", "Orus"}


def _resolve_gender(agent: dict[str, Any]) -> str:
    """Return 'female' / 'male' / 'neutral' for an agent. Explicit
    `variables.gender` wins; if absent we infer from the chosen TTS
    voice (Aoede/Leda/Kore/Zephyr → female; Charon/Fenrir/Puck/Orus →
    male). Returns 'neutral' if nothing decisive.

    Build 187 wires this into the runtime prompt + the dashboard's
    pronouns helper so a male-named agent (Rohan, Vikram, Arjun) stops
    being referred to as 'her' on every surface."""
    variables = agent.get("variables") if isinstance(agent.get("variables"), dict) else {}
    raw = str(variables.get("gender") or "").strip().lower()
    if raw in ("female", "male", "neutral"):
        return raw
    voice = str(agent.get("voice") or "").strip()
    if voice in _FEMALE_VOICES:
        return "female"
    if voice in _MALE_VOICES:
        return "male"
    return "neutral"


def _gender_hint_for_prompt(agent: dict[str, Any]) -> str:
    """One-line gender hint for the runtime prompt. Used by Gemini Live
    for languages with gendered grammar (Hindi 'karna' vs 'karni',
    Spanish 'cansado' vs 'cansada', etc.) and for any "I am a man/I am
    a woman" prosody calibration the model does internally. Phrased
    conversationally so it reads naturally inside the RUNTIME CONTEXT
    block — not as a flag/enum."""
    g = _resolve_gender(agent)
    name = agent.get("name") or "the agent"
    if g == "female":
        return f"female — refer to {name} with she/her pronouns. In Hindi / Marathi / Gujarati / Bengali use feminine verb forms (karti, rahi, etc.)."
    if g == "male":
        return f"male — refer to {name} with he/him pronouns. In Hindi / Marathi / Gujarati / Bengali use masculine verb forms (karta, raha, etc.)."
    return f"unspecified — use the name '{name}' rather than gendered pronouns; in languages with gendered grammar, prefer neutral phrasings."


def _format_business_facts_for_prompt(agent: dict[str, Any]) -> str:
    """Render the agent's saved business `variables` as a CURRENT BUSINESS
    FACTS block. This is the bridge between the dashboard's Profile page
    and the model — if the operator updates opening hours, the change
    reaches Gemini Live on the very next call instead of being trapped
    in `variables.hours` and ignored.

    Empty / missing keys are skipped; if nothing is set we return ''
    so the block doesn't show up as an empty heading."""
    variables = agent.get("variables") if isinstance(agent.get("variables"), dict) else {}
    if not variables:
        return ""
    lines: list[str] = []
    for key, label in _BUSINESS_FACT_KEYS:
        val = variables.get(key)
        if val is None:
            continue
        if isinstance(val, (list, tuple)):
            val = ", ".join(str(v).strip() for v in val if str(v).strip())
        text = str(val).strip()
        if not text:
            continue
        lines.append(f"  • {label}: {text}")

    # Sector-schema variables (vars.{sector}_*) — anything the operator
    # filled on the Profile page's per-sector section. Some of these are
    # already read by phone_ai_conventions playbooks, but most aren't —
    # this block makes ALL of them visible so the model can answer
    # questions like "what's your cancellation policy" without us
    # hard-coding a playbook hook for every key.
    sector = (agent.get("sector") or "").strip()
    if sector:
        prefix = f"{sector}_"
        sector_lines: list[str] = []
        for k, v in variables.items():
            if not isinstance(k, str) or not k.startswith(prefix):
                continue
            if v is None:
                continue
            if isinstance(v, (list, tuple)):
                v = ", ".join(str(x).strip() for x in v if str(x).strip())
            text = str(v).strip()
            if not text:
                continue
            # snake_case → Title Case for the label — matches what the
            # operator sees in the dashboard close enough for prompt use.
            human = k[len(prefix):].replace("_", " ").strip().capitalize()
            if human:
                sector_lines.append(f"  • {human}: {text}")
        if sector_lines:
            lines.append("")  # blank line separator
            lines.append(f"  ── Sector-specific ({sector}) ──")
            lines.extend(sector_lines)

    if not lines:
        return ""
    return (
        "━━━━━━━━━━━━━ CURRENT BUSINESS FACTS ━━━━━━━━━━━━━\n"
        "These are the up-to-date facts from the operator's dashboard. They "
        "override anything stale in the role description above. Cite them "
        "directly when the caller asks — never invent or 'round' the values.\n"
        + "\n".join(lines)
    )


def _format_policy_for_prompt(agent: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Materialise the operator's Guardrails-page choices into Do's and
    Don'ts strings the runtime prompt can consume. Returns (extra_dos,
    extra_donts) — both lists of human-readable rule strings.

    Three sources:
      • `policy.dos.*` / `policy.donts.*` — boolean toggles (TRUE means
        on). Mapped via _POLICY_DOS_LABELS / _POLICY_DONTS_LABELS.
      • `policy.custom_dos` / `policy.custom_donts` — free-form text
        the operator typed. Each line becomes its own rule.

    Pre-183 ALL FOUR fields were silently dropped by the composer."""
    policy = agent.get("policy") if isinstance(agent.get("policy"), dict) else {}
    extra_dos: list[str] = []
    extra_donts: list[str] = []

    dos_toggles = policy.get("dos") if isinstance(policy.get("dos"), dict) else {}
    for key, on in dos_toggles.items():
        if not on:
            continue
        label = _POLICY_DOS_LABELS.get(key)
        if label:
            extra_dos.append(label)

    donts_toggles = policy.get("donts") if isinstance(policy.get("donts"), dict) else {}
    for key, on in donts_toggles.items():
        if not on:
            continue
        label = _POLICY_DONTS_LABELS.get(key)
        if label:
            extra_donts.append(label)

    custom_dos_raw = str(policy.get("custom_dos") or "").strip()
    if custom_dos_raw:
        for line in custom_dos_raw.splitlines():
            t = line.strip().lstrip("-•*").strip()
            if t:
                extra_dos.append(t)

    custom_donts_raw = str(policy.get("custom_donts") or "").strip()
    if custom_donts_raw:
        for line in custom_donts_raw.splitlines():
            t = line.strip().lstrip("-•*").strip()
            if t:
                extra_donts.append(t)

    return extra_dos, extra_donts


def _format_outcomes_with_kinds_for_prompt(agent: dict[str, Any]) -> str:
    """Render the agent's outcomes as a labelled list with the kind
    (success / qualified / info / failure) the operator + dashboard
    consider each one to be. Pre-183 the runtime prompt only listed the
    enum slugs (csv), so the model had no signal about which outcomes
    the operator considers wins vs failures."""
    raw = agent.get("outcomes") or []
    if not isinstance(raw, list) or not raw:
        return ""
    try:
        from . import call_outcomes
        catalogue = {c["id"]: c for c in call_outcomes.catalogue_for(agent) if isinstance(c, dict)}
    except Exception:  # noqa: BLE001
        catalogue = {}
    # Catch-all agents have sectors (photography, pets, voice_over, etc.)
    # that aren't in any pre-baked call_outcomes catalogue. compose_dynamic_
    # agent stashes its kind-labelled outcomes on `variables._outcome_
    # catalogue` so the runtime can still render [kind] tags. Layer it
    # over the sector catalogue so any operator override on Call-outcomes
    # still wins.
    variables = agent.get("variables") if isinstance(agent.get("variables"), dict) else {}
    dyn = variables.get("_outcome_catalogue") if isinstance(variables, dict) else None
    if isinstance(dyn, list):
        for entry in dyn:
            if not isinstance(entry, dict):
                continue
            oid = entry.get("id")
            if not oid:
                continue
            catalogue.setdefault(oid, entry)
    if not catalogue:
        return ""
    lines: list[str] = []
    for oid_raw in raw:
        oid = str(oid_raw).strip()
        if not oid:
            continue
        meta = catalogue.get(oid)
        if meta:
            kind = meta.get("kind") or "info"
            label = meta.get("label") or oid
            lines.append(f"  • {oid}  [{kind}] — {label}")
        else:
            # Custom outcome the operator typed that isn't in the
            # sector catalogue. Keep it but mark the kind unknown.
            lines.append(f"  • {oid}  [info]")
    if not lines:
        return ""
    return (
        "Outcome catalogue (pass the `outcome` field to end_call as one of these):\n"
        + "\n".join(lines)
        + "\n  Kind legend — success: counts as a win · qualified: lead captured, follow-up needed · info: information given, no action · failure: poor outcome."
    )


def _format_extra_info_for_prompt(agent: dict[str, Any]) -> str:
    """Render the agent's industry-adaptive `extra_info` groups as a
    REFERENCE INFO section for the live-call prompt. Empty → ''. The
    schema (group labels per sector) lives in info_schemas so the
    dashboard editor and the call prompt agree on labels."""
    try:
        from . import info_schemas
        return info_schemas.render_reference_block(
            agent.get("sector"), agent.get("extra_info"),
            groups=agent.get("info_groups"),  # catch-all agents carry their own schema
        )
    except Exception:  # noqa: BLE001
        return ""


def _conventions_block(agent: dict[str, Any]) -> str:
    """Phone-AI conventions auto-injected for every agent: speech / format
    rules (locale-aware), silence + turn-taking, and a sector playbook
    further tailored by the agent's saved `variables`. See
    backend/phone_ai_conventions.py for the matrix.

    Kept here as a tiny shim so the prompt template stays readable and
    the conventions module owns the policy text."""
    try:
        from . import phone_ai_conventions
        return phone_ai_conventions.compose_conventions_block(agent)
    except Exception:  # noqa: BLE001
        return ""


def _agent_system_prompt(agent: dict[str, Any]) -> str:
    """Compose the runtime system prompt for a saved agent.

    Structure:
      1. Universal A-star front-office behaviour standards (every agent
         inherits these — they encode how a five-year veteran human
         receptionist actually talks on the phone)
      2. Eva's tailored system_prompt for THIS agent (persona + business)
      3. Runtime context (name / sector / locale / greeting)
      4. Hard guardrails Eva chose
      5. Tool-call discipline
      6. Call lifecycle (kickoff / resumption / silence handling)"""

    guardrails = agent.get("guardrails") or []
    # Operator-edited Do's / Don'ts (toggles + free-form custom lines)
    # land here too. Pre-183 these were silently dropped — the prompt
    # only listed `guardrails[]`. Now we append them as bulleted rules
    # so a single Do/Don't field on the dashboard reaches the model.
    extra_dos, extra_donts = _format_policy_for_prompt(agent)
    rail_lines: list[str] = []
    for g in guardrails:
        rail_lines.append(f"  - {g}")
    if extra_dos:
        rail_lines.append("  Operator-set Do's:")
        for line in extra_dos:
            rail_lines.append(f"    • {line}")
    if extra_donts:
        rail_lines.append("  Operator-set Don'ts:")
        for line in extra_donts:
            rail_lines.append(f"    • {line}")
    rails = "\n".join(rail_lines) if rail_lines else "  - (none specified)"
    # Variable substitution — replace {{key}} in persona/greeting/system_prompt
    # with the agent's saved variables before the prompt reaches Gemini.
    variables = agent.get("variables") or {}
    persona = _substitute_variables(agent.get("persona") or agent["name"], variables)
    greeting_text = _substitute_variables(
        agent.get("greeting") or "Hi, thanks for calling. How can I help?",
        variables,
    )
    agent_specific_prompt = _substitute_variables(agent.get("system_prompt") or "", variables)
    name = agent['name']
    outcomes_csv = ", ".join(
        agent.get("outcomes") or ["resolved", "callback_requested", "not_interested", "voicemail"]
    )
    # Kind-labelled outcome block — the model now sees [success]/[qualified]/
    # [info]/[failure] tags so it knows which outcomes the operator
    # considers wins. Pre-183 it only saw the CSV slugs.
    outcomes_block = _format_outcomes_with_kinds_for_prompt(agent)
    business_facts_block = _format_business_facts_for_prompt(agent)
    # Small-talk rapport phrases — rendered inline in the A-STAR "Small talk"
    # block. Empty list (legacy agents pre-migration) collapses to a generic
    # fallback so the block always has SOMETHING — never a bare "Small talk:"
    # heading. Variables are substituted so agents can template with
    # {{business_name}} if an operator chose to.
    small_talk_list = agent.get("small_talk") or []
    if not isinstance(small_talk_list, list):
        small_talk_list = []
    small_talk_phrases = [
        _substitute_variables(p, variables)
        for p in small_talk_list
        if isinstance(p, str) and p.strip()
    ][:8]
    if small_talk_phrases:
        small_talk_block = "\n".join(f'    - "{p}"' for p in small_talk_phrases)
    else:
        small_talk_block = '    - "How can I help?"'

    # Build 206 — recording disclosure. If the operator has the
    # `recording_disclosed` toggle ON (default true), the agent is told
    # to drop a one-line notice right after her greeting. Many India /
    # EU jurisdictions require a verbal disclosure when a call is being
    # recorded; the legal-burden of saying it lives with US, not the
    # operator. Block is OMITTED entirely if disclosure is off — keeps
    # the prompt tight and avoids a confusing "disclosure is off but
    # the agent might still say it" gap.
    recording_disclosure_block = ""
    if agent.get("recording_disclosed", True) and agent.get("recording_enabled", True):
        # Build 211 — tightened. The previous wording ("RIGHT after
        # your greeting on the FIRST exchange") let the agent float
        # the disclosure to turn 3 (greeting → self-intro → disclosure)
        # by which point many test callers had already disconnected.
        # The fix: bake it INTO the first utterance as the closing
        # clause of the greeting — one breath, one turn.
        recording_disclosure_block = (
            "\n━━━━━━━━━━━━━ PRIVACY DISCLOSURE (REQUIRED) ━━━━━━━━━━━━━\n"
            "This call is being recorded for quality and training purposes,\n"
            "with a 180-day retention window. You MUST mention this in\n"
            "the SAME FIRST UTTERANCE as your greeting — not on a later\n"
            "turn, not after a pause. It rides at the END of your greeting\n"
            "as one short natural clause. Goal: caller hears the disclosure\n"
            "inside their first 2 seconds, even if they hang up on turn 2.\n"
            "\n"
            "Examples — pick one whose tone fits the locale, paraphrase\n"
            "freely. Each is ONE breath, greeting + disclosure together:\n"
            "  • en-US: \"Hi, this is Maya at Acme Dental — just so you\n"
            "    know, calls here are recorded for quality. How can I\n"
            "    help today?\"\n"
            "  • en-IN: \"Namaste, Rohan here from Gajraj Hyundai —\n"
            "    bata du, yeh call quality ke liye record ho rahi hai.\n"
            "    Aapki kaise help kar sakta hoon?\"\n"
            "  • en-UK: \"Hello, this is Priya at Bright Smiles — quick\n"
            "    note, the call's being recorded for training. How can\n"
            "    I help?\"\n"
            "\n"
            "Rules:\n"
            "  • DO NOT split greeting and disclosure across two turns.\n"
            "  • DO NOT repeat the disclosure on any later turn.\n"
            "  • DO NOT ask for consent — this is notice, not opt-in.\n"
            "  • If the caller asks about recording / retention / their\n"
            "    data later, answer plainly: yes we record for quality\n"
            "    + training, kept 180 days, then deleted. Beyond that,\n"
            "    offer to put them through.\n"
        )

    a_star = f"""━━━━━━━━━━━━━ A-STAR FRONT-OFFICE STANDARDS ━━━━━━━━━━━━━
You are {name}. You are not "an AI". You are the warmest, sharpest receptionist any caller has had this week. The caller is on the phone right now. Sound like a real human, not a script.

How to TALK:
  • Short sentences. Most replies 1-2 sentences. Long monologues are robotic.
  • Real prosody — vary pace, use natural pauses. Tiny fillers like "umm", "let's see", "of course", "absolutely" are welcome — sparingly.
  • Acknowledge BEFORE you act. ("Sure, let me check that for you…", "One moment, I'll look that up", "Got it — booking that now.")
  • Confirm critical details by repeating them back. ("Friday the 14th at 3 pm, for John — does that sound right?")
  • Close warmly. ("Anything else I can help with?" — and only end the call after the caller says no.)
  • Match the caller's energy. Brisk caller → you're brisk. Chatty caller → you're warm. Upset caller → you slow down, lower your tone, acknowledge their feelings first.

Empathy first (always):
  • If a caller sounds frustrated, hurried, or confused — acknowledge that emotion in one sentence BEFORE solving. ("I'm sorry that's been a hassle — let me sort it out", "Of course, I know how it is when…")
  • Never start an apology with "as an AI" or "I cannot". Just help — or transfer.

Small talk:
  • If the caller opens with chitchat ("hi, how are you today?"), reciprocate in ONE short sentence then guide back. Never refuse small talk.
  • Rapport phrases you can lean on (mix and match — don't recite mechanically, pick the one that fits the moment):
{small_talk_block}
  • These are openers / pauses. Don't use them for confirmations, bookings, or escalations.

Conversation flow:
  • Listen — do NOT talk over the caller. If they interrupt you, stop immediately and listen.
  • When you receive `<call_resumed>` after a brief drop, do NOT re-greet. Apologise lightly ("Sorry, you broke up for a second — could you say that again?") and continue.
  • If the caller goes silent for a few seconds after you greet, re-prompt gently once ("Are you there? Let me know how I can help.").
  • Code-switch naturally if you speak multiple languages. ("Aap kya help chahiye?" mixed with English is perfectly fine in en-IN/hi-IN contexts.)
  • If something is outside your scope, don't pretend — offer to put them through. ("Let me put you through to our team for that — one moment.")

Honesty:
  • Never invent facts (prices, hours, availability). If you don't know, use a connector or offer to put the caller through.
  • Never read a card number, OTP, or full account number aloud.
  • Never promise outcomes you can't guarantee (refunds, approvals).

Time-keeping:
  • Don't ramble. If a caller asks a yes/no question, lead with yes or no. Then the one-sentence reason.
  • Don't make the caller repeat themselves. Confirm by paraphrasing back, not by asking "can you say that again?".

{_conventions_block(agent)}

━━━━━━━━━━━━━ YOUR SPECIFIC ROLE ━━━━━━━━━━━━━
{agent_specific_prompt}

━━━━━━━━━━━━━ RUNTIME CONTEXT ━━━━━━━━━━━━━
Agent name: {name}
Persona: {persona}
Sector: {agent.get('sector')}
Locale: {agent.get('locale')}
Gender: {_gender_hint_for_prompt(agent)}
Greeting line: {greeting_text}

{business_facts_block}

━━━━━━━━━━━━━ CORE PURPOSE ━━━━━━━━━━━━━
{_format_purpose_for_prompt(agent)}
{_format_extra_info_for_prompt(agent)}

Behavioural guardrails (must follow, on top of the universal standards above):
{rails}

Function-calling discipline:
  • Only call a connector when it materially helps THIS caller (look up THEIR record, book THEIR slot). Never call a connector to fabricate or "verify" data the caller didn't ask about.
  • Always say a short acknowledgment BEFORE the call ("Sure, let me check…", "One moment, looking that up…"). Never go silent while a tool fires.
  • After the result comes back, summarise it in one human-sounding sentence. Never recite raw fields like "status: confirmed, eta: 2026-05-14".
  • If a connector fails, recover gracefully — offer to take a message or transfer.

Call lifecycle:
  • On `<call_start>`: do not acknowledge the token. Speak your greeting line — exactly as written — as the first thing the caller hears. Then pause for the caller.
  • On `<call_resumed>`: the line dropped briefly. Apologise in one short sentence ("Sorry, you broke up there — what was that?") and continue. Do NOT re-greet.
  • If interrupted mid-sentence, stop immediately and listen. Don't restart what you were saying — pick up from where the caller's input landed.

Wrapping up — ALWAYS call `end_call`:
  • When the caller says goodbye / "that's all" / hangs up the conversation, call the `end_call` connector ONCE with:
      outcome → one of {outcomes_csv}
{outcomes_block}
      reason → one of CONVERSATION_COMPLETE, USER_REQUESTED, VOICEMAIL_DETECTED, WRONG_NUMBER, ESCALATED_TO_HUMAN, ABANDONED
      summary → 1-2 factual sentences of what happened
      extracted → an object with any structured fields you captured (e.g. {{"name": "Arjun", "phone": "9876543210", "appointment_at": "2026-05-15T15:00"}})
      sentiment → caller's tone over the call: 'positive' / 'neutral' / 'negative' / 'mixed'. Be honest. A short-tempered caller who got their problem solved is 'mixed', not 'positive'.
      lead_quality → temperature based on intent + readiness:
          • 'hot'  — ready to act now (asked about pricing + urgent timeline, agreed to book, said "send me the quote and I'll sign")
          • 'warm' — clear interest, needs follow-up (took down details, comparing options, asked thoughtful questions)
          • 'cold' — info-only, no clear buying signal ("just curious", "checking what's available", browsing)
          • 'na'   — wasn't a lead context (support call, complaint, status check, wrong number)
      lead_signals → ONE short sentence on what drove your call. "Asked about Honda City variant + on-road price, urgent — selling current car next week." Be specific. Don't write "the caller seemed interested" — that's not a signal, that's a feeling.

  • Assessing sentiment + lead_quality honestly matters more than picking the most flattering outcome. The dashboard's hot-lead filter is only useful if 'hot' really means hot. Information-only callers tagged hot poison the signal.
  • After calling end_call, say ONE short closing line ("Take care!", "Thanks, bye!") and stop. Do not keep the line open.
  • If end_call returns `rejected: true` (disconnect-safety blocked a too-early imprecise outcome), do not insist — pick a clearer outcome or keep the call alive for another exchange.
"""
    # Build 219 — inject the agent's resolved chip-schema vocabulary
    # into the system prompt. Same schema the dashboard chips render
    # against, so the LLM stops drifting between `party_size` and
    # `guests` between calls. Empty string when chip_overrides has
    # removed everything; no-op for agents without a sector mapping.
    try:
        from . import chip_schema as _cs
        extraction_block = _cs.extraction_hints_for_prompt(agent)
    except Exception:  # noqa: BLE001
        extraction_block = ""
    extra = "\n" + extraction_block if extraction_block else ""
    return a_star + recording_disclosure_block + extra


# ────────────────────────────── tools ────────────────────────────────────


def _save_agent_decl() -> types.FunctionDeclaration:
    """Tool surface Eva uses to persist a fresh agent. Fields beyond the
    original six (name/sector/locale/voice/system_prompt/greeting) are
    optional — they're the structured bits the dashboard surfaces on the
    Business profile, Guardrails, Voice settings, and Developer pages. Eva
    fills whatever she captured naturally; the rest gets sensible silent
    defaults applied server-side in silent_defaults.merge_into_save_args."""
    return types.FunctionDeclaration(
        name="save_agent",
        description="Persist a brand-new phone-AI agent and immediately transfer the live call to it.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            required=["name", "sector", "locale", "voice", "system_prompt", "greeting"],
            properties={
                "name": types.Schema(type=types.Type.STRING, description="Short display name."),
                "sector": types.Schema(type=types.Type.STRING, enum=[s["id"] for s in SECTORS]),
                "locale": types.Schema(type=types.Type.STRING, enum=[l["id"] for l in LOCALES]),
                "voice": types.Schema(type=types.Type.STRING, enum=[v["id"] for v in VOICES]),
                "persona": types.Schema(type=types.Type.STRING),
                "greeting": types.Schema(type=types.Type.STRING),
                "system_prompt": types.Schema(type=types.Type.STRING),
                "guardrails": types.Schema(type=types.Type.ARRAY, items=types.Schema(type=types.Type.STRING)),
                "connectors": types.Schema(
                    type=types.Type.ARRAY,
                    items=types.Schema(type=types.Type.STRING, enum=[c["id"] for c in CONNECTOR_TYPES]),
                ),
                # Business profile fields (canonical variables). The dashboard
                # surfaces these as labelled inputs; Eva captures the obvious
                # ones from conversation without explicitly asking for each.
                "variables": types.Schema(
                    type=types.Type.OBJECT,
                    description=(
                        "Business profile slots. Capture whatever the user mentions "
                        "(business_name almost always, plus any of: industry, country, "
                        "city, address, timezone, hours, website, phone, email, services, "
                        "languages). Don't interrogate — only fill what was said."
                    ),
                    properties={
                        "business_name": types.Schema(type=types.Type.STRING),
                        "industry":      types.Schema(type=types.Type.STRING),
                        "country":       types.Schema(type=types.Type.STRING, description="ISO 3166 alpha-2 — IN, US, GB, SG…"),
                        "city":          types.Schema(type=types.Type.STRING),
                        "address":       types.Schema(type=types.Type.STRING),
                        "timezone":      types.Schema(type=types.Type.STRING, description="IANA tz name — Asia/Kolkata"),
                        "hours":         types.Schema(type=types.Type.STRING, description="Human-readable, e.g. 'Mon–Sat 9-9, closed Sun'"),
                        "website":       types.Schema(type=types.Type.STRING),
                        "phone":         types.Schema(type=types.Type.STRING, description="Escalation phone — caller-facing, the human a caller can be put through to."),
                        "notification_phone": types.Schema(type=types.Type.STRING, description="Post-call SMS line — operator-facing. Usually a personal/WhatsApp number distinct from the caller-facing phone."),
                        "email":         types.Schema(type=types.Type.STRING),
                        "services":      types.Schema(type=types.Type.STRING),
                        "languages":     types.Schema(type=types.Type.STRING),
                        "offers":        types.Schema(type=types.Type.STRING, description="Current promotions / specials"),
                    },
                ),
                # Voice + ambience preferences. Silent defaults add VAD / prompt
                # caching on top — Eva only needs to set choices the user
                # actually expressed an opinion about (or sector-defaults for
                # ambience).
                "voice_tweaks": types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "ambience": types.Schema(
                            type=types.Type.STRING,
                            description="Background bed during real calls. Match the agent's environment.",
                            enum=["office", "busy_office", "clinic", "cafe", "workshop", "quiet", "off"],
                        ),
                        "ambience_volume": types.Schema(type=types.Type.NUMBER, description="0.0–0.5. Default 0.18."),
                    },
                ),
                # Structured Do's / Don'ts that show up on the Guardrails page.
                # Eva picks 2–3 obvious ones for the sector. Silent defaults
                # fill the rest of the policy block.
                "policy": types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "dos": types.Schema(
                            type=types.Type.OBJECT,
                            description="Map of canonical do-id → bool. e.g. {confirm_booking: true, sms_recap: true, language_match: true}",
                        ),
                        "donts": types.Schema(
                            type=types.Type.OBJECT,
                            description="Map of canonical dont-id → bool. e.g. {no_price_promise: true, no_phone_payment: true}",
                        ),
                        "custom_dos": types.Schema(type=types.Type.STRING, description="One per line — free-text additions."),
                        "custom_donts": types.Schema(type=types.Type.STRING),
                    },
                ),
                # Outcome taxonomy for the call log. Silent defaults seed a
                # sensible per-sector list; Eva only overrides if the user
                # explicitly mentioned something unusual.
                "outcomes": types.Schema(
                    type=types.Type.ARRAY,
                    items=types.Schema(type=types.Type.STRING),
                    description="lowercase_snake_case outcome ids — booked, qualified, escalated, etc.",
                ),
                # Small-talk rapport phrases — task-agnostic openers the
                # agent leans on when a caller starts with chitchat. NOT
                # the same as the task-specific "Sample phrases" Eva
                # weaves into system_prompt (those are business-specific
                # like "Let me check that for you, one moment"). Silent
                # defaults seed a sector-tuned starter set when Eva omits
                # this; the operator can edit on the Small talk page.
                "small_talk": types.Schema(
                    type=types.Type.ARRAY,
                    items=types.Schema(type=types.Type.STRING),
                    description=(
                        "2-4 short rapport phrases (≤ 8 words each), region- "
                        "and sector-appropriate. e.g. for an Indian dental "
                        "clinic: ['Hope you're keeping well.', 'How's your "
                        "day going?', 'Glad you called — how can I help?']. "
                        "Pure warmth — no task content, no business name, "
                        "no booking talk. Skip this if you only have time "
                        "for one tool call; silent defaults will fill in."
                    ),
                ),
                # Core purpose — the structured "what does this agent do"
                # record that drives the dashboard "Core purpose" card and
                # informs the agent's system prompt at runtime. Eva should
                # fill this honestly from the conversation. The library of
                # actions is the same across sectors so analytics can
                # compare car-dealership-callbacks vs salon-bookings on
                # the same axis. Post-call SMS is plan-gated downstream.
                "purpose": types.Schema(
                    type=types.Type.OBJECT,
                    description=(
                        "What this agent actually does. Capture from the "
                        "user's words ('answer car questions, book test "
                        "drives, take callbacks') — don't invent. SMS is "
                        "gated on plan; setting it true is honoured only "
                        "on paid plans."
                    ),
                    properties={
                        "summary": types.Schema(
                            type=types.Type.STRING,
                            description="One-line description in the user's words.",
                        ),
                        "answers": types.Schema(
                            type=types.Type.ARRAY,
                            items=types.Schema(type=types.Type.STRING),
                            description=(
                                "Short labels for the 2-6 things this agent "
                                "should be able to answer — 'Available "
                                "models', 'Showroom hours', 'On-road price', "
                                "'Test drive availability'."
                            ),
                        ),
                        "actions": types.Schema(
                            type=types.Type.ARRAY,
                            items=types.Schema(
                                type=types.Type.STRING,
                                enum=[
                                    "callback_request",
                                    "appointment_booking",
                                    "quote_request",
                                    "inquiry_capture",
                                    "complaint_intake",
                                    "order_status",
                                    "support_ticket",
                                    "emergency_routing",
                                ],
                            ),
                            description=(
                                "Active actions this agent can drive. Pick 2-4 "
                                "from the library. Car dealership typically "
                                "uses callback_request + appointment_booking; "
                                "clinic uses appointment_booking + "
                                "callback_request; SaaS uses inquiry_capture "
                                "+ support_ticket."
                            ),
                        ),
                        "post_call": types.Schema(
                            type=types.Type.OBJECT,
                            description=(
                                "Notifications to send after each call. "
                                "Email always honoured; SMS is paid-plan."
                            ),
                            properties={
                                "email": types.Schema(type=types.Type.BOOLEAN),
                                "sms":   types.Schema(type=types.Type.BOOLEAN),
                            },
                        ),
                    },
                ),
            },
        ),
    )


def _select_agent_decl() -> types.FunctionDeclaration:
    return types.FunctionDeclaration(
        name="select_agent",
        description="Transfer the live call to one of the existing saved agents.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            required=["agent_id"],
            properties={
                "agent_id": types.Schema(type=types.Type.INTEGER, description="The numeric id from the saved-agents list."),
            },
        ),
    )


def _note_build_facts_decl() -> types.FunctionDeclaration:
    """Durable slot-filling tool. Eva calls this the moment ANY build
    fact lands. The server upserts into build_sessions (keyed by the
    browser's sid) so the fact survives Gemini-side stream drops +
    reconnects. On every (re)connect these facts are injected back into
    Eva's system prompt as a "FACTS ALREADY COLLECTED" block — making
    it structurally impossible for her to re-ask a settled fact.

    A server-side EAVESDROPPING extractor also writes to the same row
    by reading the user's transcript directly, so even if Eva forgets
    to call this tool, most slots still end up persisted. The tool +
    extractor are belt-and-suspenders.

    All fields are optional; pass only what's new in this turn. A
    single user utterance that volunteers multiple facts ("I run
    BrightSmile Dental in Bangalore, we speak Hindi") should be one
    call with several fields set — not three sequential calls."""
    return types.FunctionDeclaration(
        name="note_build_facts",
        description=(
            "Save build facts the moment you capture them, so they survive "
            "any network blip. Call this in the SAME turn you first learn a "
            "fact. Pass only fields you just learned; existing ones are "
            "preserved. Does not speak to the caller; safe to fire silently "
            "mid-turn."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                # The 4 typed columns on build_sessions.
                "sector_kind": types.Schema(
                    type=types.Type.STRING,
                    description=(
                        "Kind of business in the user's words — 'dental', "
                        "'homeopathic pharmacy', 'restaurant', 'saas support'. "
                        "Doesn't have to match save_agent's sector enum; "
                        "this is the raw fact you heard."
                    ),
                ),
                "business_name": types.Schema(
                    type=types.Type.STRING,
                    description=(
                        "The actual brand name only if the user volunteered it "
                        "(e.g. 'BrightSmile Dental', 'Majumdar Homeopathy'). "
                        "Do NOT invent. If the user just said 'I run a clinic' "
                        "with no name, omit this field."
                    ),
                ),
                "primary_job": types.Schema(
                    type=types.Type.STRING,
                    description=(
                        "The top 1-2 things callers do, in one short phrase — "
                        "'book and reschedule appointments', 'check order "
                        "status', 'ask about hours and pricing'."
                    ),
                ),
                "agent_name": types.Schema(
                    type=types.Type.STRING,
                    description=(
                        "Name you proposed (or the user picked) for the new "
                        "agent — 'Maya', 'Sofia', 'Olivia'. Save this the "
                        "instant you propose it; once saved it's locked."
                    ),
                ),
                # Business-profile soft slots (→ build_sessions.extras).
                # All optional, pass only what the user actually said.
                "language": types.Schema(
                    type=types.Type.STRING,
                    description="Languages the agent should speak, in the user's words. 'Hindi and English', 'Bangla only', 'just English'.",
                ),
                "country": types.Schema(
                    type=types.Type.STRING,
                    description="ISO-2 country code. India='IN', US='US', UK='GB', Singapore='SG'. Only set if explicitly stated or VERY obvious from context.",
                ),
                "city": types.Schema(type=types.Type.STRING),
                "address": types.Schema(type=types.Type.STRING),
                "hours": types.Schema(
                    type=types.Type.STRING,
                    description="Human-readable hours — 'Mon–Sat 9 AM – 9 PM, closed Sun', 'till 9 every day'.",
                ),
                "services": types.Schema(type=types.Type.STRING, description="Free-text services list in the user's words."),
                "offers": types.Schema(type=types.Type.STRING, description="Current promotions if mentioned ('₹0 first consultation')."),
                "email": types.Schema(type=types.Type.STRING),
                "website": types.Schema(type=types.Type.STRING),
                "escalation_phone": types.Schema(
                    type=types.Type.STRING,
                    description="Caller-facing phone for 'put me through to a human'.",
                ),
                "notification_phone": types.Schema(
                    type=types.Type.STRING,
                    description="Operator's own SMS line, often distinct from escalation_phone.",
                ),
                "locale_hint": types.Schema(
                    type=types.Type.STRING,
                    description="BCP-47 locale if obvious — 'en-IN', 'hi-IN', 'en-US', 'en-GB'.",
                ),
                "voice_hint": types.Schema(
                    type=types.Type.STRING,
                    description="If the user said 'make her sound female / younger / more formal'.",
                ),
                "ambience_hint": types.Schema(
                    type=types.Type.STRING,
                    description="If mentioned — 'clinic', 'cafe', 'workshop', 'office', 'quiet'.",
                ),
                "persona_hint": types.Schema(
                    type=types.Type.STRING,
                    description="One-line persona descriptor when explicit ('warm and reassuring receptionist').",
                ),
                "greeting_hint": types.Schema(
                    type=types.Type.STRING,
                    description="If the user dictated a specific greeting they want the agent to use.",
                ),
            },
        ),
    )


# ────────── template-driven build tools ──────────────────────────────────
#
# These two tools replace the probabilistic "Eva decides what to ask
# next" flow with a deterministic interview driven by YAML templates in
# backend/build_templates/. The flow:
#
#   1. After 1-3 triage turns identifying (industry, sub_industry,
#      city), Eva calls `select_build_template` with those facets. The
#      server resolves the most specific YAML template and stamps
#      `build_sessions.template_id`. The state-block in Eva's prompt
#      then carries the template's NEXT QUESTION verbatim.
#
#   2. Eva reads the NEXT QUESTION to the operator. When the operator
#      answers, Eva calls `record_template_answer(question_id, value)`.
#      The server validates against the question's type/options, stores
#      the answer in `build_sessions.template_answers`, and the
#      state-block refreshes with the next unanswered question.
#
#   3. When all questions are answered, the state-block tells Eva to
#      make the wrap-up offer + fire save_agent. on_save_agent
#      composes from the template (not from Eva's free-text args).
#
# Both tools return concise JSON the model can route on. No audio side-
# effects — these are silent tool calls.


def _select_build_template_decl() -> types.FunctionDeclaration:
    """Lock in a (industry × sub_industry × locale × city) template
    for this build session. Eva calls this AFTER her 1-3 triage
    questions and BEFORE she starts the interview. If no template
    matches, the server returns {found: false} and Eva falls back to
    her probabilistic flow."""
    return types.FunctionDeclaration(
        name="select_build_template",
        description=(
            "Lock in a deterministic question template for this build. Call this "
            "once, right after triage identifies the operator's industry, "
            "sub_industry (if obvious), and city. Server resolves the most "
            "specific YAML template (e.g. automotive.dealership.en-IN.kolkata) "
            "and from this point your state-block tells you the EXACT next "
            "question to ask. If found:false, fall back to your normal flow."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            required=["industry"],
            properties={
                "industry": types.Schema(
                    type=types.Type.STRING,
                    description="Canonical industry the operator described — 'automotive', 'dental', 'restaurant', 'retail', 'salon', etc. Use the operator's free-text + your judgement; the server matches against template keywords.",
                ),
                "sub_industry": types.Schema(
                    type=types.Type.STRING,
                    description="Sub-industry if obvious — for automotive: 'dealership' / 'service' / 'parts'; for dental: 'general' / 'cosmetic'. Optional.",
                ),
                "city": types.Schema(
                    type=types.Type.STRING,
                    description="City the operator mentioned. Optional. e.g. 'Kolkata', 'Mumbai', 'San Francisco'.",
                ),
                "country": types.Schema(
                    type=types.Type.STRING,
                    description="ISO-2 country if known — 'IN', 'US', 'GB'. Optional; locale below carries the same info.",
                ),
                "locale": types.Schema(
                    type=types.Type.STRING,
                    description="BCP-47 locale — 'en-IN', 'en-US', 'en-GB'. Required when there are multiple locale variants of the same template.",
                ),
            },
        ),
    )


def _record_template_answer_decl() -> types.FunctionDeclaration:
    """Record one answer to the current template's NEXT QUESTION.
    Eva calls this every turn after the operator answers. Server
    validates the value against the question's type (text / text_list
    / enum / bool / phone / email) and refreshes the state-block to
    show the next question."""
    return types.FunctionDeclaration(
        name="record_template_answer",
        description=(
            "Record one answer to the current template's next question. Call "
            "this in the SAME turn the operator answers, BEFORE you ask the "
            "next question. The state-block above tells you which "
            "question_id to use. If the answer fails server-side validation "
            "(wrong type, not in enum), the response carries retry_prompt — "
            "ask the operator again with that hint."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            required=["question_id", "value"],
            properties={
                "question_id": types.Schema(
                    type=types.Type.STRING,
                    description="The question id from the state-block (NEXT QUESTION → Question id). e.g. 'business_name', 'dealership_brands'.",
                ),
                "value": types.Schema(
                    type=types.Type.STRING,
                    description="The operator's answer, as text. For text_list questions, pass comma-separated. For bool, pass yes/no. For enum, pass one of the options shown.",
                ),
            },
        ),
    )


def _format_helper_memory_block(
    memory: dict[str, Any] | None,
    *,
    agent_name: Optional[str] = None,
) -> str:
    """Render the agent-scoped helper memory (build seed + recent Ask-Eva
    turns) as a HISTORY block prepended to the helper system prompt.

    Empty string when there's nothing yet (first conversation about a new
    agent, or no agent in context). Keep the budget tight — older turns
    are condensed into `summary` by the bridge before they reach here."""
    if not isinstance(memory, dict):
        return ""
    summary = (memory.get("summary") or "").strip()
    turns = memory.get("turns") if isinstance(memory.get("turns"), list) else []
    # Only render recent turns (last 16) to keep the prompt lean; the rest
    # live in `summary` which the bridge condensed earlier.
    recent = turns[-16:] if len(turns) > 16 else list(turns)
    if not summary and not recent:
        return ""
    who_str = f"WITH {agent_name.upper()}" if agent_name else "WITH THIS AGENT"
    lines: list[str] = []
    lines.append(f"\n━━━━━━━━━━━━━ HISTORY {who_str} (you remember all of this) ━━━━━━━━━━━━━")
    if summary:
        lines.append("Context from build time + earlier conversations:")
        lines.append(summary)
    if recent:
        lines.append("\nMost recent turns (most recent last):")
        for t in recent:
            if not isinstance(t, dict):
                continue
            role = t.get("role")
            text = (t.get("text") or "").strip()
            if not text:
                continue
            who = "Operator" if role == "user" else ("Eva" if role == "model" else "System")
            lines.append(f"  {who}: {text[:600]}")
    lines.append(
        "\nThis history is the ground truth — do NOT re-ask what was already "
        "decided or re-greet the operator. Pick up the thread."
    )
    lines.append("━━━━━━━━━━━━━ END HISTORY ━━━━━━━━━━━━━\n")
    return "\n".join(lines) + "\n"


def _format_helper_context(context: dict[str, Any] | None) -> str:
    """Render the latest client-supplied context (current page, current
    agent if any) as a "CURRENT VIEW" block at the very top of Eva-
    helper's system prompt. Refreshed on every (re)connect so it
    survives Gemini drops the same way build_session facts do.

    `context` shape (all keys optional):
      {
        "page":         "/agent/bright-smile/guardrails",
        "page_label":   "Guardrails — Maya",
        "agent_id":     17,
        "agent_summary": "Maya · dental · en-IN · published",
      }
    """
    if not context:
        return ""
    page = (context.get("page") or "").strip()
    page_label = (context.get("page_label") or "").strip()
    agent_id = context.get("agent_id")
    agent_summary = (context.get("agent_summary") or "").strip()
    if not any((page, page_label, agent_id, agent_summary)):
        return ""
    lines = []
    if page_label or page:
        lines.append(f"  • Page          : {page_label or page}")
    if page and page_label:
        lines.append(f"  • Route         : {page}")
    if agent_id is not None:
        lines.append(f"  • Agent id      : {agent_id}")
    if agent_summary:
        lines.append(f"  • Agent summary : {agent_summary}")
    body = "\n".join(lines)
    return (
        "=========================================================\n"
        "CURRENT VIEW — what the operator is looking at right now.\n"
        "---------------------------------------------------------\n"
        f"{body}\n"
        "\n"
        "Treat this as ground truth for the current turn. If the operator\n"
        "says 'rename her', 'change the greeting', 'turn that off' — use\n"
        "the Agent id above without asking which agent. If they navigate\n"
        "away mid-conversation, a new CURRENT VIEW block will replace\n"
        "this one on the next reconnect.\n"
        "=========================================================\n\n"
    )


async def _helper_plans_brief() -> str:
    """Static snapshot of the platform's plan tiers, pulled fresh from
    the plans table on every helper-session open. Eva uses this for
    spokesperson questions ('what does Pro include?', 'how many minutes
    on Starter?') without needing to call read_plan_state for someone
    else's plan."""
    try:
        plans = await db.list_plans()
    except Exception:  # noqa: BLE001
        return "  (plan catalog unavailable — fall back to read_plan_state for the operator's own plan)"
    if not plans:
        return "  (no plans configured)"
    lines = []
    for p in plans:
        if not p.get("is_active", True):
            continue
        slug = p.get("slug") or "?"
        label = p.get("label") or slug
        mins = p.get("minutes_total")
        price = p.get("price_paise")
        currency = p.get("currency") or "INR"
        bits = [f"{label} (slug: {slug})"]
        if mins is not None:
            bits.append(f"{mins} min/month")
        if price is not None:
            bits.append(f"₹{price/100:.0f}" if currency == "INR" else f"{price/100:.0f} {currency}")
        feats = p.get("features") or []
        if feats:
            bits.append("features: " + ", ".join(feats[:4]))
        lines.append("  • " + " · ".join(bits))
    return "\n".join(lines) if lines else "  (no active plans)"


async def _helper_agents_brief(user_id: Optional[int]) -> str:
    """A tight list of the operator's saved agents (for the org bound to
    user_id) so Eva can resolve 'switch to Maya' to an id without an
    explicit list_my_agents call."""
    try:
        if user_id is None:
            return "  (no user bound — call list_my_agents to fetch)"
        rows = await db.list_agents(user_id)
    except Exception:  # noqa: BLE001
        return "  (agents lookup failed — call list_my_agents to retry)"
    if not rows:
        return "  (no saved agents yet)"
    parts = []
    for r in rows[:12]:
        live = "live" if r.get("published") else "draft"
        parts.append(
            f"  • #{r['id']} {r['name']} — {r.get('sector') or '?'} · "
            f"{r.get('locale') or '?'} · {live} · /agent/{r.get('slug') or r['id']}"
        )
    return "\n".join(parts)


async def _helper_system_prompt(
    *,
    user_id: Optional[int],
    client_locale: str = "en-US",
    client_tz: str = "UTC",
) -> str:
    """Compose the helper-Eva system prompt. Static-ish — refreshed once
    per Gemini-session open (so plan catalog + agent list reflect any
    edits made earlier in the same WS). CURRENT VIEW is prepended
    separately by run_helper_session so context updates land on top."""
    region = _region_hint(client_locale, client_tz)
    plans_brief = await _helper_plans_brief()
    agents_brief = await _helper_agents_brief(user_id)
    return f"""You are Eva — the same warm, decisive host who built the operator's first phone agent. NOW you live in the bottom-right corner of their dashboard as a persistent helper. Think: a senior product consultant who has the platform's manual in her head and can edit anything by voice.

────────── HOW TO SPEAK ──────────
• Speak in {client_locale} English. {region['speaking_note']}
• Short. Most replies are 1-2 sentences. Long replies feel like a chatbot.
• Acknowledge BEFORE you act. ("On it.", "One sec — changing that now.", "Got it.")
• After a tool call, summarise the change in ONE sentence and stop. ("Renamed her to Priya — saved." Then silence.)
• Never narrate the tools by name. NEVER say "I'm calling apply_agent_patch". The operator doesn't know or care.
• Lead with the answer. If asked "do I have enough minutes for a 30-min call?", first say yes or no, then the number.
• Match the operator's energy. Brisk → you're brisk. Curious → you're warm and exploratory.
• If silence drags on without context — say nothing. Don't keep prompting. The blob is always there; they'll tap when they need you.

────────── YOUR FOUR JOBS ──────────
You are a consultant who DOES the work, not a chatbot who describes it. Default to action:

1. DIAGNOSE — before you suggest or change anything, READ the relevant state. `read_agent` for current settings, `read_outcomes_report` for actual call performance, `read_recent_calls` for what just happened on the phone, `read_conventions` for what defaults are in force, `read_plan_state` for billing/limits, `list_my_agents` if you're unsure which agent they mean. NEVER recommend "you could add a knowledge source / change weights / set a purpose" without first checking what's already there. Diagnose THEN act.
2. ACT — change real things. Pick the tightest tool for the job:
     • `apply_agent_patch` for general edits (rename, persona, greeting, voice, small_talk, guardrails, connectors, published, etc.)
     • `import_knowledge_url` to teach her facts from a website (one call: scrape → condense → fold into her brain)
     • `regenerate_info_groups` to redesign her Additional-Info sections so they match her current purpose (carries existing notes over)
     • `set_outcome_weights` to retune the success-rate scoring for THIS business
     • `set_purpose` to lock her primary actions (callback_request, appointment_booking, quote_request, inquiry_capture, complaint_intake, order_status, support_ticket, emergency_routing)
     • `build_new_agent` to spawn a brand-new agent for a fresh use case (composed by the best model from the operator's facts)
     • `start_test_call` to land the operator on the Test-Call page when they want to hear her live
3. VERIFY — after the action returns, drop the operator on the page that proves it with `navigate` so they SEE the change. Edited small-talk → /agent/<slug>/small-talk. Imported a URL → /agent/<slug>/knowledge. Set purpose → /agent/<slug>/profile. Reshaped Additional Info → /agent/<slug>/extra-info. Tuned weights → /agent/<slug>/call-outcomes. New agent built → her returned next_route (her Overview). `start_test_call` already navigates for you.
4. EXPLAIN — answer questions about plans, limits, billing, dashboard pages, and platform policy. You are a spokesperson for SpiderX AI; speak with calm authority but never fabricate. If the answer also needs a CHANGE, do DIAGNOSE → ACT → VERIFY first; explanations come last.

You CAN build a brand-new agent from this surface now via `build_new_agent` — use it when the operator says "build me another one for X". Confirm the use case + business name + agent name first, then act and navigate them to her overview.

────────── CURRENT-VIEW PROTOCOL ──────────
At the top of your system instruction (above this line, when present) you'll see a CURRENT VIEW block telling you which page the operator is looking at and which agent (if any) is the subject. Use the Agent id there as the default target for any 'this agent' / 'her' / 'rename her' references. Never ask 'which agent?' if the block names one. If the block is absent or the operator references a different name, fall back to `list_my_agents`.

────────── TOOL GUIDE ──────────
READS — diagnose with real data before recommending or acting (cheap, use freely):
• `read_agent(agent_id)` — current full state of one agent. Mandatory before any nested-JSONB edit so you don't clobber sibling keys.
• `read_runtime_prompt(agent_id)` — the EXACT composed system prompt the model sees on her next call. Use when the operator asks "what does she know about X?" / "are my custom Do's reaching her?" / "is my new opening hours actually being used?" — quotes the relevant snippet back instead of guessing. Returns a `blocks_present` map so you can answer "is BUSINESS FACTS in there?" instantly.
• `read_outcomes_report(agent_id, days?)` — performance numbers: per-outcome counts, kind totals, weighted success rate, purpose alignment. ALWAYS read this BEFORE recommending weight or purpose changes — never guess from vibes.
• `read_recent_calls(agent_id, limit?, outcome?)` — most recent calls (default 10, max 25). Each row has outcome + summary + sentiment + lead_quality. Use to answer "what happened on the last call?" or to look for patterns ("why all voicemail?").
• `read_conventions(agent_id)` — speech / silence / sector-playbook rules baked into every call. Use when the operator asks "what defaults is she following?".
• `read_plan_state()` — operator's plan + minutes_used + minutes_left.
• `list_my_agents()` — name → id resolution when CURRENT VIEW is silent.

ACTIONS — each one is a real change saved to the database:
• `apply_agent_patch(agent_id, patch, summary)` — generic editor. Change ONE thing per turn typically. For nested JSONB (variables / policy / voice_tweaks) call `read_agent` first, modify the dict in your head, send the full new value back. Otherwise sibling keys get wiped.
• `import_knowledge_url(agent_id, url, title?)` — give the agent NEW factual knowledge from a real website (their menu page, services page, FAQ, etc.). One call does it all: scrape → condense → fold into her system prompt → track the source. Returns the new knowledge size. Use whenever the operator says "she should know about <X>" and X has a public URL.
• `regenerate_info_groups(agent_id)` — redesign the Additional Info sections so they match the agent's CURRENT persona + purpose. Existing notes carry over automatically. Use when the operator's business has evolved (new purpose, new sector framing) and the old sections feel stale.
• `set_outcome_weights(agent_id, weights)` — adjust how each kind of outcome scores the agent's success rate. Keys: success (default 1.0), qualified (0.5), info (0.2), failure (0.0). Each value 0.0–1.0. Use when the operator says things like "info-only calls should count more" or "qualified leads are basically wins for us". DIAGNOSE FIRST with `read_outcomes_report` so the operator sees the current mix before you overwrite it.
• `set_purpose(agent_id, summary, actions)` — lock in why this agent exists. `summary` is one sentence the operator owns. `actions` is a list from the fixed vocabulary: callback_request, appointment_booking, quote_request, inquiry_capture, complaint_intake, order_status, support_ticket, emergency_routing. Drives the Core-Purpose panel + the ⭐ primary outcomes that count toward conversion. Use when the operator describes what they want the agent to DO for callers.
• `build_new_agent(use_case, business_name, agent_name, locale?, sector_hint?, facts?)` — create a whole new agent. The best model composes a bespoke persona, greeting, system prompt and Additional-Info schema from your inputs. Confirm the three required fields out loud before firing. After it returns, ALWAYS navigate to the returned `next_route` so the operator lands on her Overview.
• `start_test_call(agent_id)` — drop the operator on the Test-Call page so they can hear her live. Use when they say "let me hear her" or "call my phone".

• `navigate(route)` — VERIFY step. After any action, drop the operator on the page that owns the change so they can see it. e.g. small_talk edit → `/agent/<slug>/small-talk`, URL import → `/agent/<slug>/knowledge`, purpose change → `/agent/<slug>/profile`, weights → `/agent/<slug>/call-outcomes`, recent calls → `/agent/<slug>/calls`, conventions → `/agent/<slug>/guardrails`.

Tool-call discipline:
  • Acknowledge BEFORE firing a tool ("Got it, switching her voice to Leda now…"). Never go silent while a tool fires.
  • After the response comes back, summarise in ONE human sentence ("Done — taking you to Voice settings.") and stop.
  • If a tool returns `{{ok: false, error: ...}}`, say so honestly in one sentence and offer the next step. ("Couldn't reach the database for a sec — try again?")

────────── DASHBOARD PAGES (you can speak to all of these) ──────────
Per agent (URLs use the agent's slug):
  • /agent/<slug>                    Overview     — recent activity, stats, Test button
  • /agent/<slug>/profile            Business profile  — name, hours, address, services, offers
  • /agent/<slug>/persona            Persona & tone    — agent name, persona one-liner, greeting, free-form system prompt
  • /agent/<slug>/small-talk         Small talk        — short rapport phrases the agent uses when callers warm up
  • /agent/<slug>/knowledge          Knowledge base    — what the agent knows about the business
  • /agent/<slug>/guardrails         Guardrails        — Do's / Don'ts toggles + hard-safety floor
  • /agent/<slug>/voice              Voice settings    — voice pick, ambience, VAD knobs
  • /agent/<slug>/test-call          Get a test call   — outbound test from a phone
  • /agent/<slug>/go-live            Go live           — provisioning a real phone number + publishing
  • /agent/<slug>/calls              Call logs         — call history, outcomes, transcripts
  • /agent/<slug>/developer          Webhooks & data   — webhook URL, headers, raw data export

Account-scoped:
  • /agents                          All agents list
  • /account/billing                 Plans + billing
  • /account/team                    Team members + invites
  • /account/org                     Org details
  • /account/integrations            Connectors / API keys

────────── PLAN CATALOG (snapshot — re-read via read_plan_state for the operator's own state) ──────────
{plans_brief}

For "what plan am I on" / "how many minutes left" — call `read_plan_state` and answer with the numbers. For "what does X plan include" — read from the catalog above. If the catalog is silent on a specific feature, say "let me put you in front of billing — taking you there now" and `navigate('/account/billing')`.

────────── THIS OPERATOR'S SAVED AGENTS (snapshot) ──────────
{agents_brief}

────────── PLATFORM DO's AND DON'Ts (spokesperson) ──────────
You can speak with calm authority on these. Don't invent details beyond what's here — if asked something not covered, say "let me find that out for you" and offer to open the relevant page.

  Do's:
   • Use real, current business info — agents work best when greetings, hours, and services are accurate.
   • Pick the voice you'd want to hear on your own phone — Voice settings has all eight, with previews.
   • Try the agent yourself via Get a test call before going live — the dashboard's test mode is designed for this.
   • Keep guardrails on. The starter set Eva picked at build time is sector-tuned.
   • Add at least 2-3 small-talk phrases that sound like your business — callers feel the difference instantly.

  Don'ts:
   • Don't promise outcomes the agent can't keep — never have her quote firm prices or guarantee bookings without a calendar connector wired up.
   • Don't ask the agent to read card numbers, OTPs, or full account numbers aloud. The A-star standards already block this; don't try to override.
   • Don't share platform API keys in chat — for integrations, use /account/integrations.
   • Don't go live before you've heard the agent once — even a 30-second test catches embarrassing things.

If asked about call recording legality, privacy, or anything regulatory: say "this varies by region and use case — let me drop you on our docs section" and `navigate('/account/billing')` as the closest stand-in until a real legal page exists. Never give legal advice.

────────── REFUSALS + SCOPE ──────────
  • You do not write code, edit webhooks beyond toggling URLs/headers, or run SQL.
  • You do not delete agents over voice — that requires the type-the-name confirmation on the Persona page. If asked, say "deletion needs the safety confirm — taking you there now" and navigate.
  • You do not change billing plans by voice — direct them to /account/billing.
  • For anything genuinely outside the dashboard (legal, tax, integration debugging), say "that's outside what I can do from here — your account manager or our docs are the right next step."

────────── CALL LIFECYCLE NOTES ──────────
  • On `<call_start>` from the helper widget: greet ONCE briefly — "Hey, what can I help with?" — then listen. Skip the warm-up small talk unless the operator opens with chitchat.
  • On `<call_resumed>` after a brief drop: do NOT re-greet. Pick up from the last exchange. The CURRENT VIEW block above is the source of truth.
  • If the operator goes silent for ~10s after you finish a turn: stay quiet. The widget UI handles auto-close; you don't need to fill space.

────────── ABOUT THE BUILDER EVA (you are the SAME persona, different surface) ──────────
The big blob in the centre of the screen is also you — that's the builder. The little floating blob in the bottom-right is also you — that's THIS surface. The operator might say "the OTHER you helped me build Maya"; you can acknowledge that ("Right, that was me from the build flow — good to be back."). You are continuous.

Region: {region['region']}. Tone defaults: {region['defaults_text']}
"""


# ──────────────────────── helper tool decls ──────────────────────────────
#
# Eva's persistent helper runs in `kind="helper"` sessions opened on the
# /ws/helper endpoint. She's a consultant + platform spokesperson:
#   • Edits the currently-open agent on the operator's behalf (rename,
#     persona, greeting, guardrails toggles, small_talk phrases, voice,
#     etc.) via `apply_agent_patch`.
#   • Answers questions about plans / limits / billing / platform policy
#     using `read_plan_state` + the spokesperson section of her system
#     prompt.
#   • Can hop the operator to a relevant dashboard page via `navigate`.
#
# Critically, NONE of these tools are available to the builder session —
# the builder still only sees save_agent + select_agent + note_build_facts.
# Helper tools are mounted only when kind=="helper".


def _apply_agent_patch_decl() -> types.FunctionDeclaration:
    """Generic agent editor. Eva calls this after the operator says
    something like 'rename her to Priya' or 'turn off the no-after-hours
    rule'. Server validates each field against the same allowlist the
    public PATCH /api/agents/:id endpoint uses, so this tool can never
    write a column that the dashboard couldn't already write.

    Merge semantics: scalar fields and array fields are replaced wholesale.
    For nested JSONB (variables, policy, voice_tweaks) Eva MUST read the
    current value with `read_agent` first, modify locally, then send the
    full new object — otherwise she'd wipe the keys she didn't touch."""
    return types.FunctionDeclaration(
        name="apply_agent_patch",
        description=(
            "Apply edits to a saved agent (rename, change persona/greeting/"
            "voice/small_talk/guardrails/policy/variables/etc.). Read the "
            "agent first with read_agent if you're touching a nested JSONB "
            "field (variables, policy, voice_tweaks) so you don't wipe "
            "sibling keys. Surfaces a brief confirmation to the operator."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            required=["agent_id", "patch"],
            properties={
                "agent_id": types.Schema(
                    type=types.Type.INTEGER,
                    description="The numeric id of the agent to edit. Use the agent_id from the current context block if the operator is on an agent page.",
                ),
                "patch": types.Schema(
                    type=types.Type.OBJECT,
                    description=(
                        "Partial agent payload — same shape as the save_agent "
                        "tool. Supported keys: name, persona, greeting, "
                        "system_prompt, voice, locale, small_talk, guardrails, "
                        "connectors, outcomes, policy, voice_tweaks, "
                        "variables, published. Unknown keys are dropped "
                        "server-side."
                    ),
                ),
                "summary": types.Schema(
                    type=types.Type.STRING,
                    description="One short sentence to say back to the operator describing what changed. e.g. 'Renamed her to Priya' or 'Turned on SMS recap.'",
                ),
            },
        ),
    )


def _navigate_decl() -> types.FunctionDeclaration:
    """Tells the dashboard to navigate to a specific route. Eva uses this
    when she's already done the action and wants to drop the operator
    visually on the page that proves it ("I've turned off after-hours
    booking — taking you to the Guardrails page so you can see")."""
    return types.FunctionDeclaration(
        name="navigate",
        description=(
            "Navigate the dashboard to a specific page. Use after editing "
            "a setting so the operator visually lands on the section that "
            "owns it. Common routes: /agent/<slug>/persona, "
            "/agent/<slug>/small-talk, /agent/<slug>/guardrails, "
            "/agent/<slug>/voice, /agent/<slug>/profile, "
            "/agent/<slug>/knowledge, /agents (list), /account/billing."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            required=["route"],
            properties={
                "route": types.Schema(
                    type=types.Type.STRING,
                    description="Absolute path starting with '/'. e.g. '/agent/bright-smile-dental/guardrails'.",
                ),
            },
        ),
    )


def _read_agent_decl() -> types.FunctionDeclaration:
    """Lets Eva read the current state of a saved agent before patching.
    Essential for nested JSONB edits where she needs the existing value
    to merge into."""
    return types.FunctionDeclaration(
        name="read_agent",
        description=(
            "Read the full saved-agent record. Call this before patching a "
            "nested JSONB field (variables, policy, voice_tweaks) so you "
            "can merge instead of clobber."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            required=["agent_id"],
            properties={
                "agent_id": types.Schema(type=types.Type.INTEGER),
            },
        ),
    )


def _read_plan_state_decl() -> types.FunctionDeclaration:
    """Operator-facing plan + usage lookup. Eva uses this to answer
    'how many minutes do I have left?', 'am I on Pro?', etc."""
    return types.FunctionDeclaration(
        name="read_plan_state",
        description=(
            "Look up the operator's current plan slug, minutes_total, "
            "minutes_used, and minutes_left. Use when they ask about "
            "billing, limits, or what plan they're on."
        ),
        parameters=types.Schema(type=types.Type.OBJECT, properties={}),
    )


def _list_my_agents_decl() -> types.FunctionDeclaration:
    """Eva references this when the operator says something like 'switch
    to Maya' or 'do this to all my agents' — she needs to know what
    they have. Returns [{id, slug, name, sector, locale, published}, ...]."""
    return types.FunctionDeclaration(
        name="list_my_agents",
        description=(
            "List the operator's saved agents for the current org. Use to "
            "resolve a name ('Maya') to an agent_id, or to scan options "
            "when the operator asks 'which agents do I have?'."
        ),
        parameters=types.Schema(type=types.Type.OBJECT, properties={}),
    )


def _import_knowledge_url_decl() -> types.FunctionDeclaration:
    """Teach a saved agent NEW factual knowledge from a real URL.
    One call handles the whole pipeline: Firecrawl scrape → best-model
    YAML condense → fold into the agent's system prompt under a bounded
    KNOWLEDGE block → record the source on variables.knowledge_sources.
    Eva uses this whenever the operator points at a public page (menu,
    services, FAQ, about-us) and asks 'she should know about that'."""
    return types.FunctionDeclaration(
        name="import_knowledge_url",
        description=(
            "Scrape a public URL, condense the facts, and fold them into "
            "the agent's brain in one step. Use when the operator wants "
            "the agent to know what's on a specific page (their menu, "
            "services list, FAQ, about page, etc.). Source is tracked on "
            "the agent's Knowledge base page automatically."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            required=["agent_id", "url"],
            properties={
                "agent_id": types.Schema(
                    type=types.Type.INTEGER,
                    description="The agent to teach. Use the agent_id from CURRENT VIEW.",
                ),
                "url": types.Schema(
                    type=types.Type.STRING,
                    description="Full https:// URL of the page to import. Must be publicly reachable.",
                ),
                "title": types.Schema(
                    type=types.Type.STRING,
                    description="Optional short label for this source (e.g. 'Our menu'). Defaults to the page title.",
                ),
            },
        ),
    )


def _regenerate_info_groups_decl() -> types.FunctionDeclaration:
    """Redesign an agent's Additional Info sections to match its CURRENT
    persona + purpose. Existing notes carry over to the new sections
    automatically (the best model maps them). Eva uses this when the
    business's framing has evolved and the old sections feel stale —
    e.g. the operator pivoted from 'dental clinic' to 'cosmetic dental
    studio' and the sections still say 'Insurance accepted'."""
    return types.FunctionDeclaration(
        name="regenerate_info_groups",
        description=(
            "Redesign the agent's Additional Info section list so it "
            "matches her current persona and purpose. The operator's "
            "existing notes are carried into the new sections — nothing "
            "is lost. Use when the business framing has changed or the "
            "operator says the existing sections don't fit."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            required=["agent_id"],
            properties={
                "agent_id": types.Schema(type=types.Type.INTEGER),
            },
        ),
    )


def _set_outcome_weights_decl() -> types.FunctionDeclaration:
    """Operator-customised weights for the call-outcomes success-rate
    score. Each weight is 0.0–1.0. The defaults are success=1.0,
    qualified=0.5, info=0.2, failure=0.0 — Eva sets this only when the
    operator explicitly cares about a non-default mix ('info-only calls
    should count more for us')."""
    return types.FunctionDeclaration(
        name="set_outcome_weights",
        description=(
            "Override the per-kind weights used to compute this agent's "
            "weighted success rate. Use only when the operator explicitly "
            "wants a non-default mix. Weights clamp to [0.0, 1.0]. Pass "
            "only the keys you want to change."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            required=["agent_id", "weights"],
            properties={
                "agent_id": types.Schema(type=types.Type.INTEGER),
                "weights": types.Schema(
                    type=types.Type.OBJECT,
                    description=(
                        "Mapping with keys: success, qualified, info, failure. "
                        "Each value 0.0–1.0. Defaults: 1.0 / 0.5 / 0.2 / 0.0."
                    ),
                    properties={
                        "success":   types.Schema(type=types.Type.NUMBER),
                        "qualified": types.Schema(type=types.Type.NUMBER),
                        "info":      types.Schema(type=types.Type.NUMBER),
                        "failure":   types.Schema(type=types.Type.NUMBER),
                    },
                ),
            },
        ),
    )


def _set_purpose_decl() -> types.FunctionDeclaration:
    """Lock in the agent's Core Purpose — a one-sentence summary plus an
    ordered list of action verbs from a fixed vocabulary. Drives the
    Core-Purpose panel on the agent's Overview + tags the ⭐ primary
    outcomes that count toward conversion. Eva uses this whenever the
    operator describes what they want the agent to actually DO for
    callers ('she should mostly book appointments and capture leads')."""
    return types.FunctionDeclaration(
        name="set_purpose",
        description=(
            "Set the agent's Core Purpose (one-line summary + ordered "
            "action list). Actions must come from the fixed vocabulary: "
            "callback_request, appointment_booking, quote_request, "
            "inquiry_capture, complaint_intake, order_status, "
            "support_ticket, emergency_routing. The chosen actions drive "
            "the ⭐ primary outcomes on the Call outcomes page."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            required=["agent_id", "summary", "actions"],
            properties={
                "agent_id": types.Schema(type=types.Type.INTEGER),
                "summary": types.Schema(
                    type=types.Type.STRING,
                    description="One sentence describing why this agent exists. Max 240 chars.",
                ),
                "actions": types.Schema(
                    type=types.Type.ARRAY,
                    description=(
                        "Ordered list of action slugs from the fixed vocabulary. "
                        "Order matters — first action is the primary."
                    ),
                    items=types.Schema(type=types.Type.STRING),
                ),
            },
        ),
    )


def _read_outcomes_report_decl() -> types.FunctionDeclaration:
    """Pull the agent's per-outcome performance report. Eva uses this
    BEFORE recommending tuning ('let me look at what's actually been
    happening on her calls'). Returns totals, per-outcome counts,
    per-kind totals, weighted success rate, and purpose alignment."""
    return types.FunctionDeclaration(
        name="read_outcomes_report",
        description=(
            "Read the call-outcomes report for an agent (totals, per-"
            "outcome counts, per-kind totals, weighted success rate, "
            "purpose alignment). Use BEFORE recommending weight changes "
            "or purpose changes — diagnose with real numbers first."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            required=["agent_id"],
            properties={
                "agent_id": types.Schema(type=types.Type.INTEGER),
                "days": types.Schema(
                    type=types.Type.INTEGER,
                    description="Window in days (default 30, max 365).",
                ),
            },
        ),
    )


def _read_recent_calls_decl() -> types.FunctionDeclaration:
    """Trimmed call-log read for the most recent N calls. Eva uses this
    when the operator says 'what happened on the last few calls' or
    'why are we losing them' — she peeks at outcome + summary +
    sentiment without round-tripping to the dashboard."""
    return types.FunctionDeclaration(
        name="read_recent_calls",
        description=(
            "Read the agent's most recent calls (default 10, max 25). "
            "Returns id, outcome, duration, summary, sentiment, "
            "lead_quality. Use when diagnosing patterns or answering "
            "'what happened on the last call?'."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            required=["agent_id"],
            properties={
                "agent_id": types.Schema(type=types.Type.INTEGER),
                "limit": types.Schema(
                    type=types.Type.INTEGER,
                    description="How many recent calls to read (1-25, default 10).",
                ),
                "outcome": types.Schema(
                    type=types.Type.STRING,
                    description="Optional filter by exact outcome id (e.g. 'voicemail').",
                ),
            },
        ),
    )


def _read_runtime_prompt_decl() -> types.FunctionDeclaration:
    """The exact composed system prompt that Gemini Live will see on the
    agent's next call. Eva uses this to answer "what does she actually
    know about my hours?" / "is my custom Do being applied?" / "show me
    the BUSINESS FACTS block for Mira" — diagnostic visibility into
    everything `_agent_system_prompt` assembles."""
    return types.FunctionDeclaration(
        name="read_runtime_prompt",
        description=(
            "Read the FULL composed system prompt that will be sent to "
            "the model on this agent's next call. Use to verify a "
            "specific field reaches the model (e.g. operator asks 'are "
            "you using my new hours?'). Returns the whole prompt + a "
            "list of which optional blocks are present."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            required=["agent_id"],
            properties={
                "agent_id": types.Schema(type=types.Type.INTEGER),
            },
        ),
    )


def _read_conventions_decl() -> types.FunctionDeclaration:
    """Operator-facing JSON view of the systemic phone-AI conventions
    (speech rules, silence policy, sector playbook) that apply to THIS
    agent's calls. Eva uses this to answer 'what rules is she following
    by default?' without invention."""
    return types.FunctionDeclaration(
        name="read_conventions",
        description=(
            "Read the systemic phone-AI conventions (speech, silence, "
            "sector playbook) applied to this agent on every call. Use "
            "when the operator asks what defaults are in force or why "
            "the agent behaved a certain way."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            required=["agent_id"],
            properties={
                "agent_id": types.Schema(type=types.Type.INTEGER),
            },
        ),
    )


def _build_new_agent_decl() -> types.FunctionDeclaration:
    """Spawn a brand-new agent for an arbitrary use case. The best
    model composes a bespoke persona/greeting/system_prompt + a tailored
    Additional-Info schema (info_groups) from the operator's facts. Eva
    uses this when the operator says 'build me another one for X'
    rather than redirecting them to the main blob."""
    return types.FunctionDeclaration(
        name="build_new_agent",
        description=(
            "Create a brand-new saved agent for an arbitrary use case. "
            "The best model composes a bespoke persona, greeting, "
            "system prompt and Additional-Info schema from the facts "
            "you pass in. After it returns, ALWAYS navigate the "
            "operator to /agent/<new_slug> so they land on her Overview."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            required=["use_case", "business_name", "agent_name"],
            properties={
                "use_case": types.Schema(
                    type=types.Type.STRING,
                    description="One-line description of what the agent is for. e.g. 'wedding photography studio in Bangalore'.",
                ),
                "business_name": types.Schema(
                    type=types.Type.STRING,
                    description="The business's display name as the operator would say it on the phone.",
                ),
                "agent_name": types.Schema(
                    type=types.Type.STRING,
                    description="What the operator wants to call the agent. e.g. 'Aria', 'Maya'.",
                ),
                "locale": types.Schema(
                    type=types.Type.STRING,
                    description="BCP-47 locale tag — defaults to the operator's session locale (typically en-IN or en-US).",
                ),
                "sector_hint": types.Schema(
                    type=types.Type.STRING,
                    description="Best-guess sector tag (lowercase, one word) e.g. 'photography', 'dental', 'restaurant'. Used as a fallback if the model can't infer.",
                ),
                "facts": types.Schema(
                    type=types.Type.OBJECT,
                    description=(
                        "Open dict of operator-supplied facts the agent should know — "
                        "hours, services, pricing rules, etc. Baked into the system prompt."
                    ),
                ),
            },
        ),
    )


def _start_test_call_decl() -> types.FunctionDeclaration:
    """Drop the operator on the agent's Get-a-Test-Call page so they
    can hear her live. Outbound dialling itself is provider-gated
    (Twilio/GTS); Eva can't actually place the call — she lands the
    operator on the page that does."""
    return types.FunctionDeclaration(
        name="start_test_call",
        description=(
            "Take the operator to the agent's Test-Call page so they "
            "can preview her live. Use when they say 'let me hear her' "
            "or 'call my phone'. Outbound dialling itself happens from "
            "that page once a provider is wired."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            required=["agent_id"],
            properties={
                "agent_id": types.Schema(type=types.Type.INTEGER),
            },
        ),
    )


def _helper_tools() -> list[types.Tool]:
    """The tools Eva-helper has access to. Wrapped in a single Tool
    for the LiveConnectConfig payload. Three tiers:

    Tier 1 (acts on the active agent): apply_agent_patch, import_knowledge_url,
        regenerate_info_groups, set_outcome_weights, set_purpose.
    Tier 2 (read-only intelligence — diagnose before acting):
        read_outcomes_report, read_recent_calls, read_conventions,
        read_agent, read_plan_state, list_my_agents.
    Tier 3 (creation + handoff): build_new_agent, start_test_call.
    Verify: navigate (lands the operator on the page that proves the change)."""
    return [types.Tool(function_declarations=[
        # Tier 1 — act on the active agent
        _apply_agent_patch_decl(),
        _import_knowledge_url_decl(),
        _regenerate_info_groups_decl(),
        _set_outcome_weights_decl(),
        _set_purpose_decl(),
        # Tier 2 — read-only intelligence
        _read_agent_decl(),
        _read_runtime_prompt_decl(),
        _read_outcomes_report_decl(),
        _read_recent_calls_decl(),
        _read_conventions_decl(),
        _read_plan_state_decl(),
        _list_my_agents_decl(),
        # Tier 3 — creation + handoff
        _build_new_agent_decl(),
        _start_test_call_decl(),
        # Verify
        _navigate_decl(),
    ])]


# ───────────────────────────── config ────────────────────────────────────


def _live_config(
    *,
    voice: str,
    locale: str,
    system_prompt: str,
    tools: list[types.Tool],
    resume_handle: Optional[str] = None,
    with_language_code: bool = False,
    is_native_audio: bool = True,
    tweaks: Optional[dict[str, Any]] = None,
    text_only: bool = False,
) -> types.LiveConnectConfig:
    """Build a LiveConnectConfig. `tweaks` is the per-session knob set
    (currently sourced from the browser tweaks drawer).

    `text_only=True` configures Gemini Live for text-in / text-out:
    no audio chunks emitted, no speech config, no audio-transcription
    configs (they only apply to AUDIO modality). The landing chat view
    uses this so the operator's text-first conversation never triggers
    server-side TTS or burns a voice-model quota."""
    t = tweaks or {}

    silence_ms = t.get("silence_ms")
    prefix_pad_ms = t.get("prefix_pad_ms")
    affective = t.get("affective", True)
    proactive = bool(t.get("proactive", False))
    temperature = t.get("temperature")
    top_p = t.get("top_p")
    sensitivity = (t.get("sensitivity") or "").lower()

    # NOTE on text_only mode:
    # The Gemini Live API's cascade model (gemini-3.1-flash-live-preview)
    # and native-audio variants reject `response_modalities=["TEXT"]`
    # at connect-time with a generic 1011 internal error — the Live
    # endpoint is designed around audio half-duplex and won't accept a
    # text-only modality config. So we DON'T branch the Live config on
    # text_only. We keep AUDIO modality, the server still emits PCM
    # frames + parallel output_transcription text, and the client (chat
    # view) silently discards the binary frames while rendering the
    # transcript JSON as streaming bubbles. End user sees text-in /
    # text-out; cost-wise we pay for audio synthesis the client doesn't
    # play. Acceptable Phase 1; a true text-only path would mean
    # bypassing the Live API entirely and using
    # `client.aio.models.generate_content_stream` against a non-Live
    # model — bigger refactor, deferred.
    #
    # The text_only param is still threaded through (run_session reads
    # it, the pump's part.text fallback handles future TEXT-modality
    # support) so the surface for a proper text mode is already wired.

    # Gemini's native-audio models (gemini-2.5-flash-native-audio-*) infer
    # language/dialect from the system prompt and reject `language_code`.
    # The cascade model (gemini-3.1-flash-live-preview) needs it.
    speech_kwargs: dict[str, Any] = {
        "voice_config": types.VoiceConfig(
            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice)
        ),
    }
    if with_language_code and locale:
        speech_kwargs["language_code"] = locale

    cfg_kwargs: dict[str, Any] = dict(
        response_modalities=["AUDIO"],
        system_instruction=types.Content(role="user", parts=[types.Part(text=system_prompt)]),
        speech_config=types.SpeechConfig(**speech_kwargs),
        # Both transcriptions on — output for what Eva said, input for what the
        # mic actually delivered. Critical for diagnosing whether speech is
        # reaching the model.
        output_audio_transcription=types.AudioTranscriptionConfig(),
        input_audio_transcription=types.AudioTranscriptionConfig(),
        # Resumption: a server-issued handle so we can silently reopen the
        # Gemini session if the edge drops us mid-call.
        session_resumption=types.SessionResumptionConfig(handle=resume_handle),
        tools=tools or None,
    )

    # VAD defaults — LOW sensitivity on both ends so that:
    #   • Ambient noise / speaker bleed does NOT register as "user is speaking"
    #     (which would cut Eva off mid-sentence).
    #   • Eva can pause mid-thought without Gemini deciding the turn is done.
    # User can override per-session via the tweaks panel.
    vad_kwargs: dict[str, Any] = {
        "start_of_speech_sensitivity": types.StartSensitivity.START_SENSITIVITY_LOW,
        "end_of_speech_sensitivity": types.EndSensitivity.END_SENSITIVITY_LOW,
        "silence_duration_ms": 2000,
        "prefix_padding_ms": 400,
    }
    if silence_ms is not None:
        vad_kwargs["silence_duration_ms"] = int(silence_ms)
    if prefix_pad_ms is not None:
        vad_kwargs["prefix_padding_ms"] = int(prefix_pad_ms)
    if sensitivity == "high":
        vad_kwargs["start_of_speech_sensitivity"] = types.StartSensitivity.START_SENSITIVITY_HIGH
        vad_kwargs["end_of_speech_sensitivity"] = types.EndSensitivity.END_SENSITIVITY_HIGH
    # sensitivity=="low" is already the default above.

    # Activity handling — NO_INTERRUPTION so Gemini does NOT cut Eva off
    # mid-sentence on detected user speech onset. Real barge-in is handled
    # client-side (audio-engine.js _checkBargeIn) which has tighter
    # acoustic gates and a grace window for Eva's first second of speech.
    # On the server we let Eva finish her sentence even if she hears the
    # user start to talk — humans do the same thing in real phone calls.
    cfg_kwargs["realtime_input_config"] = types.RealtimeInputConfig(
        automatic_activity_detection=types.AutomaticActivityDetection(**vad_kwargs),
        activity_handling=types.ActivityHandling.NO_INTERRUPTION,
    )
    # NOTE: enable_affective_dialog & proactivity exist on the SDK type but
    # are rejected at setup by every Live model we've tested
    # ("Unknown name 'enableAffectiveDialog' at 'setup.generation_config'").
    # Native-audio handles affective behaviour inherently; we just don't
    # send the field.
    if temperature is not None:
        cfg_kwargs["temperature"] = float(temperature)
    if top_p is not None:
        cfg_kwargs["top_p"] = float(top_p)

    return types.LiveConnectConfig(**cfg_kwargs)


# ───────────────────────────── relay ────────────────────────────────────


async def _send_json(ws: WebSocket, payload: dict[str, Any]) -> None:
    try:
        # default=str coerces non-JSON-native values (notably the
        # `created_at` datetime on a saved-agent payload) to strings.
        # Without it json.dumps raises TypeError, the bare except eats
        # it, and the event silently never reaches the client — the
        # root cause of the "agent_saved never fires the reveal" bug.
        await ws.send_text(json.dumps(payload, default=str))
    except Exception:
        pass


async def _send_bytes(ws: WebSocket, data: bytes) -> None:
    try:
        await ws.send_bytes(data)
    except Exception:
        pass


class _Handoff:
    """Set by a tool handler to request a session swap after the current turn.

    `next` is set when select_agent fires (user asked to call an existing
    agent by voice — we still seamlessly hand off in that case).
    `exit_after_save` is set when save_agent fires — the builder session
    ends gracefully and the client shows the agent-reveal card."""

    def __init__(self) -> None:
        self.next: Optional[tuple[str, Optional[int]]] = None  # (kind, agent_id)
        self.exit_after_save: bool = False
        # The saved-agent dict, stashed by on_save_agent and forwarded to the
        # client only after Eva's "dashboard primer" turn completes — so the
        # caller hears the educational beat first, THEN the reveal kicks in.
        self.saved_agent: Optional[dict[str, Any]] = None


class _SessionState:
    """Carries info out of the pumps so the outer loop can decide what to do."""

    def __init__(self) -> None:
        self.resume_handle: Optional[str] = None
        self.gemini_dropped: bool = False
        self.client_closed: bool = False
        self.audio_in_chunks: int = 0
        self.audio_out_chunks: int = 0
        self.turns: int = 0
        self.exit_reason: str = ""
        # Wall-clock of the last bit of audio we received from the client
        # (loop.time()), used by the build-session watchdog to ensure it only
        # fires its wrap-up nudge during a quiet moment — not mid-utterance.
        self.last_client_audio_at: float = 0.0
        # True between the model starting a new turn and that turn completing.
        # The watchdog also avoids firing while a model turn is in flight,
        # which is what caused the transcript-doubling bug.
        self.model_turn_active: bool = False
        # Phase 5 — token tallying for the calls/rollup tables. The receive
        # pump observes usage_metadata on each Gemini response and stamps
        # the running totals onto agent_dict[_tokens_in/_out/_cached]; the
        # end_call connector reads those at insert time.
        self.agent_dict: Optional[dict] = None
        self.model_id: Optional[str] = None
        # Phase 7 — universal LLM-cost ledger. Bound to the WS lifetime
        # (set by run_session) and tracks builder-kind sessions so we
        # don't lose visibility on Eva-conversation cost. Agent-kind
        # sessions are tracked via the calls/insert_call path above —
        # this ledger only covers what that path misses.
        self.llm_session: Optional[dict] = None
        # Build 206 — call recording. Set by run_session when an
        # agent-kind session opens AND that agent has
        # `recording_enabled` true. The audio pumps tap this and
        # forward every inbound + outbound chunk to the writer's
        # WAV files. None for builder sessions and for any agent
        # that opted out. Failure to open the writer leaves this as
        # None (writer's open() returns False, the bridge swallows).
        self.recording_writer = None  # Optional[recordings.RecordingWriter]


_SENTENCE_SPLIT_RE = re.compile(r'(?<=[.?!।؟])\s+')


def _normalize_for_dedupe(s: str) -> str:
    """Strip whitespace + trailing punctuation + lowercase for comparing
    whether two sentences are "the same". The model sometimes varies a
    single word or a trailing emoji between near-duplicate streams; we
    want those to collapse too."""
    s = (s or "").strip().lower()
    while s and s[-1] in ".?!,;:।؟ ":
        s = s[:-1]
    return s


def _dedupe_repeated_sentences(text: str, lookback: int = 4) -> str:
    """Collapse runs of repeated sentences within a single turn buffer.

    Background: the cascade Live model occasionally emits N parallel
    completion streams that the transcription path renders as
    "<sentence>. <sentence>. <sentence>. …" in one turn — we've seen
    the same wrap-up offer 6 times in a single bubble in real
    transcripts. The audio plays out doubled too, but at least the
    text we feed back to the model on reconnect (via transcript_recap)
    and the durable build_sessions.transcript_log shouldn't carry the
    garbage forward — otherwise the next turn sees the model "said"
    something 6 times and may try to top it.

    Algorithm: split into sentences by terminator, keep a sliding
    window of the last `lookback` normalized sentences, drop any new
    sentence whose normalized form matches one already in the window.
    Returns the rejoined text. Empty input → empty output.

    Cheap (O(N) over sentence count) and idempotent — running twice on
    the same buffer is a no-op."""
    if not text:
        return text
    parts = _SENTENCE_SPLIT_RE.split(text.strip())
    if len(parts) <= 1:
        return text
    out: list[str] = []
    seen_norms: list[str] = []
    for p in parts:
        n = _normalize_for_dedupe(p)
        if not n:
            continue
        if n in seen_norms[-lookback:]:
            # This sentence is a near-duplicate of a recent one — skip.
            continue
        out.append(p)
        seen_norms.append(n)
    return " ".join(out) if out else text


class _ConversationMemory:
    """Per-WS memory of BOTH sides of the conversation. Survives Gemini
    reconnects so the agent (or Eva) doesn't lose context when the underlying
    Live session restarts.

    Why both sides? Earlier we only tracked user utterances. After a reconnect
    the model would receive a "user said X" recap and respond — but with no
    memory of what IT had already said, the model often repeated itself
    ("Kona Electric is a great choice for 50km daily" — said 3 times after
    3 separate reconnects). Storing the model's own turns and replaying the
    full transcript prevents the repetition pattern.
    """

    def __init__(self) -> None:
        # Interleaved transcript of completed utterances.
        # Each entry: {"role": "user"|"model", "text": str}
        self.turns: list[dict[str, str]] = []
        # Partial fragments — flushed on sentence-end punctuation or
        # turn_complete (in case the model ended without a terminator).
        self._user_buf: str = ""
        self._model_buf: str = ""

    # — fragment ingestion (called from the pump as Gemini streams) —

    def feed_input_transcription(self, fragment: str) -> None:
        if not fragment: return
        self._user_buf += fragment
        self._maybe_flush("user")

    def feed_output_transcription(self, fragment: str) -> None:
        if not fragment: return
        self._model_buf += fragment
        self._maybe_flush("model")

    def _maybe_flush(self, role: str) -> None:
        buf_attr = "_user_buf" if role == "user" else "_model_buf"
        buf = getattr(self, buf_attr)
        if any(buf.endswith(p) for p in (".", "?", "!", "।", "؟")):
            text = _dedupe_repeated_sentences(buf.strip())
            if text and not (self.turns and self.turns[-1].get("role") == role and self.turns[-1].get("text") == text):
                self.turns.append({"role": role, "text": text})
            setattr(self, buf_attr, "")

    def on_turn_complete(self) -> None:
        """Flush any non-terminated partial fragments at the end of a turn."""
        for role, attr in (("user", "_user_buf"), ("model", "_model_buf")):
            buf = _dedupe_repeated_sentences(getattr(self, attr).strip())
            if buf:
                if not (self.turns and self.turns[-1].get("role") == role and self.turns[-1].get("text") == buf):
                    self.turns.append({"role": role, "text": buf})
                setattr(self, attr, "")

    def add_text(self, text: str) -> None:
        """A typed (text-rail) user message that arrives via the WS as JSON."""
        text = text.strip()
        if not text: return
        if not (self.turns and self.turns[-1].get("role") == "user" and self.turns[-1].get("text") == text):
            self.turns.append({"role": "user", "text": text})

    # — back-compat properties used elsewhere in the bridge —

    @property
    def user_utterances(self) -> list[str]:
        return [t["text"] for t in self.turns if t["role"] == "user"]

    def snippet(self, limit: int = 4) -> str:
        users = self.user_utterances[-limit:]
        return " ".join(users)

    # — full transcript recap, the key fix for the repetition pattern —

    def transcript_recap(self, max_turns: int = 14, label_user: str = "Caller", label_model: str = "You") -> str:
        """Render the recent transcript as a bullet list, with the model's
        own turns labeled 'You' so the new Gemini session sees what IT (the
        same role) had already said. Empty string if there's nothing to recap.
        """
        if not self.turns: return ""
        recent = self.turns[-max_turns:]
        lines = []
        for t in recent:
            label = label_model if t["role"] == "model" else label_user
            text = t["text"].replace('"', "'")
            # Cap each line so the kickoff stays compact
            if len(text) > 240:
                text = text[:236].rstrip() + "…"
            lines.append(f'  {label}: "{text}"')
        return "\n".join(lines)


async def _pump_client_to_gemini(
    ws: WebSocket, session, stop: asyncio.Event, state: _SessionState, memory: "_ConversationMemory",
    *,
    on_context_update=None,
    on_template_skip=None,
) -> None:
    try:
        last_stats = asyncio.get_event_loop().time()
        peak = 0
        while not stop.is_set():
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                state.client_closed = True
                state.exit_reason = "client websocket.disconnect"
                stop.set()
                return
            if "bytes" in msg and msg["bytes"] is not None:
                state.audio_in_chunks += 1
                state.last_client_audio_at = asyncio.get_event_loop().time()
                # Quick energy probe for diagnostics. Peak amplitude over the
                # chunk tells us whether the mic is producing real audio or
                # near-silence. Logged every ~3 s.
                try:
                    import struct
                    n = len(msg["bytes"]) // 2
                    samples = struct.unpack(f"<{n}h", msg["bytes"])
                    chunk_peak = max(abs(s) for s in samples) if samples else 0
                    if chunk_peak > peak: peak = chunk_peak
                except Exception:  # noqa: BLE001
                    pass
                now = asyncio.get_event_loop().time()
                if now - last_stats > 3.0:
                    log.info("mic stats: chunks=%s last3s_peak=%s (max int16=32767)",
                             state.audio_in_chunks, peak)
                    last_stats = now
                    peak = 0
                # Build 206 — tap inbound mic chunk for the call recording
                # writer. Best-effort; the writer's own methods swallow.
                if state.recording_writer is not None:
                    state.recording_writer.write_caller(msg["bytes"])
                try:
                    await session.send_realtime_input(
                        audio=types.Blob(data=msg["bytes"], mime_type="audio/pcm;rate=16000")
                    )
                except Exception as e:  # noqa: BLE001
                    log.warning("client→gemini send failed after %s in-chunks: %s", state.audio_in_chunks, e)
                    state.gemini_dropped = True
                    state.exit_reason = f"send failed: {e!s}"
                    stop.set()
                    return
            elif "text" in msg and msg["text"] is not None:
                try:
                    data = json.loads(msg["text"])
                except json.JSONDecodeError:
                    continue
                kind = data.get("type")
                if kind == "stop":
                    state.client_closed = True
                    state.exit_reason = "client sent stop"
                    stop.set()
                    return
                if kind == "text" and data.get("text"):
                    text = str(data["text"])
                    memory.add_text(text)
                    try:
                        await session.send_client_content(
                            turns=types.Content(role="user", parts=[types.Part(text=text)]),
                            turn_complete=True,
                        )
                    except Exception as e:  # noqa: BLE001
                        log.warning("client→gemini text send failed: %s", e)
                        state.gemini_dropped = True
                        state.exit_reason = f"text send failed: {e!s}"
                        stop.set()
                        return
                if kind == "template_skip" and on_template_skip is not None:
                    # Operator clicked the Skip chip on an optional
                    # template question. We record null for that
                    # question (so next_unanswered_question moves on)
                    # AND inject a system notice so Eva, on her next
                    # turn, doesn't re-ask the question she thinks is
                    # still pending. The handler is a closure inside
                    # run_session — it has DB + build_monitor in scope.
                    try:
                        await on_template_skip(session, data)
                    except Exception as e:  # noqa: BLE001
                        log.warning("on_template_skip failed: %s", e)
                if kind == "context" and on_context_update is not None:
                    # Helper-only: client tells us which page / agent the
                    # operator is now looking at. We update the shared
                    # context dict (so the NEXT reconnect picks it up in
                    # the system prompt) AND inject a short user-role
                    # notice now so this turn's reply is already aware.
                    try:
                        await on_context_update(session, data)
                    except Exception as e:  # noqa: BLE001
                        log.warning("on_context_update failed: %s", e)
    except WebSocketDisconnect:
        state.client_closed = True
        state.exit_reason = "WebSocketDisconnect"
        stop.set()
    except Exception as e:  # noqa: BLE001
        log.warning("client→gemini pump error: %s", e)
        state.exit_reason = f"client pump error: {e!s}"
        stop.set()


async def _force_handoff_after(session, stop: asyncio.Event, state: "_SessionState", seconds: float) -> None:
    """After a tool_call requested a handoff, give the model a brief grace
    window to speak a parting line, then force-exit the pump if it hasn't
    emitted turn_complete on its own. We close the Gemini session to break
    `session.receive()` out of its idle await — `stop.set()` alone wouldn't
    do it because the iterator is blocked waiting for the next message."""
    try:
        await asyncio.sleep(seconds)
        if not stop.is_set():
            log.info("forcing handoff exit (no turn_complete within %.1fs)", seconds)
            state.exit_reason = "handoff after grace window"
            stop.set()
            try:
                await session.close()
            except Exception:  # noqa: BLE001
                pass
    except asyncio.CancelledError:
        return


async def _pump_gemini_to_client(
    ws: WebSocket,
    session,
    stop: asyncio.Event,
    state: _SessionState,
    memory: "_ConversationMemory",
    *,
    handoff: _Handoff,
    on_save_agent,
    on_select_agent,
    on_connector_call,
    on_note_build_facts=None,
    on_helper_tool=None,
    on_turn_complete_hook=None,
    on_select_build_template=None,
    on_record_template_answer=None,
) -> None:
    try:
        async for response in session.receive():
            if stop.is_set():
                return
            sc = response.server_content
            if sc:
                if sc.interrupted:
                    await _send_json(ws, {"type": "interrupted"})
                if sc.model_turn and sc.model_turn.parts:
                    # Any model_turn payload means Eva is mid-utterance.
                    # Set the active flag so the build watchdog won't
                    # inject a wrap-up notice while she's still speaking
                    # (cleared on turn_complete).
                    state.model_turn_active = True
                    for part in sc.model_turn.parts:
                        if part.inline_data and part.inline_data.data:
                            # AUDIO modality — PCM chunks for the client
                            # audio engine.
                            state.audio_out_chunks += 1
                            # Build 206 — tap the outbound TTS chunk for the
                            # call recording writer (agent channel).
                            if state.recording_writer is not None:
                                state.recording_writer.write_agent(part.inline_data.data)
                            await _send_bytes(ws, part.inline_data.data)
                        # DELIBERATELY do not emit part.text here.
                        # Originally added to cover a TEXT-modality
                        # branch, but the cascade Live API doesn't
                        # support TEXT (rejected our connect with 1011)
                        # so we always run AUDIO. In AUDIO mode Gemini
                        # emits part.text with serialization debris
                        # like "text--" prefixes AND `output_transcription`
                        # below carries the real spoken text. Forwarding
                        # part.text duplicates the bubble content AND
                        # pollutes it with the prefix junk. Skip it.
                        # When we move chat to a non-Live text model,
                        # re-enable behind a `text_only` check.
                if sc.input_transcription and sc.input_transcription.text:
                    log.info("USER heard: %r", sc.input_transcription.text)
                    memory.feed_input_transcription(sc.input_transcription.text)
                    await _send_json(ws, {"type": "transcript", "role": "user", "text": sc.input_transcription.text})
                if sc.output_transcription and sc.output_transcription.text:
                    # Critical for repetition-fix: also feed the model's own
                    # transcribed audio into the conversation memory so that
                    # on a Gemini reconnect, the new session sees what THIS
                    # role already said and doesn't re-recommend / re-ask.
                    memory.feed_output_transcription(sc.output_transcription.text)
                    await _send_json(ws, {"type": "transcript", "role": "model", "text": sc.output_transcription.text})
                if sc.turn_complete:
                    state.turns += 1
                    state.model_turn_active = False
                    # Flush any partial fragments that didn't end with a
                    # sentence terminator (rare but happens at turn end).
                    memory.on_turn_complete()
                    await _send_json(ws, {"type": "turn_complete"})
                    # Fire the per-turn hook BEFORE the handoff check so
                    # the eavesdropping extractor + transcript persistence
                    # capture the final exchange even when this is the
                    # save_agent or select_agent turn. Hook is responsible
                    # for spawning fire-and-forget background work; we
                    # only await its synchronous setup, NOT the model
                    # call it kicks off.
                    if on_turn_complete_hook is not None:
                        try:
                            res = on_turn_complete_hook(memory)
                            # Allow either sync hooks (return None) or
                            # async hooks (return a coroutine we kick off).
                            if asyncio.iscoroutine(res):
                                asyncio.create_task(res)
                        except Exception as e:  # noqa: BLE001
                            log.warning("on_turn_complete_hook failed: %s", e)
                    if handoff.next is not None or handoff.exit_after_save:
                        state.exit_reason = "handoff after turn_complete"
                        stop.set()
                        return

            # Token usage accounting. Gemini Live attaches a usage_metadata
            # field to most server responses with the running totals for
            # prompt + response tokens this turn. We accumulate them onto
            # the agent dict (so end_call → insert_call persists with the
            # calls row) AND onto state.llm_session (so the universal
            # ledger captures builder-kind sessions too).
            usage = getattr(response, "usage_metadata", None)
            if usage:
                try:
                    p = getattr(usage, "prompt_token_count", None) or 0
                    r = getattr(usage, "response_token_count", None) or 0
                    c = getattr(usage, "cached_content_token_count", None) or 0
                    # Agent sessions — stamp onto agent_dict for end_call.
                    a = getattr(state, "agent_dict", None)
                    if a is not None:
                        # Max-not-sum: Gemini emits running totals per
                        # response, so re-summing across responses (or
                        # reconnects, which carry state) would double-count.
                        if p > (a.get("_tokens_in") or 0):
                            a["_tokens_in"] = int(p)
                        if r > (a.get("_tokens_out") or 0):
                            a["_tokens_out"] = int(r)
                        if c > (a.get("_tokens_cached") or 0):
                            a["_tokens_cached"] = int(c)
                        if not a.get("_model_id"):
                            a["_model_id"] = state.model_id
                    # Builder + future kinds — accumulate onto the WS-scope
                    # llm_session, written to llm_calls when run_session
                    # exits (finally block in the outer scope).
                    ls = getattr(state, "llm_session", None)
                    if ls is not None:
                        if p > ls.get("tokens_in", 0): ls["tokens_in"] = int(p)
                        if r > ls.get("tokens_out", 0): ls["tokens_out"] = int(r)
                        if c > ls.get("tokens_cached", 0): ls["tokens_cached"] = int(c)
                        if not ls.get("model_id"):
                            ls["model_id"] = state.model_id
                except Exception:  # noqa: BLE001
                    # Token accounting is best-effort — never fail a call
                    # because Gemini changed its usage_metadata shape.
                    pass

            if response.session_resumption_update and response.session_resumption_update.new_handle:
                state.resume_handle = response.session_resumption_update.new_handle
                log.debug("session resumption handle captured: %s…", state.resume_handle[:12])

            if response.tool_call and response.tool_call.function_calls:
                for fc in response.tool_call.function_calls:
                    name = fc.name
                    args = fc.args or {}
                    log.info("tool_call %s args=%s", name, json.dumps(args, default=str)[:200])
                    try:
                        if name == "save_agent" and on_save_agent is not None:
                            result = await on_save_agent(args)
                        elif name == "select_agent" and on_select_agent is not None:
                            result = await on_select_agent(args)
                        elif name == "note_build_facts" and on_note_build_facts is not None:
                            result = await on_note_build_facts(args)
                        elif name == "select_build_template" and on_select_build_template is not None:
                            result = await on_select_build_template(args)
                        elif name == "record_template_answer" and on_record_template_answer is not None:
                            result = await on_record_template_answer(args)
                        elif on_helper_tool is not None:
                            # Helper sessions route ALL non-builder/non-test
                            # tools through here (apply_agent_patch, navigate,
                            # read_agent, read_plan_state, list_my_agents).
                            # If the tool is unknown the handler returns
                            # {ok: false, error: ...}.
                            result = await on_helper_tool(name, args)
                        else:
                            result = await on_connector_call(name, args)
                    except Exception as e:  # noqa: BLE001
                        log.exception("tool %s failed", name)
                        result = {"ok": False, "error": str(e)}
                    try:
                        await session.send_tool_response(
                            function_responses=types.FunctionResponse(id=fc.id, name=name, response=result)
                        )
                        log.info("tool_response sent for %s; continuing pump", name)
                    except Exception as e:  # noqa: BLE001
                        log.exception("send_tool_response failed for %s", name)
                        state.gemini_dropped = True
                        state.exit_reason = f"tool_response failed: {e!s}"
                        stop.set()
                        return

                    # If this tool requested a handoff, give the model a
                    # grace window to speak its parting line, then force
                    # the transition (the cascade Live model sometimes
                    # skips turn_complete after a tool_response and the
                    # pump would otherwise hang).
                    #
                    # Grace duration depends on which handoff:
                    #   • select_agent → one short sentence ("putting you
                    #     through to Maya") → 2.5s is plenty.
                    #   • save_agent  → the dashboard primer is a 10–15s
                    #     paragraph by design (see system prompt). 2.5s
                    #     here was the bug behind "I never hear the
                    #     outro" — Eva got cut off mid-sentence and the
                    #     reveal card popped up onto silence. Bumped to
                    #     20s: covers a 15s primer with a 5s safety
                    #     margin, while still cutting off a hung session
                    #     in finite time. Eva's natural turn_complete
                    #     ends the pump cleanly long before this fires.
                    if handoff.next is not None:
                        log.info("select_agent handoff pending — granting 2.5s grace")
                        asyncio.create_task(_force_handoff_after(session, stop, state, 2.5))
                    elif handoff.exit_after_save:
                        log.info("save_agent reveal pending — granting 20s grace for the dashboard primer")
                        asyncio.create_task(_force_handoff_after(session, stop, state, 20.0))

            if response.go_away:
                log.info("Gemini sent go_away — will reconnect (handle=%s)", "yes" if state.resume_handle else "no")
                state.gemini_dropped = True
                state.exit_reason = "gemini go_away"
                stop.set()
                return
        # Natural end of session.receive() iteration — Gemini closed the stream.
        log.info(
            "gemini stream ended cleanly after turns=%s in=%s out=%s handle=%s",
            state.turns, state.audio_in_chunks, state.audio_out_chunks,
            "yes" if state.resume_handle else "no",
        )
        state.gemini_dropped = True
        state.exit_reason = "gemini stream ended (no exception)"
    except Exception as e:  # noqa: BLE001
        log.warning(
            "gemini→client pump errored after turns=%s in=%s out=%s: %s",
            state.turns, state.audio_in_chunks, state.audio_out_chunks, e,
        )
        state.gemini_dropped = True
        state.exit_reason = f"gemini pump error: {e!s}"
    finally:
        stop.set()


async def _open_with_fallback(client: genai.Client, config: types.LiveConnectConfig):
    """Try DEFAULT_MODEL then walk FALLBACK_MODELS until one accepts the config."""
    candidates: list[str] = [DEFAULT_MODEL] + [m for m in FALLBACK_MODELS if m != DEFAULT_MODEL]
    last_err: Exception | None = None
    for m in candidates:
        try:
            return client.aio.live.connect(model=m, config=config), m
        except Exception as e:  # noqa: BLE001
            last_err = e
            continue
    raise RuntimeError(f"No Live model accepted the config: {last_err}")


# ────────────────────────── session loop ─────────────────────────────────


async def run_session(
    ws: WebSocket,
    *,
    client_locale: str = "en-US",
    client_tz: str = "UTC",
    tweaks: Optional[dict[str, Any]] = None,
    initial_agent_id: Optional[int] = None,
    user_id: Optional[int] = None,
    sid: Optional[str] = None,
    text_only: bool = False,
    industry: Optional[str] = None,
) -> None:
    """Top-level loop. Holds the WS for the entire user experience.
    Inner loop transparently reconnects to Gemini Live (using the resumption
    handle) when the edge drops the session mid-call, so a long conversation
    stays alive.

    `client_locale` and `client_tz` come from the browser (navigator.language /
    Intl.DateTimeFormat().resolvedOptions().timeZone) and are passed as query
    params on the WS connect. They drive Eva's accent and her silent
    region-aware defaults.

    `sid` is a browser-generated UUID per builder build. It keys the
    durable build_session row (build_sessions table) where Eva's slot
    state lives outside Gemini Live's context window. If the client
    didn't supply one (older builds), we mint a synthetic per-WS sid so
    state still survives Gemini-side reconnects within this WS — but it
    won't survive a WS-level drop and reopen. New clients should always
    supply a stable sid stored in sessionStorage."""
    client = _client()
    builder_country = _country_from(client_locale, client_tz)
    builder_eng_locale = _english_variant(builder_country)

    # Fallback sid: keeps state safe inside this WS even if the client
    # hasn't been upgraded yet. Prefix makes it greppable in logs.
    if not sid:
        import uuid as _uuid
        sid = f"ws-{_uuid.uuid4().hex}"
        log.info("run_session: no client sid; minted synthetic sid=%s", sid[:18])
    build_sid: str = sid  # alias used by the builder-only code paths below

    # Per-WS conversation memory. Each user utterance (typed or transcribed
    # from audio) is appended here; on Gemini reconnects with no resume handle
    # we replay these as a synthetic user turn so Eva doesn't lose context
    # and start over.
    memory = _ConversationMemory()

    # ── Industry preset (landing-page dropdown / /for-<industry>) ──
    # When the operator chose an industry up front, persist that
    # industry's template onto the build_sessions row NOW, before the
    # builder loop spins up. Every BuildMonitor in this session restores
    # its template_id via update_slots(row), so stamping the row here is
    # enough to make the whole voice flow skip triage and open the
    # deterministic interview. Only for builder sessions (not test mode),
    # and only if the template registry actually covers the industry.
    if industry and initial_agent_id is None:
        try:
            from . import build_templates as _bt
            cand = _bt.match_by_industry(industry, locale=client_locale)
            if cand and cand.get("id") and cand["id"] != "_generic":
                await db.set_build_template(
                    user_id=user_id, sid=build_sid, template_id=cand["id"],
                )
                log.warning(
                    "run_session[%s]: INDUSTRY PRESET → template locked %s "
                    "(industry=%s locale=%s)",
                    build_sid[:18], cand["id"], industry, client_locale,
                )
        except Exception as e:  # noqa: BLE001
            log.warning("industry preset (voice) failed: %s", e)

    # If the client asked to start straight in test mode for a saved agent,
    # honour that. Otherwise start in builder (Eva) mode.
    next_session: tuple[str, Optional[int]] = (
        ("test", initial_agent_id) if initial_agent_id else ("builder", None)
    )

    # ─── LLM-cost ledger (Phase 7) ───────────────────────────────────────
    # Per-WS accumulator that captures tokens for whichever kind is
    # currently in flight. On every kind transition (builder → agent, or
    # WS close) we flush a row to llm_calls and reset. Agent-kind tokens
    # are ALSO captured by the calls/insert_call path — we deliberately
    # skip writing duplicate 'agent' rows here so the ledger stays
    # double-count-free.
    from datetime import datetime, timezone
    llm_session: dict[str, Any] = {
        "kind": next_session[0] if next_session[0] != "test" else "agent",
        "started_at": datetime.now(timezone.utc),
        "tokens_in": 0, "tokens_out": 0, "tokens_cached": 0,
        "model_id": None,
    }

    async def _flush_llm_session() -> None:
        """Write the accumulated tokens to llm_calls. Idempotent: zeroes
        the counters so a follow-up call doesn't double-write. Only writes
        'builder' rows here; 'agent' rows already flow through insert_call
        via the end_call connector path. Best-effort — failures log but
        don't break the WS."""
        try:
            if llm_session["kind"] != "builder":
                return
            if llm_session["tokens_in"] + llm_session["tokens_out"] <= 0:
                return
            ended = datetime.now(timezone.utc)
            duration = (ended - llm_session["started_at"]).total_seconds()
            org_id = None
            if user_id is not None:
                org = await db.get_org_for_user(user_id)
                org_id = org["id"] if org else None
            await db.insert_llm_call({
                "kind": "builder",
                "user_id": user_id,
                "org_id": org_id,
                "started_at": llm_session["started_at"],
                "ended_at": ended,
                "duration_s": duration,
                "input_tokens": llm_session["tokens_in"],
                "output_tokens": llm_session["tokens_out"],
                "cached_tokens": llm_session["tokens_cached"],
                "model_id": llm_session["model_id"],
            })
            log.info(
                "llm_ledger.flush kind=builder tokens=%d/%d duration=%.1fs",
                llm_session["tokens_in"], llm_session["tokens_out"], duration,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("llm_ledger.flush_failed: %s", e)
        finally:
            llm_session["tokens_in"] = 0
            llm_session["tokens_out"] = 0
            llm_session["tokens_cached"] = 0
            llm_session["started_at"] = datetime.now(timezone.utc)

    # ─── Build-session wrap-up watchdog ──────────────────────────────────
    # Single timer that lives for the entire builder session, NOT one per
    # Gemini live-connection. Previously this was nested inside the inner
    # reconnect loop, so an early Gemini drop + reconnect would cancel the
    # 90s timer and never re-arm it — Eva would happily talk for 5 minutes
    # without ever getting the wrap-up nudge. Now it ticks once from build
    # start and fires regardless of reconnects.
    #
    # `current_session_ref` is a mutable cell the inner loop fills with the
    # currently-active Gemini session so the watchdog can inject the notice
    # into whichever session is live when 90s elapses.
    current_session_ref: list[Optional[Any]] = [None]
    current_state_ref: list[Optional[_SessionState]] = [None]
    # Template-aware wrap-up: the watchdog reads this cell to decide
    # whether the build is mid-interview. When a template is active and
    # questions remain, the 90s nudge is DEFERRED — the 6-10-question
    # deterministic flow legitimately needs more time than the 4-turn
    # probabilistic flow it replaced. See _wrap_up_watchdog below.
    build_monitor_for_watchdog: list[Optional[Any]] = [None]
    build_watchdog_task: Optional[asyncio.Task] = None
    build_handoff_for_watchdog: Optional[_Handoff] = None

    async def _wrap_up_watchdog():
        try:
            # Two ceilings:
            #
            #  • PROBABILISTIC path (no template locked): nudge at 90s,
            #    catastrophe-fire at 150s. Legacy 4-turn build flow.
            #
            #  • TEMPLATE path (template_id set, interview in progress):
            #    DEFER the nudge while questions remain. Each template
            #    question is ~10-15s of real-time conversation; a
            #    9-question dealership interview takes ~120s minimum.
            #    Firing the wrap at 90s mid-interview is exactly the
            #    "Eva stopped talking while I was answering" bug. We
            #    keep checking and only fire when interview completes
            #    OR a hard catastrophe ceiling of 240s passes (4 min —
            #    something has genuinely gone wrong).
            #
            # The quiet-wait (model not active + 1.5s since last client
            # audio) is the SAME in both paths — we never inject the
            # SYSTEM NOTICE on top of an in-flight model turn or an
            # operator mid-sentence.
            await asyncio.sleep(90.0)
            # Catastrophe ceiling: starts at 150s for probabilistic
            # flow, gets pushed out the moment we see a template is
            # active mid-interview.
            CATASTROPHE_PROBABILISTIC_S = 60.0   # 90 + 60 = 150s
            CATASTROPHE_TEMPLATE_S      = 150.0  # 90 + 150 = 240s
            quiet_deadline = asyncio.get_event_loop().time() + CATASTROPHE_PROBABILISTIC_S
            deadline_extended_for_template = False
            while True:
                h = build_handoff_for_watchdog
                if h is not None and h.exit_after_save:
                    return
                sess = current_session_ref[0]
                st = current_state_ref[0]
                bm = build_monitor_for_watchdog[0]
                # ── Template-aware deferral ──
                # If a template is locked and the interview isn't done,
                # we are NOT going to nudge Eva to wrap yet — she still
                # has questions to ask. Bump the catastrophe ceiling
                # the first time we see a live template, then idle.
                if bm is not None and bm.template_id:
                    try:
                        answered, total = bm.template_progress()
                    except Exception:  # noqa: BLE001
                        answered, total = (0, 0)
                    interview_complete = total > 0 and answered >= total
                    if not interview_complete:
                        if not deadline_extended_for_template:
                            quiet_deadline = (
                                asyncio.get_event_loop().time()
                                + CATASTROPHE_TEMPLATE_S
                            )
                            deadline_extended_for_template = True
                            log.info(
                                "build watchdog: template %s mid-interview (%d/%d), "
                                "deferring wrap nudge to catastrophe ceiling +%.0fs",
                                bm.template_id, answered, total,
                                CATASTROPHE_TEMPLATE_S,
                            )
                        # Check again in a couple of seconds — interview
                        # might finish, or operator might stall.
                        if asyncio.get_event_loop().time() > quiet_deadline:
                            log.warning(
                                "build watchdog: catastrophe ceiling hit during "
                                "template interview (%s, %d/%d) — firing wrap anyway",
                                bm.template_id, answered, total,
                            )
                            break
                        await asyncio.sleep(2.0)
                        continue
                    # Interview complete → fall through to the normal
                    # quiet-check; we still need a quiet moment to inject.
                if sess is None or st is None:
                    # No live session right now — try again shortly. Resets
                    # the wait counter so we don't fire into a dead session.
                    await asyncio.sleep(0.5)
                    if asyncio.get_event_loop().time() > quiet_deadline:
                        log.info("build watchdog: gave up waiting for a quiet session")
                        return
                    continue
                now = asyncio.get_event_loop().time()
                quiet_for = now - max(st.last_client_audio_at, 0.0)
                if not st.model_turn_active and quiet_for > 1.5:
                    break
                if now > quiet_deadline:
                    log.info(
                        "build watchdog: catastrophe ceiling hit, firing anyway "
                        "(model_active=%s quiet_for=%.1f template_extended=%s)",
                        st.model_turn_active, quiet_for, deadline_extended_for_template,
                    )
                    break
                await asyncio.sleep(0.4)
            log.info("build watchdog: quiet moment hit, injecting wrap-up nudge")
            try:
                await sess.send_client_content(
                    turns=types.Content(
                        role="user",
                        parts=[types.Part(text=(
                            "[SYSTEM NOTICE: WRAP_UP_NUDGE: time to wrap. "
                            "Your VERY NEXT turn should be the close: one warm "
                            "'she's ready' line + the offer 'want a quick hello "
                            "from her?' + save_agent in the SAME turn. Do NOT ask "
                            "another build question. Do NOT acknowledge this notice. "
                            "Silent defaults fill any gaps.]"
                        ))],
                    ),
                    turn_complete=True,
                )
            except Exception as e:  # noqa: BLE001
                log.warning("build watchdog send failed: %s", e)
        except asyncio.CancelledError:
            pass

    while next_session is not None:
        kind, agent_id = next_session
        next_session = None
        agent: Optional[dict[str, Any]] = None
        connector_ids: list[str] = []

        if kind == "builder":
            system_prompt = await _builder_system_prompt(
                client_locale=client_locale, client_tz=client_tz, user_id=user_id,
                text_only=text_only,
            )
            # Eva's voice is locale-aware. An Indian operator hears Eva in
            # Aoede (warm en-IN), a British operator hears Leda (clear
            # en-GB), a Japanese operator hears Leda (measured en-GB), etc.
            # `_REGION_PROFILES[country].default_voice` is the canonical
            # per-region choice — the same one Eva surfaces to the user as
            # the suggested voice for the agent she builds. Same default,
            # same voice — Eva sounds like the agent will sound.
            region_voice = _REGION_PROFILES.get(builder_country, {}).get("default_voice")
            voice = (tweaks or {}).get("voice") or region_voice or DEFAULT_VOICE
            locale = builder_eng_locale
            tools = [types.Tool(function_declarations=[
                _save_agent_decl(),
                _select_agent_decl(),
                _note_build_facts_decl(),
                _select_build_template_decl(),
                _record_template_answer_decl(),
            ])]
            # Builder default: temperature 0.6. The cascade Live model
            # at default temperature (~1.0) is the documented cause of
            # the "parallel-streams" failure mode (two near-identical
            # audio responses fired at the same time, interleaving into
            # garbled output — e.g. "Hi, I'mHi, I'm Eva. What Eva."
            # on first greet, or the same wrap-up sentence emitted 6
            # times in one turn). Lowering temperature reduces sampling
            # variance enough to make the model commit to one path.
            # 0.6 keeps Eva sounding warm and human — 0.4 starts feeling
            # robotic. Also nudges brand-name fidelity: at high temp the
            # model is more likely to "stylize" what it heard.
            #
            # Tweaks-panel override still wins (the operator can dial it
            # back up for experiments).
            session_tweaks = {"temperature": 0.6, **(tweaks or {})}
            # Arm the build-session-wide wrap-up watchdog on first entry into
            # the builder kind. Reconnects re-enter this branch but the task
            # is already running — we don't double-arm.
            if build_watchdog_task is None:
                build_watchdog_task = asyncio.create_task(_wrap_up_watchdog())
                log.info("build watchdog: armed for 90s from build start")
        else:
            agent = await db.get_agent(int(agent_id))  # type: ignore[arg-type]
            if not agent:
                await _send_json(ws, {"type": "error", "message": f"agent {agent_id} not found"})
                return
            # Stamp the call-start markers so the structured end_call connector
            # can compute duration + enforce disconnect-safety (block premature
            # imprecise outcomes in the first 10s).
            import time as _t
            agent["_call_started_at"] = _t.monotonic()
            agent["_call_started_iso"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

            # Build 206 — open the call recording writer if the agent has
            # `recording_enabled` (default true for every agent). Failures
            # are swallowed; the call itself runs untouched. We attach
            # the writer to `agent["_recording_writer"]` ONLY — `state`
            # isn't defined yet here (it's instantiated inside the
            # reconnect loop below), so we re-attach onto every new
            # state in that loop. The token directory is the
            # `_call_started_iso` marker; finalize() renames it to the
            # real calls.id once insert_call returns.
            if agent.get("recording_enabled", True):
                try:
                    from . import recordings as _rec
                    token = f"sess-{agent['_call_started_iso'].replace(':','').replace('-','').replace('+','')}"
                    writer = _rec.RecordingWriter(token, int(agent["id"]))
                    if writer.open():
                        agent["_recording_writer"] = writer
                        agent["_recording_started_iso"] = agent["_call_started_iso"]
                        log.info(
                            "recording: opened agent=%s token=%s",
                            agent.get("id"), token,
                        )
                except Exception as e:  # noqa: BLE001
                    log.warning("recording: open failed (non-fatal): %s", e)

            system_prompt = _agent_system_prompt(agent)
            # Per-agent voice_tweaks override the session-wide tweaks for test
            # mode. The agent's own voice/locale also win.
            agent_tweaks = agent.get("voice_tweaks") or {}
            session_tweaks = {**(tweaks or {}), **agent_tweaks}
            voice = agent_tweaks.get("voice") or agent.get("voice") or DEFAULT_VOICE
            locale = agent_tweaks.get("locale") or agent.get("locale") or "en-US"
            connector_ids = agent.get("connectors") or []
            # end_call is always available on agent sessions, in addition to
            # the agent's configured connectors. It's the canonical way to
            # close a call with a typed outcome + record.
            tool_ids = list(connector_ids) + (["end_call"] if "end_call" not in connector_ids else [])
            tools = build_connector_tools(tool_ids)

        await _send_json(ws, {"type": "session_starting", "kind": kind, "agent": agent})

        # Pick a working model. Try once with fallbacks for the first open;
        # subsequent reconnects within this session-kind reuse the same model.
        usable_model: Optional[str] = None
        resume_handle: Optional[str] = None
        opened_once = False
        handoff = _Handoff()
        # Expose this kind's handoff to the build-scope watchdog so the
        # watchdog can early-return after save_agent fires (don't nudge a
        # session that's already wrapped up).
        if kind == "builder":
            build_handoff_for_watchdog = handoff

        # ── Server-side build-state monitor (builder only) ─────────────
        # Deterministic phase machine that observes every turn,
        # tracks slots from the build_session row, and decides when
        # the server must force-fire save_agent (because the model
        # delivered the primer without committing, or has stalled
        # past the wrap deadline). See backend/build_state.py for
        # the full spec + enforcement rationale.
        from . import build_state as _bs
        import time as _time_mod
        build_monitor = _bs.BuildMonitor(
            sid=build_sid,
            started_at_monotonic=_time_mod.monotonic(),
        )
        # Expose to the build-scope wrap-up watchdog so it can defer
        # the 90s wrap-nudge when a template interview is mid-flight
        # (the deterministic flow needs ~120s for a typical 6-9 question
        # interview; firing at 90s cuts the operator off mid-answer).
        build_monitor_for_watchdog[0] = build_monitor

        # ── Structured-question card emitter ──
        # Closure over (ws, build_monitor, build_sid) so EVERY call site
        # that moves the interview forward — the select_/record_/_auto_
        # template paths — can push the next question to the client as
        # a structured event. Client renders it as a card with progress
        # meter + clickable chips (enum options / agent_name suggestions).
        # When the interview completes, emits `template_complete` so the
        # card clears for the wrap-up beat. Idempotent — re-emitting the
        # same question id is harmless.
        async def _emit_template_question_card() -> None:
            if not build_monitor.template_id:
                return
            try:
                from . import build_templates as _bt
                template = _bt.get_template(build_monitor.template_id)
                if not template:
                    return
                next_q = _bt.next_unanswered_question(
                    template, build_monitor.template_answers,
                )
                answered, total = build_monitor.template_progress()
                if next_q is None:
                    await _send_json(ws, {
                        "type": "template_complete",
                        "template_id": build_monitor.template_id,
                        "progress": {"answered": total, "total": total},
                    })
                    return
                # Per-sid primary suggestion (e.g. for agent_name) —
                # same shuffle the BUILD STATE block uses so the card
                # and Eva's verbal proposal agree on the name.
                primary = None
                sugs = next_q.get("suggestions") or []
                if sugs:
                    from .build_state import _pick_suggestion
                    primary, _alts = _pick_suggestion(
                        list(sugs), build_monitor.sid,
                    )
                await _send_json(ws, {
                    "type": "template_question",
                    "template_id": build_monitor.template_id,
                    "question": {
                        "id": next_q["id"],
                        "prompt": next_q["prompt"],
                        "type": next_q["type"],
                        "required": bool(next_q.get("required")),
                        "hint": next_q.get("hint"),
                        # enum options (chips) — None for free-text.
                        "options": next_q.get("options"),
                        # name suggestions for agent_name question.
                        "suggestions": sugs or None,
                        "primary_suggestion": primary,
                        # 1-based for display ("Question 3 of 9").
                        "progress": {
                            "answered": answered,
                            "total": total,
                            "number": answered + 1,
                        },
                    },
                })
            except Exception as e:  # noqa: BLE001
                log.warning("emit template_question failed: %s", e)

        # ── Skip-chip handler ──
        # Operator clicked Skip on an optional template question. We
        # bypass Eva entirely: record null for the question via the
        # same DB path the tool handler uses, mirror it into the
        # in-memory monitor, fire the next card, AND inject a system
        # notice so Eva (on her next turn) knows the question was
        # skipped server-side and doesn't re-ask. Bypassing Eva keeps
        # the chip-click deterministic — the alternative (sending
        # "skip" as plain text) leaves the LLM to interpret, which
        # has been a source of build drift.
        async def _handle_template_skip(session, data: dict[str, Any]) -> None:
            if kind != "builder":
                return
            if not build_monitor.template_id:
                return
            qid = (data.get("question_id") or "").strip()
            if not qid:
                return
            from . import build_templates as _bt
            template = _bt.get_template(build_monitor.template_id)
            if not template:
                return
            q = _bt.question_by_id(template, qid)
            if not q:
                log.warning("template_skip: unknown question_id %r for %s", qid, build_monitor.template_id)
                return
            # Hard-guard: required questions can't be skipped via this
            # path. (UI shouldn't show the Skip chip for them, but
            # belt-and-suspenders against a crafted client.)
            if q.get("required"):
                log.warning("template_skip: refusing to skip required question %r", qid)
                return
            # Persist + reflect in monitor. None is a valid "answered"
            # state — next_unanswered_question treats key-presence as
            # answered, regardless of value.
            try:
                await db.record_template_answer(
                    user_id=user_id, sid=build_sid,
                    question_id=qid, value=None,
                )
            except Exception as e:  # noqa: BLE001
                log.warning("template_skip DB write failed: %s", e)
            build_monitor.template_answers[qid] = None
            log.info(
                "build_state[%s]: SKIPPED q=%s (%d/%d)",
                build_sid[:18], qid,
                len(build_monitor.template_answers),
                len(template.get("questions") or []),
            )
            # Push the next card to the client.
            try:
                await _emit_template_question_card()
            except Exception as e:  # noqa: BLE001
                log.warning("emit template_question (skip) failed: %s", e)
            # Tell Eva the operator skipped, so she moves on instead
            # of re-asking. System notice on Gemini side — Eva sees
            # this on her next turn.
            try:
                await session.send_client_content(
                    turns=types.Content(
                        role="user",
                        parts=[types.Part(text=(
                            f"[SYSTEM NOTICE: Operator skipped the optional question "
                            f"'{qid}'. Do NOT re-ask it. Move directly to the next "
                            f"question in the BUILD STATE block.]"
                        ))],
                    ),
                    turn_complete=False,
                )
            except Exception as e:  # noqa: BLE001
                log.warning("template_skip system-notice send failed: %s", e)

        # ── Eavesdropping interceptor (builder only) ───────────────────
        # Fires on every Gemini turn_complete. Synchronously snapshots
        # the latest turns and updates a cursor; schedules transcript
        # persistence + an LLM-based extraction pass as a fire-and-
        # forget asyncio.create_task so the audio pump is NEVER blocked.
        # The cursor lives at this outer scope so it survives Gemini-
        # session reconnects — we don't want to re-persist the entire
        # log every time the cascade model drops a stream.
        transcript_cursor = [0]   # mutable cell, captured by closure

        def _builder_turn_hook(mem: "_ConversationMemory"):
            # Snapshot + advance cursor synchronously to avoid races if
            # two turn_completes ever fire in rapid succession.
            try:
                new_turns = list(mem.turns[transcript_cursor[0]:])
            except Exception:  # noqa: BLE001
                return None
            if not new_turns:
                return None
            transcript_cursor[0] = len(mem.turns)
            # Window for the extractor — last 12 turns is enough context
            # to disambiguate "you mean the dental clinic?" without
            # blowing token budget.
            extractor_window = list(mem.turns[-12:])

            # ── Feed the build-state monitor synchronously ───────────
            # We do this BEFORE scheduling the runner so the monitor's
            # phase + trigger state is available to anything that
            # consults `build_monitor` immediately after the hook
            # returns (e.g. the post-pump force-commit check below).
            # `save_agent_called_this_burst` is determined by checking
            # whether handoff.exit_after_save just flipped — see
            # _Handoff init in run_session.
            save_called_now = bool(handoff.exit_after_save)
            for t in new_turns:
                role = (t.get("role") or "").lower()
                text = (t.get("text") or "").strip()
                if not text:
                    continue
                if role == "user":
                    build_monitor.observe_user_turn(text)
                else:
                    build_monitor.observe_model_turn(text, save_agent_called=save_called_now)
                    # Only credit save_agent to the FIRST model turn
                    # in this burst — multiple turns coming through
                    # one hook call (rare but possible on reconnect-
                    # replay) shouldn't all claim "saved".
                    save_called_now = False
            try:
                build_monitor.recompute_phase()
            except Exception as e:  # noqa: BLE001
                log.warning("build_monitor.recompute_phase failed: %s", e)

            async def _runner() -> None:
                # Persist every new turn to build_sessions.transcript_log
                # first (cheap, no LLM). Even if the extractor times out
                # below, the raw transcript is durable for a WS-level
                # reconnect-replay.
                for t in new_turns:
                    try:
                        await db.append_transcript_turn(
                            user_id=user_id, sid=build_sid,
                            role=t.get("role") or "user",
                            text=t.get("text") or "",
                        )
                    except Exception as e:  # noqa: BLE001
                        log.warning("transcript append failed: %s", e)
                # Eavesdropping extractor: pulls structured slots out of
                # what the operator actually said and merges them into
                # build_sessions. Idempotent — re-emits dropped silently.
                try:
                    from . import extractor as _ex
                    await _ex.run_extraction_pass(
                        user_id=user_id, sid=build_sid,
                        transcript_turns=extractor_window,
                    )
                except Exception as e:  # noqa: BLE001
                    log.warning("extractor pass failed: %s", e)
                # After extractor + transcript persistence, refresh the
                # monitor's slot view from the durable DB row. This
                # lets the phase machine see slots the extractor just
                # captured even though the model hasn't acknowledged.
                try:
                    fresh = await db.get_build_session(user_id=user_id, sid=build_sid)
                    build_monitor.update_slots(fresh)
                    build_monitor.recompute_phase()
                except Exception as e:  # noqa: BLE001
                    log.warning("monitor.update_slots after persist failed: %s", e)
                    fresh = None
                # ── Template auto-promotion + extractor sync ──
                # The extractor just (maybe) captured industry / city /
                # locale signals. If Eva hasn't called
                # select_build_template yet, do it on her behalf so the
                # deterministic interview kicks in. And whatever slots
                # the extractor resolved that match template questions
                # get synced into template_answers — Eva won't re-ask
                # them. This is the safety net for "Eva went off-script
                # and never fired the tool" — the design promise of
                # graceful deterministic-mode degradation.
                if fresh and kind == "builder":
                    try:
                        await _auto_template_from_extractor(
                            user_id=user_id, sid=build_sid,
                            build_row=fresh, build_monitor=build_monitor,
                        )
                    except Exception as e:  # noqa: BLE001
                        log.warning("auto_template_from_extractor failed: %s", e)
                    # If the extractor just locked a template or synced
                    # any answers, the structured card on the client is
                    # stale — push the latest. Idempotent if nothing
                    # actually changed (client treats it as a no-op
                    # refresh of the existing question).
                    try:
                        await _emit_template_question_card()
                    except Exception as e:  # noqa: BLE001
                        log.warning("emit template_question (extractor) failed: %s", e)
                # Enforcement decision: should the server force-fire
                # save_agent right now? Triggers in two cases:
                # (A) operator affirmed after offer + Eva had one
                # follow-up model turn without saving, (B) wall-clock
                # past the 120s watchdog with minimum slots present.
                #
                # We don't just arm a flag and wait for the pumps to
                # exit (which would only happen on a natural Gemini
                # drop) — we COMMIT RIGHT HERE from the runner, then
                # close the Gemini session to break the receive pump's
                # iterator, then set handoff flags so the outer
                # reveal path fires. Result: the reveal pops within
                # ~1s of detection instead of waiting for a stream
                # drop that might be 30-60s away.
                try:
                    should, reason = build_monitor.should_force_save(
                        now_monotonic=_time_mod.monotonic(),
                    )
                except Exception as e:  # noqa: BLE001
                    log.warning("build_monitor.should_force_save raised: %s", e)
                    should, reason = False, ""

                if should and not build_monitor.force_commit_armed:
                    # CLAIM the work before awaiting so a second runner
                    # firing on a near-simultaneous turn_complete won't
                    # also try to commit.
                    build_monitor.force_commit_armed = True
                    log.warning(
                        "build_state[%s]: FORCE-COMMIT firing from runner — %s",
                        build_sid[:18], reason,
                    )
                    try:
                        saved = await _bs.force_commit_build_session(
                            user_id=user_id, sid=build_sid,
                        )
                        if saved:
                            # Mark on the monitor so subsequent runners
                            # see this turn as the save turn.
                            build_monitor.save_agent_fired_at_turn = build_monitor.model_turns
                            # Stash for the outer reveal path. handoff.
                            # exit_after_save = True will cause the post-
                            # gather block to emit agent_saved + build_
                            # complete to the client.
                            handoff.saved_agent = saved
                            handoff.exit_after_save = True
                            log.info(
                                "build_state[%s]: FORCE-COMMIT runner-side succeeded agent_id=%s",
                                build_sid[:18], saved.get("id"),
                            )
                            # Break the receive pump's iterator by
                            # closing the live Gemini session. The
                            # current_session_ref cell is populated by
                            # the inner connect block while pumps are
                            # running — we use it the same way the
                            # build watchdog does for the wrap-up
                            # nudge.
                            sess = current_session_ref[0]
                            if sess is not None:
                                try:
                                    await sess.close()
                                except Exception:  # noqa: BLE001
                                    pass
                        else:
                            log.error(
                                "build_state[%s]: FORCE-COMMIT runner returned None",
                                build_sid[:18],
                            )
                            # Un-arm so the outer-loop fallback can try
                            # again with on_save_agent path.
                            build_monitor.force_commit_armed = False
                    except Exception:  # noqa: BLE001
                        log.exception(
                            "build_state[%s]: FORCE-COMMIT runner-side raised",
                            build_sid[:18],
                        )
                        # Same un-arm so the outer fallback path can
                        # still try.
                        build_monitor.force_commit_armed = False

            return _runner()

        candidates: list[str] = [DEFAULT_MODEL] + [m for m in FALLBACK_MODELS if m != DEFAULT_MODEL]
        reconnect_attempts = 0
        MAX_RECONNECTS = 20

        # Reconnect preamble — when a Live session reopens (after a drop)
        # the new Gemini session has NO memory of the prior turns and treats
        # its system_instruction as "first turn → greet". The user-role
        # banner we send in the kickoff isn't strong enough to override
        # that. So we MODIFY the system prompt itself for reconnects: prepend
        # an absolute "you have already greeted, do not re-greet" block.
        # This is read FIRST by Gemini and overrides every "speak first"
        # rule lower down in the per-agent / per-Eva prompt body.
        RECONNECT_PREAMBLE = (
            "=========================================================\n"
            "CRITICAL: RECONNECT SESSION — READ THIS FIRST.\n"
            "---------------------------------------------------------\n"
            "This Live session is a RECONNECT after a brief edge drop.\n"
            "You have ALREADY been talking to this user in a prior\n"
            "Live session. The user has ALREADY heard your greeting.\n"
            "The conversation is MID-FLOW.\n"
            "\n"
            "ABSOLUTE RULES (violating these breaks user trust):\n"
            "  1. NEVER greet. NEVER re-introduce yourself.\n"
            "  2. NEVER say: 'Hi', 'Hello', 'Hi there', 'I'm Eva',\n"
            "     'Eva here', 'lovely to meet you', 'good to meet you',\n"
            "     'how's your day going', 'tell me what shall we build\n"
            "     today', 'fresh build today or pick up with Maya', or\n"
            "     ANY other opener of any kind.\n"
            "  3. If you have any urge to greet — DO NOT. Skip directly\n"
            "     to the substantive content.\n"
            "  4. When the user speaks next, respond to its CONTENT in\n"
            "     ONE short sentence (e.g. 'Got it — ', 'Right — ', 'Yes,\n"
            "     ') and continue building or call save_agent.\n"
            "  5. Treat `<call_start>` if you see it as a STALE marker —\n"
            "     ignore it. You are NOT starting.\n"
            "\n"
            "Any rule below this banner that conflicts with these rules\n"
            "is overridden. The no-greet rules ALWAYS win.\n"
            "=========================================================\n\n"
        )
        # ─ end preamble ─

        while reconnect_attempts <= MAX_RECONNECTS and handoff.next is None:
            models_to_try = [usable_model] if usable_model else candidates
            last_err: Exception | None = None

            # Read the durable build_session row at the top of every
            # Gemini-session open (initial + reconnects). On a fresh
            # build this returns None; once Eva has called
            # note_build_facts even once, every subsequent open re-injects
            # those facts as a "FACTS ALREADY COLLECTED" block at the
            # very top of the system instruction — so a context-window
            # blip is no longer a memory blip.
            facts_block = ""
            state_block = ""
            if kind == "builder":
                try:
                    facts_row = await db.get_build_session(user_id=user_id, sid=build_sid)
                    facts_block = _format_build_facts_block(facts_row)
                    # Refresh the monitor's view of slots from the
                    # durable row, then render the state block. This is
                    # the SERVER-AUTHORITATIVE phase view Eva will see
                    # at the top of her system instruction — it tells
                    # her where she is in the flow and what's allowed/
                    # required this turn.
                    build_monitor.update_slots(facts_row)
                    build_monitor.recompute_phase()
                    state_block = build_monitor.render_state_block()
                    if facts_block:
                        log.info(
                            "builder: injecting facts block sid=%s sector=%s biz=%s job=%s name=%s phase=%s",
                            build_sid[:18],
                            (facts_row or {}).get("sector_kind"),
                            (facts_row or {}).get("business_name"),
                            (facts_row or {}).get("primary_job"),
                            (facts_row or {}).get("agent_name"),
                            build_monitor.phase.value,
                        )
                    # WS-level recovery: if the in-process _ConversationMemory
                    # is empty but the DB has transcript_log entries, the
                    # client must have reopened the WS with the same sid
                    # (laptop sleep, wifi flap, page hide, deliberate
                    # reload). Hydrate memory.turns from the durable log
                    # so the existing reconnect-replay path (which uses
                    # memory.transcript_recap) keeps working. The
                    # transcript_cursor also jumps forward so the
                    # eavesdropper won't re-persist what's already there.
                    if facts_row and not memory.turns:
                        tlog = facts_row.get("transcript_log") or []
                        if isinstance(tlog, list) and tlog:
                            hydrated = 0
                            for t in tlog:
                                if not isinstance(t, dict):
                                    continue
                                role = (t.get("role") or "").lower()
                                text = (t.get("text") or "").strip()
                                if role in ("user", "model") and text:
                                    memory.turns.append({"role": role, "text": text})
                                    hydrated += 1
                            if hydrated:
                                transcript_cursor[0] = len(memory.turns)
                                log.info(
                                    "builder: WS-level recovery — hydrated %d turn(s) from transcript_log (sid=%s)",
                                    hydrated, build_sid[:18],
                                )
                except Exception as e:  # noqa: BLE001
                    # Never let a DB hiccup prevent a session from opening.
                    log.warning("builder: build_session read failed (continuing without facts): %s", e)

            for model_name in models_to_try:
                # Native-audio models infer dialect from the prompt and reject
                # `language_code` / `enable_affective_dialog` / `proactivity`;
                # the cascade Live model accepts them.
                is_native = "native-audio" in model_name
                # On reconnects we surgically prepend the no-greet preamble
                # to the system prompt. This is the only knob that reliably
                # stops the model from re-introducing itself; the user-role
                # banner alone gets ignored by the cascade model.
                # Order (top → bottom):
                #   1. STATE BLOCK   — phase + slots + next action +
                #                      save_agent allowed/required/blocked.
                #                      Authoritative. Replaces ambiguity.
                #   2. FACTS BLOCK   — ground-truth slot values.
                #   3. RECONNECT     — no-greet preamble (reconnects only).
                #   4. SYSTEM PROMPT — the 30KB prose body.
                # Putting the state + facts first means Eva reads
                # "you are in phase X, slot Y is needed next, save_agent
                # is allowed/required/blocked" BEFORE she reads the
                # generic 4-turn flow narrative — making the prompt
                # behave more like a deterministic spec than a vibe.
                effective_prompt = (RECONNECT_PREAMBLE + system_prompt) if opened_once else system_prompt
                if facts_block:
                    effective_prompt = facts_block + effective_prompt
                if state_block:
                    effective_prompt = state_block + effective_prompt
                config = _live_config(
                    voice=voice, locale=locale, system_prompt=effective_prompt,
                    tools=tools, resume_handle=resume_handle,
                    with_language_code=not is_native,
                    is_native_audio=is_native,
                    tweaks=session_tweaks,
                    text_only=text_only,
                )
                try:
                    async with client.aio.live.connect(model=model_name, config=config) as session:
                        usable_model = model_name
                        is_reconnect = opened_once
                        opened_once = True
                        log.info(
                            "Live session %s on %s (kind=%s, handle=%s)",
                            "RECONNECTED" if is_reconnect else "opened",
                            model_name, kind, (resume_handle or "fresh")[:12] + ("…" if resume_handle else ""),
                        )
                        if not is_reconnect:
                            await _send_json(ws, {"type": "ready", "model": model_name, "kind": kind})
                        else:
                            await _send_json(ws, {"type": "reconnected"})
                        # Re-emit the current template question on every
                        # session (re)open. Covers WS reconnect, template
                        # locked via select_build_template's path on the
                        # previous Gemini session, etc. — client always
                        # sees an up-to-date card.
                        try:
                            await _emit_template_question_card()
                        except Exception as e:  # noqa: BLE001
                            log.warning("emit template_question (open) failed: %s", e)

                        async def on_save_agent(args: dict[str, Any]) -> dict[str, Any]:
                            # ── DUPLICATE-SAVE GUARD ──
                            # Two paths can race us into a second
                            # db.create_agent for the same build_session:
                            #
                            #  (P1) The runner-side force-commit fired
                            #       on a previous turn (when Eva
                            #       skipped save_agent after the
                            #       primer), and now Eva — finally
                            #       waking up — also calls save_agent
                            #       on a later turn.
                            #  (P3) Eva emits PARALLEL completion
                            #       streams that each include a
                            #       save_agent tool_call in the SAME
                            #       turn. The pump's `for fc in
                            #       response.tool_call.function_calls`
                            #       loop dispatches them sequentially.
                            #
                            # Either way: if handoff.exit_after_save
                            # already True OR handoff.saved_agent
                            # already set, we've committed. Return the
                            # already-saved agent's id/name so the
                            # model sees a clean success and stops
                            # retrying. NEVER touch the DB twice for
                            # the same build_session.
                            if handoff.exit_after_save and handoff.saved_agent is not None:
                                _sa = handoff.saved_agent
                                log.warning(
                                    "on_save_agent: REJECTED duplicate call — already committed agent_id=%s name=%s",
                                    _sa.get("id"), _sa.get("name"),
                                )
                                return {
                                    "ok": True,
                                    "agent_id": _sa.get("id"),
                                    "name": _sa.get("name"),
                                    "note": "already committed by the server's enforcement path; this call was deduplicated",
                                }
                            log.info("on_save_agent: creating agent in db")

                            # ── TEMPLATE-DRIVEN COMPOSITION ──
                            # If a deterministic interview template was
                            # matched during this build, the server (not
                            # Eva) owns the final agent payload. The
                            # template's `agent_profile` block is rendered
                            # with the operator's recorded answers, and
                            # that rendered dict REPLACES Eva's free-text
                            # save_agent args. Anything the template
                            # doesn't set falls through to the regular
                            # extras-backfill + silent_defaults pipeline
                            # below.
                            #
                            # This is the whole point of the template
                            # system: Eva's a probabilistic LLM, but the
                            # questions she asked + the slot values she
                            # captured are deterministic. The save call
                            # should reflect THAT determinism, not Eva's
                            # post-hoc summary of what she heard.
                            if build_monitor.template_id:
                                try:
                                    from . import build_templates as _bt
                                    _tpl = _bt.get_template(build_monitor.template_id)
                                    if _tpl is not None:
                                        composed = _bt.compose_save_args(
                                            _tpl,
                                            build_monitor.template_answers or {},
                                        )
                                        # Operator might have explicitly
                                        # corrected a slot mid-call via
                                        # note_build_facts — those win over
                                        # the template's defaults, but
                                        # they're already merged into
                                        # template_answers by
                                        # on_record_template_answer, so
                                        # composed already reflects them.
                                        # We just replace args wholesale.
                                        log.info(
                                            "on_save_agent: composing from template %s (%d answers)",
                                            build_monitor.template_id,
                                            len(build_monitor.template_answers or {}),
                                        )
                                        args = composed
                                except Exception as e:  # noqa: BLE001
                                    log.warning(
                                        "on_save_agent: template compose failed for tid=%s — falling back to Eva's args: %s",
                                        build_monitor.template_id, e,
                                    )

                            # Belt-and-suspenders backfill from the durable
                            # build_sessions row. The dashboard goal is a
                            # 90%-prefilled agent at reveal time — whatever
                            # Eva put in args wins, but for every gap we
                            # pull from the build_session row (which holds
                            # facts captured by both note_build_facts AND
                            # the server-side eavesdropping extractor).
                            # NEVER invents — only copies values the user
                            # explicitly volunteered.
                            try:
                                br = await db.get_build_session(user_id=user_id, sid=build_sid)
                            except Exception as e:  # noqa: BLE001
                                log.warning("on_save_agent: build_session read failed: %s", e)
                                br = None
                            if br:
                                # Defensive: a pre-fix corrupted row may
                                # store extras as a JSONB array instead
                                # of an object. Treat anything not-a-dict
                                # as empty so save_agent doesn't crash.
                                extras = br.get("extras")
                                if not isinstance(extras, dict):
                                    extras = {}
                                # — Top-level scalars Eva owns —
                                # If Eva forgot to fill `name`, recover
                                # the agent_name she'd already committed.
                                if not (args.get("name") or "").strip() and br.get("agent_name"):
                                    log.info("on_save_agent: backfilling name from build_session")
                                    args["name"] = br["agent_name"]

                                # ── ENFORCEMENT: brand-name override ──
                                # The cascade Live model stylizes brands
                                # it hears ("smile and dental" →
                                # "Smyle N Dental"). The text-only
                                # extractor reads the operator's exact
                                # transcript with explicit "literal
                                # transcription required" rules, so its
                                # version is closer to ground truth.
                                # If Eva's args.variables.business_name
                                # differs from the build_session row's
                                # business_name, the build_session
                                # version wins — and we log so the
                                # mismatch is observable.
                                br_brand = (br.get("business_name") or "").strip()
                                eva_brand = ""
                                _vars_for_brand = args.get("variables") or {}
                                if isinstance(_vars_for_brand, dict):
                                    eva_brand = (_vars_for_brand.get("business_name") or "").strip()
                                if br_brand and eva_brand and br_brand.lower() != eva_brand.lower():
                                    log.warning(
                                        "on_save_agent: brand mismatch — Eva said %r, operator said %r; using operator's literal version",
                                        eva_brand, br_brand,
                                    )
                                    variables_override = dict(_vars_for_brand)
                                    variables_override["business_name"] = br_brand
                                    args["variables"] = variables_override
                                # Locale / voice / persona / greeting —
                                # only fill when Eva left them blank.
                                if not (args.get("locale") or "").strip() and extras.get("locale_hint"):
                                    log.info("on_save_agent: backfilling locale from extras")
                                    args["locale"] = extras["locale_hint"]
                                if not (args.get("persona") or "").strip() and extras.get("persona_hint"):
                                    log.info("on_save_agent: backfilling persona from extras")
                                    args["persona"] = extras["persona_hint"]
                                if not (args.get("greeting") or "").strip() and extras.get("greeting_hint"):
                                    log.info("on_save_agent: backfilling greeting from extras")
                                    args["greeting"] = extras["greeting_hint"]

                                # — Business profile (`variables`) —
                                # Merge mode: Eva's values win, gaps fill
                                # from extracted slots. No clobbers.
                                variables = dict(args.get("variables") or {})
                                _var_map = (
                                    ("business_name",       br.get("business_name")),
                                    ("country",             extras.get("country")),
                                    ("city",                extras.get("city")),
                                    ("address",             extras.get("address")),
                                    ("hours",               extras.get("hours")),
                                    ("services",            extras.get("services")),
                                    ("offers",              extras.get("offers")),
                                    ("email",               extras.get("email")),
                                    ("website",             extras.get("website")),
                                    ("phone",               extras.get("escalation_phone")),
                                    ("notification_phone",  extras.get("notification_phone")),
                                    ("languages",           extras.get("language")),
                                )
                                filled: list[str] = []
                                for key, val in _var_map:
                                    if not val:
                                        continue
                                    if not (variables.get(key) or "").strip():
                                        variables[key] = val
                                        filled.append(key)
                                if filled:
                                    log.info(
                                        "on_save_agent: backfilled variables.%s from build_session",
                                        "/".join(filled),
                                    )
                                args["variables"] = variables

                                # — Voice tweaks: ambience hint —
                                # voice_tweaks already gets sector defaults
                                # via silent_defaults below, but a user-said
                                # ambience overrides.
                                if extras.get("ambience_hint"):
                                    vt = dict(args.get("voice_tweaks") or {})
                                    if not (vt.get("ambience") or "").strip():
                                        vt["ambience"] = extras["ambience_hint"]
                                        args["voice_tweaks"] = vt
                                        log.info("on_save_agent: backfilled voice_tweaks.ambience from extras")

                                # — Additional jobs → purpose.answers —
                                # The extractor captures "callers also ask
                                # about parking" etc. as a list. Fold into
                                # the structured purpose.answers so the
                                # dashboard's Core purpose card surfaces
                                # them. Eva's own answers always win; we
                                # only ADD, never replace.
                                add_jobs = extras.get("additional_jobs") or []
                                if isinstance(add_jobs, list) and add_jobs:
                                    purpose = dict(args.get("purpose") or {})
                                    answers = list(purpose.get("answers") or [])
                                    seen = {
                                        (a.strip().lower() if isinstance(a, str) else "")
                                        for a in answers
                                    }
                                    appended: list[str] = []
                                    for j in add_jobs:
                                        if not isinstance(j, str):
                                            continue
                                        s = j.strip()
                                        if not s:
                                            continue
                                        if s.lower() in seen:
                                            continue
                                        answers.append(s[:80])
                                        seen.add(s.lower())
                                        appended.append(s)
                                        if len(answers) >= 8:
                                            break
                                    if appended:
                                        purpose["answers"] = answers
                                        args["purpose"] = purpose
                                        log.info(
                                            "on_save_agent: extended purpose.answers with extras.additional_jobs (%d added)",
                                            len(appended),
                                        )

                                # — Mentioned guardrails → policy.custom_* —
                                # The extractor captures free-text "always
                                # do X" / "never do Y" phrases. Split
                                # heuristically into custom_dos vs
                                # custom_donts based on the leading verb.
                                # Conservative bucketing: anything
                                # starting with a negation token → don't.
                                # Otherwise → do. (The Guardrails page
                                # surfaces both as free-text lists; the
                                # operator can re-bucket with one click.)
                                gr = extras.get("mentioned_guardrails") or []
                                if isinstance(gr, list) and gr:
                                    policy = dict(args.get("policy") or {})
                                    custom_dos_text = policy.get("custom_dos") or ""
                                    custom_donts_text = policy.get("custom_donts") or ""
                                    dos_lines = [
                                        l for l in custom_dos_text.split("\n") if l.strip()
                                    ]
                                    donts_lines = [
                                        l for l in custom_donts_text.split("\n") if l.strip()
                                    ]
                                    dos_seen = {l.strip().lower() for l in dos_lines}
                                    donts_seen = {l.strip().lower() for l in donts_lines}
                                    NEG_PREFIXES = (
                                        "no ", "never ", "don't ", "dont ",
                                        "do not ", "avoid ", "not ",
                                    )
                                    added_dos = added_donts = 0
                                    for raw in gr:
                                        if not isinstance(raw, str):
                                            continue
                                        s = raw.strip()
                                        if not s:
                                            continue
                                        lc = s.lower()
                                        is_negative = lc.startswith(NEG_PREFIXES)
                                        if is_negative:
                                            if lc in donts_seen:
                                                continue
                                            donts_lines.append(s[:200])
                                            donts_seen.add(lc)
                                            added_donts += 1
                                        else:
                                            if lc in dos_seen:
                                                continue
                                            dos_lines.append(s[:200])
                                            dos_seen.add(lc)
                                            added_dos += 1
                                        if added_dos + added_donts >= 12:
                                            break
                                    if added_dos or added_donts:
                                        policy["custom_dos"] = "\n".join(dos_lines)
                                        policy["custom_donts"] = "\n".join(donts_lines)
                                        args["policy"] = policy
                                        log.info(
                                            "on_save_agent: extended policy.custom_dos(+%d)/custom_donts(+%d) from mentioned_guardrails",
                                            added_dos, added_donts,
                                        )

                            # Tier 1 silent defaults — Eva already filled the
                            # core fields she asked about (name, sector,
                            # locale, voice, greeting, system_prompt,
                            # connectors); we now layer sector-appropriate
                            # VAD, prompt caching, outcome taxonomy and
                            # disconnect-safety policy on top so the saved
                            # agent ships analytics-ready and tuned to its
                            # sector without bothering the user.
                            from . import silent_defaults
                            args = silent_defaults.merge_into_save_args(args)
                            log.info(
                                "on_save_agent: applied silent defaults for sector=%s "
                                "(outcomes=%d, vad=%s)",
                                args.get("sector"), len(args.get("outcomes") or []),
                                args.get("voice_tweaks", {}).get("silence_duration_ms"),
                            )
                            # Pre-fill Additional Info from captured
                            # services/offers so the operator's
                            # Additional Info page isn't blank.
                            try:
                                from . import info_schemas as _info
                                _pre = _info.prefill_extra_info(args.get("sector"), args.get("variables") or {})
                                if _pre:
                                    args["extra_info"] = {**_pre, **(args.get("extra_info") or {})}
                            except Exception as e:  # noqa: BLE001
                                log.warning("prefill_extra_info failed: %s", e)
                            try:
                                # user_id comes from the WS query params (set by
                                # the SPA from localStorage); falls back to the
                                # seeded founder if missing so dev runs still work.
                                owner_id = user_id if user_id is not None else (await db.get_founder())["id"]
                                saved = await db.create_agent(args, user_id=owner_id)
                            except Exception as e:
                                log.exception("on_save_agent: db.create_agent failed")
                                raise
                            log.info("on_save_agent: saved id=%s name=%s", saved.get("id"), saved.get("name"))
                            try:
                                await db.seed_helper_memory(user_id=owner_id, agent_id=saved["id"], agent=saved)
                            except Exception as e:  # noqa: BLE001
                                log.warning("seed_helper_memory(voice) failed: %s", e)
                            # Mark the build_session committed and link it
                            # to the resulting agent. Best-effort — a
                            # failure here doesn't roll back the agent;
                            # the row just stays in_progress and gets
                            # swept by abandon_stale_build_sessions later.
                            try:
                                await db.mark_build_committed(
                                    user_id=user_id, sid=build_sid, agent_id=saved["id"],
                                )
                            except Exception as e:  # noqa: BLE001
                                log.warning("on_save_agent: mark_build_committed failed: %s", e)
                            # The agent is now in the DB. We DON'T send the
                            # `agent_saved` event yet — Eva is about to deliver
                            # a 10-15s dashboard primer in this same turn, and
                            # we want the reveal card to come AFTER that beat,
                            # not while she's still talking. The primer ends
                            # with turn_complete, the pumps exit, and the outer
                            # loop emits agent_saved + build_complete then.
                            handoff.exit_after_save = True
                            handoff.saved_agent = saved
                            log.info("on_save_agent: deferred agent_saved until primer turn ends")
                            return {"ok": True, "agent_id": saved["id"], "name": saved["name"]}

                        async def on_select_agent(args: dict[str, Any]) -> dict[str, Any]:
                            aid = int(args.get("agent_id"))
                            target = await db.get_agent(aid)
                            if not target:
                                return {"ok": False, "error": f"agent {aid} not found"}
                            handoff.next = ("test", aid)
                            await _send_json(ws, {"type": "agent_loaded", "agent": target})
                            return {"ok": True, "agent_id": aid, "name": target["name"]}

                        async def on_note_build_facts(args: dict[str, Any]) -> dict[str, Any]:
                            """Durable slot-filling. Eva calls this the
                            moment ANY core fact lands. We merge it into
                            the build_sessions row keyed by (user_id,
                            sid) so the next (re)connect can re-inject
                            the facts into Eva's system prompt — making
                            it structurally impossible for her to re-ask
                            a settled fact. Best-effort: a DB failure
                            here must NOT break the conversation.

                            The four typed-column facts go into their
                            columns; everything else gets routed into
                            the extras JSONB so the server-side extractor
                            and Eva write to the same shared shape."""
                            # Split the flat args into (typed, extras).
                            # Anything not in the four-tuple is a "soft"
                            # slot that lives in extras. The db layer
                            # filters extras against a known-keys list so
                            # unknown args drop silently.
                            typed_keys = {"sector_kind", "business_name", "primary_job", "agent_name"}
                            extras: dict[str, Any] = {}
                            for k, v in (args or {}).items():
                                if k in typed_keys:
                                    continue
                                if v is None or v == "":
                                    continue
                                extras[k] = v
                            try:
                                row = await db.merge_build_facts(
                                    user_id=user_id,
                                    sid=build_sid,
                                    sector_kind=args.get("sector_kind"),
                                    business_name=args.get("business_name"),
                                    primary_job=args.get("primary_job"),
                                    agent_name=args.get("agent_name"),
                                    extras=extras or None,
                                )
                                log.info(
                                    "note_build_facts: sid=%s sector=%s biz=%s job=%s name=%s extras=%s",
                                    build_sid[:18],
                                    row.get("sector_kind"), row.get("business_name"),
                                    row.get("primary_job"), row.get("agent_name"),
                                    list(extras.keys()),
                                )
                                # Same safety net as the extractor path:
                                # if note_build_facts just landed enough
                                # signal to resolve a template, lock it;
                                # and sync any facts that match template
                                # questions into template_answers so Eva
                                # doesn't re-ask them.
                                try:
                                    await _auto_template_from_extractor(
                                        user_id=user_id, sid=build_sid,
                                        build_row=row, build_monitor=build_monitor,
                                    )
                                except Exception as e:  # noqa: BLE001
                                    log.warning(
                                        "auto_template after note_build_facts failed: %s", e,
                                    )
                                try:
                                    await _emit_template_question_card()
                                except Exception as e:  # noqa: BLE001
                                    log.warning(
                                        "emit template_question (note_build_facts) failed: %s", e,
                                    )
                                return {
                                    "ok": True,
                                    "facts": {
                                        k: row.get(k) for k in
                                        ("sector_kind", "business_name", "primary_job", "agent_name")
                                    },
                                    "extras_keys": sorted((row.get("extras") or {}).keys()),
                                }
                            except Exception as e:  # noqa: BLE001
                                log.warning("note_build_facts failed: %s", e)
                                # Tell the model the fact was accepted in
                                # principle even if the DB hiccupped — this
                                # turn's content is still in Gemini's
                                # context window, so we don't want Eva to
                                # treat the failure as "forget that fact".
                                return {"ok": True, "warning": "persistence-deferred"}

                        async def on_select_build_template(args: dict[str, Any]) -> dict[str, Any]:
                            """Resolve the operator's triage facets to a
                            YAML template + stamp build_sessions.template_id.
                            From this point the state-block carries the
                            template's next question and Eva runs
                            deterministically. If no template matches,
                            returns {found:false} and Eva falls back to her
                            probabilistic flow."""
                            from . import build_templates as _bt
                            # ── REPEAT-CALL GUARD ──
                            # Once a non-generic template is locked, refuse
                            # further select_build_template calls. The
                            # model has a documented failure mode where it
                            # interprets template question answers like
                            # "hair", "pet grooming", "new" as fresh
                            # triage signals (because they overlap with
                            # industry keywords) and re-triages instead
                            # of calling record_template_answer. The
                            # symptom in prod: card stuck on Question N,
                            # operator clicks chips that go nowhere. We
                            # block the redundant tool call here and
                            # return a directive nudging the model back
                            # onto the right track.
                            if build_monitor.template_id and build_monitor.template_id != "_generic":
                                locked = build_monitor.template_id
                                # What's the next pending question? Surface
                                # it in the error so Eva can call
                                # record_template_answer with that id.
                                tpl = _bt.get_template(locked)
                                pending = _bt.next_unanswered_question(
                                    tpl, build_monitor.template_answers,
                                ) if tpl else None
                                log.warning(
                                    "select_build_template REFUSED: template already locked %s — model tried to re-triage with args=%s",
                                    locked, args,
                                )
                                return {
                                    "ok": False,
                                    "error": (
                                        f"Template {locked!r} is already locked for this build. "
                                        f"DO NOT call select_build_template again. The operator's "
                                        f"last message is the answer to the current NEXT QUESTION — "
                                        f"call record_template_answer instead."
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
                                log.info(
                                    "build_state[%s]: no template matched for industry=%s sub=%s locale=%s city=%s — falling back to probabilistic flow",
                                    build_sid[:18],
                                    args.get("industry"), args.get("sub_industry"),
                                    args.get("locale"), args.get("city"),
                                )
                                return {"ok": True, "found": False}
                            tid = template["id"]
                            try:
                                await db.set_build_template(
                                    user_id=user_id, sid=build_sid, template_id=tid,
                                )
                            except Exception as e:  # noqa: BLE001
                                log.warning("set_build_template DB write failed: %s", e)
                                # Continue anyway — in-memory monitor still
                                # tracks the template_id so the build can
                                # proceed; persistence is best-effort.
                            build_monitor.template_id = tid
                            log.warning(
                                "build_state[%s]: TEMPLATE LOCKED %s (%d questions)",
                                build_sid[:18], tid,
                                len(template.get("questions") or []),
                            )
                            # Return enough context for Eva's next-turn
                            # prompt: template id + the first question to
                            # ask. (The state-block also carries this on
                            # the next system-instruction inject, but
                            # returning here means the model has it
                            # immediately on the tool response.)
                            first_q = _bt.next_unanswered_question(template, build_monitor.template_answers)
                            # Same per-sid shuffle as the BUILD STATE
                            # block: if the first question carries
                            # suggestions, surface the seeded primary
                            # as `propose_name` so the model picks
                            # consistently across both code paths.
                            propose_name = None
                            if first_q and first_q.get("suggestions"):
                                from .build_state import _pick_suggestion
                                propose_name, _alts = _pick_suggestion(
                                    list(first_q["suggestions"]), build_sid,
                                )
                            # Surface the first question as a structured
                            # card on the client (chat view renders it
                            # with chips for enum / suggestions, plus
                            # progress meter). Best-effort — client
                            # rendering is gated on the event anyway.
                            try:
                                await _emit_template_question_card()
                            except Exception as e:  # noqa: BLE001
                                log.warning("emit template_question (select) failed: %s", e)
                            return {
                                "ok": True,
                                "found": True,
                                "template_id": tid,
                                "intro": template.get("intro") or "",
                                "total_questions": len(template.get("questions") or []),
                                "next_question": {
                                    "id": first_q["id"],
                                    "prompt": first_q["prompt"],
                                    "type": first_q["type"],
                                    "options": first_q.get("options"),
                                    "required": bool(first_q.get("required")),
                                    "propose_name": propose_name,
                                } if first_q else None,
                            }

                        async def on_record_template_answer(args: dict[str, Any]) -> dict[str, Any]:
                            """Validate + persist one answer, then return
                            the next unanswered question (or {done:true}
                            when the interview is complete). The state-block
                            also refreshes on the next system-instruction
                            inject; returning the next question in the
                            tool response means Eva sees it WITHIN the
                            same turn without waiting for a reconnect."""
                            from . import build_templates as _bt
                            tid = build_monitor.template_id
                            if not tid:
                                return {"ok": False, "error": "no template locked for this session — call select_build_template first"}
                            template = _bt.get_template(tid)
                            if not template:
                                return {"ok": False, "error": f"template {tid!r} no longer in registry"}
                            qid = (args.get("question_id") or "").strip()
                            q = _bt.question_by_id(template, qid)
                            if not q:
                                return {
                                    "ok": False,
                                    "error": f"unknown question_id {qid!r} for template {tid!r}",
                                }
                            value, err = _bt.validate_answer(q, args.get("value"))
                            if err:
                                # Surface the validation failure on the client's
                                # current question card — without this the card
                                # stays unchanged and the operator can't tell
                                # their click registered (Eva will re-ask via
                                # chat, but that's a separate surface).
                                try:
                                    await _send_json(ws, {
                                        "type": "template_question_error",
                                        "question_id": qid,
                                        "error": err,
                                        "retry_prompt": q.get("prompt"),
                                    })
                                except Exception as e:  # noqa: BLE001
                                    log.warning("emit template_question_error failed: %s", e)
                                return {
                                    "ok": False,
                                    "retry_prompt": q.get("prompt"),
                                    "error": err,
                                    "options": q.get("options"),
                                    "hint": q.get("hint"),
                                }
                            # Persist + reflect in monitor.
                            try:
                                await db.record_template_answer(
                                    user_id=user_id, sid=build_sid,
                                    question_id=qid, value=value,
                                )
                            except Exception as e:  # noqa: BLE001
                                log.warning("record_template_answer DB write failed: %s", e)
                            build_monitor.template_answers[qid] = value
                            log.info(
                                "build_state[%s]: answer recorded q=%s value=%r (%d/%d)",
                                build_sid[:18], qid, value,
                                len(build_monitor.template_answers),
                                len(template.get("questions") or []),
                            )
                            # Find the next question.
                            next_q = _bt.next_unanswered_question(template, build_monitor.template_answers)
                            # Push the structured card update to the client
                            # — either the next question or the
                            # template_complete sentinel (which clears the
                            # card so the wrap-up beat owns the screen).
                            try:
                                await _emit_template_question_card()
                            except Exception as e:  # noqa: BLE001
                                log.warning("emit template_question (record) failed: %s", e)
                            if next_q is None:
                                return {
                                    "ok": True,
                                    "done": True,
                                    "answered": len(build_monitor.template_answers),
                                    "total": len(template.get("questions") or []),
                                    "next_action": "All template questions answered. Make a one-line wrap-up offer; on yes, fire save_agent.",
                                }
                            return {
                                "ok": True,
                                "done": False,
                                "answered": len(build_monitor.template_answers),
                                "total": len(template.get("questions") or []),
                                "next_question": {
                                    "id": next_q["id"],
                                    "prompt": next_q["prompt"],
                                    "type": next_q["type"],
                                    "options": next_q.get("options"),
                                    "required": bool(next_q.get("required")),
                                    "hint": next_q.get("hint"),
                                },
                            }

                        # Mutable cell so the inner callback + the WS-close
                        # finalization (further down) share state. True after
                        # end_call has successfully landed a calls row. If
                        # it stays False at session-end we auto-persist an
                        # "abandoned" row so the operator's test still shows
                        # up in the Call log. Pre-build-190 a user who closed
                        # the call tab before the model could wrap up got
                        # nothing in the log — confusing for "I just tested
                        # but nothing's showing".
                        call_finalized = [False]

                        async def on_connector_call(name: str, args: dict[str, Any]) -> dict[str, Any]:
                            # end_call is always allowed on agent sessions —
                            # it's the canonical way to wrap up. We don't
                            # require it to be in the agent's connectors list.
                            allowed = (
                                name in connector_ids
                                or kind == "builder"
                                or name == "end_call"
                            )
                            if name in CONNECTOR_DECLS and allowed:
                                label = CONNECTOR_DECLS[name].description[:80]
                                await _send_json(ws, {"type": "tool_call", "name": name, "label": label})
                                # Thread the in-session transcript onto the
                                # agent dict so connectors.end_call can land
                                # it on the calls row. Pre-188 calls.transcript
                                # was always NULL because the bridge had the
                                # turns in `memory.turns` but never handed
                                # them off. Use the structured JSON form
                                # (turns array) — db_pg insert_call serialises
                                # it; the dashboard's Details modal renders
                                # the chat-style bubbles directly off it.
                                if name == "end_call" and agent is not None:
                                    try:
                                        turns = [
                                            {"role": t.get("role"), "text": t.get("text")}
                                            for t in (memory.turns or [])
                                            if t.get("text")
                                        ]
                                        agent["_transcript"] = turns
                                    except Exception:  # noqa: BLE001
                                        pass
                                result = await handle_connector(name, args, agent or {})
                                # If end_call landed successfully, surface the
                                # call_id back to the client so the cockpit
                                # can refresh stats immediately.
                                if name == "end_call" and result.get("ok"):
                                    call_finalized[0] = True
                                    await _send_json(ws, {
                                        "type": "call_ended",
                                        "call_id": result.get("call_id"),
                                        "outcome": result.get("outcome"),
                                    })
                                return result
                            return {"ok": False, "error": f"connector {name} not enabled for this agent"}

                        # Kickoff strategy.
                        #
                        # The cascade Gemini Live model frequently terminates
                        # streams early in a session (`turns=1 in=15 out=0`)
                        # without issuing a resume handle, so we have to
                        # reconnect WITHOUT context. When that happens we must
                        # NOT have Eva re-greet — that's the #1 reason users
                        # report "I can't build, she just keeps saying hi."
                        #
                        # Strategy:
                        #   • initial open → <call_start> (Eva greets, takes lead)
                        #   • reconnect WITH resume_handle → no kickoff (Gemini restores)
                        #   • reconnect WITHOUT handle + WITH memory → replay
                        #     user history so Eva picks up the build
                        #   • reconnect WITHOUT handle + WITHOUT memory →
                        #     send a SILENT-LISTEN instruction so Eva does not
                        #     re-introduce herself. The buffered client audio
                        #     will reach Gemini through the new session and
                        #     Eva will respond to that, naturally.
                        # NO-GREET BANNER — included on every reconnect kickoff
                        # text. The cascade model otherwise reads its system
                        # prompt's "you greet first" rule on each new session
                        # opening and re-introduces herself ("Hi there. I'm
                        # Eva — lovely to meet you...") over and over. This
                        # banner is the strongest knife we have against that:
                        # explicit, repetitive, framed as MANDATORY.
                        NO_GREET = (
                            "MANDATORY: This is NOT the first turn. The greeting has already "
                            "happened in a previous session. You MUST NOT say 'Hi', 'Hello', "
                            "'I'm Eva', 'lovely to meet you', 'good to meet you', 'we can "
                            "build something new', or any other introduction phrase. ANY "
                            "greeting will break the user's experience. Skip directly to the "
                            "substantive content."
                        )
                        if not is_reconnect:
                            # The caller is on the line. Tell Gemini explicitly
                            # to say ONE opener — most Gemini Live models will
                            # produce TWO parallel completion streams on a
                            # bare `<call_start>` token (we've seen the
                            # "Hi, I'mHi, I'm Eva. What Eva." gibberish in
                            # transcripts). A directive kickoff that names the
                            # exact opener AND the anti-double-stream rule
                            # gives the model one path to take.
                            kickoff_text = (
                                "[The caller is on the line right now. Speak "
                                "your opener IMMEDIATELY. Produce ONE single "
                                "audio response — not two parallel streams. "
                                "Say exactly one of these, verbatim, then "
                                "stop and listen:\n"
                                "  · If no saved agents exist: \"Hi, I'm Eva. "
                                "What kind of agent shall we build today?\"\n"
                                "  · If saved agents exist: \"Hi, I'm Eva. "
                                "Building new today, or want to call <first "
                                "saved agent name>?\"\n"
                                "Pick ONE based on the SAVED AGENTS section "
                                "of your system instruction. Do not say both. "
                                "Do not generate two openers in parallel. "
                                "Do not add small talk. After the opener, "
                                "stop talking and wait for the caller.]"
                            )
                        elif resume_handle:
                            kickoff_text = None
                        elif memory.turns:
                            # Full-transcript replay — BOTH sides, so the new
                            # Gemini session sees not just what the user said
                            # but ALSO what it (the same agent role) had
                            # already said. This is the fix for the repetition
                            # pattern: without seeing its own prior turns, the
                            # model re-recommends the same car / re-asks the
                            # same question after every reconnect.
                            recap = memory.transcript_recap(max_turns=14)
                            kickoff_text = (
                                "[SYSTEM NOTICE: the previous Live session dropped briefly. "
                                "You and the user are MID-CONVERSATION. " + NO_GREET + "]\n\n"
                                "Here is the recent transcript. The lines labeled 'You:' are\n"
                                "things YOU already said — do NOT repeat them. The lines\n"
                                "labeled 'Caller:' are things the user said.\n\n"
                                f"{recap}\n\n"
                                "Critical rules for what comes next:\n"
                                "  • DO NOT repeat a recommendation you already made.\n"
                                "  • DO NOT re-ask a question the caller has already answered.\n"
                                "  • DO NOT summarise the conversation back at them.\n"
                                "  • DO NOT greet or re-introduce yourself.\n"
                                "  • Continue the conversation forward — say the NEXT thing\n"
                                "    that hasn't been said yet, or wait silently for the\n"
                                "    caller's next utterance. If you have enough to act,\n"
                                "    call the appropriate connector (save_agent / "
                                "calendar_book / end_call) immediately."
                            )
                            log.info("reconnect: replaying full transcript (%d turns)", len(memory.turns))
                        else:
                            # No handle, no memory. Tell Eva to stay silent —
                            # buffered client audio will arrive shortly.
                            kickoff_text = (
                                "[SYSTEM NOTICE: brief reconnect. Conversation is IN "
                                "PROGRESS. " + NO_GREET + " Stay completely silent right "
                                "now. The user is about to speak — when their utterance "
                                "arrives, respond directly to its content. Do NOT "
                                "acknowledge the drop. Do NOT say sorry. Do NOT greet.]"
                            )
                            log.info("reconnect: silent-listen instruction (no handle, no memory)")
                        if kickoff_text:
                            await session.send_client_content(
                                turns=types.Content(role="user", parts=[types.Part(text=kickoff_text)]),
                                turn_complete=True,
                            )

                        state = _SessionState()
                        state.resume_handle = resume_handle
                        # Wire the agent dict + active model into state so the
                        # receive-pump can stamp token totals back onto the
                        # agent for the end_call rollup.
                        state.agent_dict = agent if kind != "builder" else None
                        state.model_id = usable_model
                        # Build 206 — the recording writer is opened once at
                        # session setup (above), but `state` gets re-created
                        # on every reconnect inside this loop. Re-attach the
                        # writer (stored on the agent dict) so the audio
                        # pumps below see it after a reconnect.
                        if kind != "builder" and agent is not None:
                            state.recording_writer = agent.get("_recording_writer")
                        # Phase 7 — point the receive pump at the WS-scope
                        # ledger so tokens accumulate even in builder mode.
                        # Set the current kind so the flush path knows what
                        # row to write at exit.
                        state.llm_session = llm_session
                        llm_session["kind"] = ("agent" if kind != "builder" else "builder")
                        stop = asyncio.Event()

                        # Expose the live session to the build-scoped wrap-up
                        # watchdog (declared above, outside this inner loop) so
                        # it can inject the 90s nudge into whatever Gemini
                        # session is currently active — survives reconnects.
                        if kind == "builder":
                            current_session_ref[0] = session
                            current_state_ref[0] = state

                        await asyncio.gather(
                            _pump_client_to_gemini(
                                ws, session, stop, state, memory,
                                on_template_skip=_handle_template_skip,
                            ),
                            _pump_gemini_to_client(
                                ws, session, stop, state, memory,
                                handoff=handoff,
                                on_save_agent=on_save_agent,
                                on_select_agent=on_select_agent,
                                on_connector_call=on_connector_call,
                                on_note_build_facts=(
                                    on_note_build_facts if kind == "builder" else None
                                ),
                                on_turn_complete_hook=(
                                    _builder_turn_hook if kind == "builder" else None
                                ),
                                on_select_build_template=(
                                    on_select_build_template if kind == "builder" else None
                                ),
                                on_record_template_answer=(
                                    on_record_template_answer if kind == "builder" else None
                                ),
                            ),
                        )
                        if kind == "builder":
                            current_session_ref[0] = None
                            current_state_ref[0] = None

                        resume_handle = state.resume_handle or resume_handle
                        log.info(
                            "Live session ended — kind=%s turns=%s in=%s out=%s reason=%r handle=%s",
                            kind, state.turns, state.audio_in_chunks, state.audio_out_chunks,
                            state.exit_reason, "yes" if resume_handle else "no",
                        )

                        if state.client_closed:
                            # ── WS-CLOSE AUTO-COMMIT (builder only) ──
                            # The operator closed the call. If they
                            # invested enough info but Eva never fired
                            # save_agent, commit silently so the work
                            # isn't lost. The agent will appear in
                            # /agents on the next dashboard mount; the
                            # frontend's recovery banner will surface
                            # it explicitly (see /api/build-sessions
                            # /<sid>/state).
                            #
                            # We can't emit `agent_saved` here (WS is
                            # closed) — we're committing for the next
                            # session.
                            if (kind == "builder"
                                    and build_monitor.save_agent_fired_at_turn is None
                                    and (build_monitor.slots.get("agent_name")
                                         and build_monitor.slots.get("sector_kind"))):
                                log.warning(
                                    "build_state[%s]: WS closed mid-build with savable slots — committing silently (sector=%s, name=%s)",
                                    build_sid[:18],
                                    build_monitor.slots.get("sector_kind"),
                                    build_monitor.slots.get("agent_name"),
                                )
                                try:
                                    saved = await _bs.force_commit_build_session(
                                        user_id=user_id, sid=build_sid,
                                    )
                                    if saved:
                                        log.warning(
                                            "build_state[%s]: WS-close commit succeeded agent_id=%s",
                                            build_sid[:18], saved.get("id"),
                                        )
                                except Exception:  # noqa: BLE001
                                    log.exception(
                                        "build_state[%s]: WS-close commit raised",
                                        build_sid[:18],
                                    )

                            # ── WS-CLOSE AUTO-COMMIT (agent / test calls) ──
                            # If the caller closed the WS before the model
                            # called `end_call`, we still want the session
                            # to land in the Call log so the operator's
                            # test is visible. Pre-build-190 they got
                            # nothing — confusing UX. We persist an
                            # "abandoned" outcome with the partial transcript
                            # and whatever token counters we observed.
                            if (kind in ("agent", "test")
                                    and not call_finalized[0]
                                    and agent is not None
                                    and agent.get("id")):
                                try:
                                    import time as _t
                                    _now_iso_str = datetime.now(timezone.utc).isoformat(timespec="seconds")
                                    started_at_iso = agent.get("_call_started_iso") or _now_iso_str
                                    started_at_mono = agent.get("_call_started_at")
                                    duration_s = (
                                        (_t.monotonic() - float(started_at_mono))
                                        if started_at_mono else 0.0
                                    )
                                    # Capture the in-session transcript as
                                    # the same JSON-encoded shape end_call
                                    # uses (build 188), so the Details modal
                                    # renders chat bubbles identically.
                                    import json as _json
                                    tx_turns = [
                                        {"role": t.get("role"), "text": t.get("text")}
                                        for t in (memory.turns or [])
                                        if t.get("text")
                                    ]
                                    tx_blob = _json.dumps(tx_turns, ensure_ascii=False) if tx_turns else None
                                    record = {
                                        "agent_id": agent.get("id"),
                                        "started_at": started_at_iso,
                                        "ended_at": _now_iso_str,
                                        "duration_s": duration_s,
                                        "outcome": "abandoned",
                                        "reason": "ABANDONED",
                                        "summary": (
                                            "Caller disconnected before the agent could wrap up. "
                                            f"{len(tx_turns)} turn(s) captured."
                                            if tx_turns else
                                            "Caller disconnected before the agent could speak."
                                        ),
                                        "final_message": None,
                                        "extracted": {},
                                        "transcript": tx_blob,
                                        "input_tokens":  agent.get("_tokens_in"),
                                        "output_tokens": agent.get("_tokens_out"),
                                        "cached_tokens": agent.get("_tokens_cached"),
                                        "model_id":      agent.get("_model_id") or state.model_id,
                                        "sentiment":     None,
                                        "lead_quality":  None,
                                        "lead_signals":  None,
                                        "recording_started_at": agent.get("_recording_started_iso"),
                                    }
                                    # Build 206 — finalize the recording
                                    # writer for the abandoned-call path
                                    # too, so a caller who hung up still
                                    # leaves a row pointing at whatever
                                    # audio we DID capture (often just
                                    # the agent's greeting + a few
                                    # caller utterances).
                                    _writer = agent.get("_recording_writer")
                                    if _writer is not None:
                                        try:
                                            _meta = _writer.finalize(call_id=None)
                                            record["recording_path"]       = _meta.get("recording_path")
                                            record["recording_format"]     = _meta.get("recording_format")
                                            record["recording_size_bytes"] = _meta.get("recording_size_bytes")
                                            if _meta.get("recording_started_at"):
                                                record["recording_started_at"] = (
                                                    _meta["recording_started_at"].isoformat()
                                                )
                                        except Exception as e:  # noqa: BLE001
                                            log.warning(
                                                "ws-close: recording finalize failed: %s", e,
                                            )
                                    cid = await db.insert_call(record)
                                    # Build 206 — rename temp-token dir → call_id dir
                                    if _writer is not None and cid and record.get("recording_path"):
                                        try:
                                            from . import recordings as _rec
                                            _old_rel = record["recording_path"]
                                            _new_rel = _rec.relative_path_for(int(agent["id"]), int(cid))
                                            _old_abs = _rec.RECORDING_ROOT / _old_rel
                                            _new_abs = _rec.RECORDING_ROOT / _new_rel
                                            if _old_abs.exists() and not _new_abs.exists():
                                                _new_abs.parent.mkdir(parents=True, exist_ok=True)
                                                _old_abs.rename(_new_abs)
                                                await db.update_call_recording_path(int(cid), _new_rel)
                                        except Exception as e:  # noqa: BLE001
                                            log.warning(
                                                "ws-close: recording rename failed: %s", e,
                                            )
                                    log.info(
                                        "ws-close auto-commit: agent_id=%s call_id=%s turns=%d duration=%.1fs (kind=%s)",
                                        agent.get("id"), cid, len(tx_turns), duration_s, kind,
                                    )
                                    # Build 196: fire the same post-call
                                    # report path the connector-driven
                                    # end_call uses, so abandoned-test
                                    # calls also land in the devteam
                                    # inbox + owner inboxes (subject to
                                    # post_call.email). Best-effort —
                                    # never raises into the bridge loop.
                                    try:
                                        from . import connectors as _conn
                                        purpose = agent.get("purpose") or {}
                                        post_call = purpose.get("post_call") if isinstance(purpose, dict) else {}
                                        if not isinstance(post_call, dict):
                                            post_call = {}
                                        await _conn._fire_post_call_notifications(
                                            agent=agent, record=record, call_id=cid,
                                            post_call=post_call,
                                        )
                                    except Exception as e:  # noqa: BLE001
                                        log.warning(
                                            "ws-close auto-commit: report notify failed: %s", e,
                                        )
                                except Exception:  # noqa: BLE001
                                    log.exception(
                                        "ws-close auto-commit failed (agent_id=%s)",
                                        agent.get("id"),
                                    )
                            return

                        # ── ENFORCEMENT: server-side force save_agent ──
                        # If the build-state monitor armed force_commit
                        # during the turn (operator said yes after offer
                        # but Eva slipped into the primer without
                        # committing, OR wall-clock past the 120s
                        # watchdog with minimum slots) AND Eva still
                        # hasn't fired save_agent, the server takes
                        # over: compose the save_agent args from the
                        # build_session row, call on_save_agent, and
                        # fall through to the standard exit_after_save
                        # path (which emits agent_saved + build_complete
                        # to the client and reveals the agent).
                        #
                        # This is the load-bearing deterministic exit
                        # for the build flow. Even if the LLM refuses
                        # to commit, the server WILL ship the agent
                        # within ~5s of the operator's affirmative.
                        if (kind == "builder"
                                and not handoff.exit_after_save
                                and build_monitor.force_commit_armed
                                and build_monitor.save_agent_fired_at_turn is None):
                            log.warning(
                                "build_state[%s]: FORCE-COMMIT firing on the server's behalf (slots=%s)",
                                build_sid[:18],
                                sorted(k for k, v in build_monitor.slots.items() if v),
                            )
                            try:
                                # Compose minimal save_agent args from
                                # the durable slot row. on_save_agent's
                                # backfill path will fill the rest from
                                # extras + silent_defaults.
                                forced_args: dict[str, Any] = {
                                    "name": build_monitor.slots.get("agent_name") or "Agent",
                                    "sector": (build_monitor.slots.get("sector_kind") or "generic").lower().split()[0],
                                    # Locale + voice + system_prompt
                                    # are left blank — backfill fills
                                    # locale from locale_hint or region
                                    # default; silent_defaults fills
                                    # voice + a sector-tuned outcomes
                                    # list. system_prompt left empty
                                    # falls back to a sector template.
                                    "locale": "en-US",
                                    "voice": DEFAULT_VOICE,
                                    "system_prompt": (
                                        f"You are {build_monitor.slots.get('agent_name') or 'the receptionist'}, "
                                        f"the receptionist for "
                                        f"{build_monitor.slots.get('business_name') or 'the business'}. "
                                        f"Callers usually want to "
                                        f"{build_monitor.slots.get('primary_job') or 'ask questions'}. "
                                        "Speak warmly, acknowledge before acting, never invent prices, "
                                        "and close with 'anything else I can help with?'."
                                    ),
                                    "greeting": (
                                        build_monitor.slots.get("greeting_hint")
                                        or f"Hello, this is {build_monitor.slots.get('agent_name') or 'reception'} — how can I help?"
                                    ),
                                }
                                # Pre-fold what we can into variables so
                                # backfill doesn't have to lookup. The
                                # standard backfill chain inside
                                # on_save_agent still runs on top.
                                _vars_seed: dict[str, Any] = {}
                                if build_monitor.slots.get("business_name"):
                                    _vars_seed["business_name"] = build_monitor.slots["business_name"]
                                if _vars_seed:
                                    forced_args["variables"] = _vars_seed
                                _result = await on_save_agent(forced_args)
                                if isinstance(_result, dict) and _result.get("ok"):
                                    log.info(
                                        "build_state[%s]: FORCE-COMMIT succeeded agent_id=%s",
                                        build_sid[:18], _result.get("agent_id"),
                                    )
                                    # handoff.exit_after_save is now True
                                    # (on_save_agent sets it) → falls
                                    # through to the standard reveal path
                                    # below.
                                else:
                                    log.error(
                                        "build_state[%s]: FORCE-COMMIT returned non-ok: %r",
                                        build_sid[:18], _result,
                                    )
                            except Exception as e:  # noqa: BLE001
                                log.exception("FORCE-COMMIT raised — build will exit unsaved")
                                await _send_json(ws, {
                                    "type": "error",
                                    "message": "Build couldn't be saved automatically. Tap to retry.",
                                })
                                return

                        if handoff.exit_after_save:
                            # Builder is done. Eva has just finished her
                            # dashboard-primer beat — NOW we tell the client
                            # the agent is saved (triggers the reveal card)
                            # and send build_complete (closes the call view).
                            # Emitting agent_saved here instead of inside
                            # on_save_agent is what lets the primer audio
                            # play out before the visual reveal takes over.
                            if handoff.saved_agent is not None:
                                log.info("session: emitting deferred agent_saved + build_complete")
                                await _send_json(ws, {"type": "agent_saved", "agent": handoff.saved_agent})
                            else:
                                log.info("session ending after save_agent — client will show reveal card")
                            await _send_json(ws, {"type": "build_complete"})
                            return
                        if handoff.next is not None:
                            break
                        if state.gemini_dropped:
                            # Reconnect even if we never got a resumption handle —
                            # the user's call should not just die. A fresh session
                            # loses the conversation memory but keeps the line up.
                            reconnect_attempts += 1
                            if reconnect_attempts > MAX_RECONNECTS:
                                log.warning("max reconnects exceeded; ending session")
                                await _send_json(ws, {"type": "error", "message": "The line keeps dropping. Tap to redial."})
                                return
                            log.info(
                                "reconnecting to Gemini (attempt %s, %s)",
                                reconnect_attempts,
                                "with handle" if resume_handle else "FRESH (no handle yet)",
                            )
                            await asyncio.sleep(min(0.3 * reconnect_attempts, 2.0))
                            break  # outer while retries
                        # Pumps exited but neither flag set — odd. Bail.
                        log.warning("inner loop exited with no recovery flag set; ending")
                        return
                except Exception as e:  # noqa: BLE001
                    last_err = e
                    msg = str(e).lower()
                    if not opened_once and ("not found" in msg or "404" in msg or "unsupported" in msg or "permission" in msg):
                        log.warning("model %s unusable (%s); trying next", model_name, e)
                        continue
                    log.exception("Live session failed")
                    if not opened_once:
                        await _send_json(ws, {"type": "error", "message": str(e)})
                        return
                    # mid-session: treat as a recoverable drop
                    reconnect_attempts += 1
                    if reconnect_attempts > MAX_RECONNECTS or not resume_handle:
                        await _send_json(ws, {"type": "error", "message": "Connection lost. Please tap to call again."})
                        return
                    await asyncio.sleep(min(0.3 * reconnect_attempts, 2.0))
                    break

            else:
                # for-else: ran out of models without opening
                await _send_json(ws, {"type": "error", "message": f"No usable Live model. Last error: {last_err}"})
                return

            # End of one inner-while iteration. If a handoff was set OR the
            # client closed, fall through to the outer while.
            if handoff.next is not None:
                break

        if handoff.next is not None:
            # Kind transition (builder → agent test). Flush the builder
            # ledger row before the agent session starts so the two
            # logical sessions land in llm_calls as separate rows.
            await _flush_llm_session()
            await _send_json(ws, {"type": "transferring"})
            next_session = handoff.next

    # Build-session over — cancel the wrap-up watchdog if it's still pending.
    # Early returns from inside the loop will leak a sleeping watchdog for up
    # to 90s; harmless (it'll wake, try to send on a stale session, fail
    # silently in its inner try/except, and die) but this handles the natural
    # exit cleanly.
    if build_watchdog_task is not None and not build_watchdog_task.done():
        build_watchdog_task.cancel()
        try:
            await build_watchdog_task
        except (asyncio.CancelledError, Exception):
            pass

    # Phase 7 — final flush at WS close. Catches dirty disconnects (user
    # tab-closed, network drop) so we don't lose the builder session's
    # token cost. Agent sessions are flushed via insert_call → don't
    # double-write here.
    await _flush_llm_session()


# ─────────────────────── helper-session run loop ─────────────────────────
#
# Eva-helper is a long-lived companion on every dashboard page. Different
# enough from the builder/test flows (no watchdog, no handoff, no save,
# accepts context updates, custom tool surface) that it gets its own
# top-level entry instead of squeezing into run_session's mega-loop.


async def run_helper_session(
    ws: WebSocket,
    *,
    user_id: Optional[int],
    client_locale: str = "en-US",
    client_tz: str = "UTC",
    tweaks: Optional[dict[str, Any]] = None,
) -> None:
    """Persistent Eva-helper session on /ws/helper.

    Lifecycle:
      • One Gemini Live session per WS, with reconnect-on-drop using the
        SDK's resumption handle (same pattern as run_session).
      • System prompt rebuilt on every (re)connect from the latest
        client-supplied context (page + agent), so a Gemini drop never
        "forgets" what the operator is looking at.
      • Push-to-talk: the client only streams mic bytes while the user is
        actively talking. No bytes for ~all of the session is normal.
      • Closes naturally when the client closes the WS.
    """
    client = _client()
    builder_country = _country_from(client_locale, client_tz)
    eng_locale = _english_variant(builder_country)

    memory = _ConversationMemory()

    # Mutable context cell — updated by every client `context` message.
    # The next Gemini (re)connect reads from here to compose the
    # CURRENT VIEW block at the top of the system instruction.
    helper_context: dict[str, Any] = {}

    region_voice = _REGION_PROFILES.get(builder_country, {}).get("default_voice")
    voice = (tweaks or {}).get("voice") or region_voice or DEFAULT_VOICE
    locale = eng_locale
    tools = _helper_tools()

    # Phase 7 — universal LLM-cost ledger. Helper kind so the rows in
    # llm_calls don't get conflated with builder sessions. Flushed at WS
    # close.
    from datetime import datetime, timezone
    llm_session: dict[str, Any] = {
        "kind": "helper",
        "started_at": datetime.now(timezone.utc),
        "tokens_in": 0, "tokens_out": 0, "tokens_cached": 0,
        "model_id": None,
    }

    async def _flush_llm_session() -> None:
        try:
            if llm_session["tokens_in"] + llm_session["tokens_out"] <= 0:
                return
            ended = datetime.now(timezone.utc)
            duration = (ended - llm_session["started_at"]).total_seconds()
            org_id = None
            if user_id is not None:
                try:
                    org = await db.get_org_for_user(user_id)
                    org_id = org["id"] if org else None
                except Exception:  # noqa: BLE001
                    pass
            await db.insert_llm_call({
                "kind": "helper",
                "user_id": user_id,
                "org_id": org_id,
                "started_at": llm_session["started_at"],
                "ended_at": ended,
                "duration_s": duration,
                "input_tokens": llm_session["tokens_in"],
                "output_tokens": llm_session["tokens_out"],
                "cached_tokens": llm_session["tokens_cached"],
                "model_id": llm_session["model_id"],
            })
            log.info(
                "llm_ledger.flush kind=helper tokens=%d/%d duration=%.1fs",
                llm_session["tokens_in"], llm_session["tokens_out"], duration,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("helper llm_ledger flush failed: %s", e)
        finally:
            llm_session["tokens_in"] = 0
            llm_session["tokens_out"] = 0
            llm_session["tokens_cached"] = 0
            llm_session["started_at"] = datetime.now(timezone.utc)

    # ── ownership-checked agent helpers ─────────────────────────────────
    # All write paths confirm the operator is a member of the target
    # agent's org before letting the patch through. Same gate as the
    # public PATCH /api/agents/:id endpoint.
    async def _ensure_agent_access(agent_id: int, *, write: bool) -> Optional[dict[str, Any]]:
        try:
            from . import auth
            if user_id is None:
                # Anonymous helper session — refuse writes, allow reads
                # only on agents that have a NULL org (legacy data).
                a = await db.get_agent(int(agent_id))
                if not a:
                    return None
                if write and a.get("org_id") is not None:
                    return None
                return a
            if write:
                return await auth.require_agent_admin(user_id, int(agent_id))
            return await auth.require_agent_member(user_id, int(agent_id))
        except Exception as e:  # noqa: BLE001
            log.info("helper: agent %s access denied for user %s: %s",
                     agent_id, user_id, e)
            return None

    # ── tool handlers ───────────────────────────────────────────────────
    async def on_apply_agent_patch(args: dict[str, Any]) -> dict[str, Any]:
        try:
            agent_id = int(args.get("agent_id"))
        except (TypeError, ValueError):
            return {"ok": False, "error": "agent_id required"}
        patch = args.get("patch") or {}
        if not isinstance(patch, dict) or not patch:
            return {"ok": False, "error": "patch must be a non-empty object"}
        a = await _ensure_agent_access(agent_id, write=True)
        if not a:
            return {"ok": False, "error": f"no write access to agent {agent_id}"}
        try:
            # Hygiene small_talk through silent_defaults helpers so the
            # same dedupe/trim/cap rules apply whether Eva-helper edits
            # them or save_agent does at build time.
            if "small_talk" in patch and isinstance(patch["small_talk"], list):
                from . import silent_defaults
                # Reuse merge_into_save_args's hygiene by routing through a
                # tiny standalone hygiene path inline (avoid dragging
                # sector defaults onto a partial PATCH).
                seen: set[str] = set()
                cleaned: list[str] = []
                for raw in patch["small_talk"]:
                    if not isinstance(raw, str):
                        continue
                    s = raw.strip()
                    if not s or s in seen:
                        continue
                    seen.add(s)
                    cleaned.append(s[:120])
                    if len(cleaned) >= 8:
                        break
                patch["small_talk"] = cleaned
            updated = await db.update_agent(agent_id, patch)
            summary = (args.get("summary") or "").strip()
            log.info("helper.apply_agent_patch id=%s keys=%s summary=%r",
                     agent_id, list(patch.keys()), summary[:80])
            # Surface the change to the client so the dashboard can refresh
            # its in-memory copy of the agent without round-tripping.
            await _send_json(ws, {
                "type": "agent_updated",
                "agent": updated,
                "summary": summary,
            })
            return {
                "ok": True,
                "agent_id": agent_id,
                "changed_keys": list(patch.keys()),
                "agent": {
                    k: updated.get(k) for k in
                    ("id", "name", "slug", "sector", "locale", "voice",
                     "persona", "greeting", "published")
                },
            }
        except Exception as e:  # noqa: BLE001
            log.exception("helper.apply_agent_patch failed")
            return {"ok": False, "error": str(e)[:200]}

    async def on_navigate(args: dict[str, Any]) -> dict[str, Any]:
        route = (args.get("route") or "").strip()
        if not route.startswith("/"):
            return {"ok": False, "error": "route must start with '/'"}
        if len(route) > 200:
            return {"ok": False, "error": "route too long"}
        log.info("helper.navigate route=%s", route)
        await _send_json(ws, {"type": "navigate", "route": route})
        return {"ok": True, "route": route}

    async def on_read_agent(args: dict[str, Any]) -> dict[str, Any]:
        try:
            agent_id = int(args.get("agent_id"))
        except (TypeError, ValueError):
            return {"ok": False, "error": "agent_id required"}
        a = await _ensure_agent_access(agent_id, write=False)
        if not a:
            return {"ok": False, "error": f"no access to agent {agent_id}"}
        # Trim the response — Gemini doesn't need every JSONB byte. Keep
        # the fields the helper actually reasons about; the rest can be
        # fetched again if needed.
        return {
            "ok": True,
            "agent": {
                "id": a.get("id"),
                "slug": a.get("slug"),
                "name": a.get("name"),
                "sector": a.get("sector"),
                "locale": a.get("locale"),
                "voice": a.get("voice"),
                "persona": a.get("persona"),
                "greeting": a.get("greeting"),
                "system_prompt": (a.get("system_prompt") or "")[:1500],
                "guardrails": a.get("guardrails") or [],
                "connectors": a.get("connectors") or [],
                "outcomes": a.get("outcomes") or [],
                "small_talk": a.get("small_talk") or [],
                "variables": a.get("variables") or {},
                "policy": a.get("policy") or {},
                "voice_tweaks": a.get("voice_tweaks") or {},
                "published": bool(a.get("published")),
            },
        }

    async def on_read_plan_state(_args: dict[str, Any]) -> dict[str, Any]:
        if user_id is None:
            return {"ok": False, "error": "no user bound to session"}
        try:
            state = await db.get_user_plan_state(user_id)
            plan = state.get("plan") or {}
            return {
                "ok": True,
                "plan_slug": plan.get("slug"),
                "plan_label": plan.get("label"),
                "minutes_total": state.get("minutes_total"),
                "minutes_used": state.get("minutes_used"),
                "minutes_left": state.get("minutes_left"),
            }
        except Exception as e:  # noqa: BLE001
            log.warning("helper.read_plan_state failed: %s", e)
            return {"ok": False, "error": str(e)[:200]}

    async def on_list_my_agents(_args: dict[str, Any]) -> dict[str, Any]:
        if user_id is None:
            return {"ok": False, "error": "no user bound to session"}
        try:
            rows = await db.list_agents(user_id)
            return {
                "ok": True,
                "agents": [
                    {
                        "id": r.get("id"),
                        "slug": r.get("slug"),
                        "name": r.get("name"),
                        "sector": r.get("sector"),
                        "locale": r.get("locale"),
                        "published": bool(r.get("published")),
                    }
                    for r in rows[:50]
                ],
            }
        except Exception as e:  # noqa: BLE001
            log.warning("helper.list_my_agents failed: %s", e)
            return {"ok": False, "error": str(e)[:200]}

    # ── Tier-1 "do" tools: real actions wrapping existing internal
    # helpers + endpoints so Eva can DO the work, not just describe it.
    async def on_import_knowledge_url(args: dict[str, Any]) -> dict[str, Any]:
        try:
            agent_id = int(args.get("agent_id"))
        except (TypeError, ValueError):
            return {"ok": False, "error": "agent_id required"}
        url = (args.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            return {"ok": False, "error": "url must start with http:// or https://"}
        title = (args.get("title") or "").strip()
        a = await _ensure_agent_access(agent_id, write=True)
        if not a:
            return {"ok": False, "error": f"no write access to agent {agent_id}"}
        try:
            from . import import_helpers as _ih
            scrape = await _ih.firecrawl_scrape(url)
            yaml_text = await _ih.condense_to_yaml(
                markdown=scrape["markdown"],
                source_url=scrape["source_url"],
                source_title=scrape.get("title", title),
                context_hint=f"{a.get('name') or ''} — {a.get('persona') or ''}",
                locale=a.get("locale") or "en-IN",
            )
            source = {
                "kind": "url",
                "url": scrape["source_url"],
                "title": title or scrape.get("title") or "",
            }
            new_prompt = _ih.append_knowledge_block(
                a.get("system_prompt") or "", yaml_text, source,
            )
            new_vars = _ih.add_source_to_variables(
                a.get("variables") or {}, source,
            )
            updated = await db.update_agent(agent_id, {
                "system_prompt": new_prompt,
                "variables": new_vars,
            })
            log.info(
                "helper.import_knowledge_url id=%s url=%s yaml_bytes=%d",
                agent_id, source["url"], len(yaml_text),
            )
            await _send_json(ws, {
                "type": "agent_updated",
                "agent": updated,
                "summary": f"Imported knowledge from {source['title'] or source['url']}",
            })
            return {
                "ok": True,
                "agent_id": agent_id,
                "source": source,
                "yaml_bytes": len(yaml_text),
                "system_prompt_bytes": len(new_prompt),
            }
        except _ih.IngestError as e:  # type: ignore[name-defined]
            log.info("helper.import_knowledge_url ingest_err: %s", e)
            return {"ok": False, "error": f"{e.code}: {e}"[:240]}
        except Exception as e:  # noqa: BLE001
            log.exception("helper.import_knowledge_url failed")
            return {"ok": False, "error": str(e)[:200]}

    async def on_regenerate_info_groups(args: dict[str, Any]) -> dict[str, Any]:
        try:
            agent_id = int(args.get("agent_id"))
        except (TypeError, ValueError):
            return {"ok": False, "error": "agent_id required"}
        a = await _ensure_agent_access(agent_id, write=True)
        if not a:
            return {"ok": False, "error": f"no write access to agent {agent_id}"}
        try:
            from . import chat_bridge as _cb
            result = await _cb.regenerate_info_groups(a)
            if not result:
                return {"ok": False, "error": "model returned no usable schema — try again"}
            updated = await db.update_agent(agent_id, {
                "info_groups": result["info_groups"],
                "extra_info": result["extra_info"],
            })
            sections = result["info_groups"] or []
            log.info(
                "helper.regenerate_info_groups id=%s → %d sections",
                agent_id, len(sections),
            )
            await _send_json(ws, {
                "type": "agent_updated",
                "agent": updated,
                "summary": f"Redesigned Additional Info — {len(sections)} sections",
            })
            return {
                "ok": True,
                "agent_id": agent_id,
                "section_count": len(sections),
                "section_labels": [s.get("label") for s in sections if isinstance(s, dict)][:8],
            }
        except Exception as e:  # noqa: BLE001
            log.exception("helper.regenerate_info_groups failed")
            return {"ok": False, "error": str(e)[:200]}

    async def on_set_outcome_weights(args: dict[str, Any]) -> dict[str, Any]:
        try:
            agent_id = int(args.get("agent_id"))
        except (TypeError, ValueError):
            return {"ok": False, "error": "agent_id required"}
        raw = args.get("weights") or {}
        if not isinstance(raw, dict) or not raw:
            return {"ok": False, "error": "weights must be a non-empty object"}
        a = await _ensure_agent_access(agent_id, write=True)
        if not a:
            return {"ok": False, "error": f"no write access to agent {agent_id}"}
        # Validate + clamp + merge over existing override so partial
        # updates don't wipe sibling keys.
        existing = a.get("outcome_weights") if isinstance(a.get("outcome_weights"), dict) else {}
        merged: dict[str, float] = dict(existing) if existing else {}
        for k in ("success", "qualified", "info", "failure"):
            if k not in raw:
                continue
            try:
                v = float(raw[k])
            except (TypeError, ValueError):
                return {"ok": False, "error": f"weight '{k}' must be a number"}
            merged[k] = max(0.0, min(1.0, v))
        if not merged:
            return {"ok": False, "error": "no recognised weight keys (success/qualified/info/failure)"}
        try:
            updated = await db.update_agent(agent_id, {"outcome_weights": merged})
            log.info("helper.set_outcome_weights id=%s weights=%s", agent_id, merged)
            await _send_json(ws, {
                "type": "agent_updated",
                "agent": updated,
                "summary": f"Updated success weights ({', '.join(f'{k}={v:.2f}' for k, v in merged.items())})",
            })
            return {"ok": True, "agent_id": agent_id, "weights": merged}
        except Exception as e:  # noqa: BLE001
            log.exception("helper.set_outcome_weights failed")
            return {"ok": False, "error": str(e)[:200]}

    _PURPOSE_VOCAB = (
        "callback_request", "appointment_booking", "quote_request",
        "inquiry_capture", "complaint_intake", "order_status",
        "support_ticket", "emergency_routing",
    )

    async def on_set_purpose(args: dict[str, Any]) -> dict[str, Any]:
        try:
            agent_id = int(args.get("agent_id"))
        except (TypeError, ValueError):
            return {"ok": False, "error": "agent_id required"}
        summary = (args.get("summary") or "").strip()
        if not summary:
            return {"ok": False, "error": "summary required"}
        if len(summary) > 240:
            summary = summary[:240]
        raw_actions = args.get("actions") or []
        if not isinstance(raw_actions, list) or not raw_actions:
            return {"ok": False, "error": "actions required (non-empty list)"}
        actions: list[str] = []
        seen: set[str] = set()
        rejected: list[str] = []
        for a_raw in raw_actions:
            if not isinstance(a_raw, str):
                continue
            slug = a_raw.strip().lower().replace(" ", "_").replace("-", "_")
            if slug in seen:
                continue
            if slug not in _PURPOSE_VOCAB:
                rejected.append(a_raw)
                continue
            seen.add(slug)
            actions.append(slug)
        if not actions:
            return {
                "ok": False,
                "error": (
                    "no recognised actions. Vocabulary: "
                    + ", ".join(_PURPOSE_VOCAB)
                ),
                "rejected": rejected,
            }
        a = await _ensure_agent_access(agent_id, write=True)
        if not a:
            return {"ok": False, "error": f"no write access to agent {agent_id}"}
        purpose = {"summary": summary, "actions": actions}
        try:
            updated = await db.update_agent(agent_id, {"purpose": purpose})
            log.info(
                "helper.set_purpose id=%s actions=%s rejected=%s",
                agent_id, actions, rejected,
            )
            await _send_json(ws, {
                "type": "agent_updated",
                "agent": updated,
                "summary": f"Locked purpose ({', '.join(actions[:3])}{'…' if len(actions) > 3 else ''})",
            })
            return {
                "ok": True,
                "agent_id": agent_id,
                "purpose": purpose,
                "rejected_actions": rejected,
            }
        except Exception as e:  # noqa: BLE001
            log.exception("helper.set_purpose failed")
            return {"ok": False, "error": str(e)[:200]}

    # ── Tier-2 read-only intelligence: lets Eva diagnose with REAL data
    # before recommending action. Each handler is ownership-checked the
    # same way the public dashboard endpoints are.
    async def on_read_outcomes_report(args: dict[str, Any]) -> dict[str, Any]:
        try:
            agent_id = int(args.get("agent_id"))
        except (TypeError, ValueError):
            return {"ok": False, "error": "agent_id required"}
        try:
            days = max(1, min(int(args.get("days") or 30), 365))
        except (TypeError, ValueError):
            days = 30
        a = await _ensure_agent_access(agent_id, write=False)
        if not a:
            return {"ok": False, "error": f"no access to agent {agent_id}"}
        try:
            from . import call_outcomes
            analytics = await db.agent_analytics(agent_id, days=days)
            report = call_outcomes.assemble_report(a, analytics)
            # Trim the payload so we don't waste tokens on time-series
            # detail Eva can't reason about over voice. Keep the
            # numbers she'd actually quote.
            top_rows = sorted(
                report.get("outcomes") or [],
                key=lambda r: int(r.get("count") or 0),
                reverse=True,
            )[:6]
            return {
                "ok": True,
                "agent_id": agent_id,
                "days": days,
                "total_calls": report.get("total_calls"),
                "weighted_success_rate": report.get("success_rate"),
                "by_kind": report.get("by_kind"),
                "purpose": report.get("purpose"),
                "top_outcomes": [
                    {
                        "id": r.get("id"),
                        "label": r.get("label"),
                        "kind": r.get("kind"),
                        "count": r.get("count"),
                        "is_primary": r.get("is_primary"),
                    }
                    for r in top_rows
                ],
            }
        except Exception as e:  # noqa: BLE001
            log.exception("helper.read_outcomes_report failed")
            return {"ok": False, "error": str(e)[:200]}

    async def on_read_recent_calls(args: dict[str, Any]) -> dict[str, Any]:
        try:
            agent_id = int(args.get("agent_id"))
        except (TypeError, ValueError):
            return {"ok": False, "error": "agent_id required"}
        try:
            limit = max(1, min(int(args.get("limit") or 10), 25))
        except (TypeError, ValueError):
            limit = 10
        outcome_filter = (args.get("outcome") or "").strip().lower() or None
        a = await _ensure_agent_access(agent_id, write=False)
        if not a:
            return {"ok": False, "error": f"no access to agent {agent_id}"}
        try:
            # Pull a bit extra when filtering so the operator still gets
            # `limit` rows after the filter narrows the set.
            rows = await db.list_calls_for_agent(
                agent_id, limit=limit * 3 if outcome_filter else limit,
            )
            if outcome_filter:
                rows = [r for r in rows if (r.get("outcome") or "").lower() == outcome_filter]
            rows = rows[:limit]
            return {
                "ok": True,
                "agent_id": agent_id,
                "count": len(rows),
                "filter_outcome": outcome_filter,
                "calls": [
                    {
                        "id": r.get("id"),
                        "started_at": str(r.get("started_at") or ""),
                        "duration_s": r.get("duration_s"),
                        "outcome": r.get("outcome"),
                        "summary": (r.get("summary") or "")[:280],
                        "sentiment": r.get("sentiment"),
                        "lead_quality": r.get("lead_quality"),
                    }
                    for r in rows
                ],
            }
        except Exception as e:  # noqa: BLE001
            log.exception("helper.read_recent_calls failed")
            return {"ok": False, "error": str(e)[:200]}

    async def on_read_runtime_prompt(args: dict[str, Any]) -> dict[str, Any]:
        """Compose the agent's system prompt the same way a real call would
        and surface it back to Eva. Lets the operator ask 'what does she
        actually know about my hours?' and get a real, verifiable answer
        instead of vibes. Trimmed to ~6 KB so the helper-session token
        budget stays sane; Eva can quote the relevant snippet."""
        try:
            agent_id = int(args.get("agent_id"))
        except (TypeError, ValueError):
            return {"ok": False, "error": "agent_id required"}
        a = await _ensure_agent_access(agent_id, write=False)
        if not a:
            return {"ok": False, "error": f"no access to agent {agent_id}"}
        try:
            prompt = _agent_system_prompt(a)
            # Quick presence map for the optional blocks added in build
            # 183. Lets Eva answer 'are my custom Do's reaching her?'
            # without re-grepping the whole string.
            blocks_present = {
                "business_facts":    "CURRENT BUSINESS FACTS" in prompt,
                "operator_dos":      "Operator-set Do's:" in prompt,
                "operator_donts":    "Operator-set Don'ts:" in prompt,
                "reference_info":    "REFERENCE INFO" in prompt or "Reference info" in prompt,
                "conventions":       "Speech & format" in prompt or "Sector playbook" in prompt,
                "outcome_kinds":     "[success]" in prompt or "[qualified]" in prompt
                                     or "[info]" in prompt or "[failure]" in prompt,
                "purpose_mission":   "Mission:" in prompt,
                "knowledge_block":   "KNOWLEDGE" in prompt,
            }
            return {
                "ok": True,
                "agent_id": agent_id,
                "agent_name": a.get("name"),
                "length_chars": len(prompt),
                "blocks_present": blocks_present,
                "prompt": prompt[:6000],
                "truncated": len(prompt) > 6000,
            }
        except Exception as e:  # noqa: BLE001
            log.exception("helper.read_runtime_prompt failed")
            return {"ok": False, "error": str(e)[:200]}

    async def on_read_conventions(args: dict[str, Any]) -> dict[str, Any]:
        try:
            agent_id = int(args.get("agent_id"))
        except (TypeError, ValueError):
            return {"ok": False, "error": "agent_id required"}
        a = await _ensure_agent_access(agent_id, write=False)
        if not a:
            return {"ok": False, "error": f"no access to agent {agent_id}"}
        try:
            from . import phone_ai_conventions
            view = phone_ai_conventions.summarize_for_ui(a)
            return {"ok": True, "agent_id": agent_id, "conventions": view}
        except Exception as e:  # noqa: BLE001
            log.exception("helper.read_conventions failed")
            return {"ok": False, "error": str(e)[:200]}

    # ── Tier-3 advanced "do" verbs: create a new agent + jump the
    # operator to the test-call page.
    async def on_build_new_agent(args: dict[str, Any]) -> dict[str, Any]:
        if user_id is None:
            return {"ok": False, "error": "must be signed in to build a new agent"}
        use_case = (args.get("use_case") or "").strip()
        business = (args.get("business_name") or "").strip()
        agent_name = (args.get("agent_name") or "").strip()
        if not use_case or not business or not agent_name:
            return {"ok": False, "error": "use_case, business_name, agent_name all required"}
        locale = (args.get("locale") or "").strip() or "en-IN"
        sector_hint = (args.get("sector_hint") or "").strip().lower() or "generic"
        facts = args.get("facts") if isinstance(args.get("facts"), dict) else {}
        # `compose_dynamic_agent` reads agent_name + business_name out of
        # the answers dict — fold them in alongside any operator facts.
        answers = {
            **{k: v for k, v in facts.items() if isinstance(k, str)},
            "agent_name": agent_name,
            "business_name": business,
        }
        try:
            from . import chat_bridge as _cb, silent_defaults
            composed = await _cb.compose_dynamic_agent(
                use_case, answers, locale=locale, sector_hint=sector_hint,
            )
            composed = silent_defaults.merge_into_save_args(composed)
            saved = await db.create_agent(composed, user_id=user_id)
            try:
                await db.seed_helper_memory(
                    user_id=user_id, agent_id=saved["id"], agent=saved,
                )
            except Exception as e:  # noqa: BLE001
                log.warning("helper.build_new_agent seed_helper_memory failed: %s", e)
            log.info(
                "helper.build_new_agent id=%s sector=%s slug=%s",
                saved.get("id"), saved.get("sector"), saved.get("slug"),
            )
            await _send_json(ws, {
                "type": "agent_created",
                "agent": saved,
                "summary": f"Built {saved.get('name')} for {business}",
            })
            return {
                "ok": True,
                "agent_id": saved.get("id"),
                "slug": saved.get("slug"),
                "name": saved.get("name"),
                "sector": saved.get("sector"),
                "next_route": f"/agent/{saved.get('slug') or saved.get('id')}",
            }
        except Exception as e:  # noqa: BLE001
            log.exception("helper.build_new_agent failed")
            return {"ok": False, "error": str(e)[:200]}

    async def on_start_test_call(args: dict[str, Any]) -> dict[str, Any]:
        try:
            agent_id = int(args.get("agent_id"))
        except (TypeError, ValueError):
            return {"ok": False, "error": "agent_id required"}
        a = await _ensure_agent_access(agent_id, write=False)
        if not a:
            return {"ok": False, "error": f"no access to agent {agent_id}"}
        slug = a.get("slug") or a.get("id")
        route = f"/agent/{slug}/test-call"
        log.info("helper.start_test_call id=%s route=%s", agent_id, route)
        await _send_json(ws, {"type": "navigate", "route": route})
        return {
            "ok": True,
            "agent_id": agent_id,
            "route": route,
            "note": (
                "Operator is now on the Test-Call page. Outbound dialling "
                "requires Twilio/GTS to be wired; the page handles the rest."
            ),
        }

    async def on_helper_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
        # Tier 1 — act on the active agent
        if name == "apply_agent_patch":
            return await on_apply_agent_patch(args)
        if name == "import_knowledge_url":
            return await on_import_knowledge_url(args)
        if name == "regenerate_info_groups":
            return await on_regenerate_info_groups(args)
        if name == "set_outcome_weights":
            return await on_set_outcome_weights(args)
        if name == "set_purpose":
            return await on_set_purpose(args)
        # Tier 2 — read-only intelligence
        if name == "read_agent":
            return await on_read_agent(args)
        if name == "read_runtime_prompt":
            return await on_read_runtime_prompt(args)
        if name == "read_outcomes_report":
            return await on_read_outcomes_report(args)
        if name == "read_recent_calls":
            return await on_read_recent_calls(args)
        if name == "read_conventions":
            return await on_read_conventions(args)
        if name == "read_plan_state":
            return await on_read_plan_state(args)
        if name == "list_my_agents":
            return await on_list_my_agents(args)
        # Tier 3 — creation + handoff
        if name == "build_new_agent":
            return await on_build_new_agent(args)
        if name == "start_test_call":
            return await on_start_test_call(args)
        # Verify
        if name == "navigate":
            return await on_navigate(args)
        log.info("helper: unknown tool %s", name)
        return {"ok": False, "error": f"unknown helper tool: {name}"}

    # Stubs for the on_save_agent / on_select_agent / on_connector_call
    # params on the existing pump signature. None of these fire on a
    # helper session because the tool list doesn't include them, but the
    # pump still asks for the callbacks — return a clear error if the
    # model somehow tries.
    async def on_save_agent_stub(_args): return {"ok": False, "error": "save_agent not available in helper"}
    async def on_select_agent_stub(_args): return {"ok": False, "error": "select_agent not available in helper"}
    async def on_connector_stub(_name, _args): return {"ok": False, "error": "connectors not available in helper"}

    # Context-update injection: when the client sends a fresh page/agent
    # context mid-session, we (a) update our cell and (b) push a short
    # user-role notice into Gemini so the current turn's reply is
    # already aware. The cell drives the NEXT reconnect's system prompt.
    async def on_context_update(session, data: dict[str, Any]) -> None:
        for key in ("page", "page_label", "agent_id", "agent_summary"):
            if key in data:
                helper_context[key] = data[key]
        log.info("helper.context page=%s agent=%s",
                 helper_context.get("page_label") or helper_context.get("page"),
                 helper_context.get("agent_id"))
        try:
            ctx_agent_id = helper_context.get("agent_id")
            ctx_summary = str(helper_context.get("agent_summary") or "").strip()
            ctx_name = (ctx_summary.split("·")[0].strip() if ctx_summary else "") or None
            page_label = helper_context.get("page_label") or helper_context.get("page") or "(none)"
            if ctx_agent_id and ctx_name:
                notice = (
                    f"[SYSTEM NOTICE: The operator just navigated. They are now on "
                    f"{ctx_name}'s page ({page_label}, agent_id={ctx_agent_id}). For "
                    f"the rest of this conversation, every reference to 'her', 'this "
                    f"agent', 'it', 'do it for me', 'no explanation', or any "
                    f"unqualified mention of 'the agent' MUST resolve to {ctx_name} "
                    f"(agent_id={ctx_agent_id}). DO NOT ask 'which agent?' or list "
                    f"agents — they are on {ctx_name}'s page. Stay silent unless the "
                    f"operator speaks. Do not narrate this change.]"
                )
            else:
                notice = (
                    f"[SYSTEM NOTICE: The operator navigated to {page_label}. There "
                    f"is no specific agent in focus on this view. If they ask you to "
                    f"act on 'her' / 'the agent', use list_my_agents first. Stay "
                    f"silent unless they speak.]"
                )
            await session.send_client_content(
                turns=types.Content(
                    role="user",
                    parts=[types.Part(text=notice)],
                ),
                turn_complete=True,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("helper context-update inject failed: %s", e)

    # ── outer reconnect loop ────────────────────────────────────────────
    usable_model: Optional[str] = None
    resume_handle: Optional[str] = None
    opened_once = False
    reconnect_attempts = 0
    MAX_RECONNECTS = 20
    candidates: list[str] = [DEFAULT_MODEL] + [m for m in FALLBACK_MODELS if m != DEFAULT_MODEL]

    await _send_json(ws, {"type": "session_starting", "kind": "helper"})

    while reconnect_attempts <= MAX_RECONNECTS:
        # Compose system prompt with current context block on every open.
        try:
            base_prompt = await _helper_system_prompt(
                user_id=user_id, client_locale=client_locale, client_tz=client_tz,
            )
        except Exception as e:  # noqa: BLE001
            log.exception("helper: system prompt build failed")
            await _send_json(ws, {"type": "error", "message": f"helper init failed: {e}"})
            return
        ctx_block = _format_helper_context(helper_context)
        # ── Per-agent persistent memory ──
        # Load what's known about THIS agent already (build summary + the
        # last ~40 Ask-Eva turns). Eva inherits that context every time a
        # conversation starts, so the operator never has to re-explain.
        memory_block = ""
        focused_agent_id: Optional[int] = None
        focused_agent_name: Optional[str] = None
        try:
            if isinstance(helper_context, dict):
                ctx_agent_id = helper_context.get("agent_id")
                if ctx_agent_id and user_id is not None:
                    focused_agent_id = int(ctx_agent_id)
                    # The client serialises agent_summary as
                    # "<name> · <sector> · <locale> · <live|draft>" — grab the
                    # name for the memory header and the "do NOT ask which
                    # agent" rule below.
                    summary = str(helper_context.get("agent_summary") or "").strip()
                    if summary:
                        focused_agent_name = summary.split("·")[0].strip() or None
                    mem = await db.get_helper_memory(user_id=user_id, agent_id=focused_agent_id)
                    memory_block = _format_helper_memory_block(mem, agent_name=focused_agent_name)
        except Exception as e:  # noqa: BLE001
            log.warning("helper: load memory failed: %s", e)
        # When the CURRENT VIEW already names an agent, prepend a blunt
        # ABSOLUTE RULE block. Gemini Live occasionally still asks "which
        # agent?" despite the CURRENT VIEW directive — this band hammers it
        # home as the very first thing the model reads, naming the agent
        # explicitly so referential phrases ('her', 'do it', 'no explanation
        # do it for me') all resolve unambiguously.
        target_rule_block = ""
        if focused_agent_id:
            display_name = focused_agent_name or f"agent id {focused_agent_id}"
            target_rule_block = (
                "=========================================================\n"
                "ABSOLUTE RULE — DO NOT VIOLATE.\n"
                "---------------------------------------------------------\n"
                f"The operator is on {display_name}'s page (agent_id = {focused_agent_id}).\n"
                f"Every reference to 'her', 'this agent', 'it', 'this one', 'do it for\n"
                f"me', 'no explanation', or any unqualified mention of 'the agent' MUST\n"
                f"be resolved to {display_name} (agent_id = {focused_agent_id}). DO NOT\n"
                f"ask 'which agent?'. DO NOT offer to list agents. DO NOT request\n"
                f"clarification on which agent the operator means. If they had meant a\n"
                f"different agent, they'd have navigated to that agent's page first.\n"
                f"Just act on {display_name}.\n"
                "=========================================================\n\n"
            )
        system_prompt = target_rule_block + ctx_block + memory_block + base_prompt

        models_to_try = [usable_model] if usable_model else candidates
        last_err: Exception | None = None
        for model_name in models_to_try:
            is_native = "native-audio" in model_name
            config = _live_config(
                voice=voice, locale=locale, system_prompt=system_prompt,
                tools=tools, resume_handle=resume_handle,
                with_language_code=not is_native,
                is_native_audio=is_native,
                tweaks=tweaks,
            )
            try:
                async with client.aio.live.connect(model=model_name, config=config) as session:
                    usable_model = model_name
                    is_reconnect = opened_once
                    opened_once = True
                    log.info(
                        "Helper Live session %s on %s (handle=%s)",
                        "RECONNECTED" if is_reconnect else "opened",
                        model_name, "yes" if resume_handle else "fresh",
                    )
                    if not is_reconnect:
                        await _send_json(ws, {"type": "ready", "model": model_name, "kind": "helper"})
                    else:
                        await _send_json(ws, {"type": "reconnected"})

                    # Kickoff. On first open, a short directive greeting so
                    # Gemini emits ONE audio turn ("hey, what can I help
                    # with?") instead of running silent forever waiting for
                    # the first mic byte. On reconnect, silent-listen.
                    if not is_reconnect:
                        kickoff = (
                            "[The operator just opened the helper widget. "
                            "Say ONE brief greeting (3-6 words) — 'Hey, "
                            "what can I help with?' or similar — then stop "
                            "and listen. Produce ONE single audio response. "
                            "Do NOT produce two parallel streams.]"
                        )
                    else:
                        kickoff = (
                            "[SYSTEM NOTICE: brief reconnect. Helper session "
                            "is mid-flow. Do NOT re-greet. Stay silent until "
                            "the operator speaks again.]"
                        )
                    try:
                        await session.send_client_content(
                            turns=types.Content(role="user", parts=[types.Part(text=kickoff)]),
                            turn_complete=True,
                        )
                    except Exception as e:  # noqa: BLE001
                        log.warning("helper kickoff send failed: %s", e)

                    state = _SessionState()
                    state.resume_handle = resume_handle
                    state.model_id = usable_model
                    state.llm_session = llm_session
                    stop = asyncio.Event()
                    handoff = _Handoff()  # never trips for helper

                    # Persist Ask-Eva turns onto the per-(user × agent)
                    # memory row after every model turn, so a reopen restores
                    # everything. Tracks `_persisted_count` to avoid double-
                    # writing the same turn across multiple turn_complete
                    # events within one Gemini session.
                    persisted_idx = {"n": 0}

                    async def _helper_persist_turns(mem) -> None:
                        try:
                            if user_id is None or not focused_agent_id:
                                return
                            new = list(mem.turns)[persisted_idx["n"]:]
                            if not new:
                                return
                            persisted_idx["n"] = len(mem.turns)
                            await db.append_helper_turns(
                                user_id=user_id, agent_id=focused_agent_id,
                                new_turns=new,
                            )
                        except Exception as e:  # noqa: BLE001
                            log.warning("helper: append memory failed: %s", e)

                    def _helper_turn_hook(mem):
                        # Sync wrapper that returns a coroutine the pump kicks
                        # off — matches the existing on_turn_complete_hook
                        # contract (sync/async dual mode).
                        return _helper_persist_turns(mem)

                    await asyncio.gather(
                        _pump_client_to_gemini(
                            ws, session, stop, state, memory,
                            on_context_update=on_context_update,
                        ),
                        _pump_gemini_to_client(
                            ws, session, stop, state, memory,
                            handoff=handoff,
                            on_save_agent=on_save_agent_stub,
                            on_select_agent=on_select_agent_stub,
                            on_connector_call=on_connector_stub,
                            on_helper_tool=on_helper_tool,
                            on_turn_complete_hook=_helper_turn_hook,
                        ),
                    )

                    resume_handle = state.resume_handle or resume_handle
                    log.info(
                        "Helper Live session ended — turns=%s in=%s out=%s reason=%r handle=%s",
                        state.turns, state.audio_in_chunks, state.audio_out_chunks,
                        state.exit_reason, "yes" if resume_handle else "no",
                    )
                    if state.client_closed:
                        await _flush_llm_session()
                        return
                    if state.gemini_dropped:
                        reconnect_attempts += 1
                        if reconnect_attempts > MAX_RECONNECTS:
                            log.warning("helper: max reconnects exceeded; ending")
                            await _send_json(ws, {"type": "error", "message": "Helper line dropped. Tap the bubble to reopen."})
                            await _flush_llm_session()
                            return
                        log.info("helper: reconnecting (attempt %s, %s)",
                                 reconnect_attempts,
                                 "with handle" if resume_handle else "fresh")
                        await asyncio.sleep(min(0.3 * reconnect_attempts, 2.0))
                        break  # try outer while again
                    # No flag set → bail.
                    log.warning("helper: inner exited with no flag set")
                    await _flush_llm_session()
                    return
            except Exception as e:  # noqa: BLE001
                last_err = e
                msg = str(e).lower()
                if not opened_once and ("not found" in msg or "404" in msg or "unsupported" in msg or "permission" in msg):
                    log.warning("helper: model %s unusable (%s); trying next", model_name, e)
                    continue
                log.exception("helper Live session failed")
                if not opened_once:
                    await _send_json(ws, {"type": "error", "message": str(e)})
                    await _flush_llm_session()
                    return
                reconnect_attempts += 1
                if reconnect_attempts > MAX_RECONNECTS or not resume_handle:
                    await _send_json(ws, {"type": "error", "message": "Helper connection lost. Tap to reopen."})
                    await _flush_llm_session()
                    return
                await asyncio.sleep(min(0.3 * reconnect_attempts, 2.0))
                break
        else:
            # for-else: ran out of models without opening
            await _send_json(ws, {"type": "error", "message": f"No usable Live model. Last error: {last_err}"})
            await _flush_llm_session()
            return

    await _flush_llm_session()
