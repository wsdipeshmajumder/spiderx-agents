"""Telephony adapter — pluggable HTTP-webhook + WebSocket-audio providers.

One uniform interface (`TelephonyProvider`, defined in `.base`) describes any
carrier whose call flow looks like:

  1. Carrier POSTs an Answer URL when a call hits a DID.
  2. We respond with XML/JSON that says "open a stream to wss://…".
  3. Carrier streams base64-codec audio over that WS; we bridge it to
     Gemini Live for real-time bidirectional speech.
  4. Carrier POSTs a Hangup URL when the call ends.

That contract covers Twilio, Plivo, Telnyx, Bandwidth, Signalwire, Vonage,
Exotel — i.e. every modern programmable-voice provider. SIP-trunk providers
(Vobiz / Voniz / FreeSWITCH-fronted carriers) are handled separately by
`backend/sip_config.py` — they don't speak this contract at all.

Adding a new provider = one file in this package + one row in `registry.py`.
"""
from __future__ import annotations

from .base import TelephonyProvider, run_call
from .registry import get_provider, available_providers

__all__ = ["TelephonyProvider", "run_call", "get_provider", "available_providers"]
