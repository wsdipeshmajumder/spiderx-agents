"""Per-model token pricing.

Values are USD per 1M tokens at the moment of writing — we convert to
paise (₹0.01) on every call so the audit grid + analytics dashboard
display in INR without locale acrobatics. Exchange rate is a static
constant for now; once a real FX feed exists this becomes a single
function-call swap.

Hardcoded for Phase 5; will migrate to `platform_settings` in a
follow-up so finance can edit rates without a deploy. We picked
hardcoded today because:
  - Editing pricing through a JSON-blob admin UI without unit-test
    coverage is a recipe for a "we owed customers ₹4 lakh" Slack
    thread.
  - Pricing is the kind of value where a migration that lands the
    diff in version control + audit-log is the right contract.
"""
from __future__ import annotations

from typing import Optional

# USD per 1M tokens. Sourced from Google AI pricing page, refreshed
# June 2026 against https://ai.google.dev/gemini-api/docs/pricing.
#
# Audio-capable Live models moved from $0.40/$1.60 (2024) → $3.00/$12.00
# (2025+) once Google split the audio-token tier off the cheaper text-
# token tier. The bridge stores token counts at call-end time and the
# cost is computed HERE on the way in, so updating these constants
# affects all FUTURE calls; existing `calls.cost_paise` rows stay
# frozen at whatever rate they were calculated under. That's the
# audit trail we want — historical rows should never silently re-price
# under a deploy.
#
# Tokenisation rate (reference, not multiplied in here): roughly
# 32 audio-input tokens / second of caller speech, 25 audio-output
# tokens / second of agent speech.
_PRICING_USD_PER_1M = {
    "gemini-3.1-flash-live-preview":        {"in": 3.00, "out": 12.00},
    "gemini-2.5-flash-native-audio-latest": {"in": 3.00, "out": 12.00},
    "gemini-2.5-flash-native-audio-preview-12-2025": {"in": 3.00, "out": 12.00},
    "gemini-2.5-flash-native-audio-preview-09-2025": {"in": 3.00, "out": 12.00},
    "gemini-2.0-flash-live-001":            {"in": 3.00, "out": 12.00},
    # Non-Live TTS tier stays on the cheaper text-token pricing — it
    # only runs in build/helper sessions, never the live phone call.
    "gemini-2.5-flash-preview-tts":         {"in": 0.075, "out": 0.30},
    # Build 273 — text chat (the paid chat-embed add-on) runs on the cheap
    # text-token tier, not the Live audio tier. Used by chat_bridge.CHAT_MODEL.
    "gemini-2.5-flash":                     {"in": 0.30,  "out": 2.50},
}

# Static FX. Roughly the Mar-2026 USD-INR mid-market rate; refresh
# yearly. The fail-soft fallback below keeps short-term FX moves from
# zeroing the analytics columns.
_USD_TO_INR = 83.5


# Tokens per audio-second for the cost breakdown estimate. Gemini Live
# bills audio-input + audio-output per token (32 in/s, 25 out/s — see
# the module docstring above). We use these to project a per-minute
# rate on the agent's Overview card. Real per-call cost is still
# computed from actual token counts in `cost_paise`.
_AUDIO_TOKENS_PER_SEC_IN  = 32
_AUDIO_TOKENS_PER_SEC_OUT = 25


def per_minute_inr(model_id: Optional[str]) -> float:
    """Project a per-minute INR rate for this model assuming a typical
    1:1 caller/agent talk split. Used on the agent's Cost Breakdown
    card so the operator sees a single per-minute number without
    needing real call data. Frozen tokens-per-second constants live at
    the top of this module."""
    rate = _PRICING_USD_PER_1M.get(model_id) if model_id else None
    if not rate:
        return 0.0
    tokens_in_per_min  = _AUDIO_TOKENS_PER_SEC_IN  * 60
    tokens_out_per_min = _AUDIO_TOKENS_PER_SEC_OUT * 60
    cost_usd = (
        tokens_in_per_min  / 1_000_000.0 * rate["in"]
        + tokens_out_per_min / 1_000_000.0 * rate["out"]
    )
    return round(cost_usd * _USD_TO_INR, 4)


async def cost_breakdown_for_agent(agent: dict) -> dict:
    """Structured per-minute cost breakdown for the agent's Overview
    card. Each row carries vendor + service + per-minute INR + status
    badge ("included" / "extra"). The aggregate is `total_inr_per_min`.

    Telephony is a separate line because it's a PSTN pass-through, not
    something we mark up. Web/test calls don't touch PSTN at all, so
    that row carries `status: pass-through` to signal "only when the
    agent serves real phone traffic".
    """
    from . import db_pg as _db
    model_id = agent.get("model_id") or "gemini-3.1-flash-live-preview"
    ai_rate = per_minute_inr(model_id)

    # Pull current Plivo per-min from the audited pricing_versions
    # table — same source the admin Pricing tab + Agent P&L use.
    plivo_inr = 0.0
    try:
        pool = await _db.get_pool()
        async with pool.acquire() as conn:
            plivo_inr = float(await conn.fetchval(
                "SELECT inr_per_unit FROM pricing_versions "
                "WHERE provider='plivo' AND rate_kind='pstn.outbound.mobile' "
                "  AND effective_to IS NULL"
            ) or 0.0)
    except Exception:  # noqa: BLE001
        plivo_inr = 0.60

    items = [
        {"label": "Platform fee",  "vendor": None,           "status": "included", "inr_per_min": 0.0,
         "note": "Account management, dashboard, recordings, support."},
        {"label": "Speech-to-Text","vendor": "Built-in (Gemini Live)", "status": "included", "inr_per_min": 0.0,
         "note": "Caller audio → text is part of the AI Model line."},
        {"label": "Text-to-Speech","vendor": "Built-in (Gemini Live)", "status": "included", "inr_per_min": 0.0,
         "note": "Agent audio out is part of the AI Model line."},
        {"label": "AI Model",      "vendor": model_id,        "status": "included", "inr_per_min": ai_rate,
         "note": "Real-time conversational reasoning + native audio."},
        {"label": "Telephony",     "vendor": "Plivo (PSTN)",  "status": "pass_through", "inr_per_min": plivo_inr,
         "note": "Only for real phone calls — web/test calls skip this."},
    ]
    total = sum(it["inr_per_min"] for it in items if it["status"] != "pass_through")
    return {
        "items": items,
        "total_inr_per_min": round(total, 4),
        "currency": "INR",
        "basis": (
            "Estimate at the Gemini Live token rate "
            f"({_AUDIO_TOKENS_PER_SEC_IN}in/{_AUDIO_TOKENS_PER_SEC_OUT}out tokens/sec). "
            "Real per-call cost is the actual token count × rate."
        ),
    }


def cost_paise(model_id: Optional[str], input_tokens: Optional[int],
                output_tokens: Optional[int]) -> int:
    """Returns the call's cost in paise (₹0.01). Returns 0 if any of the
    inputs are missing — the analytics dashboard treats 0 as "we don't
    have token data for this call yet" rather than "this call was free",
    so the operator can tell the difference visually."""
    if not model_id or input_tokens is None or output_tokens is None:
        return 0
    rate = _PRICING_USD_PER_1M.get(model_id)
    if not rate:
        return 0
    cost_usd = (
        (int(input_tokens) / 1_000_000.0) * rate["in"]
        + (int(output_tokens) / 1_000_000.0) * rate["out"]
    )
    # paise = cost_inr × 100 = cost_usd × FX × 100
    return int(round(cost_usd * _USD_TO_INR * 100))
