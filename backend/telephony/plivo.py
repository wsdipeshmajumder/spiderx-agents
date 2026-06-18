"""Plivo Audio Stream provider — Plivo XML on the webhook side, JSON
envelopes on the WebSocket side.

Plivo's WS envelope (similar to but NOT identical to Twilio's):
    {"event": "start", "start": {"streamId": "…", "callId": "…"}}
    {"event": "playedStream"}                              (ack)
    {"event": "media", "media": {"payload": "<base64 µ-law>",
                                  "track": "inbound", "timestamp": "..."}}
    {"event": "stop"}
    {"event": "dtmf", "dtmf": {"digit": "1"}}

Outbound envelope to Plivo:
    {"event": "playAudio",
     "media": {"contentType": "audio/x-mulaw", "sampleRate": 8000,
               "payload": "<base64>"}}

Plivo XML uses `<Stream bidirectional="true" keepCallAlive="true" \
contentType="audio/x-mulaw;rate=8000">` — those attrs are NOT optional.
Without `bidirectional`, audio flows one-way (caller hears nothing).
"""
from __future__ import annotations

import base64
import json
import logging
from typing import Any, Optional

from .base import (
    TelephonyAuthError,
    TelephonyProvider,
    WsDtmf,
    WsEvent,
    WsMedia,
    WsStart,
    WsStop,
)

log = logging.getLogger("eva.telephony.plivo")


class PlivoProvider(TelephonyProvider):
    name = "plivo"
    display_name = "Plivo"
    auto_provision_supported = True

    # ── Webhook side ────────────────────────────────────────────────

    def answer_xml(self, *, stream_url: str, agent: dict[str, Any]) -> tuple[str, str]:
        body = (
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
            "<Response>\n"
            "  <Stream bidirectional=\"true\" keepCallAlive=\"true\" "
            "contentType=\"audio/x-mulaw;rate=8000\">"
            f"{stream_url}</Stream>\n"
            "</Response>"
        )
        return body, "application/xml"

    def fallback_xml(self, *, agent: Optional[dict[str, Any]] = None) -> tuple[str, str]:
        body = (
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
            "<Response>\n"
            "  <Speak>We're having trouble connecting your call. "
            "Please try again in a moment.</Speak>\n"
            "  <Hangup/>\n"
            "</Response>"
        )
        return body, "application/xml"

    # ── WS adapter ──────────────────────────────────────────────────

    def parse_ws_message(self, raw: str) -> Optional[WsEvent]:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        ev = data.get("event")
        if ev == "start":
            start = data.get("start") or {}
            return WsStart(
                stream_id=start.get("streamId") or start.get("stream_id") or "",
                call_id=start.get("callId") or start.get("call_id"),
                extra=start,
            )
        if ev == "media":
            payload = ((data.get("media") or {}).get("payload")) or ""
            if not payload:
                return None
            try:
                return WsMedia(ulaw=base64.b64decode(payload))
            except Exception:  # noqa: BLE001
                return None
        if ev == "dtmf":
            digit = ((data.get("dtmf") or {}).get("digit")) or ""
            if not digit:
                return None
            return WsDtmf(digit=digit)
        if ev == "stop":
            return WsStop()
        # playedStream / mediaFormat / etc. — ignore.
        return None

    def encode_outbound_audio(self, *, stream_id: str, ulaw_frame: bytes) -> dict[str, Any]:
        return {
            "event": "playAudio",
            "media": {
                "contentType": "audio/x-mulaw",
                "sampleRate": 8000,
                "payload": base64.b64encode(ulaw_frame).decode("ascii"),
            },
        }

    def clear_outbound(self, *, stream_id: str) -> Optional[dict[str, Any]]:
        # Plivo's interrupt verb. Skips buffered outbound audio.
        return {"event": "clearAudio"}

    # ── Hangup webhook ──────────────────────────────────────────────

    def parse_hangup_webhook(self, form: dict[str, Any]) -> dict[str, Any]:
        # Plivo Hangup URL POST: CallUUID, Duration, HangupCause, To, From, …
        return {
            "call_id": form.get("CallUUID") or form.get("call_uuid") or "",
            "duration_seconds": _to_int(form.get("Duration") or form.get("BillDuration")),
            "hangup_cause": form.get("HangupCause") or "",
            "raw": dict(form),
        }

    # ── Auto-provision (Plivo REST) ─────────────────────────────────

    async def verify_creds(self, creds: dict[str, str]) -> dict[str, Any]:
        import httpx
        auth_id, auth_token = _plivo_creds(creds)
        url = f"https://api.plivo.com/v1/Account/{auth_id}/"
        try:
            async with httpx.AsyncClient(timeout=10.0) as c:
                r = await c.get(url, auth=(auth_id, auth_token))
        except Exception as e:  # noqa: BLE001
            raise TelephonyAuthError(f"Couldn't reach Plivo ({e})") from e
        if r.status_code == 401:
            raise TelephonyAuthError("Plivo rejected those credentials — check Auth ID + Auth Token.")
        if r.status_code >= 400:
            raise TelephonyAuthError(f"Plivo returned HTTP {r.status_code}.")
        body = r.json()
        return {
            "ok": True,
            "account_name": body.get("name") or auth_id,
            "balance": body.get("cash_credits"),
            "currency": "USD",
        }

    async def list_numbers(self, creds: dict[str, str]) -> list[dict[str, Any]]:
        import httpx
        auth_id, auth_token = _plivo_creds(creds)
        url = f"https://api.plivo.com/v1/Account/{auth_id}/Number/"
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.get(url, auth=(auth_id, auth_token), params={"limit": "100"})
        r.raise_for_status()
        body = r.json()
        out: list[dict[str, Any]] = []
        for n in body.get("objects") or []:
            num = n.get("number") or ""
            if num and not num.startswith("+"):
                num = "+" + num
            out.append({
                "number": num,
                "country": n.get("country_iso2") or n.get("country") or "",
                "type": (n.get("type") or "local").lower(),
                "alias": n.get("alias") or "",
                "current_app_id": n.get("application") or None,
                "current_voice_url": None,  # Plivo doesn't echo URLs on Number listing
                "sid": None,
                "region": n.get("region") or "",
            })
        return out

    async def create_application(
        self, *,
        creds: dict[str, str],
        name: str,
        answer_url: str,
        hangup_url: str,
        fallback_url: str,
    ) -> dict[str, Any]:
        import httpx
        auth_id, auth_token = _plivo_creds(creds)
        url = f"https://api.plivo.com/v1/Account/{auth_id}/Application/"
        data = {
            "app_name": name,
            "answer_url": answer_url,
            "answer_method": "POST",
            "hangup_url": hangup_url,
            "hangup_method": "POST",
            "fallback_answer_url": fallback_url,
            "fallback_method": "POST",
            "default_number_app": False,
        }
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.post(url, auth=(auth_id, auth_token), json=data)
        if r.status_code >= 400:
            try:
                msg = (r.json() or {}).get("error") or f"HTTP {r.status_code}"
            except Exception:  # noqa: BLE001
                msg = f"HTTP {r.status_code}"
            raise TelephonyAuthError(f"Plivo refused to create the Application: {msg}.")
        body = r.json()
        return {
            "app_id": str(body.get("app_id") or body.get("api_id") or ""),
            "app_name": name,
        }

    async def bind_number(
        self, *,
        creds: dict[str, str],
        number: str,
        app_id: str,
        alias: str = "",
    ) -> dict[str, Any]:
        import httpx
        auth_id, auth_token = _plivo_creds(creds)
        # Plivo number resource uses the bare number (no leading +) in the
        # path. Mirror what their dashboard does.
        bare = number.lstrip("+")
        url = f"https://api.plivo.com/v1/Account/{auth_id}/Number/{bare}/"
        data: dict[str, str] = {"app_id": app_id}
        if alias:
            data["alias"] = alias
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.post(url, auth=(auth_id, auth_token), json=data)
        if r.status_code >= 400:
            try:
                msg = (r.json() or {}).get("error") or f"HTTP {r.status_code}"
            except Exception:  # noqa: BLE001
                msg = f"HTTP {r.status_code}"
            raise TelephonyAuthError(f"Plivo refused to bind the number: {msg}.")
        return {"ok": True}

    async def read_number_config(
        self, *,
        creds: dict[str, str],
        number: str,
    ) -> dict[str, Any]:
        import httpx
        auth_id, auth_token = _plivo_creds(creds)
        bare = number.lstrip("+")
        url = f"https://api.plivo.com/v1/Account/{auth_id}/Number/{bare}/"
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(url, auth=(auth_id, auth_token))
        if r.status_code >= 400:
            return {}
        body = r.json()
        return {
            "app_id": str(body.get("application") or "") or None,
            "alias": body.get("alias"),
            "answer_url": None,  # Plivo binds via app_id, not URL on the Number row
        }


def _plivo_creds(creds: dict[str, str]) -> tuple[str, str]:
    auth_id = (creds.get("auth_id") or "").strip()
    auth_token = (creds.get("auth_token") or "").strip()
    if not auth_id or not auth_token:
        raise TelephonyAuthError("Plivo Auth ID and Auth Token are required.")
    return auth_id, auth_token


def _to_int(v: Any) -> Optional[int]:
    try:
        return int(float(v)) if v is not None else None
    except (TypeError, ValueError):
        return None
