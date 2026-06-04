"""Single write + read path for the platform's event ledger.

Every observability feature flows through here. The intent:
  - One write call (`emit`) so call sites are uniform across the codebase.
  - One read call (`list_events`) so the Observability page + future
    customer-facing event API run against the same query shape.
  - Idempotency via `dedupe_key` so jobs that wake repeatedly only
    write the first distinct logical occurrence.
  - Best-effort writes — an emit failure never raises into the caller's
    business logic (we'd rather miss an info-level lifecycle event than
    crash a save_agent).
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from . import db_pg as _db

log = logging.getLogger("eva.events")


# Severity ladder — tight string-set, not a Postgres enum so we can add
# a new level later without a migration. Validation runs in `emit`; an
# unknown value falls back to "info" so a typo never silences a real
# critical event behind a constraint error.
_VALID_SEVERITIES = {"info", "warning", "error", "critical"}
_VALID_SOURCES = {"user", "system", "scheduler", "webhook", "external"}


async def emit(
    kind: str,
    *,
    title: str,
    severity: str = "info",
    source: str = "system",
    org_id: Optional[int] = None,
    agent_id: Optional[int] = None,
    user_id: Optional[int] = None,
    message: Optional[str] = None,
    payload: Optional[dict] = None,
    dedupe_key: Optional[str] = None,
) -> Optional[int]:
    """Append one row to events. Returns the new id, or the existing id
    if `dedupe_key` matched a prior row (idempotent re-emit).

    `kind` is the canonical event type, dotted-namespace:
      `agent.created` · `pricing.drift.detected` · `system.scheduler.run.missed`
    Use `backend/events.py:KINDS` for the registered set; arbitrary
    kinds are accepted so we can ship a new event source without a
    central registry update.
    """
    sev = severity if severity in _VALID_SEVERITIES else "info"
    src = source if source in _VALID_SOURCES else "system"
    payload_json = json.dumps(payload or {}, default=str)
    try:
        pool = await _db.get_pool()
        async with pool.acquire() as conn:
            if dedupe_key:
                # ON CONFLICT DO NOTHING + RETURNING gives the new id on
                # insert and NULL on conflict; we then SELECT for the
                # existing row's id. Cheaper than two round-trips when
                # the row is fresh.
                row = await conn.fetchrow(
                    """
                    INSERT INTO events (
                        kind, severity, source, org_id, agent_id, user_id,
                        title, message, payload, dedupe_key
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10)
                    ON CONFLICT (dedupe_key) DO NOTHING
                    RETURNING id
                    """,
                    kind, sev, src, org_id, agent_id, user_id,
                    title, message, payload_json, dedupe_key,
                )
                if row:
                    return int(row["id"])
                # Conflict — return existing id for the caller's logs.
                existing = await conn.fetchval(
                    "SELECT id FROM events WHERE dedupe_key = $1", dedupe_key,
                )
                return int(existing) if existing else None
            row = await conn.fetchrow(
                """
                INSERT INTO events (
                    kind, severity, source, org_id, agent_id, user_id,
                    title, message, payload
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb)
                RETURNING id
                """,
                kind, sev, src, org_id, agent_id, user_id,
                title, message, payload_json,
            )
            return int(row["id"]) if row else None
    except Exception as e:  # noqa: BLE001
        # Best-effort. An emit failure must never poison a caller's
        # business logic. We log loudly so observability of the
        # observer's failures is at least there in the app logs.
        log.warning("events.emit_failed kind=%s err=%s", kind, e)
        return None


async def list_events(
    *,
    severity: Optional[str] = None,
    kind_prefix: Optional[str] = None,
    org_id: Optional[int] = None,
    agent_id: Optional[int] = None,
    only_open: bool = False,
    limit: int = 200,
    before_id: Optional[int] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Read events with composable filters. `kind_prefix` does a
    LIKE 'prefix.%' match so the caller can say 'all pricing.*' or
    'all agent.*' without enumerating each kind. `before_id` enables
    cursor-style pagination by id (DESC) without OFFSET pain.

    Build 202: `start` / `end` are ISO timestamps applied against
    `created_at` so the admin filter bar can scope the feed to a
    date range (default last 7 days, configurable).
    """
    where: list[str] = []
    args: list[Any] = []
    if severity:
        where.append(f"severity = ${len(args)+1}")
        args.append(severity)
    if kind_prefix:
        where.append(f"(kind = ${len(args)+1} OR kind LIKE ${len(args)+2})")
        args.append(kind_prefix)
        args.append(kind_prefix + ".%")
    if org_id is not None:
        where.append(f"org_id = ${len(args)+1}")
        args.append(int(org_id))
    if agent_id is not None:
        where.append(f"agent_id = ${len(args)+1}")
        args.append(int(agent_id))
    if only_open:
        where.append("resolved_at IS NULL")
    if before_id is not None:
        where.append(f"id < ${len(args)+1}")
        args.append(int(before_id))
    if start:
        where.append(f"created_at >= ${len(args)+1}::timestamptz")
        args.append(start)
    if end:
        where.append(f"created_at < ${len(args)+1}::timestamptz")
        args.append(end)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    args.append(int(max(1, min(limit, 500))))
    sql = (
        "SELECT id, kind, severity, source, org_id, agent_id, user_id, "
        "       title, message, payload, dedupe_key, created_at, "
        "       resolved_at, resolved_by "
        f"FROM events {where_sql} "
        f"ORDER BY id DESC LIMIT ${len(args)}"
    )
    pool = await _db.get_pool()
    async with pool.acquire() as conn:
        rs = await conn.fetch(sql, *args)
    out = []
    for r in rs:
        d = dict(r)
        # asyncpg returns JSONB as str — coerce to dict for callers
        if isinstance(d.get("payload"), str):
            try:
                d["payload"] = json.loads(d["payload"])
            except Exception:  # noqa: BLE001
                d["payload"] = {}
        # ISO-stringify timestamps for the JSON response
        for k in ("created_at", "resolved_at"):
            if d.get(k):
                d[k] = d[k].isoformat()
        out.append(d)
    return out


async def event_counts() -> dict[str, Any]:
    """KPI tiles for the Observability page header. Cheap one-shot
    aggregation across the events table for the last 24h."""
    pool = await _db.get_pool()
    async with pool.acquire() as conn:
        total24 = await conn.fetchval(
            "SELECT COUNT(*) FROM events WHERE created_at > NOW() - INTERVAL '24 hours'"
        )
        open_critical = await conn.fetchval(
            "SELECT COUNT(*) FROM events "
            "WHERE resolved_at IS NULL AND severity IN ('error','critical')"
        )
        drifts_open = await conn.fetchval(
            "SELECT COUNT(*) FROM events "
            "WHERE resolved_at IS NULL AND kind = 'pricing.drift.detected'"
        )
        last_price_check = await conn.fetchrow(
            "SELECT MAX(created_at) AS ts FROM events WHERE kind = 'pricing.observed'"
        )
        # By-kind histogram for last 24h, top 8
        by_kind = await conn.fetch(
            "SELECT kind, COUNT(*) AS n FROM events "
            "WHERE created_at > NOW() - INTERVAL '24 hours' "
            "GROUP BY kind ORDER BY n DESC LIMIT 8"
        )
        # By-severity breakdown last 24h
        by_severity = await conn.fetch(
            "SELECT severity, COUNT(*) AS n FROM events "
            "WHERE created_at > NOW() - INTERVAL '24 hours' "
            "GROUP BY severity"
        )
    return {
        "total_24h": int(total24 or 0),
        "open_critical": int(open_critical or 0),
        "drifts_open": int(drifts_open or 0),
        "last_price_check": last_price_check["ts"].isoformat() if last_price_check and last_price_check["ts"] else None,
        "by_kind": [{"kind": r["kind"], "n": int(r["n"])} for r in by_kind],
        "by_severity": {r["severity"]: int(r["n"]) for r in by_severity},
    }


async def resolve_event(event_id: int, user_id: int) -> bool:
    """Mark an event resolved. No-op if already resolved or non-existent."""
    pool = await _db.get_pool()
    async with pool.acquire() as conn:
        n = await conn.fetchval(
            "UPDATE events SET resolved_at = NOW(), resolved_by = $2 "
            "WHERE id = $1 AND resolved_at IS NULL "
            "RETURNING id",
            int(event_id), int(user_id),
        )
    return n is not None


# ─── Registered kinds (documentation only — not enforced) ────────────────
# Kept as a constant so editors can grep for it and any new kind shows up
# in code review. New kinds are valid the moment emit() is called with
# them; this is a help, not a gatekeeper.
KINDS = {
    # agent lifecycle
    "agent.created", "agent.updated", "agent.published", "agent.unpublished",
    "agent.deleted", "agent.purpose.changed", "agent.knowledge.imported",
    "agent.info_groups.regenerated", "agent.voice.changed",
    # calls
    "call.completed", "call.abandoned", "call.outcome.captured",
    # cost
    "cost.agent.monthly.computed", "cost.agent.threshold.warning",
    "cost.agent.threshold.exceeded",
    "cost.org.monthly.computed",
    "cost.org.threshold.warning", "cost.org.threshold.exceeded",
    # pricing
    "pricing.observed", "pricing.drift.detected", "pricing.rate.rolled_forward",
    # notify
    "notify.email.sent", "notify.email.failed",
    "notify.whatsapp.sent", "notify.slack.sent",
    # quality
    "quality.purpose.conversion.dropped",
    "quality.outcome.distribution.shifted",
    "quality.lead_quality.collapsed",
    "quality.sentiment.trending_negative",
    # system
    "system.gemini.error_rate.high", "system.scheduler.run.ok",
    "system.scheduler.run.missed", "system.telephony.provider.error_spike",
    "system.webhook.delivery.failed",
}
