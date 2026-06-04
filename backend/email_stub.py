"""Email delivery — pluggable provider with a log-only default.

`EMAIL_PROVIDER` env selects the transport:
  - unset / "log"   → log the message + return (default; dev)
  - "gmail"         → SMTP over Gmail; needs EMAIL_USER + EMAIL_PWD (App Password)
  - "postmark"      → POST to Postmark transactional API; needs POSTMARK_TOKEN
  - "resend"        → POST to Resend API; needs RESEND_API_KEY

All sends are best-effort: a provider failure logs a warning, never raises.
"""
from __future__ import annotations

import html
import json
import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from typing import Optional
from urllib import request as urlrequest

log = logging.getLogger("eva.email")


# ─── public-config helpers ───────────────────────────────────────────────


def _public_base_url() -> str:
    """The URL users see in emails. Reads PUBLIC_BASE_URL env so prod and
    Railway swap cleanly; defaults to localhost for dev."""
    return (os.environ.get("PUBLIC_BASE_URL") or "http://localhost:8765").rstrip("/")


def _provider() -> str:
    return (os.environ.get("EMAIL_PROVIDER") or "log").lower()


def _from_address() -> str:
    return (os.environ.get("EMAIL_FROM")
            or os.environ.get("EMAIL_USER")
            or "noreply@spiderx.ai")


def _report_recipient() -> Optional[str]:
    """The fixed recipient that every persisted call report also CCs. Used
    as a dev/ops feed alongside the per-agent owner emails."""
    v = (os.environ.get("REPORT_EMAIL_TO") or "").strip()
    return v or None


# ─── transport chokepoint ───────────────────────────────────────────────


async def _send(to: str, subject: str, body: str,
                *, html_body: Optional[str] = None) -> None:
    """The single chokepoint. Selects a provider, never raises.

    Build 198 — also emits a notify.email.sent / notify.email.failed
    event so the Observability page tracks delivery health. Provider
    failures stay best-effort: the event captures the failure mode
    so ops can see Gmail rate-limiting or auth misconfig without
    sifting through logs."""
    provider = _provider()
    sent_ok = False
    err_msg = None
    try:
        if provider == "gmail":
            _send_gmail(to, subject, body, html_body=html_body)
        elif provider == "postmark":
            _send_postmark(to, subject, body, html_body=html_body)
        elif provider == "resend":
            _send_resend(to, subject, body, html_body=html_body)
        else:
            log.info("email.send to=%s subject=%r\n%s", to, subject, body)
        sent_ok = True
    except Exception as e:  # noqa: BLE001
        err_msg = str(e)[:240]
        log.warning("email.send_failed provider=%s to=%s err=%s", provider, to, e)
    # Lifecycle event — never raises into the caller. We import lazily
    # to avoid a circular import between email_stub and events (events
    # → db_pg; email_stub stays leaf).
    try:
        from . import events as _ev
        if sent_ok:
            await _ev.emit(
                "notify.email.sent", source="system",
                title=f"Email sent — {subject[:60]}",
                payload={"to": to, "provider": provider, "subject": subject},
            )
        else:
            await _ev.emit(
                "notify.email.failed", source="system", severity="error",
                title=f"Email FAILED — {subject[:60]}",
                message=err_msg,
                payload={"to": to, "provider": provider, "subject": subject, "error": err_msg},
            )
    except Exception:  # noqa: BLE001
        pass


def _post_json(url: str, headers: dict, payload: dict) -> dict:
    """Synchronous POST for the HTTP API providers."""
    data = json.dumps(payload).encode("utf-8")
    hdrs = {"Content-Type": "application/json", **headers}
    req = urlrequest.Request(url, data=data, headers=hdrs, method="POST")
    with urlrequest.urlopen(req, timeout=10) as resp:
        return {"status": resp.status, "body": resp.read(2048).decode("utf-8", "replace")}


# ─── provider: Gmail SMTP ────────────────────────────────────────────────


def _send_gmail(to: str, subject: str, body: str,
                *, html_body: Optional[str] = None) -> None:
    """Send via Gmail's SMTP-over-SSL endpoint. Requires an App Password —
    the regular account password no longer works once 2-Step Verification
    is on. Generate at https://myaccount.google.com/apppasswords.

    Multipart alternative — recipients with HTML rendering get the rich
    layout, plain-text clients (terminal mail, screen-readers) get the
    text body we already build. Email is multipart by design, not via
    fallback, so both representations are available."""
    user = os.environ.get("EMAIL_USER")
    pwd = os.environ.get("EMAIL_PWD")
    if not (user and pwd):
        log.warning("gmail.creds_missing; falling back to log")
        log.info("email.send to=%s subject=%r\n%s", to, subject, body)
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr(("SpiderX.AI", _from_address()))
    msg["To"] = to
    msg.attach(MIMEText(body, "plain", "utf-8"))
    if html_body:
        msg.attach(MIMEText(html_body, "html", "utf-8"))
    # Gmail App Passwords ignore the visible spaces but accept them for
    # paste-convenience; strip just in case any provider tightens this.
    clean_pwd = (pwd or "").replace(" ", "")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as s:
        s.login(user, clean_pwd)
        s.sendmail(_from_address(), [to], msg.as_string())
    log.info("gmail.sent to=%s subject=%r", to, subject)


def _send_postmark(to: str, subject: str, body: str,
                   *, html_body: Optional[str] = None) -> None:
    token = os.environ.get("POSTMARK_TOKEN")
    if not token:
        log.warning("postmark.token_missing; falling back to log")
        log.info("email.send to=%s subject=%r\n%s", to, subject, body)
        return
    payload = {
        "From": _from_address(), "To": to, "Subject": subject,
        "TextBody": body, "MessageStream": "outbound",
    }
    if html_body:
        payload["HtmlBody"] = html_body
    r = _post_json(
        "https://api.postmarkapp.com/email",
        {"Accept": "application/json", "X-Postmark-Server-Token": token},
        payload,
    )
    log.info("postmark.sent to=%s status=%s", to, r["status"])


def _send_resend(to: str, subject: str, body: str,
                 *, html_body: Optional[str] = None) -> None:
    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        log.warning("resend.api_key_missing; falling back to log")
        log.info("email.send to=%s subject=%r\n%s", to, subject, body)
        return
    payload = {"from": _from_address(), "to": [to], "subject": subject, "text": body}
    if html_body:
        payload["html"] = html_body
    r = _post_json(
        "https://api.resend.com/emails",
        {"Authorization": f"Bearer {api_key}"},
        payload,
    )
    log.info("resend.sent to=%s status=%s", to, r["status"])


# ─── invite emails ───────────────────────────────────────────────────────


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


# ─── post-call summary email (build 196: rich HTML) ──────────────────────
# Fired by the end_call connector + the gemini_bridge WS-close auto-commit.
# The HTML report mirrors the dashboard's Call Details modal: widgets for
# outcome / sentiment / lead, summary, extracted-entity chips, transcript
# preview, and a CTA to open the full call on the dashboard.


def _fmt_duration(seconds: float) -> str:
    s = int(seconds or 0)
    if s < 60:
        return f"{s}s"
    return f"{s // 60}m {s % 60}s"


def _fmt_extracted(extracted: Optional[dict]) -> str:
    if not extracted or not isinstance(extracted, dict):
        return ""
    rows = []
    for k, v in extracted.items():
        if v in (None, "") or k.startswith("_"):
            continue
        rows.append(f"  {k}: {v}")
    if not rows:
        return ""
    return "Captured details:\n" + "\n".join(rows) + "\n\n"


# Kind → header colour. Mirrors the Call Outcomes page palette so the
# email reads visually the same as the dashboard.
_KIND_COLOR = {
    "success":   {"bg": "#dcfce7", "fg": "#166534", "label": "Success"},
    "qualified": {"bg": "#ede9fe", "fg": "#6d28d9", "label": "Qualified"},
    "info":      {"bg": "#e0f2fe", "fg": "#075985", "label": "Info"},
    "failure":   {"bg": "#fee2e2", "fg": "#991b1b", "label": "Failure"},
}
_OUTCOME_KIND = {
    "resolved": "success", "info_given": "info", "info_only": "info",
    "booking_made": "success", "reservation_made": "success",
    "appointment_booked": "success", "consultation_booked": "success",
    "lead_captured": "qualified", "callback_requested": "qualified",
    "transferred_human": "info", "human_transfer": "info",
    "not_interested": "failure", "voicemail": "failure",
    "abandoned": "failure", "complaint_logged": "failure",
}

# Chip palette for the extracted entities — same 9-pastel rotation the
# Call Details modal uses, picked by hashing the key so e.g. `name`
# always renders the same colour across calls.
_CHIP_PALETTE = [
    ("#fee2e2", "#991b1b"), ("#fed7aa", "#9a3412"),
    ("#fef3c7", "#92400e"), ("#d9f99d", "#3f6212"),
    ("#d1fae5", "#065f46"), ("#cffafe", "#155e75"),
    ("#dbeafe", "#1e3a8a"), ("#ede9fe", "#5b21b6"),
    ("#fce7f3", "#9f1239"),
]


def _chip_color(key: str) -> tuple:
    h = 0
    for c in str(key or ""):
        h = (h * 31 + ord(c)) & 0xFFFFFFFF
    return _CHIP_PALETTE[h % len(_CHIP_PALETTE)]


def _sentiment_color(sent: Optional[str]) -> dict:
    s = (sent or "").lower()
    if s == "positive": return {"bg": "#dcfce7", "fg": "#166534"}
    if s == "negative": return {"bg": "#fee2e2", "fg": "#991b1b"}
    if s == "mixed":    return {"bg": "#fef3c7", "fg": "#92400e"}
    return {"bg": "#e5e7eb", "fg": "#374151"}


def _lead_color(lead: Optional[str]) -> dict:
    l = (lead or "").lower()
    if l == "hot":  return {"bg": "#fee2e2", "fg": "#991b1b"}
    if l == "warm": return {"bg": "#fef3c7", "fg": "#92400e"}
    if l == "cold": return {"bg": "#e0f2fe", "fg": "#075985"}
    return {"bg": "#e5e7eb", "fg": "#374151"}


def _build_call_report_html(*,
        agent_name: str, agent_slug: Optional[str], org_name: str,
        outcome: Optional[str], sentiment: Optional[str],
        lead_quality: Optional[str], lead_signals: Optional[str],
        summary: Optional[str], duration_s: float,
        extracted: Optional[dict], call_id: Optional[int],
        transcript_turns: Optional[list], started_at_iso: Optional[str],
) -> str:
    """Render a self-contained HTML email mirroring the dashboard's Call
    Details modal. Inline-styled because email clients strip <style>;
    table-based so Outlook + Gmail Mobile render correctly."""
    base = _public_base_url()
    slug = agent_slug or ""
    call_link = f"{base}/agent/{slug}/calls" + (f"?call_id={call_id}" if call_id else "")
    recording_link = call_link  # forward-compatible — same destination today

    outcome_label = (outcome or "abandoned").replace("_", " ").title()
    kind = _OUTCOME_KIND.get((outcome or "").lower(), "info")
    kind_col = _KIND_COLOR.get(kind, _KIND_COLOR["info"])
    sent_col = _sentiment_color(sentiment)
    lead_col = _lead_color(lead_quality)

    # Widget cards — outcome / sentiment / lead / duration
    def widget(label: str, value: str, palette: dict) -> str:
        return (
            f'<td valign="top" align="center" '
            f'style="padding:0 6px;width:25%;">'
            f'  <table cellpadding="0" cellspacing="0" border="0" '
            f'         width="100%" style="background:{palette["bg"]};'
            f'         border-radius:10px;">'
            f'    <tr><td align="center" style="padding:12px 10px;">'
            f'      <div style="font-size:11px;color:{palette["fg"]};'
            f'           text-transform:uppercase;letter-spacing:.06em;'
            f'           font-weight:600;opacity:.85;">{html.escape(label)}</div>'
            f'      <div style="font-size:17px;color:{palette["fg"]};'
            f'           font-weight:700;margin-top:4px;">'
            f'        {html.escape(value)}</div>'
            f'    </td></tr>'
            f'  </table>'
            f'</td>'
        )

    widgets_row = (
        '<table cellpadding="0" cellspacing="0" border="0" width="100%" '
        '       style="margin:18px 0 8px;"><tr>'
        + widget("Outcome",   outcome_label, kind_col)
        + widget("Sentiment", (sentiment or "—").title(), sent_col)
        + widget("Lead",      (lead_quality or "—").upper(), lead_col)
        + widget("Duration",  _fmt_duration(duration_s),
                 {"bg": "#e5e7eb", "fg": "#374151"})
        + '</tr></table>'
    )

    # Extracted-entity chips
    chips_html = ""
    if isinstance(extracted, dict):
        chip_parts = []
        for k, v in extracted.items():
            if v in (None, "") or k.startswith("_") or isinstance(v, bool):
                continue
            text = v if isinstance(v, str) else (
                ", ".join(str(x) for x in v) if isinstance(v, list) else str(v)
            )
            text = text.strip()
            if not text:
                continue
            bg, fg = _chip_color(k)
            chip_parts.append(
                f'<span style="display:inline-block;padding:5px 11px;'
                f'border-radius:999px;background:{bg};color:{fg};'
                f'font-size:12px;font-weight:600;margin:0 6px 6px 0;">'
                f'{html.escape(text)}</span>'
            )
        if chip_parts:
            chips_html = (
                '<tr><td style="padding:6px 24px 8px;">'
                '<div style="font-size:13px;font-weight:600;color:#1f2230;'
                '            margin-bottom:8px;">Call Analysis</div>'
                + "".join(chip_parts) +
                '</td></tr>'
            )

    # Lead signals + summary
    summary_html = ""
    if summary:
        summary_html = (
            '<tr><td style="padding:6px 24px 8px;">'
            '<div style="font-size:13px;font-weight:600;color:#1f2230;'
            '            margin-bottom:6px;">Summary</div>'
            f'<div style="background:#f7f8fc;border:1px solid #e6e7ec;'
            f'           border-radius:8px;padding:12px 14px;font-size:14px;'
            f'           line-height:1.5;color:#1f2230;">'
            f'{html.escape(summary)}</div>'
            '</td></tr>'
        )
    signals_html = ""
    if lead_signals:
        signals_html = (
            '<tr><td style="padding:0 24px 6px;">'
            f'<div style="font-size:12.5px;color:#6a6f7d;font-style:italic;">'
            f'Why this lead grade: {html.escape(lead_signals)}</div>'
            '</td></tr>'
        )

    # Transcript preview — first 8 turns, formatted like the dashboard modal
    transcript_html = ""
    if transcript_turns:
        rows = []
        for t in transcript_turns[:8]:
            if not isinstance(t, dict):
                continue
            text = str(t.get("text") or "").strip()
            if not text:
                continue
            role = str(t.get("role") or "model").lower()
            is_user = role.startswith(("user", "caller", "human"))
            bubble_bg = "#e0e7ff" if is_user else "#dcfce7"
            bubble_fg = "#1e3a8a" if is_user else "#14532d"
            who = "User" if is_user else agent_name
            align = "left" if is_user else "right"
            rows.append(
                f'<tr><td align="{align}" style="padding:4px 0;">'
                f'<div style="display:inline-block;max-width:84%;text-align:left;'
                f'           padding:8px 14px;border-radius:12px;'
                f'           background:{bubble_bg};color:{bubble_fg};'
                f'           font-size:13.5px;line-height:1.5;">'
                f'<b>{html.escape(who)}:</b> {html.escape(text)}</div>'
                f'</td></tr>'
            )
        if rows:
            more = ""
            if len(transcript_turns) > 8:
                more = (
                    f'<tr><td align="center" style="padding:8px 0 0;'
                    f'                            font-size:12px;color:#6a6f7d;">'
                    f'+ {len(transcript_turns) - 8} more turn(s) on the dashboard</td></tr>'
                )
            transcript_html = (
                '<tr><td style="padding:6px 24px 12px;">'
                '<div style="font-size:13px;font-weight:600;color:#1f2230;'
                '            margin-bottom:8px;">Transcript preview</div>'
                f'<div style="background:#f5f6fa;border:1px solid #e6e7ec;'
                f'           border-radius:10px;padding:14px;">'
                f'<table cellpadding="0" cellspacing="0" border="0" width="100%">'
                + "".join(rows) + more +
                f'</table></div>'
                '</td></tr>'
            )

    # When started
    when_str = ""
    if started_at_iso:
        when_str = str(started_at_iso).replace("T", " ").split("+")[0].split(".")[0]

    # Top-of-card header band
    header_html = (
        '<tr><td style="padding:20px 24px 8px;background:#fafbff;'
        '            border-bottom:1px solid #e6e7ec;">'
        f'<div style="font-size:11px;color:#6a6f7d;text-transform:uppercase;'
        f'           letter-spacing:.06em;font-weight:600;">Call report</div>'
        f'<div style="font-size:22px;color:#1f2230;font-weight:700;'
        f'           margin-top:4px;letter-spacing:-.005em;">'
        f'{html.escape(agent_name)}'
        f'<span style="font-weight:500;color:#6a6f7d;font-size:15px;'
        f'             margin-left:8px;">· {html.escape(org_name)}</span>'
        f'</div>'
        + (f'<div style="font-size:12.5px;color:#6a6f7d;margin-top:4px;">'
           f'{html.escape(when_str)}</div>' if when_str else "")
        + '</td></tr>'
    )

    # CTA block
    cta_html = (
        '<tr><td align="left" style="padding:14px 24px 22px;">'
        f'<a href="{html.escape(call_link)}" '
        f'   style="display:inline-block;background:#3b82f6;color:#fff;'
        f'         text-decoration:none;font-weight:600;font-size:14.5px;'
        f'         padding:10px 22px;border-radius:8px;">'
        f'  Open call on dashboard →</a>'
        f'<a href="{html.escape(recording_link)}" '
        f'   style="display:inline-block;margin-left:10px;color:#3b82f6;'
        f'         text-decoration:none;font-size:13.5px;font-weight:500;'
        f'         padding:10px 6px;">'
        f'  ▶ Listen to recording (coming soon)</a>'
        '</td></tr>'
    )

    foot_html = (
        '<tr><td align="center" style="padding:14px 24px;background:#fafbff;'
        '            border-top:1px solid #e6e7ec;font-size:11.5px;'
        '            color:#9095a3;">'
        f'Call ID #{call_id or "—"} · sent by SpiderX.AI · '
        '<a href="' + html.escape(base) + '" '
        '   style="color:#6b7280;text-decoration:underline;">dashboard</a>'
        '</td></tr>'
    )

    return (
        '<!doctype html><html><body style="margin:0;padding:24px;'
        '       background:#eef0f4;font-family:-apple-system,BlinkMacSystemFont,'
        '       \'Segoe UI\',Helvetica,Arial,sans-serif;color:#1f2230;">'
        '<table cellpadding="0" cellspacing="0" border="0" width="100%" '
        '       style="max-width:640px;margin:0 auto;background:#fff;'
        '       border:1px solid #e6e7ec;border-radius:14px;overflow:hidden;">'
        + header_html
        + '<tr><td style="padding:6px 18px 0;">'
        + widgets_row
        + '</td></tr>'
        + summary_html
        + signals_html
        + chips_html
        + transcript_html
        + cta_html
        + foot_html
        + '</table></body></html>'
    )


async def send_call_summary_email(
    to: str, agent_name: str, org_name: str, *,
    outcome: Optional[str], sentiment: Optional[str],
    lead_quality: Optional[str], lead_signals: Optional[str],
    summary: Optional[str], duration_s: float,
    extracted: Optional[dict], call_id: Optional[int],
    agent_slug: Optional[str] = None,
    transcript_turns: Optional[list] = None,
    started_at_iso: Optional[str] = None,
) -> None:
    """Send the rich HTML call report to a single recipient. Falls back to
    a plain-text body for terminal mail clients.

    Build 196 upgrade: was plain-text only; now multipart (text + HTML)
    with widgets, sentiment / lead colour chips, extracted-entity chips,
    transcript preview, and a CTA to the dashboard."""
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
    if agent_slug:
        body += f"Open: {_public_base_url()}/agent/{agent_slug}/calls\n"
    body += "\n— SpiderX.AI"

    html_body = _build_call_report_html(
        agent_name=agent_name, agent_slug=agent_slug, org_name=org_name,
        outcome=outcome, sentiment=sentiment, lead_quality=lead_quality,
        lead_signals=lead_signals, summary=summary, duration_s=duration_s,
        extracted=extracted, call_id=call_id,
        transcript_turns=transcript_turns, started_at_iso=started_at_iso,
    )
    await _send(to, subject, body, html_body=html_body)


async def send_call_report_to_devteam(*,
    agent_name: str, agent_slug: Optional[str], org_name: str,
    outcome: Optional[str], sentiment: Optional[str],
    lead_quality: Optional[str], lead_signals: Optional[str],
    summary: Optional[str], duration_s: float,
    extracted: Optional[dict], call_id: Optional[int],
    transcript_turns: Optional[list] = None,
    started_at_iso: Optional[str] = None,
) -> None:
    """Fire-and-forget: send the same HTML report to the fixed
    REPORT_EMAIL_TO recipient (defaults to devteam@spiderx.ai). No-op
    when REPORT_EMAIL_TO is empty.

    This is in addition to the per-agent owner emails — used to keep
    the dev/ops team in the loop on every persisted call regardless of
    `agent.purpose.post_call.email` (which controls owner notification)."""
    to = _report_recipient()
    if not to:
        return
    await send_call_summary_email(
        to=to, agent_name=agent_name, org_name=org_name,
        outcome=outcome, sentiment=sentiment, lead_quality=lead_quality,
        lead_signals=lead_signals, summary=summary, duration_s=duration_s,
        extracted=extracted, call_id=call_id, agent_slug=agent_slug,
        transcript_turns=transcript_turns, started_at_iso=started_at_iso,
    )


async def send_call_summary_sms(
    to: str, agent_name: str, *,
    outcome: Optional[str], lead_quality: Optional[str], duration_s: float,
) -> None:
    """160-char-ish SMS summary. Plan-gated — caller must verify tier
    BEFORE invoking this (we don't reach into the DB from here)."""
    bits = [agent_name]
    if outcome:      bits.append(outcome.replace("_", " "))
    if lead_quality: bits.append(f"{lead_quality.upper()} lead")
    bits.append(_fmt_duration(duration_s))
    msg = " · ".join(bits)
    await _send(to, "[call] " + msg[:80], msg)
