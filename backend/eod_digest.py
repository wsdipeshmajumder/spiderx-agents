"""Daily end-of-day per-organisation digest email.

Scheduler fires this at 19:00 IST. For each ORG that had at least one
call across any of its agents today, builds a single HTML digest:
  - Org-level KPI tiles (total calls / minutes / cost / agents active)
  - One section per agent that had calls, with the agent's own KPI
    tiles + outcome-mix bar + top 3 calls
  - Cost month-to-date strip
  - CTA back to the dashboard

Build 201 — restructured from per-agent emails (one per agent, noisy
for multi-agent orgs) to ONE email per org with all the agent
sections inside. Owners with 5 agents now get 1 email, not 5.

Strict design:
  - One email per org per owner per day. The day's individual call
    reports already went via build 196 (post-call email).
  - Day window = today's calendar day in IST.
  - Skip orgs with zero calls today (no email instead of an empty one).
  - Emits ONE cost.org.monthly.computed event per org per day with
    the day's totals. Per-agent events still fire too so the per-
    agent P&L view stays accurate.
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


# ─── HTML builders ────────────────────────────────────────────────────────


def _widget_td(label: str, value: str, palette: dict) -> str:
    """One KPI tile rendered as a table cell. Email-safe HTML: every
    cell value/colour is inline-styled because most clients strip
    <style>. Width 25% so 4 tiles fill the parent table row."""
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


def _build_agent_section(*, agent: dict, calls: list[dict],
                         cost_paise_today: int, base_url: str) -> str:
    """One agent's slice of the org digest. Self-contained — the org
    template stitches multiple of these together."""
    from . import call_outcomes
    slug = agent.get("slug") or agent.get("id")
    agent_link = f"{base_url}/agent/{slug}"
    calls_link = f"{base_url}/agent/{slug}/calls"

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

    # Top 3 calls — most aligned with purpose, longest duration as tiebreak
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

    grey = {"bg": "#f5f6fa", "fg": "#1f2230"}
    widgets_row = (
        '<table cellpadding="0" cellspacing="0" border="0" width="100%" '
        '       style="margin:12px 0 14px;"><tr>'
        + _widget_td("Calls", str(n_calls), grey)
        + _widget_td("Minutes", f"{total_mins:.1f}", {"bg": "#e0f2fe", "fg": "#075985"})
        + _widget_td("Wins", str(by_kind["success"] + by_kind["qualified"]),
                     {"bg": "#dcfce7", "fg": "#166534"})
        + _widget_td("LLM cost", f"₹{cost_paise_today/100:.2f}",
                     {"bg": "#fef3c7", "fg": "#92400e"})
        + '</tr></table>'
    )

    # Outcome mix proportional bar
    bar_segments = []
    for kind, n in by_kind.items():
        if n == 0:
            continue
        share = (n / n_calls * 100) if n_calls else 0
        col = _KIND_COLOR[kind]
        bar_segments.append(
            f'<span style="display:inline-block;background:{col["fg"]};'
            f'height:18px;width:{share:.1f}%;"></span>'
        )
    bar_html = "".join(bar_segments) or (
        '<span style="color:#9095a3;font-size:12px;">No calls.</span>'
    )
    mix_legend = " · ".join(
        f'<span style="color:{_KIND_COLOR[k]["fg"]}">●</span> '
        f'{_KIND_COLOR[k]["label"]} <b>{by_kind[k]}</b>'
        for k in ("success", "qualified", "info", "failure") if by_kind[k] > 0
    ) or "—"
    mix_html = (
        '<div style="font-size:12px;font-weight:600;color:#4a4f5e;margin:6px 0 6px;">By kind</div>'
        f'<div style="background:#f5f6fa;border:1px solid #e6e7ec;border-radius:6px;'
        f'           overflow:hidden;line-height:0;font-size:0;">{bar_html}</div>'
        f'<div style="font-size:12px;color:#6a6f7d;margin-top:6px;">{mix_legend}</div>'
    )

    # Top 3 calls rows
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
        '<div style="font-size:12px;font-weight:600;color:#4a4f5e;margin:14px 0 4px;">Top calls</div>'
        '<table cellpadding="0" cellspacing="0" border="0" width="100%">'
        + "".join(rows_html)
        + '</table>'
    ) if rows_html else ""

    return (
        '<tr><td style="padding:18px 24px 8px;border-top:1px solid #eef0f4;">'
        f'  <div style="display:block;">'
        f'    <a href="{html.escape(agent_link)}" '
        f'       style="text-decoration:none;color:#1f2230;">'
        f'      <span style="font-size:16px;font-weight:700;letter-spacing:-.005em;">'
        f'        {html.escape(agent.get("name") or "Agent")}</span>'
        f'    </a>'
        f'    <span style="font-size:12px;color:#6a6f7d;margin-left:8px;">'
        f'      {html.escape(agent.get("sector") or "")} · '
        f'      {html.escape(agent.get("locale") or "")}'
        f'    </span>'
        f'    <a href="{html.escape(calls_link)}" '
        f'       style="font-size:12.5px;color:#3b82f6;margin-left:10px;'
        f'             text-decoration:none;float:right;">'
        f'      View {n_calls} call{("" if n_calls == 1 else "s")} →</a>'
        f'  </div>'
        f'  {widgets_row}'
        f'  {mix_html}'
        f'  {top_calls_html}'
        '</td></tr>'
    )


def _build_org_digest_html(*, org_name: str, day_iso: str,
                           agent_summaries: list[dict],
                           org_totals: dict, base_url: str) -> str:
    """Stitch one HTML email from the per-agent sections. Header + org-
    totals tiles + each agent section + cost MTD + CTA + footer."""
    n_agents = len(agent_summaries)
    header_html = (
        '<tr><td style="padding:20px 24px 8px;background:#fafbff;'
        '            border-bottom:1px solid #e6e7ec;">'
        '<div style="font-size:11px;color:#6a6f7d;text-transform:uppercase;'
        '           letter-spacing:.06em;font-weight:600;">Daily digest</div>'
        f'<div style="font-size:22px;color:#1f2230;font-weight:700;'
        f'           margin-top:4px;letter-spacing:-.005em;">'
        f'{html.escape(org_name)}'
        f'<span style="font-weight:500;color:#6a6f7d;font-size:14px;'
        f'             margin-left:10px;">· {n_agents} agent'
        f'{("" if n_agents == 1 else "s")} active today</span>'
        f'</div>'
        f'<div style="font-size:12.5px;color:#6a6f7d;margin-top:4px;">'
        f'  {html.escape(day_iso)}</div>'
        '</td></tr>'
    )

    grey = {"bg": "#f5f6fa", "fg": "#1f2230"}
    org_tiles_html = (
        '<tr><td style="padding:6px 18px 0;">'
        '<table cellpadding="0" cellspacing="0" border="0" width="100%" '
        '       style="margin:18px 0 4px;"><tr>'
        + _widget_td("Calls (org)", str(org_totals["calls"]), grey)
        + _widget_td("Minutes", f'{org_totals["minutes"]:.1f}',
                     {"bg": "#e0f2fe", "fg": "#075985"})
        + _widget_td("Active agents", str(n_agents),
                     {"bg": "#ede9fe", "fg": "#6d28d9"})
        + _widget_td("LLM cost",
                     f'₹{org_totals["cost_today"]/100:.2f}',
                     {"bg": "#fef3c7", "fg": "#92400e"})
        + '</tr></table>'
        '</td></tr>'
    )

    # Per-agent sections — already HTML strings
    agents_html = "".join(s["html"] for s in agent_summaries)

    cost_html = (
        '<tr><td style="padding:14px 24px 16px;border-top:1px solid #eef0f4;">'
        '<div style="font-size:13px;font-weight:600;color:#1f2230;margin-bottom:4px;">'
        'Cost month-to-date (org)</div>'
        f'<div style="font-size:14px;color:#1f2230;">'
        f'  Today: <b>₹{org_totals["cost_today"]/100:.2f}</b> · '
        f'  Month-to-date: <b>₹{org_totals["cost_mtd"]/100:.2f}</b>'
        f'</div>'
        '<div style="font-size:11.5px;color:#9095a3;margin-top:4px;">'
        '  LLM only — telephony + DID rental excluded.'
        '</div>'
        '</td></tr>'
    )

    cta_html = (
        '<tr><td align="left" style="padding:14px 24px 22px;">'
        f'<a href="{html.escape(base_url)}/agents" '
        f'   style="display:inline-block;background:#3b82f6;color:#fff;'
        f'         text-decoration:none;font-weight:600;font-size:14.5px;'
        f'         padding:10px 22px;border-radius:8px;">'
        f'  Open all agents →</a>'
        '</td></tr>'
    )

    foot_html = (
        '<tr><td align="center" style="padding:14px 24px;background:#fafbff;'
        '            border-top:1px solid #e6e7ec;font-size:11.5px;'
        '            color:#9095a3;">'
        'Daily digest from SpiderX.AI · '
        f'<a href="{html.escape(base_url)}" '
        f'   style="color:#6b7280;text-decoration:underline;">dashboard</a>'
        '</td></tr>'
    )

    return (
        '<!doctype html><html><body style="margin:0;padding:24px;'
        '       background:#eef0f4;font-family:-apple-system,BlinkMacSystemFont,'
        '       \'Segoe UI\',Helvetica,Arial,sans-serif;color:#1f2230;">'
        '<table cellpadding="0" cellspacing="0" border="0" width="100%" '
        '       style="max-width:680px;margin:0 auto;background:#fff;'
        '       border:1px solid #e6e7ec;border-radius:14px;overflow:hidden;">'
        + header_html
        + org_tiles_html
        + agents_html
        + cost_html
        + cta_html
        + foot_html
        + '</table></body></html>'
    )


# ─── scheduler entry ─────────────────────────────────────────────────────


async def run_daily_eod_digest() -> None:
    """Build + send one digest per org, with per-agent sections inside.

    Best-effort throughout — one org's failure doesn't block the rest;
    one agent inside an org failing to render doesn't block the org
    email."""
    from . import db_pg as _dbp
    log.info("eod_digest.start")

    from zoneinfo import ZoneInfo
    ist = ZoneInfo("Asia/Kolkata")
    now_ist = datetime.now(ist)
    day_start_ist = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
    day_start_utc = day_start_ist.astimezone(timezone.utc)
    day_end_utc = (day_start_ist + timedelta(days=1)).astimezone(timezone.utc)
    month_start_utc = day_start_ist.replace(day=1).astimezone(timezone.utc)
    day_iso = day_start_ist.strftime("%A, %d %B %Y")
    base_url = email_stub._public_base_url()

    pool = await _dbp.get_pool()
    # First pull: distinct agents that had calls today, joined to their org.
    async with pool.acquire() as conn:
        agent_rows = await conn.fetch(
            "SELECT DISTINCT c.agent_id, a.org_id "
            "FROM calls c JOIN agents a ON a.id = c.agent_id "
            "WHERE c.started_at >= $1 AND c.started_at < $2 "
            "  AND a.org_id IS NOT NULL",
            day_start_utc, day_end_utc,
        )
    # Group by org_id
    org_to_agent_ids: dict[int, list[int]] = {}
    for r in agent_rows:
        oid = int(r["org_id"])
        org_to_agent_ids.setdefault(oid, []).append(int(r["agent_id"]))
    log.info("eod_digest: %d org(s) had call activity today", len(org_to_agent_ids))

    for org_id, agent_ids in org_to_agent_ids.items():
        try:
            await _digest_one_org(
                org_id=org_id, agent_ids=agent_ids,
                day_start_ist=day_start_ist, day_start_utc=day_start_utc,
                day_end_utc=day_end_utc, month_start_utc=month_start_utc,
                day_iso=day_iso, base_url=base_url, ist=ist,
            )
        except Exception:  # noqa: BLE001
            log.exception("eod_digest: org_id=%s failed", org_id)

    log.info("eod_digest.done")


async def _digest_one_org(*, org_id: int, agent_ids: list[int],
                          day_start_ist: datetime,
                          day_start_utc: datetime, day_end_utc: datetime,
                          month_start_utc: datetime,
                          day_iso: str, base_url: str, ist) -> None:
    """Render + send one org's digest. Pulls agent + call data and
    aggregates org totals across the agent sections."""
    from . import db_pg as _dbp
    pool = await _dbp.get_pool()

    members = await _dbp.list_org_members(org_id) or []
    owners = [m for m in members if m.get("role") == "owner"] or members
    if not owners:
        log.info("eod_digest: org_id=%s has no recipients — skipping", org_id)
        return
    org = await _dbp.get_org_for_user(owners[0]["user_id"]) if owners else None
    org_name = (org or {}).get("name") or "your team"

    agent_summaries: list[dict] = []
    totals = {"calls": 0, "minutes": 0.0, "cost_today": 0, "cost_mtd": 0}

    for aid in agent_ids:
        try:
            agent = await db.get_agent(aid)
            if not agent:
                continue
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
            for c in calls:
                if c.get("started_at"):
                    c["started_at"] = c["started_at"].isoformat()
            if not calls:
                continue

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
            today_cost = int(today_cost)
            mtd_cost = int(mtd_cost)

            section_html = _build_agent_section(
                agent=agent, calls=calls,
                cost_paise_today=today_cost,
                base_url=base_url,
            )
            agent_summaries.append({
                "agent_id": aid,
                "agent_name": agent.get("name"),
                "html": section_html,
                "calls": len(calls),
                "minutes": sum(float(c.get("duration_s") or 0) for c in calls) / 60.0,
                "cost_today": today_cost,
                "cost_mtd": mtd_cost,
            })
            totals["calls"] += len(calls)
            totals["minutes"] += sum(float(c.get("duration_s") or 0) for c in calls) / 60.0
            totals["cost_today"] += today_cost
            totals["cost_mtd"] += mtd_cost

            # Per-agent event still fires — keeps the per-agent P&L page
            # and the Observability feed accurate. Dedupe-keyed per day.
            await events.emit(
                "cost.agent.monthly.computed", source="scheduler",
                agent_id=aid, org_id=org_id,
                title=f"EOD digest — {agent.get('name')} · {len(calls)} call"
                      f"{'' if len(calls) == 1 else 's'} · ₹{today_cost/100:.2f}",
                payload={
                    "agent_id": aid, "agent_name": agent.get("name"),
                    "calls_today": len(calls),
                    "minutes_today": round(
                        sum(float(c.get('duration_s') or 0) for c in calls) / 60, 2,
                    ),
                    "cost_paise_today": today_cost,
                    "cost_paise_mtd": mtd_cost,
                    "day": day_start_ist.date().isoformat(),
                },
                dedupe_key=f"cost.agent.monthly.computed.{aid}.{day_start_ist.date().isoformat()}",
            )
        except Exception:  # noqa: BLE001
            log.exception("eod_digest: org_id=%s agent_id=%s failed", org_id, aid)

    if not agent_summaries:
        log.info("eod_digest: org_id=%s produced 0 agent sections — no email", org_id)
        return

    # Build the per-org HTML once, send to each owner. For multi-owner
    # orgs every owner sees the same digest (one mail each).
    html_body = _build_org_digest_html(
        org_name=org_name, day_iso=day_iso,
        agent_summaries=agent_summaries,
        org_totals=totals, base_url=base_url,
    )
    n_agents = len(agent_summaries)
    txt_body = (
        f"Daily digest for {org_name} — {day_iso}\n\n"
        f"  {totals['calls']} call(s) across {n_agents} agent(s)\n"
        f"  Minutes:        {totals['minutes']:.1f}\n"
        f"  Cost today:     ₹{totals['cost_today']/100:.2f}\n"
        f"  Month-to-date:  ₹{totals['cost_mtd']/100:.2f}\n\n"
        + "".join(
            f"  • {s['agent_name']}: {s['calls']} call(s), "
            f"{s['minutes']:.1f} min, ₹{s['cost_today']/100:.2f}\n"
            for s in agent_summaries
        )
        + f"\nOpen the dashboard: {base_url}/agents\n"
    )

    subject = (
        f"[{org_name}] {totals['calls']} call"
        f"{'' if totals['calls'] == 1 else 's'} today across {n_agents} agent"
        f"{'' if n_agents == 1 else 's'} — ₹{totals['cost_today']/100:.2f}"
    )

    for m in owners:
        to = (m.get("email") or "").strip()
        if not to:
            continue
        try:
            await email_stub._send(to, subject, txt_body, html_body=html_body)
        except Exception as e:  # noqa: BLE001
            log.warning("eod_digest: send to %s failed: %s", to, e)

    # Org-level event — one per org per day, dedupe-keyed
    await events.emit(
        "cost.org.monthly.computed", source="scheduler",
        org_id=org_id,
        title=(
            f"EOD digest — {org_name} · {totals['calls']} call"
            f"{'' if totals['calls'] == 1 else 's'} across "
            f"{n_agents} agent{'' if n_agents == 1 else 's'} · "
            f"₹{totals['cost_today']/100:.2f}"
        ),
        payload={
            "org_id": org_id, "org_name": org_name,
            "agents_active": n_agents,
            "calls_today": totals["calls"],
            "minutes_today": round(totals["minutes"], 2),
            "cost_paise_today": totals["cost_today"],
            "cost_paise_mtd": totals["cost_mtd"],
            "day": day_start_ist.date().isoformat(),
            "agents": [
                {"id": s["agent_id"], "name": s["agent_name"],
                 "calls": s["calls"], "minutes": round(s["minutes"], 2),
                 "cost_today": s["cost_today"]}
                for s in agent_summaries
            ],
        },
        dedupe_key=f"cost.org.monthly.computed.{org_id}.{day_start_ist.date().isoformat()}",
    )
