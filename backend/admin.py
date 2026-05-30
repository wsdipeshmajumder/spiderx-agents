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
async def admin_calls(request: Request, limit: int = 100) -> list[dict]:
    await _admin_user(request)
    return await db.admin_recent_calls(limit=limit)


# ─── audit log ───────────────────────────────────────────────────────────

@router.get("/audit")
async def admin_audit(request: Request, limit: int = 100, offset: int = 0,
                      actor_id: Optional[int] = None,
                      target_kind: Optional[str] = None,
                      target_id: Optional[str] = None) -> list[dict]:
    await _admin_user(request)
    return await db.list_audit(
        limit=limit, offset=offset,
        actor_id=actor_id, target_kind=target_kind, target_id=target_id,
    )


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
async def admin_llm_analytics(request: Request, days: int = 30) -> dict:
    """Universal LLM-cost ledger view (Phase 7). Includes Eva-builder
    sessions that don't surface in the customer-call analytics — gives
    finance the full token + cost picture. Split by kind so we can see
    whether the spend is build-time vs runtime."""
    await _admin_user(request)
    return await db.llm_analytics_platform(days=max(1, min(int(days), 365)))


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
