"""Daily end-of-day per-agent digest email.

Scheduler fires this at 19:00 IST. For each agent that had at least one
call today, builds an HTML digest (calls / minutes / outcome mix / top
calls / cost so far this month) and emails it to the org owner.

Reuses build 196's send infrastructure (Gmail SMTP) but writes its own
HTML body because the post-call email is per-call and this is a daily
roll-up — different KPIs, different layout.

Strict design:
  - One email per agent owner per day (not per call). The day's
    individual call reports already went via build 196.
  - Day window = today's calendar day in IST (the org's primary
    market). A future per-org timezone setting would replace this.
  - Emits `cost.agent.monthly.computed` event with the day's tally
    so the Observability feed shows when the digest ran for each
    agent.
"""
from __future__ import annotations

import html
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from . import db, events, email_stub

log = logging.getLogger("eva.eod_digest")


_KIND_COLOR = {
    "success":   {"bg": "#dcfce7", "fg": "#166534", "label": "Success"},
    "qualified": {"bg": "#ede9fe", "fg": "#6d28d9", "label": "Qualified"},
    "info":      {"bg": "#e0f2fe", "fg": "#075985", "label": "Info"},
    "failure":   {"bg": "#fee2e2", "fg": "#991b1b", "label": "Failure"},
}


def _fmt_duration_short(seconds: float) -> str:
    s = int(seconds or 0)
    if s < 60:
        return f"{s}s"
    return f"{s // 60}m"


def _build_digest_html(*, agent: dict, org_name: str, day_iso: str,
                       calls: list[dict], cost_paise_today: int,
                       cost_paise_mtd: int) -> str:
    """Self-contained HTML email mirroring the Observability palette.
    Goes to the agent's org owner — designed to be glanced at on a
    phone in the evening: how many calls, top outcome, money in /
    money so far this month."""
    from . import call_outcomes
    base = email_stub._public_base_url()
    slug = agent.get("slug") or agent.get("id")
    dashboard_link = f"{base}/agent/{slug}"
    calls_link = f"{base}/agent/{slug}/calls"

    n_calls = len(calls)
    total_mins = sum(float(c.get("duration_s") or 0) for c in calls) / 60.0
    catalogue = {c["id"]: c for c in call_outcomes.catalogue_for(agent) if isinstance(c, dict)}

    # Outcome mix by kind
    by_kind = {"success": 0, "qualified": 0, "info": 0, "failure": 0}
    for c in calls:
        oid = (c.get("outcome") or "").lower()
        meta = catalogue.get(oid) or {}
        kind = meta.get("kind", "info")
        by_kind[kind] = by_kind.get(kind, 0) + 1

    # Top calls — pick the 3 most aligned with purpose, fall back to
    # most recent if there's no purpose data on the agent.
    primary_outcomes = set()
    purpose = agent.get("purpose") if isinstance(agent.get("purpose"), dict) else {}
    if purpose:
        primary_outcomes = set(call_outcomes.purpose_aligned_outcome_ids(agent) or [])
    sorted_calls = sorted(
        calls,
        key=lambda c: (
            (c.get("outcome") or "").lower() in primary_outcomes,
            float(c.get("duration_s") or 0),
        ),
        reverse=True,
    )
    top_calls = sorted_calls[:3]

    # Widget tile renderer
    def widget(label: str, value: str, palette: dict) -> str:
        return (
            f'<td valign="top" align="center" '
            f'style="padding:0 6px;width:25%;">'
            f'  <table cellpadding="0" cellspacing="0" border="0" width="100%" '
            f'         style="background:{palette["bg"]};border-radius:10px;">'
            f'    <tr><td align="center" style="padding:14px 10px;">'
            f'      <div style="font-size:11px;color:{palette["fg"]};'
            f'           text-transform:uppercase;letter-spacing:.06em;'
            f'           font-weight:600;opacity:.85;">{html.escape(label)}</div>'
            f'      <div style="font-size:22px;color:{palette["fg"]};'
            f'           font-weight:700;margin-top:6px;line-height:1.1;">'
            f'        {html.escape(value)}</div>'
            f'    </td></tr>'
            f'  </table>'
            f'</td>'
        )

    grey = {"bg": "#f5f6fa", "fg": "#1f2230"}
    widgets_row = (
        '<table cellpadding="0" cellspacing="0" border="0" width="100%" '
        '       style="margin:18px 0 14px;"><tr>'
        + widget("Calls", str(n_calls), grey)
        + widget("Minutes", f"{total_mins:.1f}", {"bg": "#e0f2fe", "fg": "#075985"})
        + widget("Wins", str(by_kind["success"] + by_kind["qualified"]), {"bg": "#dcfce7", "fg": "#166534"})
        + widget("LLM cost", f"₹{cost_paise_today/100:.2f}", {"bg": "#fef3c7", "fg": "#92400e"})
        + '</tr></table>'
    )

    # Outcome mix bar — proportional stacked bar similar to the dashboard
    # By-kind tile. Falls back to "no calls" when n_calls=0.
    bar_segments = []
    for kind, n in by_kind.items():
        if n == 0:
            continue
        share = (n / n_calls * 100) if n_calls else 0
        col = _KIND_COLOR[kind]
        bar_segments.append(
            f'<span style="display:inline-block;background:{col["fg"]};'
            f'height:20px;width:{share:.1f}%;"></span>'
        )
    bar_html = "".join(bar_segments) or '<span style="color:#9095a3;font-size:12px;">No calls today.</span>'
    mix_legend = " · ".join(
        f'<span style="color:{_KIND_COLOR[k]["fg"]}">●</span> {_KIND_COLOR[k]["label"]} <b>{by_kind[k]}</b>'
        for k in ("success", "qualified", "info", "failure") if by_kind[k] > 0
    ) or "—"
    mix_html = (
        '<tr><td style="padding:6px 24px 14px;">'
        '<div style="font-size:13px;font-weight:600;color:#1f2230;margin-bottom:8px;">By kind</div>'
        f'<div style="background:#f5f6fa;border:1px solid #e6e7ec;border-radius:6px;'
        f'           overflow:hidden;line-height:0;font-size:0;">{bar_html}</div>'
        f'<div style="font-size:12.5px;color:#6a6f7d;margin-top:6px;">{mix_legend}</div>'
        '</td></tr>'
    )

    # Top 3 calls table
    rows_html = []
    for c in top_calls:
        oid = (c.get("outcome") or "—").lower()
        meta = catalogue.get(oid) or {}
        kind = meta.get("kind", "info")
        kc = _KIND_COLOR.get(kind, _KIND_COLOR["info"])
        when = ""
        if c.get("started_at"):
            try:
                when = str(c["started_at"]).split(".")[0].replace("T", " ")[11:16]
            except Exception:  # noqa: BLE001
                when = ""
        summary = (c.get("summary") or c.get("reason") or "")[:120]
        rows_html.append(
            f'<tr><td style="padding:8px 0;border-top:1px solid #eef0f4;">'
            f'  <div style="font-size:12.5px;color:#1f2230;">'
            f'    <span style="display:inline-block;background:{kc["bg"]};color:{kc["fg"]};'
            f'           font-size:11px;font-weight:600;padding:2px 8px;border-radius:999px;'
            f'           margin-right:8px;">{html.escape(meta.get("label") or oid)}</span>'
            f'    <span style="color:#6a6f7d;">{when}</span>'
            f'    {("· <b>" + html.escape((c.get("lead_quality") or "").upper()) + "</b>") if c.get("lead_quality") else ""}'
            f'  </div>'
            f'  <div style="font-size:12.5px;color:#6a6f7d;margin-top:3px;">{html.escape(summary)}</div>'
            f'</td></tr>'
        )
    top_calls_html = (
        '<tr><td style="padding:0 24px 14px;">'
        '<div style="font-size:13px;font-weight:600;color:#1f2230;margin-bottom:6px;">Top calls</div>'
        '<table cellpadding="0" cellspacing="0" border="0" width="100%">'
        + ("".join(rows_html) if rows_html else
           '<tr><td style="font-size:12.5px;color:#6a6f7d;padding:8px 0;">No calls today.</td></tr>')
        + '</table>'
        '</td></tr>'
    ) if n_calls > 0 else ""

    # Cost MTD strip
    cost_html = (
        '<tr><td style="padding:6px 24px 16px;">'
        '<div style="font-size:13px;font-weight:600;color:#1f2230;margin-bottom:4px;">Cost month-to-date</div>'
        f'<div style="font-size:14px;color:#1f2230;">'
        f'  Today: <b>₹{cost_paise_today/100:.2f}</b> · '
        f'  Month-to-date: <b>₹{cost_paise_mtd/100:.2f}</b>'
        f'</div>'
        '<div style="font-size:11.5px;color:#9095a3;margin-top:4px;">'
        '  LLM only — telephony + DID rental excluded.'
        '</div>'
        '</td></tr>'
    )

    header_html = (
        '<tr><td style="padding:20px 24px 8px;background:#fafbff;'
        '            border-bottom:1px solid #e6e7ec;">'
        '<div style="font-size:11px;color:#6a6f7d;text-transform:uppercase;'
        '           letter-spacing:.06em;font-weight:600;">Daily digest</div>'
        f'<div style="font-size:22px;color:#1f2230;font-weight:700;'
        f'           margin-top:4px;letter-spacing:-.005em;">'
        f'{html.escape(agent.get("name") or "Agent")}'
        f'<span style="font-weight:500;color:#6a6f7d;font-size:15px;'
        f'             margin-left:8px;">· {html.escape(org_name)}</span>'
        f'</div>'
        f'<div style="font-size:12.5px;color:#6a6f7d;margin-top:4px;">{html.escape(day_iso)}</div>'
        '</td></tr>'
    )

    cta_html = (
        '<tr><td align="left" style="padding:14px 24px 22px;">'
        f'<a href="{html.escape(dashboard_link)}" '
        f'   style="display:inline-block;background:#3b82f6;color:#fff;'
        f'         text-decoration:none;font-weight:600;font-size:14.5px;'
        f'         padding:10px 22px;border-radius:8px;">'
        f'  Open dashboard →</a>'
        f'<a href="{html.escape(calls_link)}" '
        f'   style="display:inline-block;margin-left:10px;color:#3b82f6;'
        f'         text-decoration:none;font-size:13.5px;font-weight:500;'
        f'         padding:10px 6px;">'
        f'  View all {n_calls} call{"" if n_calls == 1 else "s"} →</a>'
        '</td></tr>'
    )

    foot_html = (
        '<tr><td align="center" style="padding:14px 24px;background:#fafbff;'
        '            border-top:1px solid #e6e7ec;font-size:11.5px;'
        '            color:#9095a3;">'
        'Daily digest from SpiderX.AI · '
        f'<a href="{html.escape(base)}" style="color:#6b7280;text-decoration:underline;">dashboard</a>'
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
        + mix_html
        + top_calls_html
        + cost_html
        + cta_html
        + foot_html
        + '</table></body></html>'
    )


async def run_daily_eod_digest() -> None:
    """Scheduler entry — runs at 19:00 IST every day.

    Walks every agent with ≥1 call today, builds a digest, emails the
    org owner, emits cost.agent.monthly.computed. Best-effort: a single
    agent's failure (template error, owner has no email, etc.) doesn't
    block the rest."""
    from . import db_pg as _dbp
    log.info("eod_digest.start")
    # Window: today's IST day in UTC bounds. Pull the daily-stats row
    # for today to get the cost cheaply.
    from zoneinfo import ZoneInfo
    ist = ZoneInfo("Asia/Kolkata")
    now_ist = datetime.now(ist)
    day_start_ist = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
    day_start_utc = day_start_ist.astimezone(timezone.utc)
    day_end_utc = (day_start_ist + timedelta(days=1)).astimezone(timezone.utc)
    month_start_utc = day_start_ist.replace(day=1).astimezone(timezone.utc)

    pool = await _dbp.get_pool()
    async with pool.acquire() as conn:
        # Agents with any call in today's window. Each call has an
        # agent_id, so a DISTINCT scan gets us the candidates without
        # joining agents twice.
        rows = await conn.fetch(
            "SELECT DISTINCT agent_id FROM calls "
            "WHERE started_at >= $1 AND started_at < $2",
            day_start_utc, day_end_utc,
        )
    agent_ids = [int(r["agent_id"]) for r in rows]
    log.info("eod_digest: %d agent(s) had calls today", len(agent_ids))

    for aid in agent_ids:
        try:
            agent = await db.get_agent(aid)
            if not agent:
                continue
            # Today's calls — bounded list (most agents will have <50)
            async with pool.acquire() as conn:
                cs = await conn.fetch(
                    "SELECT id, started_at, ended_at, duration_s, outcome, reason, "
                    "       summary, lead_quality, sentiment, lead_signals "
                    "FROM calls "
                    "WHERE agent_id = $1 AND started_at >= $2 AND started_at < $3 "
                    "ORDER BY started_at DESC",
                    aid, day_start_utc, day_end_utc,
                )
            calls = [dict(r) for r in cs]
            # ISO-ify timestamps in dicts for the template
            for c in calls:
                if c.get("started_at"):
                    c["started_at"] = c["started_at"].isoformat()

            # Cost today + month-to-date from the rollup table
            async with pool.acquire() as conn:
                today_cost = await conn.fetchval(
                    "SELECT COALESCE(SUM(cost_paise), 0) FROM agent_daily_stats "
                    "WHERE agent_id = $1 AND day = $2::date",
                    aid, day_start_ist.date(),
                ) or 0
                mtd_cost = await conn.fetchval(
                    "SELECT COALESCE(SUM(cost_paise), 0) FROM agent_daily_stats "
                    "WHERE agent_id = $1 AND day >= $2::date",
                    aid, month_start_utc.astimezone(ist).date(),
                ) or 0

            # Recipient: org owner — same resolution the post-call
            # email uses, so the digest goes to whoever already gets
            # post-call summaries.
            org_id = agent.get("org_id")
            members = await _dbp.list_org_members(org_id) if org_id else []
            owners = [m for m in members if m.get("role") == "owner"] or members
            if not owners:
                continue
            org = await _dbp.get_org_for_user(owners[0]["user_id"]) if owners else None
            org_name = (org or {}).get("name") or "your team"
            day_iso = day_start_ist.strftime("%A, %d %B %Y")

            html_body = _build_digest_html(
                agent=agent, org_name=org_name, day_iso=day_iso,
                calls=calls, cost_paise_today=int(today_cost),
                cost_paise_mtd=int(mtd_cost),
            )
            txt_body = (
                f"Daily digest for {agent.get('name')} ({org_name}) — {day_iso}\n\n"
                f"  Calls today:   {len(calls)}\n"
                f"  Minutes:       {sum(float(c.get('duration_s') or 0) for c in calls)/60:.1f}\n"
                f"  Cost today:    ₹{today_cost/100:.2f}\n"
                f"  Cost MTD:      ₹{mtd_cost/100:.2f}\n\n"
                f"Open the dashboard: {email_stub._public_base_url()}/agent/{agent.get('slug') or aid}\n"
            )

            # Send to every owner. For multi-owner orgs each gets the
            # digest — common case is single-owner so this is normally
            # one email per agent.
            for m in owners:
                to = (m.get("email") or "").strip()
                if not to:
                    continue
                try:
                    subj = (f"[{agent.get('name')}] {len(calls)} call"
                            f"{'' if len(calls) == 1 else 's'} today — "
                            f"₹{today_cost/100:.2f}")
                    await email_stub._send(to, subj, txt_body, html_body=html_body)
                except Exception as e:  # noqa: BLE001
                    log.warning("eod_digest: send to %s failed: %s", to, e)

            # Emit event with the day's cost contribution — feeds the
            # Observability feed + future per-agent P&L view.
            await events.emit(
                "cost.agent.monthly.computed", source="scheduler",
                agent_id=aid, org_id=org_id,
                title=f"EOD digest — {agent.get('name')} · {len(calls)} call"
                      f"{'' if len(calls) == 1 else 's'} · ₹{today_cost/100:.2f}",
                payload={
                    "agent_id": aid, "agent_name": agent.get("name"),
                    "calls_today": len(calls),
                    "minutes_today": round(sum(float(c.get('duration_s') or 0) for c in calls)/60, 2),
                    "cost_paise_today": int(today_cost),
                    "cost_paise_mtd": int(mtd_cost),
                    "day": day_start_ist.date().isoformat(),
                },
                dedupe_key=f"cost.agent.monthly.computed.{aid}.{day_start_ist.date().isoformat()}",
            )
        except Exception:  # noqa: BLE001
            log.exception("eod_digest: agent_id=%s failed", aid)

    log.info("eod_digest.done")
