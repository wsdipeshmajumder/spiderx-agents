"""Twilio Media Streams ↔ Gemini Live bridge.

Build 249 — this module is now a thin compatibility shim. The actual call
loop lives in `backend.telephony.base.run_call`, parameterised by a
`TwilioProvider`. Same end-to-end behaviour as before; importing
`run_twilio_call` from here still works for the existing `/ws/twilio/{agent_id}`
route in `backend/app.py`.

Setup
─────
    PUBLIC_HOST=your-ngrok-host.ngrok-free.app  (no scheme, no path)
    # point a Twilio Voice number's webhook at:
    # https://<PUBLIC_HOST>/api/sip/twilio/twiml/<agent_id>
"""
from __future__ import annotations

from fastapi import WebSocket

from .telephony import run_call
from .telephony.twilio import TwilioProvider


_PROVIDER = TwilioProvider()


async def run_twilio_call(ws: WebSocket, agent_id: int) -> None:
    """Bridge a Twilio Media Streams call leg into Gemini Live."""
    await run_call(ws, _PROVIDER, agent_id)
