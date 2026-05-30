"""Twilio Media Streams ↔ Gemini Live bridge.

Twilio sends inbound audio as base64-encoded µ-law 8 kHz mono in JSON envelopes
on a WebSocket. We:
  1. Decode + resample to PCM16 16 kHz, feed to Gemini Live.
  2. Receive PCM16 24 kHz from Gemini, downsample to 8 kHz, encode µ-law,
     base64 it back into a Twilio JSON envelope.

Saved agent's connectors are wired the same way as the browser path so the
phone caller gets the same end-to-end behaviour as the in-browser tester.

Setup
─────
    PUBLIC_HOST=your-ngrok-host.ngrok-free.app  (no scheme, no path)
    # point a Twilio Voice number's webhook at:
    # https://<PUBLIC_HOST>/api/sip/twilio/twiml/<agent_id>
"""

from __future__ import annotations

import asyncio
import audioop
import base64
import json
import logging
import os
from typing import Any, Optional

from fastapi import WebSocket, WebSocketDisconnect
from google import genai
from google.genai import types

from . import db
from .connectors import CONNECTOR_DECLS, build_tools as build_connector_tools, handle as handle_connector
from .gemini_bridge import (
    DEFAULT_MODEL,
    FALLBACK_MODELS,
    _agent_system_prompt,
    _live_config,
)

log = logging.getLogger("eva.twilio")

TWILIO_FRAME_BYTES = 160  # 20 ms of µ-law 8 kHz


def _client() -> genai.Client:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY env var is not set")
    return genai.Client(api_key=api_key)


async def _send_twilio(ws: WebSocket, payload: dict[str, Any]) -> None:
    try:
        await ws.send_text(json.dumps(payload))
    except Exception:
        pass


async def run_twilio_call(ws: WebSocket, agent_id: int) -> None:
    agent = db.get_agent(agent_id)
    if not agent:
        log.warning("twilio: agent %s not found", agent_id)
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
                log.info("twilio call: agent=%s model=%s", agent_id, model_name)
                await _bridge(ws, session, agent, connector_ids)
                return
        except Exception as e:  # noqa: BLE001
            last_err = e
            msg = str(e).lower()
            if "not found" in msg or "404" in msg or "unsupported" in msg:
                log.warning("twilio: model %s unusable; trying next", model_name)
                continue
            log.exception("twilio session failed")
            return
    log.error("twilio: no usable model. last err=%s", last_err)


async def _bridge(ws: WebSocket, session, agent: dict[str, Any], connector_ids: list[str]) -> None:
    state_in: Optional[Any] = None
    state_out: Optional[Any] = None
    stream_sid: Optional[str] = None
    stop = asyncio.Event()

    # kick the agent into speaking first when Twilio's stream starts
    kickoff_sent = asyncio.Event()

    async def pump_twilio_to_gemini() -> None:
        nonlocal state_in, stream_sid
        try:
            while not stop.is_set():
                msg = await ws.receive_text()
                try:
                    data = json.loads(msg)
                except json.JSONDecodeError:
                    continue
                ev = data.get("event")
                if ev == "start":
                    stream_sid = (data.get("start") or {}).get("streamSid")
                    log.info("twilio stream started sid=%s", stream_sid)
                    if not kickoff_sent.is_set():
                        await session.send_client_content(
                            turns=types.Content(role="user", parts=[types.Part(text="<call_start>")]),
                            turn_complete=True,
                        )
                        kickoff_sent.set()
                elif ev == "media":
                    payload_b64 = (data.get("media") or {}).get("payload")
                    if not payload_b64:
                        continue
                    ulaw = base64.b64decode(payload_b64)
                    lin8 = audioop.ulaw2lin(ulaw, 2)
                    lin16, state_in = audioop.ratecv(lin8, 2, 1, 8000, 16000, state_in)
                    await session.send_realtime_input(
                        audio=types.Blob(data=lin16, mime_type="audio/pcm;rate=16000")
                    )
                elif ev == "stop":
                    stop.set()
                    return
                elif ev == "mark":
                    pass
        except WebSocketDisconnect:
            stop.set()
        except Exception as e:  # noqa: BLE001
            log.warning("twilio→gemini error: %s", e)
            stop.set()

    async def pump_gemini_to_twilio() -> None:
        nonlocal state_out
        try:
            async for response in session.receive():
                if stop.is_set():
                    return
                sc = response.server_content
                if sc:
                    if sc.interrupted and stream_sid:
                        await _send_twilio(ws, {"event": "clear", "streamSid": stream_sid})
                    if sc.model_turn and sc.model_turn.parts:
                        for part in sc.model_turn.parts:
                            if part.inline_data and part.inline_data.data:
                                pcm24 = part.inline_data.data
                                pcm8, state_out = audioop.ratecv(pcm24, 2, 1, 24000, 8000, state_out)
                                ulaw = audioop.lin2ulaw(pcm8, 2)
                                # split into ~20 ms frames Twilio is happiest with
                                for i in range(0, len(ulaw), TWILIO_FRAME_BYTES):
                                    chunk = ulaw[i:i + TWILIO_FRAME_BYTES]
                                    if not stream_sid:
                                        continue
                                    await _send_twilio(ws, {
                                        "event": "media",
                                        "streamSid": stream_sid,
                                        "media": {"payload": base64.b64encode(chunk).decode("ascii")},
                                    })
                if response.tool_call and response.tool_call.function_calls:
                    for fc in response.tool_call.function_calls:
                        name, args = fc.name, fc.args or {}
                        log.info("twilio tool_call %s", name)
                        try:
                            if name in CONNECTOR_DECLS and name in connector_ids:
                                result = await handle_connector(name, args, agent)
                            else:
                                result = {"ok": False, "error": f"connector {name} not enabled"}
                        except Exception as e:  # noqa: BLE001
                            log.exception("twilio connector %s failed", name)
                            result = {"ok": False, "error": str(e)}
                        await session.send_tool_response(
                            function_responses=types.FunctionResponse(id=fc.id, name=name, response=result)
                        )
        except Exception as e:  # noqa: BLE001
            log.warning("gemini→twilio error: %s", e)
        finally:
            stop.set()

    await asyncio.gather(pump_twilio_to_gemini(), pump_gemini_to_twilio())
