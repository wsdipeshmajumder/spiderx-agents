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
    _ConversationMemory,
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


# Connector-arg keys that are transport/content, not structured lead data —
# excluded when harvesting `extracted` from a connector call. Industry-
# agnostic: works for send_email, book_appointment, create_lead, etc.
_HARVEST_SKIP_KEYS = {
    "to", "cc", "bcc", "from", "subject", "body", "body_html", "html",
    "message", "text", "content", "summary", "priority", "attachments",
    "template", "template_id", "url", "webhook", "method", "headers",
}


def _harvest_extracted(args: dict[str, Any]) -> dict[str, Any]:
    """Pull structured, scalar lead fields out of an arbitrary connector's
    args so they can be merged into the call's `extracted`. Generic across
    industries: scalar top-level fields are taken as-is, and any nested
    `metadata`/`data`/`fields` dict is flattened one level. Transport and
    free-text content keys (subject, body_html, …) are skipped so the
    structured record stays clean."""
    out: dict[str, Any] = {}

    def _take(d: dict[str, Any]) -> None:
        for k, v in (d or {}).items():
            if not isinstance(k, str) or k.lower() in _HARVEST_SKIP_KEYS or k.startswith("_"):
                continue
            if isinstance(v, (str, int, float, bool)) and not (isinstance(v, str) and len(v) > 200):
                out[k] = v

    if not isinstance(args, dict):
        return out
    _take(args)
    for nested_key in ("metadata", "data", "fields", "extracted"):
        nested = args.get(nested_key)
        if isinstance(nested, dict):
            _take(nested)
    return out


async def run_call(ws: WebSocket, provider: TelephonyProvider, agent_id: int) -> None:
    """Bridge a carrier's WebSocket call leg into Gemini Live for the
    given saved agent. Identical end-to-end behaviour as the in-browser
    voice tester — same connector tools, same model fallback chain, same
    interrupt semantics.

    Resilience (Build 261): Gemini periodically ends a Live session
    (`go_away` or a clean stream end ~every few minutes, sometimes much
    sooner). A single `session.receive()` loop therefore can't carry a
    real phone call — when the session ends the call would drop. We wrap
    the session in a reconnect loop keyed off the server-issued
    `session_resumption` handle (the same mechanism the browser tester
    uses), so the call survives session turnover. We only stop when the
    CALLER hangs up (carrier WsStop / WS disconnect).

    Persistence (Build 261): the conversation transcript is captured into
    a `_ConversationMemory` across reconnects and written to `calls` via
    `db.insert_call` when the call ends — so phone calls land in Call
    Logs + Outcomes like browser calls do."""
    import time
    from datetime import datetime, timezone

    agent = await db.get_agent(agent_id)
    if not agent:
        log.warning("telephony[%s]: agent %s not found", provider.name, agent_id)
        return

    connector_ids: list[str] = agent.get("connectors") or []
    # ALWAYS offer end_call — the universal system prompt instructs the model
    # to call it at wrap-up with {outcome, reason, summary, extracted,
    # sentiment, lead_quality}. Its handler (connectors.py) persists the call
    # AND fires the agent's webhook/notifications. Mirrors gemini_bridge.
    tool_ids = list(connector_ids) + (["end_call"] if "end_call" not in connector_ids else [])
    tools = build_connector_tools(tool_ids)
    memory = _ConversationMemory()
    started_at = datetime.now(timezone.utc)
    # Context the end_call connector reads off the agent dict at insert time
    # (started_at for duration, transcript, model). Kept fresh in _bridge.
    agent["_call_started_iso"] = started_at.isoformat()
    agent["_call_started_at"] = time.monotonic()
    agent["_transcript"] = []
    # Structured fields harvested from connector calls (send_email metadata,
    # book_appointment args, …) — merged into `extracted` at persist time so
    # any-industry agents log their captured variables even when end_call's
    # own extracted is sparse. See _harvest_extracted.
    agent["_extracted_extra"] = {}
    # Call recording (Build 262) — stereo WAV pair, same writer the browser
    # tester uses. Gated on the agent's recording_enabled toggle (default on).
    # The end_call connector finalizes it; the hangup fallback finalizes it
    # in _persist_call. A recording failure must NEVER break the call.
    if agent.get("recording_enabled", True):
        try:
            from .. import recordings as _rec
            token = "tel-" + started_at.isoformat().replace(":", "").replace("-", "").replace("+", "")
            writer = _rec.RecordingWriter(token, int(agent["id"]))
            if writer.open():
                agent["_recording_writer"] = writer
                agent["_recording_started_iso"] = agent["_call_started_iso"]
                log.info("telephony[%s] recording opened agent=%s token=%s",
                         provider.name, agent_id, token)
        except Exception as e:  # noqa: BLE001
            log.warning("telephony[%s] recording open failed: %s", provider.name, e)
    caller_done = asyncio.Event()          # set ONLY when the caller hangs up
    used_model = DEFAULT_MODEL
    resume_handle: Optional[str] = None
    first_session = True
    # stream_id: WsStart arrives ONCE; hold it so reconnects can still address
    # outbound audio. persisted: set once end_call has written the call row, so
    # the hangup fallback in _persist_call doesn't insert a duplicate.
    call_state: dict[str, Any] = {"stream_id": None, "persisted": False}
    # Guard against a tight reconnect loop if Gemini keeps closing instantly
    # (e.g. a config/quota problem rather than normal session turnover).
    short_sessions = 0
    MAX_SHORT_SESSIONS = 3

    client = _client()
    try:
        while not caller_done.is_set():
            config = _live_config(
                voice=agent.get("voice") or "Aoede",
                locale=agent.get("locale") or "en-US",
                system_prompt=_agent_system_prompt(agent),
                tools=tools,
                resume_handle=resume_handle,
            )
            sess_started = datetime.now(timezone.utc)
            opened = False
            new_handle: Optional[str] = None
            last_err: Exception | None = None
            for model_name in [DEFAULT_MODEL] + [m for m in FALLBACK_MODELS if m != DEFAULT_MODEL]:
                try:
                    async with client.aio.live.connect(model=model_name, config=config) as session:
                        opened = True
                        used_model = model_name
                        agent["_model_id"] = model_name  # end_call reads this at insert time
                        log.info("telephony[%s] session: agent=%s model=%s resume=%s",
                                 provider.name, agent_id, model_name, "yes" if resume_handle else "no")
                        new_handle = await _bridge(
                            ws, provider, session, agent, connector_ids,
                            memory=memory, caller_done=caller_done,
                            send_kickoff=first_session, call_state=call_state,
                        )
                        first_session = False
                        break
                except Exception as e:  # noqa: BLE001
                    last_err = e
                    msg = str(e).lower()
                    if "not found" in msg or "404" in msg or "unsupported" in msg:
                        log.warning("telephony[%s]: model %s unusable; trying next", provider.name, model_name)
                        continue
                    log.exception("telephony[%s] session failed", provider.name)
                    break
            if not opened:
                log.error("telephony[%s]: no usable model. last err=%s", provider.name, last_err)
                break
            if new_handle:
                resume_handle = new_handle
            if caller_done.is_set():
                break
            # Session ended but the caller is still on the line → reconnect.
            session_secs = (datetime.now(timezone.utc) - sess_started).total_seconds()
            short_sessions = short_sessions + 1 if session_secs < 2.0 else 0
            if short_sessions >= MAX_SHORT_SESSIONS:
                log.error("telephony[%s]: %d consecutive short sessions — giving up to avoid a tight loop",
                          provider.name, short_sessions)
                break
            log.info("telephony[%s] session ended after %.1fs; reconnecting (caller still connected)",
                     provider.name, session_secs)
    finally:
        # The model's end_call (if it fired) already wrote a rich call row
        # with outcome + extracted via the connector handler. Only fall back
        # to our transcript-derived row when it didn't (caller hung up, silence).
        if not call_state.get("persisted"):
            ended_at = datetime.now(timezone.utc)
            await _persist_call(agent, provider, memory, started_at, ended_at, used_model)
        else:
            log.info("telephony[%s]: call already persisted via end_call", provider.name)


async def _persist_call(
    agent: dict[str, Any],
    provider: TelephonyProvider,
    memory: "_ConversationMemory",
    started_at,
    ended_at,
    model_id: str,
) -> None:
    """Write the finished phone call to `calls` so it shows in Call Logs +
    Outcomes. Best-effort: a persistence failure must never bubble out of
    the call teardown.

    Outcome is rule-based for now (transcript-derived): no real two-way
    exchange → `abandoned`; otherwise `completed`. A richer, agent-
    catalogue-aware classification (matching the browser end_call tool)
    is a follow-up."""
    import json as _json

    try:
        memory.on_turn_complete()  # flush any unterminated fragments
        turns = list(memory.turns)
        duration_s = max(0.0, (ended_at - started_at).total_seconds())
        had_user = any(t.get("role") == "user" and t.get("text") for t in turns)
        had_model = any(t.get("role") == "model" and t.get("text") for t in turns)
        if not turns:
            outcome, reason, summary = (
                "abandoned", "ABANDONED",
                "Caller disconnected before any conversation.",
            )
        elif not (had_user and had_model):
            outcome, reason, summary = (
                "abandoned", "ABANDONED",
                "Caller disconnected during the greeting.",
            )
        else:
            outcome, reason, summary = (
                "completed", "COMPLETED",
                f"Phone call via {provider.display_name} — {len(turns)} turns.",
            )
        # Structured fields harvested from connector calls (industry-agnostic).
        extra = agent.get("_extracted_extra") if isinstance(agent.get("_extracted_extra"), dict) else {}
        record = {
            "agent_id": agent.get("id"),
            "started_at": started_at.isoformat(),
            "ended_at": ended_at.isoformat(),
            "duration_s": duration_s,
            "outcome": outcome,
            "reason": reason,
            "summary": summary,
            "final_message": None,
            "extracted": dict(extra) if extra else {},
            "transcript": _json.dumps(turns, ensure_ascii=False) if turns else None,
            "model_id": model_id,
            "recording_started_at": agent.get("_recording_started_iso"),
        }
        # Finalize the recording (if any) BEFORE insert so its path/size land
        # in the same row; rename the temp dir to the real call_id after.
        writer = agent.get("_recording_writer")
        if writer is not None:
            try:
                meta = writer.finalize(call_id=None)
                record["recording_path"] = meta.get("recording_path")
                record["recording_format"] = meta.get("recording_format")
                record["recording_size_bytes"] = meta.get("recording_size_bytes")
                if meta.get("recording_started_at"):
                    record["recording_started_at"] = meta["recording_started_at"].isoformat()
            except Exception as e:  # noqa: BLE001
                log.warning("telephony[%s] recording finalize failed: %s", provider.name, e)
        cid = await db.insert_call(record)
        if writer is not None and cid and record.get("recording_path"):
            try:
                from .. import recordings as _rec
                old_rel = record["recording_path"]
                new_rel = _rec.relative_path_for(int(agent["id"]), int(cid))
                old_abs = _rec.RECORDING_ROOT / old_rel
                new_abs = _rec.RECORDING_ROOT / new_rel
                if old_abs.exists() and not new_abs.exists():
                    new_abs.parent.mkdir(parents=True, exist_ok=True)
                    old_abs.rename(new_abs)
                    await db.update_call_recording_path(int(cid), new_rel)
            except Exception as e:  # noqa: BLE001
                log.warning("telephony[%s] recording rename failed: %s", provider.name, e)
        log.info("telephony[%s]: persisted call id=%s turns=%d dur=%.1fs outcome=%s rec=%s",
                 provider.name, cid, len(turns), duration_s, outcome,
                 "yes" if record.get("recording_path") else "no")
    except Exception:  # noqa: BLE001
        log.exception("telephony[%s]: insert_call failed — call not logged", provider.name)


async def _bridge(
    ws: WebSocket,
    provider: TelephonyProvider,
    session,
    agent: dict[str, Any],
    connector_ids: list[str],
    *,
    memory: "_ConversationMemory",
    caller_done: asyncio.Event,
    send_kickoff: bool,
    call_state: dict[str, Any],
) -> Optional[str]:
    """Run ONE Gemini Live session against the carrier leg. Inbound audio
    gets µ-law-decoded + upsampled to 16 kHz PCM and fed to Gemini;
    outbound 24 kHz PCM from Gemini gets downsampled to 8 kHz + µ-law-
    encoded + framed back through the provider's envelope.

    Returns the latest `session_resumption` handle so the caller's
    `run_call` loop can reopen the session if Gemini ended it (vs the
    caller hanging up, which sets `caller_done`).

    Transcript is accumulated into `memory` (input/output transcription)
    so it survives reconnects and can be persisted at call end."""
    state_in: Optional[object] = None
    state_out: Optional[object] = None
    # Seed from prior session(s) so reconnects can address outbound audio
    # even though WsStart only arrives once.
    stream_id: Optional[str] = call_state.get("stream_id")
    stop = asyncio.Event()              # this session is done (either side)
    new_handle: Optional[str] = None
    rec = agent.get("_recording_writer")   # may be None (recording off / failed)

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
                    call_state["stream_id"] = stream_id
                    log.info("telephony[%s] stream started sid=%s", provider.name, stream_id)
                    if send_kickoff:
                        await session.send_client_content(
                            turns=types.Content(role="user", parts=[types.Part(text="<call_start>")]),
                            turn_complete=True,
                        )
                elif isinstance(ev, WsMedia):
                    if not ev.ulaw:
                        continue
                    pcm16k, state_in = ulaw_to_pcm16k(ev.ulaw, state_in)
                    if rec is not None:
                        rec.write_caller(pcm16k)   # 16 kHz PCM caller channel
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
                    # Caller hung up — authoritative end of the WHOLE call.
                    caller_done.set()
                    stop.set()
                    return
        except WebSocketDisconnect:
            caller_done.set()   # carrier dropped the WS → caller is gone
            stop.set()
        except Exception as e:  # noqa: BLE001
            log.warning("telephony[%s] carrier→gemini error: %s", provider.name, e)
            stop.set()

    async def gemini_to_carrier() -> None:
        nonlocal state_out, new_handle
        wrapping_up = False   # set once end_call fired; end on next turn_complete
        try:
            async for response in session.receive():
                if stop.is_set():
                    return
                # Capture the resumption handle so a reconnect is seamless.
                sru = getattr(response, "session_resumption_update", None)
                if sru and getattr(sru, "new_handle", None):
                    new_handle = sru.new_handle
                sc = response.server_content
                if sc:
                    if sc.interrupted and stream_id:
                        clear = provider.clear_outbound(stream_id=stream_id)
                        if clear is not None:
                            await _send(ws, clear)
                    # Transcripts → conversation memory (persisted at call end).
                    it = getattr(sc, "input_transcription", None)
                    if it and getattr(it, "text", None):
                        memory.feed_input_transcription(it.text)
                    ot = getattr(sc, "output_transcription", None)
                    if ot and getattr(ot, "text", None):
                        memory.feed_output_transcription(ot.text)
                    if getattr(sc, "turn_complete", False):
                        memory.on_turn_complete()
                        if wrapping_up:
                            # The closing line after end_call has finished —
                            # end the call now (caller_done already set).
                            return
                    if sc.model_turn and sc.model_turn.parts:
                        for part in sc.model_turn.parts:
                            if part.inline_data and part.inline_data.data:
                                if rec is not None:
                                    rec.write_agent(part.inline_data.data)  # 24 kHz PCM agent channel
                                ulaw, state_out = pcm24k_to_ulaw(part.inline_data.data, state_out)
                                if not stream_id:
                                    continue
                                for chunk in chunk_ulaw(ulaw, ULAW_FRAME_BYTES):
                                    env = provider.encode_outbound_audio(
                                        stream_id=stream_id,
                                        ulaw_frame=chunk,
                                    )
                                    await _send(ws, env)
                # Gemini signalled it's about to drop the session — stop reading
                # so run_call can reconnect with the resumption handle.
                if getattr(response, "go_away", None):
                    log.info("telephony[%s] gemini go_away — will reconnect", provider.name)
                    return
                if response.tool_call and response.tool_call.function_calls:
                    for fc in response.tool_call.function_calls:
                        name, args = fc.name, fc.args or {}
                        log.info("telephony[%s] tool_call %s", provider.name, name)
                        try:
                            # end_call is always available (not gated on the
                            # agent's connector list); it persists the call +
                            # fires webhooks. Refresh the transcript so the
                            # connector captures the full conversation.
                            if name == "end_call":
                                memory.on_turn_complete()
                                agent["_transcript"] = list(memory.turns)
                                result = await handle_connector(name, args, agent)
                            elif name in CONNECTOR_DECLS and name in connector_ids:
                                # Harvest structured fields from the connector
                                # args (industry-agnostic) so the captured lead
                                # data lands in the call's `extracted`.
                                try:
                                    harvested = _harvest_extracted(args)
                                    if harvested and isinstance(agent.get("_extracted_extra"), dict):
                                        agent["_extracted_extra"].update(harvested)
                                except Exception:  # noqa: BLE001
                                    pass
                                result = await handle_connector(name, args, agent)
                            else:
                                result = {"ok": False, "error": f"connector {name} not enabled"}
                        except Exception as e:  # noqa: BLE001
                            log.exception("telephony[%s] connector %s failed", provider.name, name)
                            result = {"ok": False, "error": str(e)}
                        await session.send_tool_response(
                            function_responses=types.FunctionResponse(id=fc.id, name=name, response=result)
                        )
                        # A successful end_call means the call is wrapped: it
                        # persisted the row. Flag it (skip the fallback) and set
                        # caller_done so we never reconnect — but DON'T cut off
                        # the model's closing line; end on its turn_complete (or
                        # session end as a backstop).
                        if (name == "end_call" and isinstance(result, dict)
                                and result.get("ok") and not result.get("rejected")):
                            call_state["persisted"] = True
                            caller_done.set()
                            wrapping_up = True
        except Exception as e:  # noqa: BLE001
            log.warning("telephony[%s] gemini→carrier error: %s", provider.name, e)
        finally:
            stop.set()

    # Run both pumps; return as soon as EITHER finishes, cancelling the other.
    # (gather would wait for the carrier reader to also exit, but it's blocked
    # on receive_text() — so we race + cancel instead.)
    t_in = asyncio.create_task(carrier_to_gemini())
    t_out = asyncio.create_task(gemini_to_carrier())
    _done, pending = await asyncio.wait({t_in, t_out}, return_when=asyncio.FIRST_COMPLETED)
    stop.set()
    for t in pending:
        t.cancel()
        try:
            await t
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    return new_handle
