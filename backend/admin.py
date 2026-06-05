"""Super-admin routes — `/api/admin/*`.

Every endpoint here:
  1. Gates on `require_super_admin(caller)`.
  2. For writes, calls `db.write_audit(...)` in the same logical block as
     the mutation so the trail can't get desynced from the action.

Mount path is `/api/admin`. Authentication still flows through the same
`current_user` shim (stub dev auth → Auth0 later) — there's no separate
admin login; super-admin status is just a role flag on top of normal auth.

Routes:
  GET    /api/admin/summary            platform-wide totals
  GET    /api/admin/orgs               list with member/agent/calls counts
  GET    /api/admin/users              search + paginate
  GET    /api/admin/calls              global recent feed
  GET    /api/admin/audit              log paginated, filterable
  GET    /api/admin/super-admins       list grants
  POST   /api/admin/super-admins       grant super-admin to a user
  DELETE /api/admin/super-admins/{id}  revoke (last-admin guard applies)
  POST   /api/admin/orgs/{id}/plan     force-set an org's plan
  DELETE /api/admin/orgs/{id}          (Phase 4 candidate — not wired yet)
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from . import auth, db, settings as cfg


router = APIRouter(prefix="/api/admin")


def _ip_ua(request: Request) -> tuple[Optional[str], Optional[str]]:
    """Extract IP + user-agent for the audit-log row. Reverse proxies set
    X-Forwarded-For — trust the first hop only (we run behind a single
    Railway proxy in production)."""
    fwd = request.headers.get("X-Forwarded-For")
    ip = fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else None)
    return ip, request.headers.get("User-Agent")


# ─── platform overview ───────────────────────────────────────────────────

@router.get("/summary")
async def admin_summary(request: Request) -> dict:
    user = await _admin_user(request)
    return await db.admin_platform_summary()


@router.get("/orgs")
async def admin_orgs(request: Request, limit: int = 100, offset: int = 0) -> list[dict]:
    await _admin_user(request)
    return await db.admin_list_orgs(limit=limit, offset=offset)


@router.get("/users")
async def admin_users(request: Request, limit: int = 100, offset: int = 0, q: str = "") -> list[dict]:
    await _admin_user(request)
    return await db.admin_list_users(limit=limit, offset=offset, search=q.strip() or None)


@router.get("/calls")
async def admin_calls(
    request: Request,
    limit: int = 100,
    org_id: Optional[int] = None,
    agent_id: Optional[int] = None,
    phone: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
) -> list[dict]:
    """Build 202: accepts the unified AdminFilterBar params. Empty
    strings are treated as 'not set' so the frontend can send the
    full QS shape unconditionally without server-side coercion
    surprises."""
    await _admin_user(request)
    return await db.admin_recent_calls(
        limit=limit,
        org_id=org_id, agent_id=agent_id,
        phone=(phone or None),
        start=(start or None), end=(end or None),
    )


# ─── audit log ───────────────────────────────────────────────────────────

@router.get("/audit")
async def admin_audit(request: Request, limit: int = 100, offset: int = 0,
                      actor_id: Optional[int] = None,
                      target_kind: Optional[str] = None,
                      target_id: Optional[str] = None,
                      start: Optional[str] = None,
                      end: Optional[str] = None) -> list[dict]:
    await _admin_user(request)
    return await db.list_audit(
        limit=limit, offset=offset,
        actor_id=actor_id, target_kind=target_kind, target_id=target_id,
        start=(start or None), end=(end or None),
    )


# ─── per-agent health status (build 219) ─────────────────────────────────

@router.get("/agents/health")
async def admin_agents_health(request: Request) -> list[dict]:
    """Latest healthcheck status per published agent. Reads the most
    recent `agent.healthcheck.{passed,degraded,failed}` event per
    agent via a LATERAL join (one tiny query, no N+1). Powers the
    Observability page's Agent-Health card."""
    await _admin_user(request)
    from . import agent_healthcheck as _ahc
    return await _ahc.latest_status_per_agent()


@router.post("/agents/health/run-now")
async def admin_agents_health_run_now(request: Request) -> dict:
    """Out-of-band trigger — same hourly probe but on-demand. Useful
    for verifying a fix didn't regress without waiting for the next
    :05 of the hour."""
    await _admin_user(request)
    from . import agent_healthcheck as _ahc
    await _ahc.run_hourly_healthchecks()
    return {"ok": True}


@router.post("/agents/health/run-full")
async def admin_agents_health_run_full(request: Request) -> dict:
    """Build 231 — manual trigger for the Level 3 (full conversational)
    probe. Same path the daily scheduler uses; this is the "Run now"
    affordance on the Health-checks settings card. Honours the
    `healthcheck.level3_sample_size` cap so a stray click doesn't
    burn budget by probing every agent."""
    await _admin_user(request)
    from . import agent_healthcheck as _ahc
    await _ahc.run_daily_full_healthchecks()
    return {"ok": True}


@router.post("/agents/health/run-pstn")
async def admin_agents_health_run_pstn(request: Request) -> dict:
    """Build 231 — manual trigger for the Level 4 PSTN probe. Stubbed
    today (outbound integration pending) — returns a structured
    response describing why it didn't dial so the UI can render the
    operator-facing explanation without guessing."""
    await _admin_user(request)
    from . import agent_healthcheck as _ahc
    result = await _ahc.run_pstn_healthcheck()
    return result


# ─── lookups for admin filter bar (build 202) ────────────────────────────

@router.get("/orgs-lookup")
async def admin_orgs_lookup(request: Request) -> list[dict]:
    """Minimal `[{id, name}]` list for the AdminFilterBar's org
    dropdown. Cheap query (no joins, name-sorted) — safe to call
    on every admin-page mount without a cache layer."""
    await _admin_user(request)
    return await db.admin_orgs_lookup()


@router.get("/agents-lookup")
async def admin_agents_lookup(request: Request) -> list[dict]:
    """Minimal `[{id, name, slug, org_id, org_name}]` list for the
    AdminFilterBar's agent dropdown. The org join lets the bar
    constrain the agent list when an org is already selected."""
    await _admin_user(request)
    return await db.admin_agents_lookup()


# ─── super admin management ──────────────────────────────────────────────

@router.get("/super-admins")
async def list_admins(request: Request) -> list[dict]:
    await _admin_user(request)
    return await db.list_super_admins()


@router.post("/super-admins")
async def grant_admin(request: Request) -> dict:
    actor = await _admin_user(request)
    body = await _json(request)
    target_id = body.get("user_id")
    try:
        target_id = int(target_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="user_id required")
    target = await db.get_user(target_id)
    if not target:
        raise HTTPException(status_code=404, detail="user not found")
    granted = await db.grant_super_admin(target_id, granted_by=actor["id"])
    ip, ua = _ip_ua(request)
    await db.write_audit(
        actor_id=actor["id"],
        action="super_admin.grant",
        target_kind="user", target_id=str(target_id),
        diff={"new_role": "super_admin", "target_email": target["email"]},
        ip=ip, user_agent=ua,
    )
    return {"granted": granted, "user_id": target_id, "email": target["email"]}


@router.delete("/super-admins/{user_id}")
async def revoke_admin(user_id: int, request: Request) -> dict:
    actor = await _admin_user(request)
    # Last-admin guard — can't lock everyone out.
    if await db.count_super_admins() <= 1:
        raise HTTPException(
            status_code=400,
            detail={"code": "last_super_admin",
                    "message": "Promote another user to super-admin first."},
        )
    target = await db.get_user(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="user not found")
    revoked = await db.revoke_super_admin(user_id)
    if not revoked:
        raise HTTPException(status_code=404, detail="not a super-admin")
    ip, ua = _ip_ua(request)
    await db.write_audit(
        actor_id=actor["id"],
        action="super_admin.revoke",
        target_kind="user", target_id=str(user_id),
        diff={"target_email": target["email"]},
        ip=ip, user_agent=ua,
    )
    return {"revoked": revoked, "user_id": user_id}


# ─── org plan override (free-text + paid plan-flip without Razorpay) ─────

@router.post("/orgs/{org_id}/plan")
async def set_org_plan(org_id: int, request: Request) -> dict:
    """Force-set the plan of every user in an org. Used to grant comp / refund /
    onboard a paid customer manually. Bypasses Razorpay — audit-logged so we
    can reconcile to invoicing later."""
    actor = await _admin_user(request)
    body = await _json(request)
    plan_slug = (body.get("plan") or "").strip()
    if not plan_slug:
        raise HTTPException(status_code=400, detail="plan slug required")
    plan = await db.get_plan_by_slug(plan_slug)
    if not plan:
        raise HTTPException(status_code=404, detail=f"plan {plan_slug!r} not found")
    # Apply to every user in the org. Single-tenant today; team-aware
    # tomorrow when multiple users share an org's plan.
    org_users = await db.list_org_members(org_id)
    if not org_users:
        raise HTTPException(status_code=404, detail="org has no members")
    before: list[dict] = []
    for m in org_users:
        prev = await db.get_user_plan_state(m["user_id"])
        before.append({"user_id": m["user_id"], "from": prev["plan"]["slug"]})
        await db.set_user_plan(m["user_id"], plan["id"], reset_usage=True)
    ip, ua = _ip_ua(request)
    await db.write_audit(
        actor_id=actor["id"],
        action="plan.override",
        target_kind="org", target_id=str(org_id),
        diff={"new_plan": plan_slug, "members_touched": len(org_users), "before": before},
        ip=ip, user_agent=ua,
    )
    return {"ok": True, "org_id": org_id, "new_plan": plan_slug, "members_touched": len(org_users)}


# ─── analytics (Phase 5 + 7) ─────────────────────────────────────────────

@router.get("/analytics")
async def admin_analytics(request: Request, days: int = 30) -> dict:
    """Platform-wide time series + totals + per-org ranking."""
    await _admin_user(request)
    return await db.platform_analytics(days=max(1, min(int(days), 365)))


@router.get("/analytics/llm")
async def admin_llm_analytics(
    request: Request,
    days: int = 30,
    org_id: Optional[int] = None,
    agent_id: Optional[int] = None,
) -> dict:
    """Universal LLM-cost ledger view (Phase 7). Includes Eva-builder
    sessions that don't surface in the customer-call analytics — gives
    finance the full token + cost picture. Split by kind so we can see
    whether the spend is build-time vs runtime.

    Build 202: optional `org_id` / `agent_id` to scope the ledger
    from the AdminFilterBar."""
    await _admin_user(request)
    return await db.llm_analytics_platform(
        days=max(1, min(int(days), 365)),
        org_id=org_id, agent_id=agent_id,
    )


# ─── platform settings (Phase 4) ─────────────────────────────────────────

@router.get("/settings")
async def admin_list_settings(request: Request, category: Optional[str] = None) -> list[dict]:
    await _admin_user(request)
    return await cfg.get_many(category=category)


@router.patch("/settings/{key:path}")
async def admin_set_setting(key: str, request: Request) -> dict:
    """Update one platform setting. `value` in the body is JSONB-shaped —
    strings, numbers, booleans, arrays, or objects all work. The audit-log
    captures `{before, after}` so we can reconstruct any flag flip later."""
    actor = await _admin_user(request)
    body = await _json(request)
    if "value" not in body:
        raise HTTPException(status_code=400, detail="`value` required in body")
    diff = await cfg.set(key, body["value"], updated_by=actor["id"])
    if not diff["row"]:
        raise HTTPException(status_code=404, detail=f"unknown setting {key!r}")
    ip, ua = _ip_ua(request)
    await db.write_audit(
        actor_id=actor["id"],
        action="setting.update",
        target_kind="setting", target_id=key,
        diff={"before": diff["before"], "after": diff["after"]},
        ip=ip, user_agent=ua,
    )
    return diff["row"]


# ─── observability (build 198) ───────────────────────────────────────────

@router.get("/events")
async def admin_events(
    request: Request,
    severity: Optional[str] = None,
    kind_prefix: Optional[str] = None,
    org_id: Optional[int] = None,
    agent_id: Optional[int] = None,
    only_open: bool = False,
    limit: int = 200,
    before_id: Optional[int] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
) -> dict:
    """Filtered event feed for the Observability page. Returns
    `{items, counts}` so the page can render the KPI tiles + the list
    in one round-trip on initial mount.

    Build 202: `start` / `end` ISO timestamps come from the
    AdminFilterBar's date range picker (default last 7 days)."""
    await _admin_user(request)
    from . import events as _ev
    items = await _ev.list_events(
        severity=severity, kind_prefix=kind_prefix,
        org_id=org_id, agent_id=agent_id,
        only_open=only_open, limit=limit, before_id=before_id,
        start=(start or None), end=(end or None),
    )
    counts = await _ev.event_counts()
    return {"items": items, "counts": counts}


@router.post("/events/{event_id}/resolve")
async def admin_event_resolve(event_id: int, request: Request) -> dict:
    """Mark one event as resolved. Idempotent — no-op if already resolved
    or non-existent."""
    user = await _admin_user(request)
    from . import events as _ev
    ok = await _ev.resolve_event(event_id, user_id=user["id"])
    return {"ok": ok, "event_id": event_id}


@router.get("/schedulers")
async def admin_schedulers(request: Request) -> list[dict]:
    """Registered scheduler jobs with their cron + last-run timestamps.
    Backs the Schedulers tab on the Observability page."""
    await _admin_user(request)
    from . import scheduler
    return scheduler.list_jobs()


@router.post("/schedulers/{name}/run")
async def admin_scheduler_run_now(name: str, request: Request) -> dict:
    """Out-of-band 'Run now' trigger — useful for verifying the price
    monitor without waiting for 05:00 IST."""
    await _admin_user(request)
    from . import scheduler
    ok = await scheduler.run_now(name)
    if not ok:
        raise HTTPException(status_code=404, detail=f"job {name!r} not registered")
    return {"ok": True, "name": name}


# ─── pricing (build 199) ─────────────────────────────────────────────────


@router.get("/pricing/current")
async def admin_pricing_current(request: Request) -> dict:
    """Current effective rates across every provider × rate_kind. Used
    by the Pricing tab on the Observability page to show what's in
    force + diff against the latest pricing.observed events."""
    await _admin_user(request)
    rates = await db.list_current_pricing()
    # Pull last observed rate per (provider, rate_kind) from the events
    # table so the UI can show "observed today vs effective".
    from . import events as _ev
    observed = await _ev.list_events(kind_prefix="pricing.observed", limit=50)
    drifts = await _ev.list_events(kind_prefix="pricing.drift.detected", only_open=True, limit=50)
    return {"rates": rates, "observed": observed, "drifts": drifts}


@router.post("/pricing/roll-forward")
async def admin_pricing_roll_forward(request: Request) -> dict:
    """Promote an observed rate to be the new effective rate. Body:
      { provider, rate_kind, model_id?, unit, usd_per_unit?, inr_per_unit?,
        note?, observed_event_id?, resolve_drift_event_id? }

    Closes the currently-in-force version + writes a new version + emits
    a `pricing.rate.rolled_forward` event + resolves the originating
    drift event (if `resolve_drift_event_id` is set). All in one
    audited action — the only sanctioned path to mutate rates."""
    user = await _admin_user(request)
    body = await _json(request)
    provider = (body.get("provider") or "").strip()
    rate_kind = (body.get("rate_kind") or "").strip()
    unit = (body.get("unit") or "").strip()
    if not (provider and rate_kind and unit):
        raise HTTPException(status_code=400, detail="provider, rate_kind, unit required")
    usd = body.get("usd_per_unit")
    inr = body.get("inr_per_unit")
    if usd is None and inr is None:
        raise HTTPException(status_code=400, detail="provide usd_per_unit OR inr_per_unit")
    new_id = await db.roll_forward_rate(
        provider=provider, rate_kind=rate_kind,
        model_id=body.get("model_id"),
        unit=unit,
        usd_per_unit=float(usd) if usd is not None else None,
        inr_per_unit=float(inr) if inr is not None else None,
        rolled_by=user["id"],
        note=(body.get("note") or "Rolled forward via admin UI"),
        observed_event_id=body.get("observed_event_id"),
    )
    if not new_id:
        raise HTTPException(status_code=500, detail="roll-forward failed — check server log")
    from . import events as _ev
    await _ev.emit(
        "pricing.rate.rolled_forward", severity="info", source="user",
        user_id=user["id"],
        title=f"Rate rolled forward — {provider} {rate_kind}"
              + (f" ({body.get('model_id')})" if body.get("model_id") else ""),
        message=body.get("note"),
        payload={
            "provider": provider, "rate_kind": rate_kind,
            "model_id": body.get("model_id"), "unit": unit,
            "usd_per_unit": usd, "inr_per_unit": inr,
            "new_version_id": new_id,
        },
    )
    # If this roll was triggered to clear an open drift, resolve it
    resolve_id = body.get("resolve_drift_event_id")
    if resolve_id:
        try:
            await _ev.resolve_event(int(resolve_id), user_id=user["id"])
        except Exception:  # noqa: BLE001
            pass
    return {"ok": True, "new_version_id": new_id}


# ─── per-agent P&L (build 199) ───────────────────────────────────────────


@router.get("/agent-pnl")
async def admin_agent_pnl(
    request: Request,
    days: int = 30,
    org_id: Optional[int] = None,
    agent_id: Optional[int] = None,
) -> dict:
    """Per-agent COGS roll-up for the last N days. Surfaces minutes,
    LLM cost (from agent_daily_stats), telephony estimate (computed
    at read-time as minutes × current Plivo per-min rate), and total
    COGS. Sorted by cost descending so the most expensive agents
    surface first.

    Build 202: optional `org_id` / `agent_id` filters from the
    AdminFilterBar. Date range is captured by `days` (already
    present) — the bar's date-range picker collapses to the closest
    preset (7/30/60/90) for this endpoint.

    Revenue / margin TODO once `plans.monthly_inr` exists — for now
    the P&L view is COGS-only, which is already enough to spot
    loss-makers on a flat-monthly plan."""
    await _admin_user(request)
    rows = await db.agent_pnl_report(
        days=days, org_id=org_id, agent_id=agent_id,
    )
    return {"days": days, "agents": rows}


# ─── helpers ─────────────────────────────────────────────────────────────

async def _admin_user(request: Request) -> dict:
    """Resolve caller via the same stub auth, then gate on super-admin."""
    from .app import current_user  # imported lazily to avoid circular
    user = await current_user(request)
    await auth.require_super_admin(user["id"])
    return user


async def _json(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    return body
