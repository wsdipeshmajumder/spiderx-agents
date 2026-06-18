"""Telephony provider ABC + the generic call-loop.

A provider implements three small methods describing its WebSocket envelope
shape and (optionally) a few more for auto-provisioning via the provider's
REST API. The big Gemini-Live bridge — pumping inbound audio in, outbound
audio out, dispatching tool calls — lives in `run_call()` here, completely
provider-agnostic.

That's the whole reason for this package: when we add Exotel, we write a
~120-line `ExotelProvider` class. We don't re-implement the call loop.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Optional, Union

from fastapi import WebSocket, WebSocketDisconnect
from google import genai
from google.genai import types

from .. import db
from ..connectors import (
    CONNECTOR_DECLS,
    build_tools as build_connector_tools,
    handle as handle_connector,
)
from ..gemini_bridge import (
    DEFAULT_MODEL,
    FALLBACK_MODELS,
    _agent_system_prompt,
    _live_config,
)
from .audio import ULAW_FRAME_BYTES, chunk_ulaw, pcm24k_to_ulaw, ulaw_to_pcm16k

log = logging.getLogger("eva.telephony")


# ─── Normalised event vocabulary ─────────────────────────────────────────
#
# Every provider's WS envelope is parsed into one of these. Keeps the call
# loop blind to provider quirks (Twilio's `streamSid`, Plivo's `streamId`,
# whatever Exotel calls it).


@dataclass
class WsStart:
    """Stream is now open. `stream_id` is the carrier-side identifier we
    must echo back in every outbound media envelope so they know which
    leg to play the audio on."""
    stream_id: str
    call_id: Optional[str] = None
    extra: dict[str, Any] | None = None


@dataclass
class WsMedia:
    """One inbound audio frame. Always µ-law 8 kHz mono (raw bytes already
    base64-decoded for you). 20 ms typical (~160 bytes)."""
    ulaw: bytes


@dataclass
class WsDtmf:
    """Caller pressed a DTMF digit. Some providers send these inline on the
    WS, others over a separate webhook — when in-band, surface them here."""
    digit: str


@dataclass
class WsStop:
    """Carrier signalled end-of-stream. Treat as authoritative — clean up."""


# PEP 604 unions evaluated at runtime need Python 3.10+; the prod image
# is 3.13 but local dev .venv is still 3.9 — keep the Union[] form so the
# module imports cleanly on both.
WsEvent = Union[WsStart, WsMedia, WsDtmf, WsStop]


# ─── Provider ABC ────────────────────────────────────────────────────────


class TelephonyProvider(ABC):
    """Implement to plug a new programmable-voice carrier into SpiderX."""

    # Display name + short id used in URLs/registry. e.g. ("Twilio", "twilio").
    name: str = ""
    display_name: str = ""

    # Capability flags drive the UI:
    #   auto_provision_supported = True →  Phone Number tab shows the
    #     "Auto-setup" panel (paste creds, we'll create the Application).
    #   sip_trunk = True →  this provider is a SIP trunk (Vobiz et al);
    #     the call loop here is NOT used — see backend/sip_config.py.
    auto_provision_supported: bool = False
    sip_trunk: bool = False

    # ── Webhook side ───────────────────────────────────────────────

    @abstractmethod
    def answer_xml(self, *, stream_url: str, agent: dict[str, Any]) -> tuple[str, str]:
        """Return `(body, content_type)` the carrier expects in response to
        its inbound Answer-URL POST.

        `stream_url` is the wss:// URL the carrier should connect to. For
        TwiML/Plivo XML the body is XML; for Vonage NCCO the body is JSON.
        The Content-Type header switches accordingly.
        """

    def fallback_xml(self, *, agent: Optional[dict[str, Any]] = None) -> tuple[str, str]:
        """Returned when the carrier hits our Fallback URL (primary timed
        out or 5xx'd). Default is a polite hangup. Override for
        provider-specific verbs."""
        return ("<Response><Hangup/></Response>", "application/xml")

    # ── WS envelope adapter ────────────────────────────────────────

    @abstractmethod
    def parse_ws_message(self, raw: str) -> Optional[WsEvent]:
        """Parse one inbound WS text frame. Return None for unknown events
        (carriers add new ones over time — be liberal in what we accept)."""

    @abstractmethod
    def encode_outbound_audio(self, *, stream_id: str, ulaw_frame: bytes) -> dict[str, Any]:
        """Wrap one outbound µ-law frame in the carrier's JSON envelope.
        The return value is JSON-serialised + sent over the WS as-is."""

    def clear_outbound(self, *, stream_id: str) -> Optional[dict[str, Any]]:
        """Tell the carrier to flush its outbound audio buffer (e.g. when
        the model interrupts itself mid-utterance). Return None if the
        carrier doesn't support it — the call loop will skip the send."""
        return None

    # ── Hangup webhook ─────────────────────────────────────────────

    @abstractmethod
    def parse_hangup_webhook(self, form: dict[str, Any]) -> dict[str, Any]:
        """Normalise the hangup webhook body to:
            { call_id, duration_seconds, hangup_cause, raw }
        where unknown fields collapse to None."""

    # ── Auto-provision (optional) ──────────────────────────────────
    #
    # The default raises NotImplementedError so unsupported providers are
    # caught at the API layer (the UI gates on `auto_provision_supported`,
    # but a code-path that ignores the gate must still fail safely).

    async def verify_creds(self, creds: dict[str, str]) -> dict[str, Any]:
        """Hit the provider's account-info endpoint to confirm the creds
        are valid. Return `{ok: True, account_name, balance, currency}`
        on success; raise `TelephonyAuthError` with a short, operator-
        friendly message on failure."""
        raise NotImplementedError

    async def list_numbers(self, creds: dict[str, str]) -> list[dict[str, Any]]:
        """List the operator's owned numbers in this provider. Each entry:
            { number (E.164), country, type, alias?, current_app_id? }"""
        raise NotImplementedError

    async def create_application(
        self, *,
        creds: dict[str, str],
        name: str,
        answer_url: str,
        hangup_url: str,
        fallback_url: str,
    ) -> dict[str, Any]:
        """Create the Application in the carrier's dashboard. Return
            { app_id, app_name }
        Idempotent on name re-use is provider-dependent — most APIs let you
        re-create with the same name. If you want strict idempotency,
        check first via `read_existing_app_by_name`."""
        raise NotImplementedError

    async def bind_number(
        self, *,
        creds: dict[str, str],
        number: str,
        app_id: str,
        alias: str = "",
    ) -> dict[str, Any]:
        """Attach the Application to a DID so inbound calls trigger it.
        Return `{ ok: True }` or raise."""
        raise NotImplementedError

    async def read_number_config(
        self, *,
        creds: dict[str, str],
        number: str,
    ) -> dict[str, Any]:
        """Read the carrier's current binding for the DID — used by the
        UI's "Verify live" button so we can tell the operator exactly
        what's misconfigured. Return `{ app_id?, alias?, answer_url?, … }`."""
        raise NotImplementedError


class TelephonyAuthError(Exception):
    """Operator-facing creds-validation failure. The `args[0]` message is
    safe to show in the UI (don't include the token in it)."""


# ─── Generic call loop ───────────────────────────────────────────────────


def _client() -> genai.Client:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY env var is not set")
    return genai.Client(api_key=api_key)


async def _send(ws: WebSocket, payload: dict[str, Any]) -> None:
    try:
        await ws.send_text(json.dumps(payload))
    except Exception:
        # WS errors here are routine when the carrier disconnects mid-flush;
        # the pump task will see the disconnect on its receive() and exit.
        pass


async def run_call(ws: WebSocket, provider: TelephonyProvider, agent_id: int) -> None:
    """Bridge a carrier's WebSocket call leg into Gemini Live for the
    given saved agent. Identical end-to-end behaviour as the in-browser
    voice tester — same connector tools, same model fallback chain, same
    interrupt semantics."""
    agent = db.get_agent(agent_id)
    if not agent:
        log.warning("telephony[%s]: agent %s not found", provider.name, agent_id)
        return

    connector_ids: list[str] = agent.get("connectors") or []
    tools = build_connector_tools(connector_ids)
    config = _live_config(
        voice=agent.get("voice") or "Aoede",
        locale=agent.get("locale") or "en-US",
        system_prompt=_agent_system_prompt(agent),
        tools=tools,
    )

    client = _client()
    last_err: Exception | None = None
    for model_name in [DEFAULT_MODEL] + [m for m in FALLBACK_MODELS if m != DEFAULT_MODEL]:
        try:
            async with client.aio.live.connect(model=model_name, config=config) as session:
                log.info("telephony[%s] call: agent=%s model=%s", provider.name, agent_id, model_name)
                await _bridge(ws, provider, session, agent, connector_ids)
                return
        except Exception as e:  # noqa: BLE001
            last_err = e
            msg = str(e).lower()
            if "not found" in msg or "404" in msg or "unsupported" in msg:
                log.warning("telephony[%s]: model %s unusable; trying next", provider.name, model_name)
                continue
            log.exception("telephony[%s] session failed", provider.name)
            return
    log.error("telephony[%s]: no usable model. last err=%s", provider.name, last_err)


async def _bridge(
    ws: WebSocket,
    provider: TelephonyProvider,
    session,
    agent: dict[str, Any],
    connector_ids: list[str],
) -> None:
    """The actual pump-pair. Inbound audio gets µ-law-decoded + upsampled
    to 16 kHz PCM and fed to Gemini. Outbound 24 kHz PCM from Gemini gets
    downsampled to 8 kHz + µ-law-encoded + chopped into 20 ms frames + sent
    back through the provider's envelope."""
    state_in: Optional[object] = None
    state_out: Optional[object] = None
    stream_id: Optional[str] = None
    stop = asyncio.Event()
    kickoff_sent = asyncio.Event()

    async def carrier_to_gemini() -> None:
        nonlocal state_in, stream_id
        try:
            while not stop.is_set():
                raw = await ws.receive_text()
                ev = provider.parse_ws_message(raw)
                if ev is None:
                    continue
                if isinstance(ev, WsStart):
                    stream_id = ev.stream_id
                    log.info("telephony[%s] stream started sid=%s", provider.name, stream_id)
                    if not kickoff_sent.is_set():
                        await session.send_client_content(
                            turns=types.Content(role="user", parts=[types.Part(text="<call_start>")]),
                            turn_complete=True,
                        )
                        kickoff_sent.set()
                elif isinstance(ev, WsMedia):
                    if not ev.ulaw:
                        continue
                    pcm16k, state_in = ulaw_to_pcm16k(ev.ulaw, state_in)
                    await session.send_realtime_input(
                        audio=types.Blob(data=pcm16k, mime_type="audio/pcm;rate=16000")
                    )
                elif isinstance(ev, WsDtmf):
                    # Surface DTMF as a tiny text turn so the model can react
                    # ("press 1 to confirm" flows). The model can choose to
                    # ignore it — the system prompt decides.
                    await session.send_client_content(
                        turns=types.Content(role="user", parts=[types.Part(text=f"<dtmf:{ev.digit}>")]),
                        turn_complete=True,
                    )
                elif isinstance(ev, WsStop):
                    stop.set()
                    return
        except WebSocketDisconnect:
            stop.set()
        except Exception as e:  # noqa: BLE001
            log.warning("telephony[%s] carrier→gemini error: %s", provider.name, e)
            stop.set()

    async def gemini_to_carrier() -> None:
        nonlocal state_out
        try:
            async for response in session.receive():
                if stop.is_set():
                    return
                sc = response.server_content
                if sc:
                    if sc.interrupted and stream_id:
                        clear = provider.clear_outbound(stream_id=stream_id)
                        if clear is not None:
                            await _send(ws, clear)
                    if sc.model_turn and sc.model_turn.parts:
                        for part in sc.model_turn.parts:
                            if part.inline_data and part.inline_data.data:
                                ulaw, state_out = pcm24k_to_ulaw(part.inline_data.data, state_out)
                                if not stream_id:
                                    continue
                                for chunk in chunk_ulaw(ulaw, ULAW_FRAME_BYTES):
                                    env = provider.encode_outbound_audio(
                                        stream_id=stream_id,
                                        ulaw_frame=chunk,
                                    )
                                    await _send(ws, env)
                if response.tool_call and response.tool_call.function_calls:
                    for fc in response.tool_call.function_calls:
                        name, args = fc.name, fc.args or {}
                        log.info("telephony[%s] tool_call %s", provider.name, name)
                        try:
                            if name in CONNECTOR_DECLS and name in connector_ids:
                                result = await handle_connector(name, args, agent)
                            else:
                                result = {"ok": False, "error": f"connector {name} not enabled"}
                        except Exception as e:  # noqa: BLE001
                            log.exception("telephony[%s] connector %s failed", provider.name, name)
                            result = {"ok": False, "error": str(e)}
                        await session.send_tool_response(
                            function_responses=types.FunctionResponse(id=fc.id, name=name, response=result)
                        )
        except Exception as e:  # noqa: BLE001
            log.warning("telephony[%s] gemini→carrier error: %s", provider.name, e)
        finally:
            stop.set()

    await asyncio.gather(carrier_to_gemini(), gemini_to_carrier())
