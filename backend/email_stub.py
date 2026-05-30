"""Email delivery — pluggable provider with a log-only default.

`EMAIL_PROVIDER` env selects the transport:
  - unset / "log"   → log the message + return (default; dev)
  - "postmark"      → POST to Postmark transactional API; needs POSTMARK_TOKEN
  - "resend"        → POST to Resend API; needs RESEND_API_KEY

The provider switch is intentionally tiny — we POST a single fixed JSON
shape to each. When we need richer templating (variables, locale,
attachments), the rewrite lives here and call sites stay unchanged.

All sends are best-effort: a provider failure logs a warning, never
raises. Inviting a teammate shouldn't 500 because Postmark is briefly
unavailable — the link is also displayed in the inviter's UI, so the
fallback path is "copy and paste".
"""
from __future__ import annotations

import json
import logging
import os
from typing import Optional
from urllib import request as urlrequest

log = logging.getLogger("eva.email")


def _public_base_url() -> str:
    """The URL users see in emails. Reads PUBLIC_BASE_URL env so prod and
    Railway swap cleanly; defaults to localhost for dev."""
    return (os.environ.get("PUBLIC_BASE_URL") or "http://localhost:8765").rstrip("/")


def _provider() -> str:
    return (os.environ.get("EMAIL_PROVIDER") or "log").lower()


def _from_address() -> str:
    return (os.environ.get("EMAIL_FROM") or "noreply@spiderx.ai")


async def _send(to: str, subject: str, body: str) -> None:
    """The single chokepoint. Selects a provider, never raises."""
    provider = _provider()
    try:
        if provider == "postmark":
            _send_postmark(to, subject, body)
        elif provider == "resend":
            _send_resend(to, subject, body)
        else:
            log.info("email.send to=%s subject=%r\n%s", to, subject, body)
    except Exception as e:  # noqa: BLE001
        log.warning("email.send_failed provider=%s to=%s err=%s", provider, to, e)


def _post_json(url: str, headers: dict, payload: dict) -> dict:
    """Synchronous POST — emails are slow + low-volume relative to the
    voice loop; not worth pulling in httpx for an httpurlopen-shaped
    feature. Wrapped in a thread by callers that care."""
    data = json.dumps(payload).encode("utf-8")
    hdrs = {"Content-Type": "application/json", **headers}
    req = urlrequest.Request(url, data=data, headers=hdrs, method="POST")
    with urlrequest.urlopen(req, timeout=10) as resp:
        return {"status": resp.status, "body": resp.read(2048).decode("utf-8", "replace")}


def _send_postmark(to: str, subject: str, body: str) -> None:
    token = os.environ.get("POSTMARK_TOKEN")
    if not token:
        log.warning("postmark.token_missing; falling back to log")
        log.info("email.send to=%s subject=%r\n%s", to, subject, body)
        return
    r = _post_json(
        "https://api.postmarkapp.com/email",
        {"Accept": "application/json", "X-Postmark-Server-Token": token},
        {"From": _from_address(), "To": to, "Subject": subject,
         "TextBody": body, "MessageStream": "outbound"},
    )
    log.info("postmark.sent to=%s status=%s", to, r["status"])


def _send_resend(to: str, subject: str, body: str) -> None:
    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        log.warning("resend.api_key_missing; falling back to log")
        log.info("email.send to=%s subject=%r\n%s", to, subject, body)
        return
    r = _post_json(
        "https://api.resend.com/emails",
        {"Authorization": f"Bearer {api_key}"},
        {"from": _from_address(), "to": [to], "subject": subject, "text": body},
    )
    log.info("resend.sent to=%s status=%s", to, r["status"])


async def send_invite_email(to: str, inviter_name: Optional[str],
                             org_name: str, role: str, token: str) -> None:
    accept_url = f"{_public_base_url()}/invite/{token}"
    inviter = inviter_name or "A SpiderX.AI teammate"
    subject = f"{inviter} invited you to {org_name} on SpiderX.AI"
    body = (
        f"Hi,\n\n"
        f"{inviter} has invited you to join {org_name} on SpiderX.AI as a {role}.\n\n"
        f"Accept here (link expires in 7 days):\n"
        f"  {accept_url}\n\n"
        f"If you weren't expecting this, ignore the email — the invite will\n"
        f"expire on its own.\n\n"
        f"— SpiderX.AI"
    )
    await _send(to, subject, body)


# ─── post-call summary email ─────────────────────────────────────────────
# Fired by the end_call connector after the calls row + llm_calls row land.
# Phase 8 promised "after every call, a confirmation goes out" — this is
# that delivery. SMS is gated by `agent.purpose.post_call.sms` AND the
# org's plan tier (Free → SMS off regardless of the flag).

def _fmt_duration(seconds: float) -> str:
    s = int(seconds or 0)
    if s < 60:
        return f"{s}s"
    return f"{s // 60}m {s % 60}s"


def _fmt_extracted(extracted: dict | None) -> str:
    if not extracted or not isinstance(extracted, dict):
        return ""
    rows = []
    for k, v in extracted.items():
        if v in (None, ""):
            continue
        rows.append(f"  {k}: {v}")
    if not rows:
        return ""
    return "Captured details:\n" + "\n".join(rows) + "\n\n"


async def send_call_summary_email(
    to: str, agent_name: str, org_name: str, *,
    outcome: str | None, sentiment: str | None, lead_quality: str | None,
    lead_signals: str | None, summary: str | None, duration_s: float,
    extracted: dict | None, call_id: int | None,
) -> None:
    """Email the operator a one-glance summary of a finished call.

    The subject line is the punchy bit — designed to be scannable in an
    inbox without opening: agent + outcome + lead temperature. The body
    is plain-text first; HTML lands when the real provider is wired.
    """
    parts: list[str] = []
    if outcome:      parts.append(outcome.replace("_", " "))
    if lead_quality: parts.append(f"{lead_quality.upper()} lead")
    if sentiment:    parts.append(sentiment)
    tag = " · ".join(parts) if parts else "call ended"
    subject = f"[{agent_name}] {tag} — {_fmt_duration(duration_s)}"

    body = (
        f"A call to {agent_name} ({org_name}) just wrapped up.\n\n"
        f"Outcome:    {outcome or '—'}\n"
        f"Sentiment:  {sentiment or '—'}\n"
        f"Lead:       {(lead_quality or '—').upper()}\n"
        f"Duration:   {_fmt_duration(duration_s)}\n\n"
    )
    if lead_signals:
        body += f"Why: {lead_signals}\n\n"
    if summary:
        body += f"Summary:\n{summary}\n\n"
    body += _fmt_extracted(extracted)
    if call_id:
        body += f"Call ID: {call_id}\n"
    body += (
        "\n"
        "You can review the full call log + transcript in your dashboard.\n\n"
        "— SpiderX.AI"
    )
    await _send(to, subject, body)


async def send_call_summary_sms(
    to: str, agent_name: str, *,
    outcome: str | None, lead_quality: str | None, duration_s: float,
) -> None:
    """160-char-ish SMS summary. Plan-gated — caller must verify tier
    BEFORE invoking this (we don't reach into the DB from here)."""
    bits = [agent_name]
    if outcome:      bits.append(outcome.replace("_", " "))
    if lead_quality: bits.append(f"{lead_quality.upper()} lead")
    bits.append(_fmt_duration(duration_s))
    msg = " · ".join(bits)
    # For Phase 8 the log path also handles SMS — when a real provider
    # (Twilio, MSG91) is wired, swap the `_send` call for an SMS POST.
    await _send(to, "[call] " + msg[:80], msg)
