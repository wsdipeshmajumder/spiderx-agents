"""GeminiHandler — bridges a SIP call's RTP audio to a live Gemini agent.

This is the RTP-native twin of `telephony/base.py::_bridge` (which is WebSocket-
native for Twilio/Plivo). It reuses ALL the real agent machinery unchanged —
`_live_config`, `_agent_system_prompt`, the connector tools, the model-fallback
list — and only swaps the transport: instead of µ-law-over-WebSocket it consumes
`session.inbound_q` (PCM8k from RTP) and produces `session.outbound_q` (PCM8k to
RTP). The MediaSession owns the G.711 codec, so here we only resample:
  caller → Gemini:  PCM 8 kHz → 16 kHz     (send_realtime_input)
  Gemini → caller:  PCM 24 kHz → 8 kHz      (chunked to 20 ms frames)

Barge-in (server_content.interrupted) flushes the outbound queue so the agent
stops talking the instant the caller does — same behaviour as the carrier path.

⚠️ UNVERIFIED LIVE: everything below the transport is exercised by the existing
carrier tests, but this exact wiring can only be confirmed by a real call into a
real Gemini Live session (needs GOOGLE/GEMINI creds + audio). The SIP+RTP layer
under it is fully loopback-proven. Reconnect/resume, recording, and a server-
initiated BYE on the `end_call` tool are the known follow-ups (see notes inline).
"""
from __future__ import annotations

import asyncio
import audioop
import logging

from google.genai import types

from .. import db
from ..connectors import build_tools as build_connector_tools, handle as handle_connector
from ..gemini_bridge import (
    DEFAULT_MODEL, FALLBACK_MODELS, _agent_system_prompt, _client, _live_config,
)
from . import g711
from .media import MediaHandler, MediaSession

log = logging.getLogger("sip.gemini")

_OUT_FRAME_BYTES = g711.FRAME_BYTES * 2   # 160 samples × 2 bytes = one 20 ms PCM8k frame


class GeminiHandler(MediaHandler):
    def __init__(self, agent_id: int):
        self.agent_id = agent_id
        self._end = asyncio.Event()

    async def run(self, session: MediaSession):
        agent = await db.get_agent(self.agent_id)
        if not agent:
            log.error("SIP call for unknown agent %s — dropping media", self.agent_id)
            return

        connector_ids = agent.get("connectors") or []
        tool_ids = list(connector_ids) + (["end_call"] if "end_call" not in connector_ids else [])
        tools = build_connector_tools(tool_ids)
        config = _live_config(
            voice=agent.get("voice") or "Aoede",
            locale=agent.get("locale") or "en-US",
            system_prompt=_agent_system_prompt(agent),
            tools=tools,
        )
        client = _client()

        # Model fallback mirrors run_call: try the default, then alternates.
        for model_name in [DEFAULT_MODEL] + [m for m in FALLBACK_MODELS if m != DEFAULT_MODEL]:
            try:
                async with client.aio.live.connect(model=model_name, config=config) as gsession:
                    agent["_model_id"] = model_name       # end_call reads this at insert time
                    log.info("SIP↔Gemini up: agent=%s model=%s rtp=%s",
                             self.agent_id, model_name, session.local_port)
                    await self._pump(session, gsession, agent)
                return
            except Exception as e:                        # noqa: BLE001
                m = str(e).lower()
                if "not found" in m or "404" in m or "unsupported" in m:
                    log.warning("model %s unusable; trying next", model_name)
                    continue
                log.exception("SIP Gemini session failed")
                return

    async def _pump(self, session: MediaSession, gsession, agent):
        in_state = None            # audioop resample state, caller→Gemini (8k→16k)
        out_state = None           # audioop resample state, Gemini→caller (24k→8k)
        out_carry = b""            # leftover PCM8k not yet aligned to a 20 ms frame

        async def caller_to_gemini():
            nonlocal in_state
            while not self._end.is_set():
                pcm8k = await session.inbound_q.get()
                pcm16k, in_state = audioop.ratecv(pcm8k, 2, 1, g711.CLOCK_HZ, 16000, in_state)
                try:
                    await gsession.send_realtime_input(
                        audio=types.Blob(data=pcm16k, mime_type="audio/pcm;rate=16000"))
                except Exception:                         # session closed under us
                    self._end.set()
                    return

        async def gemini_to_caller():
            nonlocal out_state, out_carry
            wrapping_up = False
            async for response in gsession.receive():
                sc = getattr(response, "server_content", None)
                if sc is not None:
                    # Barge-in: caller started talking → drop everything queued so
                    # the agent goes quiet immediately.
                    if getattr(sc, "interrupted", False):
                        _flush(session.outbound_q)
                        out_carry = b""
                    mt = getattr(sc, "model_turn", None)
                    for part in (getattr(mt, "parts", None) or []):
                        inline = getattr(part, "inline_data", None)
                        if inline and inline.data:
                            pcm8k, out_state = audioop.ratecv(inline.data, 2, 1, 24000, g711.CLOCK_HZ, out_state)
                            out_carry += pcm8k
                            while len(out_carry) >= _OUT_FRAME_BYTES:
                                frame, out_carry = out_carry[:_OUT_FRAME_BYTES], out_carry[_OUT_FRAME_BYTES:]
                                try:
                                    session.outbound_q.put_nowait(frame)
                                except asyncio.QueueFull:
                                    pass
                    if getattr(sc, "turn_complete", False) and wrapping_up:
                        self._end.set()
                        return

                tc = getattr(response, "tool_call", None)
                if tc and tc.function_calls:
                    for fc in tc.function_calls:
                        result = await handle_connector(fc.name, dict(fc.args or {}), agent)
                        try:
                            await gsession.send_tool_response(function_responses=[
                                types.FunctionResponse(name=fc.name, id=fc.id,
                                                       response=result if isinstance(result, dict) else {"result": result})])
                        except Exception:
                            pass
                        if fc.name == "end_call":
                            # Let the model speak its closing line, then end on the
                            # next turn_complete. NOTE (M5): we should also send a
                            # UAC BYE to the caller here — the server is UAS-only
                            # today, so for now the caller's own hang-up ends it.
                            wrapping_up = True

        t_in = asyncio.ensure_future(caller_to_gemini())
        t_out = asyncio.ensure_future(gemini_to_caller())
        try:
            await self._end.wait()
        finally:
            for t in (t_in, t_out):
                t.cancel()


def _flush(q: asyncio.Queue):
    while True:
        try:
            q.get_nowait()
        except asyncio.QueueEmpty:
            return


# Factory for SipServer: resolve the agent id from the INVITE's request-URI
# user-part. The existing sip_config.inbound_uri_for() already mints
# `sip:agent-<id>@host`, so pointing the UCM route at that URI dispatches here
# with zero DB lookup. Falls back to a configured default agent id.
import re as _re
_AGENT_RE = _re.compile(r"agent-(\d+)")


def gemini_factory(default_agent_id: int | None = None):
    def factory(agent_id, invite, session: MediaSession) -> MediaHandler | None:
        # agent_id is set when a DB directory resolved the call; else fall back to
        # an `agent-<id>` user-part in the URI, then the env default.
        if agent_id is None:
            m = _AGENT_RE.search(invite.request_uri or "") or _AGENT_RE.search(invite.get("to") or "")
            agent_id = int(m.group(1)) if m else default_agent_id
        if agent_id is None:
            log.error("INVITE %s: cannot resolve agent (no agent-<id> in URI, no default)", invite.call_id)
            return None
        return GeminiHandler(agent_id)
    return factory
