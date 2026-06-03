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
}

# Static FX. Roughly the Mar-2026 USD-INR mid-market rate; refresh
# yearly. The fail-soft fallback below keeps short-term FX moves from
# zeroing the analytics columns.
_USD_TO_INR = 83.5


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
