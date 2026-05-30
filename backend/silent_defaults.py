"""Sector-aware silent defaults Eva applies on save_agent.

Tier 1 of the "Eva does the hard work" approach. Instead of asking the user
to choose between Sales / Support / Survey / Healthcare / Patient presets
(like every form-based builder does), we pick the right one from the sector
she already inferred from the conversation. The user never sees these knobs
unless they open the advanced editor — and even then they're pre-filled with
sensible values.

What's covered:
  • VAD knobs (silence_duration_ms, prefix_padding_ms, sensitivity) tuned
    per sector style — patient sectors get more breathing room, fast sectors
    get snappier turn-taking.
  • Prompt caching — pure cost win for static prompts; always on.
  • Outcome taxonomy — seed a sensible list for each sector so analytics
    work out of the box (booking sectors get booked/rescheduled/info_only,
    sales sectors get qualified/callback/voicemail, …).
  • Disconnect-safety policy — block premature `end_call` with imprecise
    outcomes when the call is too young.
  • Voicemail detection flag — on for outbound, off for inbound by default.
"""

from __future__ import annotations

from typing import Any


# ──────────────────────────── VAD presets ─────────────────────────────────
# Three "communication styles" map onto Gemini's VAD knobs.
#
#   patient  → long pauses, low start sensitivity. Caller may be elderly,
#              hard of hearing, or thinking. Default for healthcare-shaped
#              sectors.
#   balanced → general-purpose default. Most service sectors.
#   fast     → snappy turn-taking. Retail/restaurant/quick-service.

_VAD_PRESETS: dict[str, dict[str, Any]] = {
    "patient": {
        "silence_duration_ms": 2400,
        "prefix_padding_ms": 450,
        "sensitivity": "low",
    },
    "balanced": {
        "silence_duration_ms": 2000,
        "prefix_padding_ms": 400,
        "sensitivity": "low",
    },
    "fast": {
        "silence_duration_ms": 1500,
        "prefix_padding_ms": 320,
        "sensitivity": "low",
    },
}


# ─────────────────────── Sector → communication style ────────────────────
_SECTOR_STYLE: dict[str, str] = {
    "healthcare":   "patient",
    "dental":       "patient",
    "salon":        "patient",
    "education":    "patient",
    "legal":        "patient",
    "insurance":    "balanced",
    "banking":      "balanced",
    "real_estate":  "balanced",
    "automotive":   "balanced",
    "travel":       "balanced",
    "events":       "balanced",
    "logistics":    "balanced",
    "saas_support": "balanced",
    "retail":       "fast",
    "restaurant":   "fast",
    "generic":      "balanced",
}


# ────────────────────────── Outcome taxonomies ───────────────────────────
# Eva seeds these on save. The user can edit later in the advanced drawer.
_OUTCOMES: dict[str, list[str]] = {
    "_booking": [
        "booked", "rescheduled", "info_only",
        "callback_requested", "not_interested", "wrong_number", "voicemail",
    ],
    "_sales": [
        "qualified", "callback_requested",
        "not_interested", "wrong_number", "voicemail",
    ],
    "_support": [
        "resolved", "escalated", "callback_requested",
        "info_only", "voicemail",
    ],
    "_intake": [
        "intake_complete", "callback_requested",
        "not_interested", "wrong_number", "voicemail",
    ],
}

_SECTOR_OUTCOMES: dict[str, str] = {
    "healthcare":   "_booking",
    "dental":       "_booking",
    "salon":        "_booking",
    "restaurant":   "_booking",
    "travel":       "_booking",
    "events":       "_booking",
    "automotive":   "_booking",
    "education":    "_intake",
    "legal":        "_intake",
    "insurance":    "_sales",
    "banking":      "_sales",
    "real_estate":  "_sales",
    "retail":       "_support",
    "logistics":    "_support",
    "saas_support": "_support",
    "generic":      "_support",
}


# Imprecise outcomes — these are easy to fire incorrectly in the first
# seconds of a call (e.g. "not_interested" when the caller is just thinking).
# Disconnect-safety blocks them until the call is mature.
_IMPRECISE = {"not_interested", "voicemail", "wrong_number", "no_decision"}


# ─────────────── Sector → background ambience (Beta) ─────────────────────
# Mirrors SECTOR_AMBIENCE_LABEL on the frontend so Eva's pick + dashboard
# display + actual playback all line up.
_SECTOR_AMBIENCE: dict[str, str] = {
    "healthcare":   "clinic",
    "dental":       "clinic",
    "salon":        "quiet",
    "restaurant":   "cafe",
    "retail":       "cafe",
    "travel":       "cafe",
    "events":       "cafe",
    "automotive":   "workshop",
    "logistics":    "workshop",
    "legal":        "quiet",
    "insurance":    "office",
    "banking":      "office",
    "real_estate":  "office",
    "education":    "office",
    "saas_support": "office",
    "generic":      "office",
}


# ─────────── Sector → starter Do's / Don'ts (Guardrails page) ────────────
# Eva only ever sees the IDs; the Guardrails page renders the labels. Keys
# match the canonical lists in app.js AgentGuardrailsPage.
_SECTOR_DOS: dict[str, list[str]] = {
    "healthcare":   ["confirm_booking", "sms_recap", "language_match"],
    "dental":       ["confirm_booking", "sms_recap", "language_match"],
    "salon":        ["confirm_booking", "sms_recap"],
    "restaurant":   ["confirm_booking", "sms_recap", "language_match"],
    "travel":       ["confirm_booking", "sms_recap", "language_match"],
    "events":       ["confirm_booking", "sms_recap"],
    "automotive":   ["confirm_booking", "sms_recap"],
    "real_estate":  ["sms_recap", "name_caller"],
    "insurance":    ["confirm_booking", "sms_recap"],
    "banking":      ["confirm_booking", "sms_recap"],
    "legal":        ["sms_recap", "name_caller", "offer_transcript"],
    "education":    ["confirm_booking", "sms_recap"],
    "retail":       ["sms_recap", "language_match"],
    "logistics":    ["sms_recap", "language_match"],
    "saas_support": ["sms_recap", "offer_transcript", "language_match"],
    "generic":      ["sms_recap", "language_match"],
}
_SECTOR_SMALL_TALK: dict[str, list[str]] = {
    # Short, repeatable rapport openers per sector. The agent reaches
    # for these when a caller opens with chitchat ("hi, how are you?")
    # — they're NOT replacements for the task-specific sample phrases
    # Eva embeds in system_prompt. Keep each line under ~8 words and
    # natural-sounding; Eva (or the operator on the dashboard) can
    # localize per region.
    "healthcare":   ["Hope you're doing alright today.", "How are you keeping?", "Glad you called — how can I help?"],
    "dental":       ["Hope you're keeping well.", "How's your day going?", "Glad you called — how can I help?"],
    "salon":        ["How's your day been so far?", "Hope you're treating yourself today.", "Lovely to hear from you."],
    "restaurant":   ["Hope you're hungry!", "How's your day going?", "Lovely to hear from you."],
    "travel":       ["Excited to plan something nice?", "How's your day going?", "Lovely to hear from you."],
    "events":       ["Excited about what you're planning?", "Hope your day's going well.", "Lovely to hear from you."],
    "automotive":   ["Hope your day's going smoothly.", "Thanks for calling — how can I help?", "Good to hear from you."],
    "real_estate":  ["Hope your day's going well.", "Thanks for reaching out.", "Good to hear from you."],
    "insurance":    ["Hope your day's going well.", "Thanks for calling — how can I help?", "Good to hear from you."],
    "banking":      ["Hope your day's going well.", "Thanks for calling.", "Good to hear from you."],
    "legal":        ["Thanks for reaching out today.", "Hope your day's going well.", "Good to hear from you."],
    "education":    ["Hope your day's going well.", "Lovely to hear from you.", "Thanks for reaching out."],
    "retail":       ["Hope you're having a good one.", "Thanks for calling!", "How can I make your day better?"],
    "logistics":    ["Hope your day's going smoothly.", "Thanks for calling.", "How can I help?"],
    "saas_support": ["Hope your day's going smoothly so far.", "Thanks for reaching out.", "How can I help?"],
    "generic":      ["Hope your day's going well.", "Thanks for calling.", "How can I help?"],
}
_SECTOR_DONTS: dict[str, list[str]] = {
    "healthcare":   ["no_price_promise", "no_phone_payment", "no_after_hours"],
    "dental":       ["no_price_promise", "no_phone_payment", "no_after_hours"],
    "salon":        ["no_phone_payment", "no_after_hours"],
    "restaurant":   ["no_phone_payment", "no_after_hours"],
    "travel":       ["no_price_promise", "no_phone_payment"],
    "events":       ["no_price_promise", "no_phone_payment"],
    "automotive":   ["no_price_promise", "no_delivery_eta"],
    "real_estate":  ["no_price_promise", "no_phone_payment"],
    "insurance":    ["no_price_promise", "no_phone_payment", "no_competitors"],
    "banking":      ["no_price_promise", "no_phone_payment", "no_competitors"],
    "legal":        ["no_price_promise", "no_phone_payment"],
    "education":    ["no_price_promise", "no_phone_payment"],
    "retail":       ["no_price_promise", "no_phone_payment", "no_delivery_eta"],
    "logistics":    ["no_delivery_eta", "no_phone_payment"],
    "saas_support": ["no_price_promise", "no_phone_payment"],
    "generic":      ["no_price_promise", "no_phone_payment"],
}


def defaults_for(sector: str | None) -> dict[str, Any]:
    """Compute the silent-default blob for an agent of this sector.

    Returns a dict with three sub-blobs:
      • voice_tweaks : merged into the agent's voice_tweaks JSON (VAD +
                       ambience + prompt caching)
      • outcomes     : array of canonical outcome IDs Eva seeds
      • policy       : top-level agent policies (disconnect safety,
                       voicemail detection, starter Do's / Don'ts toggles
                       for the Guardrails page)
    """
    s = sector or "generic"
    style = _SECTOR_STYLE.get(s, "balanced")
    vad = _VAD_PRESETS[style]
    outcomes_key = _SECTOR_OUTCOMES.get(s, "_support")
    outcomes = list(_OUTCOMES[outcomes_key])
    ambience = _SECTOR_AMBIENCE.get(s, "office")
    dos = {d: True for d in _SECTOR_DOS.get(s, [])}
    donts = {d: True for d in _SECTOR_DONTS.get(s, [])}
    small_talk = list(_SECTOR_SMALL_TALK.get(s, _SECTOR_SMALL_TALK["generic"]))
    return {
        "voice_tweaks": {
            **vad,
            "prompt_caching": True,
            "affective": True,
            "proactive": False,
            # Background ambience (Beta) — Eva still wins if she set it
            # explicitly; this is the sector-tuned fallback.
            "ambience": ambience,
            "ambience_volume": 0.18,
        },
        "outcomes": outcomes,
        "small_talk": small_talk,
        "policy": {
            "disconnect_safety": {
                "enabled": True,
                "min_call_age_s": 10,
                "max_premature": 2,
                "imprecise_outcomes": sorted(_IMPRECISE & set(outcomes)),
            },
            "voicemail_detection": {
                # Default to off — inbound agents shouldn't run voicemail
                # detection (they're being called). Outbound campaigns enable
                # it via the advanced drawer when they wire up Twilio.
                "enabled": False,
            },
            # Starter toggles for the Guardrails page. Eva can override
            # by passing policy.dos / policy.donts in save_agent.
            "dos": dos,
            "donts": donts,
            "custom_dos": "",
            "custom_donts": "",
        },
    }


def merge_into_save_args(args: dict[str, Any]) -> dict[str, Any]:
    """Apply silent defaults to a fresh save_agent payload.

    Existing fields the caller already filled win over our defaults — Eva's
    explicit choice always beats the silent baseline. For nested blobs
    (voice_tweaks, policy) we deep-merge so Eva can set just one field
    (e.g. policy.dos.confirm_booking=false) without erasing everything else.
    """
    sd = defaults_for(args.get("sector"))
    out = dict(args)
    # voice_tweaks merge: silent defaults below, caller above.
    out["voice_tweaks"] = {**sd["voice_tweaks"], **(args.get("voice_tweaks") or {})}
    # Outcomes seeded only if caller didn't provide.
    if not (args.get("outcomes") and isinstance(args["outcomes"], list)):
        out["outcomes"] = sd["outcomes"]
    # Small-talk phrases — Eva's array wins; sector default is the fallback
    # so every freshly-saved agent ships with 2-3 rapport phrases ready to
    # go. Operators can edit them later on the Small talk dashboard page.
    if not (args.get("small_talk") and isinstance(args["small_talk"], list)):
        out["small_talk"] = sd["small_talk"]
    else:
        # Trim + de-dupe + cap. The dashboard textarea splits on newlines
        # and can produce blank lines; the model occasionally emits dupes.
        seen: set[str] = set()
        cleaned: list[str] = []
        for raw in args["small_talk"]:
            if not isinstance(raw, str):
                continue
            s_phrase = raw.strip()
            if not s_phrase or s_phrase in seen:
                continue
            seen.add(s_phrase)
            cleaned.append(s_phrase[:120])
            if len(cleaned) >= 8:
                break
        out["small_talk"] = cleaned or sd["small_talk"]
    # Policy bundle — deep-merge so Eva's dos/donts pick is layered on top
    # of the sector-default toggles instead of nuking them.
    caller_policy = args.get("policy") or {}
    merged_policy = {**sd["policy"]}
    for k, v in caller_policy.items():
        if k in ("dos", "donts") and isinstance(v, dict):
            merged_policy[k] = {**merged_policy.get(k, {}), **v}
        else:
            merged_policy[k] = v
    out["policy"] = merged_policy
    # Variables — keep whatever Eva captured. Server doesn't fabricate
    # business details (would be misleading on the dashboard).
    if args.get("variables"):
        out["variables"] = args["variables"]
    return out
