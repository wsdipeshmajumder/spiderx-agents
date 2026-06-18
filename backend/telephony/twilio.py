"""Twilio Media Streams provider — TwiML on the webhook side, JSON
envelopes on the WebSocket side.

Twilio's WS envelope:
    {"event": "start", "start": {"streamSid": "MZ…", "callSid": "CA…"}}
    {"event": "media", "media": {"payload": "<base64 µ-law>", "track": "inbound"}}
    {"event": "stop"}
    {"event": "dtmf", "dtmf": {"digit": "1"}}      # if dtmf-in-band enabled
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

log = logging.getLogger("eva.telephony.twilio")


class TwilioProvider(TelephonyProvider):
    name = "twilio"
    display_name = "Twilio"
    auto_provision_supported = True  # Twilio REST: POST Applications, IncomingPhoneNumbers

    # ── Webhook side ────────────────────────────────────────────────

    def answer_xml(self, *, stream_url: str, agent: dict[str, Any]) -> tuple[str, str]:
        body = (
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
            "<Response>\n"
            "  <Connect>\n"
            f"    <Stream url=\"{stream_url}\" />\n"
            "  </Connect>\n"
            "</Response>"
        )
        return body, "application/xml"

    def fallback_xml(self, *, agent: Optional[dict[str, Any]] = None) -> tuple[str, str]:
        body = (
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
            "<Response>\n"
            "  <Say>We're having trouble connecting your call. "
            "Please try again in a moment.</Say>\n"
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
                stream_id=start.get("streamSid") or "",
                call_id=start.get("callSid"),
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
        if ev == "mark":
            # We don't currently use marks, but it's not an error.
            return None
        return None

    def encode_outbound_audio(self, *, stream_id: str, ulaw_frame: bytes) -> dict[str, Any]:
        return {
            "event": "media",
            "streamSid": stream_id,
            "media": {"payload": base64.b64encode(ulaw_frame).decode("ascii")},
        }

    def clear_outbound(self, *, stream_id: str) -> Optional[dict[str, Any]]:
        return {"event": "clear", "streamSid": stream_id}

    # ── Hangup webhook ──────────────────────────────────────────────

    def parse_hangup_webhook(self, form: dict[str, Any]) -> dict[str, Any]:
        # Twilio Status Callback fields:
        #   CallSid, CallDuration, CallStatus, To, From, AccountSid, …
        return {
            "call_id": form.get("CallSid") or "",
            "duration_seconds": _to_int(form.get("CallDuration")),
            "hangup_cause": form.get("CallStatus") or "",
            "raw": dict(form),
        }

    # ── Auto-provision (Twilio REST) ────────────────────────────────
    #
    # Twilio's REST API uses Basic auth: account SID + auth token. Auth-key
    # based flows are also possible (SK-prefixed keys with scopes) — that's
    # the stronger model long-term. For now, support the dashboard-default
    # Auth Token flow.

    async def verify_creds(self, creds: dict[str, str]) -> dict[str, Any]:
        import httpx
        sid, token = _twilio_creds(creds)
        url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}.json"
        try:
            async with httpx.AsyncClient(timeout=10.0) as c:
                r = await c.get(url, auth=(sid, token))
        except Exception as e:  # noqa: BLE001
            raise TelephonyAuthError(f"Couldn't reach Twilio ({e})") from e
        if r.status_code == 401:
            raise TelephonyAuthError("Twilio rejected those credentials — check Account SID + Auth Token.")
        if r.status_code >= 400:
            raise TelephonyAuthError(f"Twilio returned HTTP {r.status_code}.")
        body = r.json()
        return {
            "ok": True,
            "account_name": body.get("friendly_name") or sid,
            "balance": None,  # separate /Balance endpoint; skip for v1
            "currency": None,
        }

    async def list_numbers(self, creds: dict[str, str]) -> list[dict[str, Any]]:
        import httpx
        sid, token = _twilio_creds(creds)
        url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/IncomingPhoneNumbers.json"
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.get(url, auth=(sid, token), params={"PageSize": "100"})
        r.raise_for_status()
        body = r.json()
        out: list[dict[str, Any]] = []
        for n in body.get("incoming_phone_numbers") or []:
            out.append({
                "number": n.get("phone_number") or "",
                "country": (n.get("phone_number") or "")[:2] if (n.get("phone_number") or "").startswith("+") else "",
                "type": "local",
                "alias": n.get("friendly_name") or "",
                "current_app_id": n.get("voice_application_sid") or None,
                "current_voice_url": n.get("voice_url") or None,
                "sid": n.get("sid"),
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
        sid, token = _twilio_creds(creds)
        url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Applications.json"
        data = {
            "FriendlyName": name,
            "VoiceUrl": answer_url,
            "VoiceMethod": "POST",
            "VoiceFallbackUrl": fallback_url,
            "VoiceFallbackMethod": "POST",
            "StatusCallback": hangup_url,
            "StatusCallbackMethod": "POST",
        }
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.post(url, auth=(sid, token), data=data)
        if r.status_code >= 400:
            raise TelephonyAuthError(f"Twilio refused to create the Application: HTTP {r.status_code}.")
        body = r.json()
        return {"app_id": body.get("sid") or "", "app_name": body.get("friendly_name") or name}

    async def bind_number(
        self, *,
        creds: dict[str, str],
        number: str,
        app_id: str,
        alias: str = "",
    ) -> dict[str, Any]:
        # Bind via the IncomingPhoneNumbers resource — needs the number's SID
        # not the E.164. Resolve by listing and matching.
        nums = await self.list_numbers(creds)
        match = next((n for n in nums if n.get("number") == number), None)
        if not match or not match.get("sid"):
            raise TelephonyAuthError(f"Couldn't find {number} in your Twilio account.")
        import httpx
        sid, token = _twilio_creds(creds)
        url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/IncomingPhoneNumbers/{match['sid']}.json"
        data: dict[str, str] = {"VoiceApplicationSid": app_id}
        if alias:
            data["FriendlyName"] = alias
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.post(url, auth=(sid, token), data=data)
        if r.status_code >= 400:
            raise TelephonyAuthError(f"Twilio refused to bind the number: HTTP {r.status_code}.")
        return {"ok": True}

    async def read_number_config(
        self, *,
        creds: dict[str, str],
        number: str,
    ) -> dict[str, Any]:
        nums = await self.list_numbers(creds)
        match = next((n for n in nums if n.get("number") == number), None)
        if not match:
            return {}
        return {
            "app_id": match.get("current_app_id"),
            "alias": match.get("alias"),
            "answer_url": match.get("current_voice_url"),
        }

    async def place_outbound_call(
        self, *,
        creds: dict[str, str],
        from_number: str,
        to_number: str,
        answer_url: str,
    ) -> dict[str, Any]:
        import httpx
        sid, token = _twilio_creds(creds)
        url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Calls.json"
        data = {
            "From": from_number,
            "To": to_number,
            "Url": answer_url,      # TwiML fetched when the callee answers
            "Method": "POST",
        }
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.post(url, auth=(sid, token), data=data)
        if r.status_code >= 400:
            try:
                msg = (r.json() or {}).get("message") or f"HTTP {r.status_code}"
            except Exception:  # noqa: BLE001
                msg = f"HTTP {r.status_code}"
            raise TelephonyAuthError(f"Twilio refused to place the call: {msg}.")
        body = r.json() if r.content else {}
        return {"ok": True, "call_id": body.get("sid")}


def _twilio_creds(creds: dict[str, str]) -> tuple[str, str]:
    sid = (creds.get("auth_id") or creds.get("account_sid") or "").strip()
    token = (creds.get("auth_token") or "").strip()
    if not sid or not token:
        raise TelephonyAuthError("Twilio Account SID and Auth Token are required.")
    return sid, token


def _to_int(v: Any) -> Optional[int]:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None
