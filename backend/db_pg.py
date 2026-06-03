"""Postgres backend — asyncpg-driven.

The single DB backend after Phase 6. `db.py` is a thin re-export shim
over this module; app code calls `from backend import db; await
db.list_agents(...)`. Two notable shape differences vs the original
SQLite implementation, both transparent to callers:

  * Timestamps come back as `datetime` objects, not ISO strings. Our routes
    JSON-encode via FastAPI which serializes datetime → ISO automatically.
  * JSONB columns come back as Python dict/list, so the `json.loads` wrapping
    that `_row_to_dict` did in SQLite is no longer needed.

All public functions are `async`. Callers that were sync must `await`.
A small sync facade lives at the bottom for code paths that haven't migrated
yet (e.g. background scripts) — it spins up a private loop and runs the
coroutine. Use sparingly; everything in the FastAPI request path should be
properly async.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
from datetime import datetime, timezone
from typing import Any, Optional

import asyncpg

from .presets import DEFAULT_VOICE as _DEFAULT_VOICE

# ─── pool ────────────────────────────────────────────────────────────────

_POOL: Optional[asyncpg.Pool] = None
_POOL_LOCK = asyncio.Lock()


def _pg_url() -> str:
    url = os.environ.get("PG_URL") or os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("PG_URL / DATABASE_URL not set")
    # asyncpg wants `postgres://` or `postgresql://`, not the `+psycopg` variants
    # we use for Alembic. Strip the driver suffix if present.
    url = re.sub(r"^postgresql\+\w+://", "postgresql://", url)
    return url


def _pool_sizes() -> tuple[int, int]:
    """Read pool min/max from env (PG_POOL_MIN, PG_POOL_MAX) with a sensible
    default. The Phase-9a audit flagged the previous hardcoded 2/10 as
    starve-prone — at 100 calls/sec one slow admin query can park the
    FastAPI queue. Defaults bumped to 4/24 for direct-Postgres
    deployments; behind PgBouncer transaction-pooling set both to 4–8
    so the app side stays light and the bouncer multiplexes."""
    try:
        mn = max(1, int(os.environ.get("PG_POOL_MIN", "4")))
        mx = max(mn, int(os.environ.get("PG_POOL_MAX", "24")))
    except ValueError:
        mn, mx = 4, 24
    return mn, mx


async def get_pool() -> asyncpg.Pool:
    global _POOL
    if _POOL is None:
        async with _POOL_LOCK:
            if _POOL is None:
                mn, mx = _pool_sizes()
                _POOL = await asyncpg.create_pool(
                    dsn=_pg_url(),
                    min_size=mn,
                    max_size=mx,
                    # Decode JSONB columns to Python dict/list automatically.
                    init=_init_codecs,
                )
    return _POOL


async def _init_codecs(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec(
        "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )
    await conn.set_type_codec(
        "json", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )


async def close_pool() -> None:
    global _POOL
    if _POOL is not None:
        await _POOL.close()
        _POOL = None


# ─── helpers ─────────────────────────────────────────────────────────────

FOUNDER_EMAIL = "dipesh.majumder@webspiders.com"
FOUNDER_NAME = "Dipesh"


def _slugify(name: str) -> str:
    s = (name or "").strip().lower()
    s = re.sub(r"[^a-z0-9\s-]+", "", s)
    s = re.sub(r"[\s_-]+", "-", s)
    s = s.strip("-")
    return s or "agent"


async def _unique_slug(conn: asyncpg.Connection, base: str, org_id: int,
                        exclude_id: int | None = None) -> str:
    """Reserve a slug **within an org**. Phase 9b made agent slugs
    org-scoped (composite UNIQUE on (org_id, slug)) so two different
    orgs can both own a "support-bot". The SELECT-then-INSERT race is
    mitigated by the UNIQUE index — on collision, `create_agent`
    retries via its outer loop."""
    slug = base
    i = 2
    while True:
        if exclude_id is None:
            row = await conn.fetchval(
                "SELECT 1 FROM agents WHERE org_id = $1 AND slug = $2",
                org_id, slug,
            )
        else:
            row = await conn.fetchval(
                "SELECT 1 FROM agents WHERE org_id = $1 AND slug = $2 AND id != $3",
                org_id, slug, exclude_id,
            )
        if not row:
            return slug
        slug = f"{base}-{i}"
        i += 1


def _record_to_dict(r: asyncpg.Record | None) -> Optional[dict[str, Any]]:
    return dict(r) if r else None


def _records_to_list(rs: list[asyncpg.Record]) -> list[dict[str, Any]]:
    return [dict(r) for r in rs]


# ─── init (no-op for Postgres) ───────────────────────────────────────────

async def init() -> None:
    """Postgres schema is owned by Alembic, not by app boot. Kept as a
    function so the existing `db.init()` call at startup doesn't break —
    we just verify connectivity here plus do two idempotent seeds:

      1. founder user (if absent) — keeps the X-User-Id stub auth path
         functional on a fresh DB.
      2. founder as super_admin — the 0003 migration tries to do this
         too, but on a freshly-reset schema the founder doesn't exist
         yet when the migration runs. The seed has to live somewhere
         that runs AFTER user-creation, so it lives here."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.fetchval("SELECT 1")
    existing = await get_user_by_email(FOUNDER_EMAIL)
    if not existing:
        existing = await create_user(FOUNDER_EMAIL, FOUNDER_NAME, provider="stub")
    # Idempotent super-admin grant for founder. ON CONFLICT DO NOTHING means
    # re-running boot doesn't churn the grant timestamp.
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO super_admins (user_id) VALUES ($1) "
            "ON CONFLICT (user_id) DO NOTHING",
            existing["id"],
        )


# ─── users ───────────────────────────────────────────────────────────────

async def get_user(user_id: int) -> Optional[dict[str, Any]]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        r = await conn.fetchrow(
            "SELECT id, email, name, avatar_url, provider, created_at "
            "FROM users WHERE id = $1",
            user_id,
        )
    return _record_to_dict(r)


async def get_user_by_email(email: str) -> Optional[dict[str, Any]]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        r = await conn.fetchrow(
            "SELECT id, email, name, avatar_url, provider, created_at "
            "FROM users WHERE lower(email) = lower($1)",
            (email or "").strip(),
        )
    return _record_to_dict(r)


async def create_user(email: str, name: Optional[str] = None, provider: str = "stub") -> dict[str, Any]:
    """Idempotent. Same contract as SQLite db.create_user — also auto-spins
    an org named "{display}'s workspace", links it on users.org_id, and
    inserts the user as the OWNER of that org in org_members.

    The org_members insert is essential: without it, list_agents(user_id)
    (which filters by org_members) would return 0 even for the user's own
    agents. Phase 2 made membership the source of truth — signup has to
    seed it."""
    existing = await get_user_by_email(email)
    if existing:
        return existing
    display = (name or email.split("@")[0]).strip()
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            org_id = await conn.fetchval(
                "INSERT INTO orgs (name) VALUES ($1) RETURNING id",
                f"{display}'s workspace",
            )
            free_id = await conn.fetchval("SELECT id FROM plans WHERE slug = 'free'")
            user_id = await conn.fetchval(
                "INSERT INTO users (email, name, provider, org_id, plan_id) "
                "VALUES (lower($1), $2, $3, $4, $5) RETURNING id",
                (email or "").strip(),
                (name or "").strip() or None,
                provider,
                org_id,
                free_id,
            )
            await conn.execute(
                "INSERT INTO org_members (org_id, user_id, role) VALUES ($1, $2, 'owner')",
                org_id, user_id,
            )
    return await get_user(user_id)  # type: ignore[return-value]


async def get_founder() -> dict[str, Any]:
    u = await get_user_by_email(FOUNDER_EMAIL)
    return u or {"id": 1, "email": FOUNDER_EMAIL, "name": FOUNDER_NAME}


# ─── orgs ────────────────────────────────────────────────────────────────

async def get_org_for_user(user_id: int) -> Optional[dict[str, Any]]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        r = await conn.fetchrow(
            """
            SELECT o.id, o.name, o.country, o.tax_id, o.billing_address,
                   o.currency, o.timezone, o.created_at
            FROM orgs o JOIN users u ON u.org_id = o.id
            WHERE u.id = $1
            """,
            user_id,
        )
    return _record_to_dict(r)


_ORG_PATCHABLE = ("name", "country", "tax_id", "billing_address", "currency", "timezone")


async def update_org_for_user(user_id: int, fields: dict[str, Any]) -> Optional[dict[str, Any]]:
    safe = {k: v for k, v in (fields or {}).items() if k in _ORG_PATCHABLE}
    if not safe:
        return await get_org_for_user(user_id)
    org = await get_org_for_user(user_id)
    if not org:
        return None
    # Build $1, $2, … placeholders matching dict insertion order.
    cols = list(safe.keys())
    sets = ", ".join(f"{c} = ${i+1}" for i, c in enumerate(cols))
    params = [safe[c] for c in cols]
    params.append(org["id"])
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(f"UPDATE orgs SET {sets} WHERE id = ${len(params)}", *params)
    return await get_org_for_user(user_id)


# ─── plans ───────────────────────────────────────────────────────────────

async def list_plans() -> list[dict[str, Any]]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rs = await conn.fetch(
            "SELECT id, slug, label, tagline, price_paise, currency, minutes_total, features, sort_order "
            "FROM plans WHERE is_active = true ORDER BY sort_order ASC, id ASC"
        )
    return _records_to_list(rs)


async def get_plan(plan_id: int | None) -> Optional[dict[str, Any]]:
    if plan_id is None:
        return None
    pool = await get_pool()
    async with pool.acquire() as conn:
        r = await conn.fetchrow(
            "SELECT id, slug, label, tagline, price_paise, currency, minutes_total, features "
            "FROM plans WHERE id = $1",
            plan_id,
        )
    return _record_to_dict(r)


async def get_plan_by_slug(slug: str) -> Optional[dict[str, Any]]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        r = await conn.fetchrow(
            "SELECT id, slug, label, tagline, price_paise, currency, minutes_total, features "
            "FROM plans WHERE slug = $1",
            slug,
        )
    return _record_to_dict(r)


async def get_user_plan_state(user_id: int) -> dict[str, Any]:
    _FALLBACK_FREE = {"slug": "free", "label": "Free", "minutes_total": 30, "features": []}
    pool = await get_pool()
    async with pool.acquire() as conn:
        r = await conn.fetchrow(
            "SELECT plan_id, minutes_used, plan_started_at FROM users WHERE id = $1",
            user_id,
        )
    if not r:
        plan = await get_plan_by_slug("free") or _FALLBACK_FREE
        total = plan.get("minutes_total", 30)
        return {"plan": plan, "minutes_total": total, "minutes_used": 0.0,
                "minutes_left": total, "plan_started_at": None}
    plan = await get_plan(r["plan_id"]) if r["plan_id"] else await get_plan_by_slug("free")
    if not plan:
        plan = await get_plan_by_slug("free") or _FALLBACK_FREE
    used = float(r["minutes_used"] or 0)
    total = int(plan.get("minutes_total") or 0)
    return {
        "plan": plan,
        "minutes_total": total,
        "minutes_used": round(used, 1),
        "minutes_left": max(0, round(total - used, 1)),
        "plan_started_at": r["plan_started_at"],
    }


async def set_user_plan(user_id: int, plan_id: int, reset_usage: bool = True) -> dict[str, Any]:
    if not await get_plan(plan_id):
        raise ValueError(f"unknown plan_id {plan_id!r}")
    pool = await get_pool()
    async with pool.acquire() as conn:
        if reset_usage:
            await conn.execute(
                "UPDATE users SET plan_id = $1, minutes_used = 0, plan_started_at = now() WHERE id = $2",
                plan_id, user_id,
            )
        else:
            await conn.execute(
                "UPDATE users SET plan_id = $1, plan_started_at = now() WHERE id = $2",
                plan_id, user_id,
            )
    return await get_user_plan_state(user_id)


# ─── agents ──────────────────────────────────────────────────────────────

# Columns we map straight through (scalar). JSONB columns are listed separately
# because asyncpg + our jsonb codec want them as native Python values.
_AGENT_SCALARS = ("name", "sector", "locale", "persona", "greeting",
                  "system_prompt", "voice", "webhook_url")
_AGENT_JSON = ("guardrails", "connectors", "sip_config", "voice_tweaks",
               "outcomes", "policy", "webhook_headers", "variables", "purpose",
               "small_talk", "extra_info", "info_groups", "outcome_weights")
# Per the baseline schema, these JSONB cols are NOT NULL with a structural
# default. Callers (and the SQLite-compat contract) sometimes send `None` to
# mean "clear" — we coerce to the column default so the constraint holds.
_AGENT_JSON_NOT_NULL = {
    "guardrails": [],
    "connectors": [],
    "outcomes":   [],
    "variables":  {},
    "purpose":    {},
    "small_talk": [],
    "extra_info": {},
}


async def create_agent(payload: dict[str, Any], user_id: int,
                        org_id: Optional[int] = None) -> dict[str, Any]:
    """Create an agent owned by an org. If org_id isn't supplied, we look it
    up from the user's primary org (users.org_id) — Phase 2 still keeps that
    pointer around for single-org backward compatibility.

    Slug-collision handling: `_unique_slug` does SELECT-then-INSERT under
    one connection, which is racy. The DB's UNIQUE constraint on
    agents.slug is the safety net — on a concurrent collision we retry
    up to 3 times with a freshly-resolved slug. Beyond 3 attempts we
    surface the original error rather than burning the request budget.
    """
    name = (payload.get("name") or "Untitled Agent").strip()
    base_slug = _slugify(name)
    pool = await get_pool()
    last_err: Optional[Exception] = None
    for attempt in range(3):
        try:
            async with pool.acquire() as conn:
                if org_id is None:
                    org_id = await conn.fetchval(
                        "SELECT org_id FROM users WHERE id = $1", user_id,
                    )
                    if org_id is None:
                        raise ValueError(f"user {user_id} has no org — cannot create agent")
                async with conn.transaction():
                    slug = await _unique_slug(conn, base_slug, org_id)
                    agent_id = await conn.fetchval(
                        """
                        INSERT INTO agents (
                            user_id, org_id, slug, name, sector, locale, persona, greeting,
                            system_prompt, voice,
                            guardrails, connectors, sip_config, voice_tweaks,
                            outcomes, policy, webhook_url, webhook_headers, variables,
                            purpose, small_talk, extra_info, info_groups
                        ) VALUES (
                            $1, $2, $3, $4, $5, $6, $7, $8,
                            $9, $10,
                            $11, $12, $13, $14,
                            $15, $16, $17, $18, $19,
                            $20, $21, $22, $23
                        ) RETURNING id
                        """,
                        user_id, org_id, slug, name,
                        payload.get("sector"), payload.get("locale"),
                        payload.get("persona"), payload.get("greeting"),
                        payload.get("system_prompt") or "",
                        payload.get("voice") or _DEFAULT_VOICE,
                        payload.get("guardrails") or [],
                        payload.get("connectors") or [],
                        payload.get("sip_config"),
                        payload.get("voice_tweaks"),
                        payload.get("outcomes") or [],
                        payload.get("policy"),
                        (payload.get("webhook_url") or "").strip() or None,
                        payload.get("webhook_headers"),
                        payload.get("variables") or {},
                        payload.get("purpose") or {},
                        payload.get("small_talk") or [],
                        payload.get("extra_info") or {},
                        payload.get("info_groups"),  # NULL → sector fallback
                    )
            return await get_agent(agent_id)  # type: ignore[return-value]
        except asyncpg.exceptions.UniqueViolationError as e:
            # Slug raced with another concurrent insert. `_unique_slug`
            # walked to slug-2 / slug-3 but a sibling request beat us
            # to that exact id. Retry; the next `_unique_slug` call
            # will see the freshly-inserted row and pick the next gap.
            last_err = e
            continue
    raise last_err or RuntimeError("create_agent failed after 3 attempts")


async def get_agent(agent_id: int) -> Optional[dict[str, Any]]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        r = await conn.fetchrow("SELECT * FROM agents WHERE id = $1", agent_id)
    return _record_to_dict(r)


async def get_agent_by_slug(slug: str, org_id: Optional[int] = None) -> Optional[dict[str, Any]]:
    """Resolve a slug to an agent. Phase 9b made slugs org-scoped (composite
    UNIQUE on `(org_id, slug)`), so passing an org_id is the correct
    full-fidelity lookup. Without org_id we return the first match — the
    caller's permission shim (`require_agent_member`) is the safety net
    that prevents cross-tenant access via slug guess."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if org_id is not None:
            r = await conn.fetchrow(
                "SELECT * FROM agents WHERE org_id = $1 AND slug = $2",
                org_id, slug,
            )
        else:
            r = await conn.fetchrow("SELECT * FROM agents WHERE slug = $1", slug)
    return _record_to_dict(r)


async def delete_agent(agent_id: int) -> bool:
    """`ON DELETE CASCADE` on calls.agent_id and number_requests.agent_id
    means the parent delete cleans up children atomically. No explicit
    child-deletes needed."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute("DELETE FROM agents WHERE id = $1", agent_id)
    # asyncpg returns "DELETE <n>"
    return result.endswith(" 0") is False


async def update_agent(agent_id: int, patch: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Same allow-list semantics as SQLite db.update_agent."""
    fields: dict[str, Any] = {}
    for k, v in (patch or {}).items():
        if k in _AGENT_SCALARS:
            fields[k] = v
        elif k in _AGENT_JSON:
            # JSONB codec handles encoding. For NOT NULL cols, coerce None
            # to the column's structural default so SQLite-compat "set to
            # null to clear" callers don't trip the NOT NULL constraint.
            if v is None and k in _AGENT_JSON_NOT_NULL:
                fields[k] = _AGENT_JSON_NOT_NULL[k]
            else:
                fields[k] = v
        elif k == "published":
            new_val = bool(v)
            fields["published"] = new_val
            if new_val:
                fields["published_at"] = datetime.now(timezone.utc)
    if not fields:
        return await get_agent(agent_id)
    cols = list(fields.keys())
    sets = ", ".join(f"{c} = ${i+1}" for i, c in enumerate(cols))
    params = [fields[c] for c in cols]
    params.append(agent_id)
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            f"UPDATE agents SET {sets} WHERE id = ${len(params)}",
            *params,
        )
    # asyncpg returns "UPDATE <n>"
    if result.endswith(" 0"):
        # Verify the row exists — Postgres counts matched rows so 0 means missing.
        exists = await get_agent(agent_id)
        return exists  # None if truly missing; otherwise the row (no-op patch)
    return await get_agent(agent_id)


async def list_agents(user_id: int) -> list[dict[str, Any]]:
    """List every agent in every org the user is a member of.

    Phase 9b: reads denormalised `agents.calls_count` and
    `agents.last_call_at` directly instead of the previous correlated
    subqueries against `calls`. At the 12-month target (50k agents,
    5M calls) this took the listing from ~300ms to single-digit-ms —
    the per-agent read is now bounded by the agents index hit alone.

    The denormalised columns are maintained transactionally inside
    `insert_call` (same txn as the calls insert + rollup UPSERTs), so
    they can never lag behind the calls table by more than a tx
    commit window."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rs = await conn.fetch(
            """
            SELECT a.id, a.name, a.slug, a.sector, a.locale, a.voice,
                   a.persona, a.greeting, a.variables,
                   a.org_id, a.user_id AS created_by,
                   a.created_at, a.published, a.published_at,
                   a.calls_count, a.last_call_at
              FROM agents a
             WHERE a.org_id IN (
               SELECT org_id FROM org_members WHERE user_id = $1
             )
             ORDER BY a.id DESC
            """,
            user_id,
        )
    return _records_to_list(rs)


# ─── calls ───────────────────────────────────────────────────────────────

async def insert_call(record: dict[str, Any]) -> int:
    """Inserts a call AND UPSERTs the agent_daily_stats + org_daily_stats
    rollups in the same transaction. If `cost_paise` isn't supplied,
    we compute it from `model_id` + token counts via pricing.py — the
    write path is the single point where call-cost gets locked in.

    Rollups update atomically so the analytics dashboard never sees a
    call without its contribution to the day's total."""
    from . import pricing  # local import — pricing has no side effects
    in_tok = record.get("input_tokens")
    out_tok = record.get("output_tokens")
    model_id = record.get("model_id")
    explicit_cost = record.get("cost_paise")
    cost = explicit_cost if explicit_cost is not None else pricing.cost_paise(model_id, in_tok, out_tok)
    duration_s = float(record.get("duration_s") or 0)
    minutes = duration_s / 60.0
    outcome_key = record.get("outcome") or "unknown"
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Look up the org + created_by user FIRST so we can stamp the
            # calls row with org_id directly (Phase 9b — immutable per-call
            # tenant attribution). The trigger `calls_org_stamp` would fill
            # this on its own if we passed NULL, but writing it explicitly
            # is the defense-in-depth pattern: the DB still has the
            # trigger as a backstop on every future code path.
            row = await conn.fetchrow(
                "SELECT org_id, user_id FROM agents WHERE id = $1",
                int(record["agent_id"]),
            )
            org_id = row["org_id"] if row else None
            user_id = row["user_id"] if row else None
            ended_ts = _parse_ts(record.get("ended_at"))
            cid = await conn.fetchval(
                """
                INSERT INTO calls (
                    agent_id, org_id, started_at, ended_at, duration_s,
                    outcome, reason, summary, final_message,
                    extracted, transcript,
                    input_tokens, output_tokens, cached_tokens, model_id, cost_paise,
                    sentiment, lead_quality, lead_signals
                ) VALUES (
                    $1, $2, COALESCE($3, now()), COALESCE($4, now()), $5,
                    $6, $7, $8, $9,
                    $10, $11,
                    $12, $13, $14, $15, $16,
                    $17, $18, $19
                ) RETURNING id
                """,
                int(record["agent_id"]),
                org_id,
                _parse_ts(record.get("started_at")),
                ended_ts,
                duration_s,
                record.get("outcome"),
                record.get("reason"),
                record.get("summary"),
                record.get("final_message"),
                record.get("extracted"),
                record.get("transcript"),
                in_tok, out_tok,
                record.get("cached_tokens"),
                model_id,
                cost,
                record.get("sentiment"),
                record.get("lead_quality"),
                record.get("lead_signals"),
            )
            # Phase 9b denormalisation — keep agents.last_call_at + calls_count
            # in lockstep with the calls table. `list_agents` reads these
            # directly now instead of running correlated subqueries. Falls
            # back to `now()` if the caller didn't provide `ended_at` (the
            # calls row's own ended_at defaults to now() via column DEFAULT,
            # so this keeps both columns in agreement).
            await conn.execute(
                """
                UPDATE agents
                   SET calls_count  = calls_count + 1,
                       last_call_at = GREATEST(
                         COALESCE(last_call_at, now()),
                         COALESCE($1::timestamptz, now())
                       )
                 WHERE id = $2
                """,
                ended_ts,
                int(record["agent_id"]),
            )
            # Universal LLM-cost ledger row, in the same transaction so
            # tokens + cost can never desync from the customer-call record.
            await conn.execute(
                """
                INSERT INTO llm_calls (
                    kind, user_id, org_id, agent_id, call_id,
                    started_at, ended_at, duration_s,
                    input_tokens, output_tokens, cached_tokens, model_id, cost_paise
                ) VALUES (
                    'agent', $1, $2, $3, $4,
                    COALESCE($5, now()), COALESCE($6, now()), $7,
                    COALESCE($8,0), COALESCE($9,0), COALESCE($10,0), $11, $12
                )
                """,
                user_id, org_id, int(record["agent_id"]), cid,
                _parse_ts(record.get("started_at")),
                _parse_ts(record.get("ended_at")),
                duration_s,
                in_tok, out_tok, record.get("cached_tokens"),
                model_id, cost,
            )
            # agent_daily_stats — ON CONFLICT do atomic increment. Outcomes
            # JSONB gets merged: existing[outcome] + 1.
            await conn.execute(
                """
                INSERT INTO agent_daily_stats AS s (
                    agent_id, day, calls, minutes, input_tokens, output_tokens,
                    cost_paise, outcomes
                )
                VALUES ($1, current_date, 1, $2, COALESCE($3,0), COALESCE($4,0),
                         COALESCE($5,0), jsonb_build_object($6::text, 1))
                ON CONFLICT (agent_id, day) DO UPDATE SET
                  calls         = s.calls + 1,
                  minutes       = s.minutes + EXCLUDED.minutes,
                  input_tokens  = s.input_tokens + COALESCE(EXCLUDED.input_tokens, 0),
                  output_tokens = s.output_tokens + COALESCE(EXCLUDED.output_tokens, 0),
                  cost_paise    = s.cost_paise + COALESCE(EXCLUDED.cost_paise, 0),
                  outcomes      = jsonb_set(
                    s.outcomes,
                    ARRAY[$6::text],
                    to_jsonb(COALESCE((s.outcomes->>$6)::int, 0) + 1)
                  )
                """,
                int(record["agent_id"]), minutes, in_tok, out_tok, cost, outcome_key,
            )
            if org_id is not None:
                await conn.execute(
                    """
                    INSERT INTO org_daily_stats AS s (
                        org_id, day, calls, minutes, input_tokens, output_tokens, cost_paise
                    )
                    VALUES ($1, current_date, 1, $2, COALESCE($3,0), COALESCE($4,0), COALESCE($5,0))
                    ON CONFLICT (org_id, day) DO UPDATE SET
                      calls         = s.calls + 1,
                      minutes       = s.minutes + EXCLUDED.minutes,
                      input_tokens  = s.input_tokens + COALESCE(EXCLUDED.input_tokens, 0),
                      output_tokens = s.output_tokens + COALESCE(EXCLUDED.output_tokens, 0),
                      cost_paise    = s.cost_paise + COALESCE(EXCLUDED.cost_paise, 0)
                    """,
                    org_id, minutes, in_tok, out_tok, cost,
                )
    return cid


def _parse_ts(v: Any) -> Optional[datetime]:
    """Coerce ISO strings to aware datetime, leave datetime alone, None passthrough."""
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, str):
        try:
            d = datetime.fromisoformat(v.replace("Z", "+00:00"))
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


async def get_call_detail(agent_id: int, call_id: int) -> Optional[dict[str, Any]]:
    """Full call row for the Call Details modal (build 188). Includes
    transcript + extracted + final_message, which the list endpoint omits
    to keep the table response small."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        r = await conn.fetchrow(
            "SELECT id, agent_id, started_at, ended_at, duration_s, outcome, reason, "
            "       summary, final_message, extracted, transcript, "
            "       sentiment, lead_quality, lead_signals "
            "FROM calls WHERE id = $1 AND agent_id = $2",
            int(call_id), int(agent_id),
        )
    return _record_to_dict(r) if r else None


async def list_calls_for_agent(agent_id: int, limit: int = 50) -> list[dict[str, Any]]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rs = await conn.fetch(
            "SELECT id, agent_id, started_at, ended_at, duration_s, outcome, reason, summary, "
            "       sentiment, lead_quality, lead_signals "
            "FROM calls WHERE agent_id = $1 ORDER BY id DESC LIMIT $2",
            agent_id, limit,
        )
    return _records_to_list(rs)


async def call_stats_for_agent(agent_id: int) -> dict[str, Any]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        total = await conn.fetchval(
            "SELECT COUNT(*) FROM calls WHERE agent_id = $1", agent_id
        )
        rs = await conn.fetch(
            "SELECT outcome, COUNT(*) AS n FROM calls "
            "WHERE agent_id = $1 GROUP BY outcome ORDER BY n DESC",
            agent_id,
        )
    outcomes = [{"outcome": r["outcome"] or "unknown", "count": r["n"]} for r in rs]
    return {"total": total or 0, "outcomes": outcomes}


# ─── number_requests ─────────────────────────────────────────────────────

async def list_number_requests_for_agent(agent_id: int, limit: int = 100) -> list[dict[str, Any]]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rs = await conn.fetch(
            "SELECT id, agent_id, country, city, delivery_handle, notes, status, created_at "
            "FROM number_requests WHERE agent_id = $1 ORDER BY id DESC LIMIT $2",
            agent_id, limit,
        )
    return _records_to_list(rs)


async def create_number_request(payload: dict[str, Any], user_id: int) -> dict[str, Any]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        r = await conn.fetchrow(
            """
            INSERT INTO number_requests (
                agent_id, user_id, country, city, delivery_handle, notes, status
            ) VALUES ($1, $2, $3, $4, $5, $6, 'pending')
            RETURNING id, agent_id, user_id, country, city, delivery_handle,
                      notes, status, created_at
            """,
            int(payload["agent_id"]),
            user_id,
            (payload.get("country") or "").strip() or None,
            (payload.get("city") or "").strip() or None,
            (payload.get("delivery_handle") or "").strip(),
            (payload.get("notes") or "").strip() or None,
        )
    return dict(r)


# ─── org_members (Phase 2 — teams) ───────────────────────────────────────

async def list_org_members(org_id: int) -> list[dict[str, Any]]:
    """Members of an org with role + display info. Joined to users so the
    UI doesn't need a second fetch to show names/avatars."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rs = await conn.fetch(
            """
            SELECT m.org_id, m.user_id, m.role, m.invited_by, m.joined_at,
                   u.email, u.name, u.avatar_url
              FROM org_members m
              JOIN users u ON u.id = m.user_id
             WHERE m.org_id = $1
             ORDER BY
               CASE m.role WHEN 'owner' THEN 0 WHEN 'admin' THEN 1 ELSE 2 END,
               m.joined_at ASC
            """,
            org_id,
        )
    return _records_to_list(rs)


async def get_member_role(org_id: int, user_id: int) -> Optional[str]:
    """Returns 'owner' | 'admin' | 'member' | None (not a member)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        r = await conn.fetchval(
            "SELECT role FROM org_members WHERE org_id = $1 AND user_id = $2",
            org_id, user_id,
        )
    return r


async def add_org_member(org_id: int, user_id: int, role: str,
                          invited_by: Optional[int] = None) -> dict[str, Any]:
    """Idempotent — on conflict (already a member) returns the existing
    row without changing the role. Use update_member_role to change."""
    if role not in ("owner", "admin", "member"):
        raise ValueError(f"bad role {role!r}")
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO org_members (org_id, user_id, role, invited_by)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (org_id, user_id) DO NOTHING
            """,
            org_id, user_id, role, invited_by,
        )
        r = await conn.fetchrow(
            "SELECT org_id, user_id, role, invited_by, joined_at "
            "FROM org_members WHERE org_id = $1 AND user_id = $2",
            org_id, user_id,
        )
    return dict(r) if r else None  # type: ignore[return-value]


async def update_member_role(org_id: int, user_id: int, role: str) -> Optional[dict[str, Any]]:
    """Owner-only operation. Returns the updated row or None if member missing."""
    if role not in ("owner", "admin", "member"):
        raise ValueError(f"bad role {role!r}")
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE org_members SET role = $1 WHERE org_id = $2 AND user_id = $3",
            role, org_id, user_id,
        )
        if result.endswith(" 0"):
            return None
        r = await conn.fetchrow(
            "SELECT org_id, user_id, role, invited_by, joined_at "
            "FROM org_members WHERE org_id = $1 AND user_id = $2",
            org_id, user_id,
        )
    return dict(r) if r else None


async def remove_org_member(org_id: int, user_id: int) -> bool:
    """Removes a member from an org. Caller must enforce "can't remove the
    last owner" — this function just does the delete."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM org_members WHERE org_id = $1 AND user_id = $2",
            org_id, user_id,
        )
    return not result.endswith(" 0")


async def count_owners(org_id: int) -> int:
    """Used to enforce the "every org needs at least one owner" invariant."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT COUNT(*) FROM org_members WHERE org_id = $1 AND role = 'owner'",
            org_id,
        ) or 0


async def list_orgs_for_user(user_id: int) -> list[dict[str, Any]]:
    """Every org the user is a member of, with their role. Powers the future
    multi-org picker in the topbar."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rs = await conn.fetch(
            """
            SELECT o.id, o.name, o.country, m.role, m.joined_at
              FROM org_members m
              JOIN orgs o ON o.id = m.org_id
             WHERE m.user_id = $1
             ORDER BY m.joined_at ASC
            """,
            user_id,
        )
    return _records_to_list(rs)


# ─── org_invites ─────────────────────────────────────────────────────────

async def create_invite(org_id: int, email: str, role: str, invited_by: int,
                         ttl_days: int = 7) -> dict[str, Any]:
    """Issues a fresh invite with a 32-byte urlsafe token. Caller maps that
    token into the link sent over email/Slack/whatever channel.

    If an active invite for the same (org, email) already exists, returns
    that one instead of issuing a duplicate — keeps the inbox clean."""
    if role not in ("admin", "member"):
        raise ValueError(f"bad role {role!r} (invites can't grant 'owner')")
    email_norm = (email or "").strip().lower()
    if not email_norm:
        raise ValueError("email required")
    pool = await get_pool()
    async with pool.acquire() as conn:
        existing = await conn.fetchrow(
            """
            SELECT id, token, expires_at FROM org_invites
             WHERE org_id = $1 AND lower(email) = $2
               AND accepted_at IS NULL AND declined_at IS NULL AND revoked_at IS NULL
               AND expires_at > now()
             ORDER BY id DESC LIMIT 1
            """,
            org_id, email_norm,
        )
        if existing:
            r = await conn.fetchrow(
                "SELECT * FROM org_invites WHERE id = $1", existing["id"]
            )
            return dict(r)
        token = secrets.token_urlsafe(32)
        r = await conn.fetchrow(
            """
            INSERT INTO org_invites (org_id, email, role, token, invited_by, expires_at)
            VALUES ($1, $2, $3, $4, $5, now() + ($6 || ' days')::interval)
            RETURNING *
            """,
            org_id, email_norm, role, token, invited_by, str(ttl_days),
        )
    return dict(r)


async def get_invite_by_token(token: str) -> Optional[dict[str, Any]]:
    """Returns the invite + org name (for the accept-invite preview screen)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        r = await conn.fetchrow(
            """
            SELECT i.*, o.name AS org_name, u.email AS inviter_email, u.name AS inviter_name
              FROM org_invites i
              JOIN orgs o ON o.id = i.org_id
              LEFT JOIN users u ON u.id = i.invited_by
             WHERE i.token = $1
            """,
            token,
        )
    return dict(r) if r else None


async def list_pending_invites(org_id: int) -> list[dict[str, Any]]:
    """Active invites only — not accepted, declined, revoked, or expired."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rs = await conn.fetch(
            """
            SELECT id, org_id, email, role, invited_by, expires_at, created_at
              FROM org_invites
             WHERE org_id = $1
               AND accepted_at IS NULL AND declined_at IS NULL AND revoked_at IS NULL
               AND expires_at > now()
             ORDER BY id DESC
            """,
            org_id,
        )
    return _records_to_list(rs)


async def accept_invite(token: str, user_id: int) -> Optional[dict[str, Any]]:
    """Atomically: validate the invite, mark it accepted, and add the user
    to org_members. Returns the membership row, or None if invite invalid.

    Edge cases:
      - already accepted / declined / revoked → returns None
      - expired → returns None
      - user already a member → still PASS (idempotent — accepted_at stamped,
        membership unchanged)
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            inv = await conn.fetchrow(
                """
                SELECT id, org_id, role, expires_at, accepted_at, declined_at, revoked_at
                  FROM org_invites WHERE token = $1
                  FOR UPDATE
                """,
                token,
            )
            if not inv:
                return None
            from datetime import datetime, timezone as _tz
            now = datetime.now(_tz.utc)
            if inv["accepted_at"] or inv["declined_at"] or inv["revoked_at"]:
                return None
            if inv["expires_at"] and inv["expires_at"] < now:
                return None
            await conn.execute(
                "UPDATE org_invites SET accepted_at = now() WHERE id = $1",
                inv["id"],
            )
            await conn.execute(
                """
                INSERT INTO org_members (org_id, user_id, role, invited_by)
                VALUES ($1, $2, $3, (SELECT invited_by FROM org_invites WHERE id = $4))
                ON CONFLICT (org_id, user_id) DO NOTHING
                """,
                inv["org_id"], user_id, inv["role"], inv["id"],
            )
            r = await conn.fetchrow(
                "SELECT org_id, user_id, role, joined_at FROM org_members "
                "WHERE org_id = $1 AND user_id = $2",
                inv["org_id"], user_id,
            )
    return dict(r) if r else None


async def decline_invite(token: str) -> bool:
    """Public endpoint — anyone with the token can decline. Stamps decline_at
    so the inviting org sees the rejection in their pending list."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE org_invites SET declined_at = now()
             WHERE token = $1
               AND accepted_at IS NULL AND declined_at IS NULL AND revoked_at IS NULL
            """,
            token,
        )
    return not result.endswith(" 0")


async def revoke_invite(invite_id: int, org_id: int) -> bool:
    """Caller (an admin/owner) cancels a pending invite. Scoped by org_id so
    one org's admins can't revoke another org's invites if IDs leak."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE org_invites SET revoked_at = now()
             WHERE id = $1 AND org_id = $2
               AND accepted_at IS NULL AND declined_at IS NULL AND revoked_at IS NULL
            """,
            invite_id, org_id,
        )
    return not result.endswith(" 0")


# ─── agents — Phase 2 amendments (org-scoped listing + permission) ───────

async def list_agents_for_org(org_id: int) -> list[dict[str, Any]]:
    """Every agent in the org, regardless of creator. Reads denormalised
    calls_count + last_call_at from `agents` (Phase 9b)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rs = await conn.fetch(
            """
            SELECT a.id, a.name, a.slug, a.sector, a.locale, a.voice,
                   a.persona, a.greeting, a.variables,
                   a.org_id, a.user_id AS created_by,
                   a.created_at, a.published, a.published_at,
                   a.calls_count, a.last_call_at
              FROM agents a
             WHERE a.org_id = $1
             ORDER BY a.id DESC
            """,
            org_id,
        )
    return _records_to_list(rs)


async def get_agent_org(agent_id: int) -> Optional[int]:
    """Returns the org_id of the agent, or None if missing. Used by the
    permission shim to scope access checks without a full SELECT."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT org_id FROM agents WHERE id = $1", agent_id
        )


# ─── super_admins + audit_log (Phase 3) ──────────────────────────────────

async def is_super_admin(user_id: int) -> bool:
    """Single source of truth for "is this user a platform-level admin?".
    Every super-admin route's permission check funnels through here so
    revocations take effect immediately (no cached boolean on the user
    row to invalidate)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        n = await conn.fetchval(
            "SELECT 1 FROM super_admins WHERE user_id = $1", user_id
        )
    return bool(n)


async def list_super_admins() -> list[dict[str, Any]]:
    """Joined to users so the admin UI gets emails + names without a second fetch."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rs = await conn.fetch(
            """
            SELECT s.user_id, s.granted_at, s.granted_by,
                   u.email, u.name, u.avatar_url,
                   gu.email AS granted_by_email
              FROM super_admins s
              JOIN users u ON u.id = s.user_id
              LEFT JOIN users gu ON gu.id = s.granted_by
             ORDER BY s.granted_at ASC
            """
        )
    return _records_to_list(rs)


async def grant_super_admin(user_id: int, granted_by: int) -> bool:
    """Promote a user. Idempotent — re-granting is a no-op."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            INSERT INTO super_admins (user_id, granted_by)
            VALUES ($1, $2)
            ON CONFLICT (user_id) DO NOTHING
            """,
            user_id, granted_by,
        )
    return not result.endswith(" 0")


async def revoke_super_admin(user_id: int) -> bool:
    """Demote. The caller's route is responsible for guarding "can't revoke
    the last super-admin" — this function just does the DELETE."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM super_admins WHERE user_id = $1", user_id
        )
    return not result.endswith(" 0")


async def count_super_admins() -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT COUNT(*) FROM super_admins") or 0


# ─── audit_log ───────────────────────────────────────────────────────────

async def write_audit(actor_id: int, action: str,
                       target_kind: Optional[str] = None,
                       target_id: Optional[str] = None,
                       diff: Optional[dict[str, Any]] = None,
                       ip: Optional[str] = None,
                       user_agent: Optional[str] = None) -> int:
    """Append an audit-log row. Caller wraps every super-admin mutation
    in `await write_audit(...)` so the trail survives even if the route
    handler crashes between the mutation and the response.

    `action` is a dotted string: 'plan.override', 'agent.delete',
    'user.impersonate', 'super_admin.grant', etc.
    `target_kind` + `target_id` let us drill: "everything that touched
    agent 42" or "every action against user 7".
    `diff` is freeform JSONB — typically {before, after}."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            """
            INSERT INTO audit_log (actor_id, action, target_kind, target_id, diff, ip, user_agent)
            VALUES ($1, $2, $3, $4, $5, $6::inet, $7)
            RETURNING id
            """,
            actor_id, action, target_kind, target_id, diff, ip, user_agent,
        )


async def list_audit(limit: int = 100, offset: int = 0,
                      actor_id: Optional[int] = None,
                      target_kind: Optional[str] = None,
                      target_id: Optional[str] = None) -> list[dict[str, Any]]:
    """Paginated audit feed. Filters compose with AND. Joined to users so
    each row carries `actor_email` without a second query in the UI."""
    where = []
    params: list[Any] = []
    if actor_id is not None:
        params.append(actor_id); where.append(f"a.actor_id = ${len(params)}")
    if target_kind is not None:
        params.append(target_kind); where.append(f"a.target_kind = ${len(params)}")
    if target_id is not None:
        params.append(target_id); where.append(f"a.target_id = ${len(params)}")
    sql = """
        SELECT a.id, a.actor_id, a.action, a.target_kind, a.target_id,
               a.diff, a.ip, a.user_agent, a.created_at,
               u.email AS actor_email, u.name AS actor_name
          FROM audit_log a
          JOIN users u ON u.id = a.actor_id
    """
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY a.id DESC LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}"
    params.extend([limit, offset])
    pool = await get_pool()
    async with pool.acquire() as conn:
        rs = await conn.fetch(sql, *params)
    return _records_to_list(rs)


# ─── admin queries — read across all orgs/users/agents ───────────────────

async def admin_list_orgs(limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
    """All orgs across the platform with member + agent counts +
    rollup-sourced minutes_used.

    Phase 9b rewrite: `minutes_used` previously joined the entire
    `calls` table; now reads `org_daily_stats` keyed by `org_id`. At
    1k orgs × 5M calls this saves a multi-GB scan per dashboard load.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rs = await conn.fetch(
            """
            SELECT o.id, o.name, o.country, o.currency, o.created_at,
                   COALESCE((SELECT COUNT(*) FROM org_members WHERE org_id = o.id), 0) AS members_count,
                   COALESCE((SELECT COUNT(*) FROM agents WHERE org_id = o.id), 0) AS agents_count,
                   COALESCE((SELECT SUM(minutes) FROM org_daily_stats
                              WHERE org_id = o.id), 0) AS minutes_used,
                   (SELECT p.slug FROM users u JOIN plans p ON p.id = u.plan_id
                     WHERE u.org_id = o.id ORDER BY u.id LIMIT 1) AS primary_plan
              FROM orgs o
             ORDER BY o.id ASC
             LIMIT $1 OFFSET $2
            """,
            limit, offset,
        )
    return _records_to_list(rs)


async def admin_list_users(limit: int = 100, offset: int = 0,
                            search: Optional[str] = None) -> list[dict[str, Any]]:
    """Paginated user listing with plan + super-admin status. Optional
    `search` matches email or name (case-insensitive substring)."""
    where = []
    params: list[Any] = []
    if search:
        params.append(f"%{search.lower()}%")
        where.append(f"(lower(u.email) LIKE ${len(params)} OR lower(coalesce(u.name,'')) LIKE ${len(params)})")
    sql = """
        SELECT u.id, u.email, u.name, u.created_at, u.org_id,
               p.slug AS plan_slug, p.label AS plan_label,
               EXISTS (SELECT 1 FROM super_admins s WHERE s.user_id = u.id) AS is_super_admin
          FROM users u
          LEFT JOIN plans p ON p.id = u.plan_id
    """
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY u.id ASC LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}"
    params.extend([limit, offset])
    pool = await get_pool()
    async with pool.acquire() as conn:
        rs = await conn.fetch(sql, *params)
    return _records_to_list(rs)


async def admin_recent_calls(limit: int = 100) -> list[dict[str, Any]]:
    """Global recent-calls feed with agent + org joined in."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rs = await conn.fetch(
            """
            SELECT c.id, c.agent_id, c.started_at, c.ended_at, c.duration_s,
                   c.outcome, c.summary, c.input_tokens, c.output_tokens,
                   c.cost_paise, c.model_id,
                   a.name AS agent_name, a.slug AS agent_slug,
                   a.org_id, o.name AS org_name
              FROM calls c
              JOIN agents a ON a.id = c.agent_id
              JOIN orgs o ON o.id = a.org_id
             ORDER BY c.id DESC
             LIMIT $1
            """,
            limit,
        )
    return _records_to_list(rs)


async def admin_platform_summary() -> dict[str, Any]:
    """Single-query rollup for the admin dashboard header tiles.

    Phase 9b rewrite: the previous 9-subquery version did **six full
    scans of the calls table** for every dashboard load. At 5M calls
    that's 2–5s of seq-scan per page render. This version reads the
    five "calls-derived" tiles from `org_daily_stats` (which is
    pre-aggregated by the same insert_call txn) and only does direct
    COUNTs on the small entity tables (users, orgs, agents).

    Cost at 12-month scale:
      - 4 entity COUNTs:  microseconds each
      - 1 rollup SUM:     scans ~365k rows in org_daily_stats
      Total: <20ms vs. 2-5s before.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        r = await conn.fetchrow(
            """
            WITH ods AS (
              SELECT
                COALESCE(SUM(calls), 0)        AS calls_count,
                COALESCE(SUM(minutes), 0)      AS minutes_total,
                COALESCE(SUM(input_tokens), 0) AS input_tokens_total,
                COALESCE(SUM(output_tokens), 0) AS output_tokens_total,
                COALESCE(SUM(cost_paise), 0)   AS cost_paise_total
                FROM org_daily_stats
            )
            SELECT
              (SELECT COUNT(*) FROM users)                   AS users_count,
              (SELECT COUNT(*) FROM orgs)                    AS orgs_count,
              (SELECT COUNT(*) FROM agents)                  AS agents_count,
              (SELECT COUNT(*) FROM agents WHERE published)  AS published_count,
              ods.calls_count,
              ods.minutes_total,
              ods.input_tokens_total,
              ods.output_tokens_total,
              ods.cost_paise_total
              FROM ods
            """
        )
    return dict(r)


# ─── llm_calls ledger (Phase 7) ──────────────────────────────────────────

async def insert_llm_call(record: dict[str, Any]) -> int:
    """Universal LLM-session ledger insert. Used by builder + tts paths
    that have no `calls` row to link to. Agent calls write here via
    `insert_call` (which inserts both `calls` AND `llm_calls` in one txn).

    `kind` is required; the CHECK constraint rejects anything outside
    ('agent','builder','tts'). cost_paise is computed from model_id +
    tokens if not supplied — same pattern as `insert_call`."""
    from . import pricing
    kind = record.get("kind")
    if kind not in ("agent", "builder", "tts"):
        raise ValueError(f"bad llm_call kind: {kind!r}")
    in_tok = record.get("input_tokens") or 0
    out_tok = record.get("output_tokens") or 0
    model_id = record.get("model_id")
    explicit_cost = record.get("cost_paise")
    cost = explicit_cost if explicit_cost is not None else pricing.cost_paise(model_id, in_tok, out_tok)
    duration_s = float(record.get("duration_s") or 0)
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            """
            INSERT INTO llm_calls (
                kind, user_id, org_id, agent_id, call_id,
                started_at, ended_at, duration_s,
                input_tokens, output_tokens, cached_tokens, model_id, cost_paise
            ) VALUES (
                $1, $2, $3, $4, $5,
                COALESCE($6, now()), COALESCE($7, now()), $8,
                COALESCE($9,0), COALESCE($10,0), COALESCE($11,0), $12, $13
            ) RETURNING id
            """,
            kind, record.get("user_id"), record.get("org_id"),
            record.get("agent_id"), record.get("call_id"),
            _parse_ts(record.get("started_at")),
            _parse_ts(record.get("ended_at")),
            duration_s, in_tok, out_tok, record.get("cached_tokens"),
            model_id, cost,
        )


async def llm_analytics_for_org(org_id: int, days: int = 30) -> dict[str, Any]:
    """Per-org LLM ledger: totals split by kind (agent vs builder), plus the
    derived cost-per-minute (sum of cost / sum of minutes — NOT the average
    of cost_per_minute_paise across rows, which would weight short calls
    equally with long ones)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        by_kind = await conn.fetch(
            """
            SELECT kind,
                   COUNT(*)                                     AS sessions,
                   COALESCE(SUM(duration_s)/60.0, 0)            AS minutes,
                   COALESCE(SUM(input_tokens), 0)               AS input_tokens,
                   COALESCE(SUM(output_tokens), 0)              AS output_tokens,
                   COALESCE(SUM(cost_paise), 0)                 AS cost_paise
              FROM llm_calls
             WHERE org_id = $1
               AND started_at >= now() - ($2 || ' days')::interval
             GROUP BY kind
             ORDER BY cost_paise DESC
            """,
            org_id, str(int(days)),
        )
        totals = await conn.fetchrow(
            """
            SELECT COUNT(*) AS sessions,
                   COALESCE(SUM(duration_s)/60.0, 0)  AS minutes,
                   COALESCE(SUM(input_tokens), 0)     AS input_tokens,
                   COALESCE(SUM(output_tokens), 0)    AS output_tokens,
                   COALESCE(SUM(cost_paise), 0)       AS cost_paise,
                   CASE WHEN COALESCE(SUM(duration_s),0) > 0
                        THEN (COALESCE(SUM(cost_paise),0)::numeric * 60.0)
                             / SUM(duration_s)
                        ELSE NULL END                 AS cost_per_minute_paise
              FROM llm_calls
             WHERE org_id = $1
               AND started_at >= now() - ($2 || ' days')::interval
            """,
            org_id, str(int(days)),
        )
    return {
        "org_id": org_id, "range_days": int(days),
        "totals": dict(totals) if totals else {},
        "by_kind": _records_to_list(by_kind),
    }


async def llm_analytics_platform(days: int = 30) -> dict[str, Any]:
    """Cross-org LLM ledger summary for the super-admin grid."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        by_kind = await conn.fetch(
            """
            SELECT kind,
                   COUNT(*)                                     AS sessions,
                   COALESCE(SUM(duration_s)/60.0, 0)            AS minutes,
                   COALESCE(SUM(input_tokens), 0)               AS input_tokens,
                   COALESCE(SUM(output_tokens), 0)              AS output_tokens,
                   COALESCE(SUM(cost_paise), 0)                 AS cost_paise,
                   CASE WHEN COALESCE(SUM(duration_s),0) > 0
                        THEN (COALESCE(SUM(cost_paise),0)::numeric * 60.0)
                             / SUM(duration_s)
                        ELSE NULL END                           AS cost_per_minute_paise
              FROM llm_calls
             WHERE started_at >= now() - ($1 || ' days')::interval
             GROUP BY kind
             ORDER BY cost_paise DESC
            """,
            str(int(days)),
        )
        totals = await conn.fetchrow(
            """
            SELECT COUNT(*) AS sessions,
                   COALESCE(SUM(duration_s)/60.0, 0)  AS minutes,
                   COALESCE(SUM(input_tokens), 0)     AS input_tokens,
                   COALESCE(SUM(output_tokens), 0)    AS output_tokens,
                   COALESCE(SUM(cost_paise), 0)       AS cost_paise,
                   CASE WHEN COALESCE(SUM(duration_s),0) > 0
                        THEN (COALESCE(SUM(cost_paise),0)::numeric * 60.0)
                             / SUM(duration_s)
                        ELSE NULL END                 AS cost_per_minute_paise
              FROM llm_calls
             WHERE started_at >= now() - ($1 || ' days')::interval
            """,
            str(int(days)),
        )
    return {
        "range_days": int(days),
        "totals": dict(totals) if totals else {},
        "by_kind": _records_to_list(by_kind),
    }


# ─── analytics (Phase 5) ─────────────────────────────────────────────────

async def agent_analytics(agent_id: int, days: int = 30) -> dict[str, Any]:
    """Per-agent time-series + totals for the last `days` calendar days.
    Reads the materialized rollup; sub-millisecond at any volume."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT day, calls, minutes, input_tokens, output_tokens, cost_paise, outcomes
              FROM agent_daily_stats
             WHERE agent_id = $1
               AND day >= current_date - ($2 || ' days')::interval
             ORDER BY day ASC
            """,
            agent_id, str(int(days)),
        )
        totals = await conn.fetchrow(
            """
            SELECT COALESCE(SUM(calls),0)         AS calls,
                   COALESCE(SUM(minutes),0)       AS minutes,
                   COALESCE(SUM(input_tokens),0)  AS input_tokens,
                   COALESCE(SUM(output_tokens),0) AS output_tokens,
                   COALESCE(SUM(cost_paise),0)    AS cost_paise
              FROM agent_daily_stats
             WHERE agent_id = $1
               AND day >= current_date - ($2 || ' days')::interval
            """,
            agent_id, str(int(days)),
        )
    # Combine outcomes across the window — sum the per-day dicts.
    outcome_totals: dict[str, int] = {}
    for r in rows:
        for k, v in (r["outcomes"] or {}).items():
            outcome_totals[k] = outcome_totals.get(k, 0) + int(v)
    return {
        "agent_id": agent_id,
        "range_days": int(days),
        "totals": dict(totals) if totals else {},
        "series": _records_to_list(rows),
        "by_outcome": sorted(
            [{"outcome": k, "count": v} for k, v in outcome_totals.items()],
            key=lambda x: x["count"], reverse=True,
        ),
    }


async def org_analytics(org_id: int, days: int = 30) -> dict[str, Any]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT day, calls, minutes, input_tokens, output_tokens, cost_paise
              FROM org_daily_stats
             WHERE org_id = $1
               AND day >= current_date - ($2 || ' days')::interval
             ORDER BY day ASC
            """,
            org_id, str(int(days)),
        )
        totals = await conn.fetchrow(
            """
            SELECT COALESCE(SUM(calls),0)         AS calls,
                   COALESCE(SUM(minutes),0)       AS minutes,
                   COALESCE(SUM(input_tokens),0)  AS input_tokens,
                   COALESCE(SUM(output_tokens),0) AS output_tokens,
                   COALESCE(SUM(cost_paise),0)    AS cost_paise
              FROM org_daily_stats
             WHERE org_id = $1
               AND day >= current_date - ($2 || ' days')::interval
            """,
            org_id, str(int(days)),
        )
        # Top-5 agents within the org for the window — useful org-level signal.
        top_agents = await conn.fetch(
            """
            SELECT a.id, a.name, a.slug,
                   COALESCE(SUM(s.calls),0)      AS calls,
                   COALESCE(SUM(s.minutes),0)    AS minutes,
                   COALESCE(SUM(s.cost_paise),0) AS cost_paise
              FROM agents a
              LEFT JOIN agent_daily_stats s
                ON s.agent_id = a.id
               AND s.day >= current_date - ($2 || ' days')::interval
             WHERE a.org_id = $1
             GROUP BY a.id, a.name, a.slug
             ORDER BY calls DESC, minutes DESC
             LIMIT 5
            """,
            org_id, str(int(days)),
        )
    return {
        "org_id": org_id, "range_days": int(days),
        "totals": dict(totals) if totals else {},
        "series": _records_to_list(rows),
        "top_agents": _records_to_list(top_agents),
    }


async def platform_analytics(days: int = 30) -> dict[str, Any]:
    """Cross-org rollup. Super-admin only."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT day,
                   SUM(calls)         AS calls,
                   SUM(minutes)       AS minutes,
                   SUM(input_tokens)  AS input_tokens,
                   SUM(output_tokens) AS output_tokens,
                   SUM(cost_paise)    AS cost_paise
              FROM org_daily_stats
             WHERE day >= current_date - ($1 || ' days')::interval
             GROUP BY day
             ORDER BY day ASC
            """,
            str(int(days)),
        )
        totals = await conn.fetchrow(
            """
            SELECT COALESCE(SUM(calls),0)         AS calls,
                   COALESCE(SUM(minutes),0)       AS minutes,
                   COALESCE(SUM(input_tokens),0)  AS input_tokens,
                   COALESCE(SUM(output_tokens),0) AS output_tokens,
                   COALESCE(SUM(cost_paise),0)    AS cost_paise
              FROM org_daily_stats
             WHERE day >= current_date - ($1 || ' days')::interval
            """,
            str(int(days)),
        )
        # Per-org ranking — useful for "who uses us most" on admin grid.
        per_org = await conn.fetch(
            """
            SELECT o.id, o.name,
                   COALESCE(SUM(s.calls),0)      AS calls,
                   COALESCE(SUM(s.minutes),0)    AS minutes,
                   COALESCE(SUM(s.cost_paise),0) AS cost_paise
              FROM orgs o
              LEFT JOIN org_daily_stats s
                ON s.org_id = o.id
               AND s.day >= current_date - ($1 || ' days')::interval
             GROUP BY o.id, o.name
             ORDER BY calls DESC, minutes DESC
            """,
            str(int(days)),
        )
    return {
        "range_days": int(days),
        "totals": dict(totals) if totals else {},
        "series": _records_to_list(rows),
        "by_org": _records_to_list(per_org),
    }


# ─── platform_settings (Phase 4) ─────────────────────────────────────────

async def list_platform_settings(category: Optional[str] = None) -> list[dict[str, Any]]:
    """Every row, optionally scoped to a category. Used by the read-through
    cache in backend/settings.py + the admin UI's settings tab."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if category:
            rs = await conn.fetch(
                "SELECT key, value, category, label, description, updated_by, updated_at "
                "FROM platform_settings WHERE category = $1 ORDER BY key",
                category,
            )
        else:
            rs = await conn.fetch(
                "SELECT key, value, category, label, description, updated_by, updated_at "
                "FROM platform_settings ORDER BY category, key"
            )
    return _records_to_list(rs)


async def get_platform_setting(key: str) -> Optional[dict[str, Any]]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        r = await conn.fetchrow(
            "SELECT key, value, category, label, description, updated_by, updated_at "
            "FROM platform_settings WHERE key = $1",
            key,
        )
    return _record_to_dict(r)


async def set_platform_setting(key: str, value: Any,
                                 updated_by: Optional[int] = None) -> Optional[dict[str, Any]]:
    """Update an existing setting. Returns the new row (with audit fields
    stamped) or None if the key doesn't exist. We intentionally only
    update — creating new keys is an Alembic migration concern, not a
    runtime knob, so an admin can't accidentally introduce typos."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        r = await conn.fetchrow(
            """
            UPDATE platform_settings
               SET value = $2, updated_by = $3, updated_at = now()
             WHERE key = $1
            RETURNING key, value, category, label, description, updated_by, updated_at
            """,
            key, value, updated_by,
        )
    return _record_to_dict(r)


# ─── build_sessions (Phase: Eva durable build state) ────────────────────
#
# Each browser-build gets one row, keyed by (user_id, sid). The four
# typed fact columns + JSONB extras live here until either save_agent
# fires (status → 'committed') or the cleanup job times the row out
# (status → 'abandoned'). The whole point is that this row outlives
# any single Gemini Live session: a stream drop without a resume
# handle no longer means "Eva forgets everything", because she reads
# the row back from here on every (re)connect.
#
# UPSERT semantics: merge_build_facts only writes columns whose value
# is non-None in the call, so a partial fact update never clobbers an
# already-captured slot with NULL. The (user_id, sid) unique index +
# ON CONFLICT DO UPDATE makes the path race-safe even if two rapid
# note_build_facts tool calls land concurrently.


_BUILD_FACT_FIELDS = ("sector_kind", "business_name", "primary_job", "agent_name")


def _trim_fact(value: Any, *, max_len: int = 200) -> Optional[str]:
    """Normalise a fact value coming in from a Gemini tool call. Strips,
    caps length, and converts the empty string to None so we don't write
    blanks that look like real values. Returns None for any non-string."""
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    s = value.strip()
    if not s:
        return None
    return s[:max_len]


async def get_build_session(*, user_id: Optional[int], sid: str,
                              include_committed: bool = False) -> Optional[dict[str, Any]]:
    """Read the build_session row for this browser-build. By default,
    filters to status='in_progress' — so a committed or abandoned
    row returns None. Pass include_committed=True to also surface
    committed/abandoned rows (used by the recovery banner's /state
    endpoint, which wants to tell the operator "Eva already saved
    Maya in the background — open her")."""
    if not sid:
        return None
    pool = await get_pool()
    async with pool.acquire() as conn:
        status_clause = "" if include_committed else "AND status = 'in_progress'"
        r = await conn.fetchrow(
            f"""
            SELECT id, sid, user_id, org_id,
                   sector_kind, business_name, primary_job, agent_name,
                   extras, transcript_log, extraction_count,
                   template_id, template_answers,
                   status, committed_agent_id, created_at, updated_at
              FROM build_sessions
             WHERE user_id IS NOT DISTINCT FROM $1
               AND sid = $2
               {status_clause}
            """,
            user_id, sid,
        )
    return _record_to_dict(r)


# ──── soft slots the eavesdropping extractor + Eva can both write ────────
# These all live inside build_sessions.extras (JSONB). Typed columns are
# reserved for the load-bearing four facts. Anything in this list is a
# free-text or short-array slot the dashboard will eventually surface
# under "Business profile" / "Persona" / "Voice settings" — capturing
# it during build means the operator lands on a 90%-filled dashboard.
_BUILD_EXTRA_SCALARS = (
    "language",            # user-said languages, free text — e.g. "Hindi, English"
    "country",             # ISO-2 — "IN", "US", "GB", "SG"
    "city",                # free text — "Bangalore"
    "address",             # free text — "HSR Layout, Bangalore"
    "hours",               # human-readable — "Mon–Sat 9 AM – 9 PM, closed Sun"
    "services",            # free text — "cleanings, root canals, whitening"
    "offers",              # current promotions — "₹0 first consultation"
    "email",               # business email — "hello@brightsmile.in"
    "website",             # URL string
    "escalation_phone",    # caller-facing phone for "put me through"
    "notification_phone",  # operator's SMS line (often a WhatsApp number)
    "locale_hint",         # e.g. "en-IN" — inferred from country+language
    "voice_hint",          # if the user said "make her sound female" / specific voice
    "ambience_hint",       # if mentioned — "clinic", "cafe", etc.
    "persona_hint",        # one-line persona descriptor — Eva or extractor fills
    "greeting_hint",       # proposed first line the agent says
)
_BUILD_EXTRA_ARRAYS = (
    "additional_jobs",      # array of strings beyond primary_job
    "mentioned_guardrails", # array of free-text guardrail hints heard
)


async def merge_build_facts(
    *, user_id: Optional[int], sid: str,
    sector_kind: Optional[str] = None,
    business_name: Optional[str] = None,
    primary_job: Optional[str] = None,
    agent_name: Optional[str] = None,
    extras: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Upsert facts into the build_session row. Only non-None fact
    columns are written — passing a single field preserves the other
    three. `extras` is JSONB-merged (existing keys overwritten, missing
    keys untouched) so future slot tools can layer in without a
    migration.

    The extras dict can carry any of the keys in _BUILD_EXTRA_SCALARS or
    _BUILD_EXTRA_ARRAYS. Callers shouldn't include keys outside that set
    — silently dropping unknowns here keeps the column shape predictable
    even if Eva's tool args drift.

    Returns the row as it now exists. Raises ValueError if `sid` is
    empty — callers must always pass the browser-stable sid."""
    if not sid:
        raise ValueError("merge_build_facts requires a non-empty sid")

    facts = {
        "sector_kind":   _trim_fact(sector_kind),
        "business_name": _trim_fact(business_name),
        "primary_job":   _trim_fact(primary_job),
        "agent_name":    _trim_fact(agent_name),
    }

    # Sanitize extras: keep only known keys, normalize values. Scalars
    # are trimmed-and-capped strings; arrays are de-duped lists of
    # trimmed strings. Anything else is dropped silently so a sloppy
    # extractor output can't poison the row.
    cleaned_extras: dict[str, Any] = {}
    if isinstance(extras, dict):
        for k, v in extras.items():
            if k in _BUILD_EXTRA_SCALARS:
                sv = _trim_fact(v, max_len=400)
                if sv:
                    cleaned_extras[k] = sv
            elif k in _BUILD_EXTRA_ARRAYS:
                if isinstance(v, list):
                    seen: set[str] = set()
                    items: list[str] = []
                    for raw in v:
                        sv = _trim_fact(raw, max_len=200)
                        if sv and sv not in seen:
                            seen.add(sv)
                            items.append(sv)
                        if len(items) >= 12:
                            break
                    if items:
                        cleaned_extras[k] = items
    # Pass the dict directly — DON'T pre-serialize with json.dumps. The
    # asyncpg JSONB type codec (set in _init_codecs) ALREADY calls
    # json.dumps on the value before sending. Pre-serializing
    # double-encodes: the wire gets `'"{\\"city\\":\\"X\\"}"'`, Postgres
    # parses it as a JSONB STRING (not a dict), then the `||` merge does
    # `{} || "json-string"` → `[{}, "json-string"]` (PG auto-wraps
    # mixed-type concat as an array). Every subsequent merge appended
    # another stringified blob, corrupting the column into an
    # ever-growing array of JSON strings. Symptoms: `.get()` failures
    # in _format_build_facts_block and on_save_agent at read time.
    extras_clean = cleaned_extras or None

    # Resolve org_id from users.org_id once at insert time — same
    # "immutable tenant stamp" pattern as calls.org_id (see 0010). The
    # build_session is owned by whichever org the user was in at the
    # moment they started the build; later org changes don't retro
    # the row.
    pool = await get_pool()
    async with pool.acquire() as conn:
        org_id: Optional[int] = None
        if user_id is not None:
            row = await conn.fetchrow("SELECT org_id FROM users WHERE id = $1", user_id)
            org_id = row["org_id"] if row else None

        # The COALESCE-on-UPDATE preserves existing column values when
        # the incoming column is NULL. Without it, a follow-up call
        # supplying just `business_name` would wipe an already-captured
        # `sector_kind`. The JSONB || merge does the same for extras.
        r = await conn.fetchrow(
            """
            INSERT INTO build_sessions
                (sid, user_id, org_id,
                 sector_kind, business_name, primary_job, agent_name,
                 extras)
            VALUES
                ($1, $2, $3, $4, $5, $6, $7, COALESCE($8::jsonb, '{}'::jsonb))
            ON CONFLICT (user_id, sid) DO UPDATE SET
                sector_kind   = COALESCE(EXCLUDED.sector_kind,   build_sessions.sector_kind),
                business_name = COALESCE(EXCLUDED.business_name, build_sessions.business_name),
                primary_job   = COALESCE(EXCLUDED.primary_job,   build_sessions.primary_job),
                agent_name    = COALESCE(EXCLUDED.agent_name,    build_sessions.agent_name),
                extras        = (
                    -- Defensive: if a prior row got corrupted (pre-fix
                    -- builds stored a JSONB array instead of a dict),
                    -- reset to {} before merging so the new write
                    -- recovers cleanly instead of compounding.
                    CASE WHEN jsonb_typeof(build_sessions.extras) = 'object'
                         THEN build_sessions.extras
                         ELSE '{}'::jsonb
                    END
                ) || COALESCE($8::jsonb, '{}'::jsonb)
            RETURNING id, sid, user_id, org_id,
                      sector_kind, business_name, primary_job, agent_name,
                      extras, transcript_log, extraction_count,
                      status, committed_agent_id, created_at, updated_at
            """,
            sid, user_id, org_id,
            facts["sector_kind"], facts["business_name"],
            facts["primary_job"], facts["agent_name"],
            extras_clean,
        )
    return _record_to_dict(r) or {}


# ─── Ask-Eva helper memory (per user × agent) ────────────────────────────


async def get_helper_memory(*, user_id: int, agent_id: int) -> dict[str, Any]:
    """Load the helper memory row for (user_id, agent_id). Returns
    {turns: [...], summary: "", updated_at: "..."} — empty defaults when
    no row exists yet."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT turns, summary, updated_at FROM agent_helper_memory "
            "WHERE user_id = $1 AND agent_id = $2",
            int(user_id), int(agent_id),
        )
    if not row:
        return {"turns": [], "summary": "", "updated_at": None}
    return {
        "turns": list(row["turns"] or []),
        "summary": row["summary"] or "",
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


async def append_helper_turns(
    *, user_id: int, agent_id: int, new_turns: list[dict[str, Any]],
    max_turns: int = 40,
) -> dict[str, Any]:
    """Append `new_turns` to the agent's helper memory, capping at
    `max_turns`. Returns the resulting `{turns, summary, updated_at}`."""
    if not new_turns:
        return await get_helper_memory(user_id=user_id, agent_id=agent_id)
    # Sanitise — keep only well-formed entries.
    safe: list[dict[str, Any]] = []
    for t in new_turns:
        if not isinstance(t, dict):
            continue
        role = str(t.get("role") or "").strip()
        text = str(t.get("text") or "").strip()
        if role not in ("user", "model", "system") or not text:
            continue
        safe.append({"role": role, "text": text[:2000], "ts": str(t.get("ts") or "")})
    if not safe:
        return await get_helper_memory(user_id=user_id, agent_id=agent_id)
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            existing = await conn.fetchrow(
                "SELECT turns, summary FROM agent_helper_memory "
                "WHERE user_id = $1 AND agent_id = $2 FOR UPDATE",
                int(user_id), int(agent_id),
            )
            turns_before = list(existing["turns"] or []) if existing else []
            summary_before = existing["summary"] if existing else ""
            combined = turns_before + safe
            # Keep the most recent `max_turns`; the bridge condenses older
            # ones into `summary` separately.
            if len(combined) > max_turns:
                combined = combined[-max_turns:]
            row = await conn.fetchrow(
                """
                INSERT INTO agent_helper_memory (user_id, agent_id, turns, summary, updated_at)
                VALUES ($1, $2, $3::jsonb, $4, NOW())
                ON CONFLICT (user_id, agent_id) DO UPDATE
                  SET turns      = EXCLUDED.turns,
                      updated_at = NOW()
                RETURNING turns, summary, updated_at
                """,
                int(user_id), int(agent_id),
                json.dumps(combined), summary_before,
            )
    return {
        "turns": list(row["turns"] or []),
        "summary": row["summary"] or "",
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


async def set_helper_summary(*, user_id: int, agent_id: int, summary: str) -> None:
    """Set the running summary for a (user × agent) memory row. Used by the
    helper bridge when it folds old turns into a condensed prose summary."""
    if summary is None:
        summary = ""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO agent_helper_memory (user_id, agent_id, turns, summary, updated_at)
            VALUES ($1, $2, '[]'::jsonb, $3, NOW())
            ON CONFLICT (user_id, agent_id) DO UPDATE
              SET summary    = EXCLUDED.summary,
                  updated_at = NOW()
            """,
            int(user_id), int(agent_id), summary[:8000],
        )


async def seed_helper_memory(*, user_id: int, agent_id: int, agent: dict[str, Any]) -> None:
    """Write an initial summary the moment an agent is created — so the FIRST
    Ask-Eva conversation already has the build context (sector, locale,
    purpose, business name, persona). Idempotent: skips if a row already
    exists, so re-seeding doesn't overwrite the operator's later notes."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        existing = await conn.fetchval(
            "SELECT 1 FROM agent_helper_memory WHERE user_id = $1 AND agent_id = $2",
            int(user_id), int(agent_id),
        )
    if existing:
        return
    # Build the seed summary from facts on the agent payload.
    name = (agent.get("name") or "").strip() or "this agent"
    sector = (agent.get("sector") or "").strip() or "unspecified"
    locale = (agent.get("locale") or "").strip() or "unspecified"
    business = (agent.get("variables") or {}).get("business_name") if isinstance(agent.get("variables"), dict) else None
    business = (business or "").strip()
    persona = (agent.get("persona") or "").strip()
    purpose_obj = agent.get("purpose") if isinstance(agent.get("purpose"), dict) else {}
    purpose_summary = (purpose_obj.get("summary") or "").strip()
    purpose_actions = list(purpose_obj.get("actions") or [])

    lines: list[str] = [
        f"Agent {name!r} was built today.",
        f"Sector: {sector}. Locale: {locale}.",
    ]
    if business:
        lines.append(f"Business: {business}.")
    if persona:
        lines.append(f"Persona: {persona[:240]}")
    if purpose_summary:
        lines.append(f"Core purpose: {purpose_summary[:280]}")
    if purpose_actions:
        lines.append(f"Purpose actions chosen at build time: {', '.join(purpose_actions)}.")
    lines.append(
        "Treat this as the operator's introduction — do NOT re-ask what the "
        "agent does, who she's for, or what was decided at build. If they ask "
        "to change something, just do it."
    )
    summary = "\n".join(lines)[:8000]
    await set_helper_summary(user_id=user_id, agent_id=agent_id, summary=summary)


async def append_transcript_turn(
    *, user_id: Optional[int], sid: str, role: str, text: str,
    max_turns: int = 80,
) -> None:
    """Append one turn to build_sessions.transcript_log. Lazy-creates the
    row if it doesn't exist (so the very first user utterance — which
    may arrive before Eva calls note_build_facts — still gets logged).
    The array is capped at `max_turns` by slicing the head off; we keep
    the most recent ones because that's what reconnect-replay needs.

    Best-effort: failures log a warning and return None — never blocks
    the audio pump.
    """
    if not sid:
        return
    role = (role or "").lower()
    if role not in ("user", "model"):
        return
    text = (text or "").strip()
    if not text:
        return
    turn = {"role": role, "text": text[:2000]}  # individual turn cap

    pool = await get_pool()
    async with pool.acquire() as conn:
        # Ensure the row exists first (idempotent — no-op if already there
        # via merge_build_facts). We can't rely on merge_build_facts
        # having run yet: the extractor pipeline persists the transcript
        # BEFORE attempting to extract anything (if extraction fails,
        # the transcript is still safe).
        org_id: Optional[int] = None
        if user_id is not None:
            row = await conn.fetchrow("SELECT org_id FROM users WHERE id = $1", user_id)
            org_id = row["org_id"] if row else None
        await conn.execute(
            """
            INSERT INTO build_sessions (sid, user_id, org_id)
            VALUES ($1, $2, $3)
            ON CONFLICT (user_id, sid) DO NOTHING
            """,
            sid, user_id, org_id,
        )
        # Append + cap. jsonb || jsonb concatenates arrays. When the
        # combined length exceeds max_turns we drop the HEAD (oldest
        # turns) and keep the TAIL — in CHRONOLOGICAL ORDER.
        #
        # Encoding: pass the Python list directly. The asyncpg JSONB
        # codec already calls json.dumps; pre-serializing would
        # double-encode and Postgres would store a JSONB STRING
        # (not an array), which the `||` would then concat as
        # `[...existing..., "stringified-array"]` — same family of
        # corruption that hit `extras`. The `safe_log` CTE coerces a
        # corrupted non-array column back to `[]` before appending,
        # so existing rows recover on the next write.
        await conn.execute(
            """
            WITH safe_log AS (
                SELECT CASE WHEN jsonb_typeof(transcript_log) = 'array'
                            THEN transcript_log
                            ELSE '[]'::jsonb
                       END AS log
                  FROM build_sessions
                 WHERE user_id IS NOT DISTINCT FROM $1
                   AND sid = $2
                   AND status = 'in_progress'
            )
            UPDATE build_sessions bs
               SET transcript_log = (
                       CASE WHEN jsonb_array_length((SELECT log FROM safe_log) || $3::jsonb) > $4
                            THEN (
                                SELECT jsonb_agg(elem ORDER BY idx)
                                  FROM jsonb_array_elements((SELECT log FROM safe_log) || $3::jsonb)
                                       WITH ORDINALITY AS t(elem, idx)
                                 WHERE idx > jsonb_array_length((SELECT log FROM safe_log) || $3::jsonb) - $4
                              )
                            ELSE (SELECT log FROM safe_log) || $3::jsonb
                       END
                   )
             WHERE bs.user_id IS NOT DISTINCT FROM $1
               AND bs.sid = $2
               AND bs.status = 'in_progress'
            """,
            user_id, sid, [turn], int(max_turns),
        )


async def bump_extraction_count(*, user_id: Optional[int], sid: str) -> None:
    """Increment extraction_count by one. Cheap and idempotent; if the
    row doesn't exist this is a no-op."""
    if not sid:
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE build_sessions
               SET extraction_count = extraction_count + 1
             WHERE user_id IS NOT DISTINCT FROM $1
               AND sid = $2
               AND status = 'in_progress'
            """,
            user_id, sid,
        )


async def set_build_template(*, user_id: Optional[int], sid: str,
                              template_id: str) -> None:
    """Stamp the resolved template_id on the build_session. Called
    once, right after Eva's triage classifies the operator's facets
    and the server matches a YAML template. Idempotent: a second
    call overwrites (lets the operator change industry mid-build —
    the new template_id wins, the questions list resets)."""
    if not sid or not template_id:
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Ensure the row exists (matches the lazy-create pattern in
        # append_transcript_turn).
        org_id: Optional[int] = None
        if user_id is not None:
            row = await conn.fetchrow("SELECT org_id FROM users WHERE id = $1", user_id)
            org_id = row["org_id"] if row else None
        await conn.execute(
            """
            INSERT INTO build_sessions (sid, user_id, org_id, template_id)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (user_id, sid) DO UPDATE SET
                template_id = EXCLUDED.template_id
            """,
            sid, user_id, org_id, template_id,
        )


async def record_template_answer(
    *, user_id: Optional[int], sid: str, question_id: str, value: Any,
) -> Optional[dict[str, Any]]:
    """Merge one answer into the template_answers JSONB dict.

    Stored shape: `{"<question_id>": <value>}`. Value can be string,
    list[str], bool, etc. — validation runs in build_templates.py
    BEFORE this is called, so by here the value is canonical.

    Returns the full updated row so the caller can immediately decide
    which question to ask next via build_templates.next_unanswered_question.
    """
    if not sid:
        return None
    if not question_id:
        return None
    pool = await get_pool()
    async with pool.acquire() as conn:
        org_id: Optional[int] = None
        if user_id is not None:
            row = await conn.fetchrow("SELECT org_id FROM users WHERE id = $1", user_id)
            org_id = row["org_id"] if row else None
        # Ensure row exists, then merge. The `{key: value}` patch is
        # built server-side via jsonb_set so we don't double-encode.
        await conn.execute(
            """
            INSERT INTO build_sessions (sid, user_id, org_id)
            VALUES ($1, $2, $3)
            ON CONFLICT (user_id, sid) DO NOTHING
            """,
            sid, user_id, org_id,
        )
        r = await conn.fetchrow(
            """
            UPDATE build_sessions
               SET template_answers = jsonb_set(
                       CASE WHEN jsonb_typeof(template_answers) = 'object'
                            THEN template_answers
                            ELSE '{}'::jsonb
                       END,
                       ARRAY[$3::text],
                       -- A skipped optional question records value=None.
                       -- asyncpg binds Python None as SQL NULL (it never
                       -- invokes the jsonb codec for None), and
                       -- jsonb_set(target, path, NULL) returns NULL for
                       -- the ENTIRE document — which then violates the
                       -- NOT NULL constraint on template_answers. COALESCE
                       -- maps that NULL param to a JSONB `null` token so
                       -- the key is recorded as JSON null (answered =
                       -- key-present) without nuking the column.
                       COALESCE($4::jsonb, 'null'::jsonb),
                       true
                   )
             WHERE user_id IS NOT DISTINCT FROM $1
               AND sid = $2
               AND status = 'in_progress'
            RETURNING id, sid, user_id, org_id,
                      sector_kind, business_name, primary_job, agent_name,
                      extras, transcript_log, extraction_count,
                      template_id, template_answers,
                      status, committed_agent_id, created_at, updated_at
            """,
            user_id, sid, question_id, value,
        )
    return _record_to_dict(r)


async def mark_build_committed(
    *, user_id: Optional[int], sid: str, agent_id: int,
) -> Optional[dict[str, Any]]:
    """Flip the in-progress build row to status='committed' and link it
    to the resulting agents.id. Idempotent: if there's no row (Eva built
    an agent without ever calling note_build_facts) or it's already
    committed, this is a silent no-op returning None. Best-effort —
    callers shouldn't block save_agent on it."""
    if not sid:
        return None
    pool = await get_pool()
    async with pool.acquire() as conn:
        r = await conn.fetchrow(
            """
            UPDATE build_sessions
               SET status = 'committed',
                   committed_agent_id = $3
             WHERE user_id IS NOT DISTINCT FROM $1
               AND sid = $2
               AND status = 'in_progress'
            RETURNING id, status, committed_agent_id
            """,
            user_id, sid, agent_id,
        )
    return _record_to_dict(r)


async def abandon_stale_build_sessions(*, older_than_hours: int = 24) -> int:
    """Sweep build_sessions older than N hours into status='abandoned'.
    Returns the number of rows flipped. Intended for a daily background
    job (cron / scheduled task) — NOT called from the request path.
    Uses the partial index idx_build_sessions_inprogress_updated so the
    scan is bounded even at scale."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE build_sessions
               SET status = 'abandoned'
             WHERE status = 'in_progress'
               AND updated_at < now() - ($1 || ' hours')::interval
            """,
            str(int(older_than_hours)),
        )
    # asyncpg execute returns a status string like 'UPDATE 17'
    try:
        return int(result.rsplit(" ", 1)[-1])
    except (ValueError, AttributeError):
        return 0


# ─── sync façade (one-shot scripts only) ─────────────────────────────────

def _run(coro):
    """Run an async function from sync code by spinning a private loop.
    Don't use this from within FastAPI request handlers — those are already
    in an async context. This exists for migration scripts and one-off CLIs."""
    return asyncio.run(coro)
