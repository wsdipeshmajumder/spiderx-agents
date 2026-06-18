"""Provider registry — name → provider instance.

Adding a new provider:
  1. Implement the TelephonyProvider ABC in a new file in this package.
  2. Append (name, class) to `_PROVIDERS` below.
  3. Done — routes, UI provider-picker, and webhook dispatch all pick it up.
"""
from __future__ import annotations

from typing import Optional

from .base import TelephonyProvider
from .plivo import PlivoProvider
from .twilio import TwilioProvider


_PROVIDERS: dict[str, TelephonyProvider] = {
    "twilio": TwilioProvider(),
    "plivo":  PlivoProvider(),
}


def get_provider(name: str) -> Optional[TelephonyProvider]:
    """Look up by short id (`twilio`, `plivo`, …). Case-insensitive.
    Returns None for unknown providers; the route layer turns that into
    a 404."""
    if not name:
        return None
    return _PROVIDERS.get(name.strip().lower())


def available_providers() -> list[dict[str, object]]:
    """For the UI provider-picker — list every registered provider with
    its capability flags."""
    return [
        {
            "id": p.name,
            "name": p.display_name,
            "auto_provision": p.auto_provision_supported,
            "sip_trunk": p.sip_trunk,
        }
        for p in _PROVIDERS.values()
    ]
