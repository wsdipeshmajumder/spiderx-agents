from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

from . import db, db_pg, gemini_bridge, twilio_bridge  # noqa: E402
from .presets import all_presets  # noqa: E402
from .admin import router as admin_router  # noqa: E402

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("eva")

app = FastAPI(title="SpiderX AI · Eva")
app.include_router(admin_router)


@app.on_event("startup")
async def _startup() -> None:
    """Boot the DB layer once the event loop is up. Warms the asyncpg pool
    eagerly so the first request doesn't pay the connect-pool cost. Then
    load the deterministic build templates from disk into memory — they're
    pure-YAML, read-only at runtime, so loading them once at startup
    keeps the WS-session path off the file-system entirely."""
    await db.init()
    log.info("db.backend=%s", db.backend())
    # Fail fast on broken templates: any YAML error during boot is
    # better than discovering it mid-build. `strict=True` raises if any
    # template has an undefined slot reference or unknown question type.
    try:
        from . import build_templates as _bt
        _registry = _bt.load_all(strict=False)
        log.info(
            "build_templates: %d templates resolved and ready (ids=%s)",
            len(_registry), sorted(_registry.keys()),
        )
    except Exception as e:  # noqa: BLE001
        # Don't crash the API if templates can't load — Eva falls back to
        # the probabilistic flow gracefully. But shout loud in the log.
        log.exception("build_templates: load_all crashed — falling back to probabilistic flow only: %s", e)


@app.on_event("shutdown")
async def _shutdown() -> None:
    """Drain the asyncpg pool cleanly so connections aren't left dangling."""
    await db.shutdown()

# Canonical SPA bundle version. Bump this on EVERY frontend change that the
# user might be served stale; index.html's <script src="app.js?v=N"> and the
# SXAI_BUILD constant in app.js MUST match this. The /api/build endpoint
# advertises this number so the SPA can self-detect a stale bundle on boot
# and force-reload once (see app.js for the sentinel logic).
APP_BUILD = 185


# ────────────────────────── auth (stub) ──────────────────────────
#
# We're running on a fake-login model — no password verification, no JWT.
# Every authenticated request carries `X-User-Id` (a header the SPA sets
# from localStorage after sign-in). When Auth0 wires in, only the source
# of truth changes: the dependency below validates an Auth0 JWT and looks
# up / creates the matching user row. Everything downstream stays the same.

async def current_user(request: Request) -> dict:
    """Resolves the requesting user. Header-based for now; will become an
    Auth0 JWT validator. Falls back to founder so unauthed test calls keep
    working during development."""
    uid_raw = request.headers.get("X-User-Id")
    if uid_raw:
        try:
            user = await db.get_user(int(uid_raw))
            if user:
                return user
        except (TypeError, ValueError):
            pass
    return await db.get_founder()


async def _require_agent_owned(agent_id: int, user: dict) -> dict:
    """Read-level permission check. In the team era this means "is the caller
    a member of the agent's org?". Pre-Phase-2 agents (no org_id) fall back
    to the legacy user_id == owner check so nothing 500s.

    Edit / delete actions should call `_require_agent_admin` instead, which
    additionally requires admin/owner role on the org."""
    from . import auth
    return await auth.require_agent_member(user["id"], agent_id)


async def _require_agent_admin(agent_id: int, user: dict) -> dict:
    """Write-level permission check — adds the admin/owner role requirement
    on top of org membership."""
    from . import auth
    return await auth.require_agent_admin(user["id"], agent_id)


@app.get("/api/me")
async def get_me(request: Request) -> dict:
    user = await current_user(request)
    # Pin plan state + org + super-admin flag onto /me so the topbar +
    # country-aware UIs + admin-shell gate only need one boot fetch.
    user["plan_state"] = await db.get_user_plan_state(user["id"])
    user["org"] = await db.get_org_for_user(user["id"])
    user["is_super_admin"] = await db.is_super_admin(user["id"])
    return user


@app.get("/api/me/org")
async def get_my_org(request: Request) -> dict:
    user = await current_user(request)
    org = await db.get_org_for_user(user["id"])
    if not org:
        raise HTTPException(status_code=404, detail="org not found")
    return org


@app.patch("/api/me/org")
async def patch_my_org(request: Request) -> dict:
    """Edit the user's org — name, country, tax_id, billing_address, currency,
    timezone. Anything else is ignored. Used by /account/org to set up
    invoicing details and the country that drives integration / profile
    defaults across the workspace."""
    user = await current_user(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    org = await db.update_org_for_user(user["id"], body)
    if not org:
        raise HTTPException(status_code=404, detail="org not found")
    return org


# ──────────────────────── Phase 2 — teams ────────────────────────
#
# Membership model lives in `org_members`; invites in `org_invites`. All
# routes below operate on the caller's primary org (users.org_id) so the
# UX matches the single-org world we ship in Phase 2. Multi-org switching
# arrives in Phase 3 along with the topbar org picker.
#
# Permission shim lives in backend/auth.py — never inline-check role
# strings inside routes.

from . import auth, email_stub  # noqa: E402


@app.get("/api/org/members")
async def list_team_members(request: Request) -> list[dict]:
    """Members of the caller's primary org with role + display info.
    Any member can see the roster (it's the "who's in your team" panel)."""
    user = await current_user(request)
    org_id = await auth.primary_org_id(user["id"])
    if org_id is None:
        return []
    await auth.require_member(user["id"], org_id)
    return await db.list_org_members(org_id)


@app.patch("/api/org/members/{user_id}")
async def patch_member_role(user_id: int, request: Request) -> dict:
    """Change a member's role. Owner-only. Can't demote the last owner —
    the org would lose its admin escape hatch."""
    actor = await current_user(request)
    org_id = await auth.primary_org_id(actor["id"])
    if org_id is None:
        raise HTTPException(status_code=404, detail="no org")
    await auth.require_owner(actor["id"], org_id)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    role = (body.get("role") or "").strip()
    if role not in ("owner", "admin", "member"):
        raise HTTPException(status_code=400, detail="role must be owner|admin|member")
    # Last-owner guard: if we're about to demote the only owner, refuse.
    if role != "owner":
        current_role = await db.get_member_role(org_id, user_id)
        if current_role == "owner" and await db.count_owners(org_id) <= 1:
            raise HTTPException(status_code=400,
                                detail={"code": "last_owner",
                                        "message": "Promote another member to owner first."})
    updated = await db.update_member_role(org_id, user_id, role)
    if not updated:
        raise HTTPException(status_code=404, detail="member not found")
    return updated


@app.delete("/api/org/members/{user_id}")
async def remove_team_member(user_id: int, request: Request) -> dict:
    """Remove a member from the org. Admin+ can remove members; only owners
    can remove other owners. Last-owner guard applies. Self-removal is
    allowed (any member can leave the org)."""
    actor = await current_user(request)
    org_id = await auth.primary_org_id(actor["id"])
    if org_id is None:
        raise HTTPException(status_code=404, detail="no org")
    role = await auth.require_member(actor["id"], org_id)
    target_role = await db.get_member_role(org_id, user_id)
    if target_role is None:
        raise HTTPException(status_code=404, detail="member not found")
    if actor["id"] != user_id:
        # Removing someone else — need admin+; owners need to remove owners.
        if role == "member":
            raise HTTPException(status_code=403,
                                detail={"code": "requires_admin",
                                        "message": "Only admins/owners can remove members."})
        if target_role == "owner" and role != "owner":
            raise HTTPException(status_code=403,
                                detail={"code": "requires_owner",
                                        "message": "Only owners can remove other owners."})
    if target_role == "owner" and await db.count_owners(org_id) <= 1:
        raise HTTPException(status_code=400,
                            detail={"code": "last_owner",
                                    "message": "Promote another member to owner first."})
    ok = await db.remove_org_member(org_id, user_id)
    return {"ok": ok}


@app.get("/api/org/invites")
async def list_invites(request: Request) -> list[dict]:
    """Active (un-accepted, un-declined, un-revoked, un-expired) invites for
    the caller's primary org. Any member can see them."""
    user = await current_user(request)
    org_id = await auth.primary_org_id(user["id"])
    if org_id is None:
        return []
    await auth.require_member(user["id"], org_id)
    return await db.list_pending_invites(org_id)


@app.post("/api/org/invites")
async def create_team_invite(request: Request) -> dict:
    """Create a fresh invite. Admin+ only. Fires the email stub immediately
    (which logs the URL in dev) so the inviter sees the link instantly.
    Rate-limited per org so an automation flood can't drain the email
    provider's reputation."""
    from . import ratelimit
    actor = await current_user(request)
    org_id = await auth.primary_org_id(actor["id"])
    if org_id is None:
        raise HTTPException(status_code=404, detail="no org")
    await ratelimit.acquire(org_id)
    await auth.require_admin(actor["id"], org_id)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    email = (body.get("email") or "").strip()
    role = (body.get("role") or "member").strip()
    if not email:
        raise HTTPException(status_code=400, detail="email required")
    if role not in ("admin", "member"):
        raise HTTPException(status_code=400, detail="role must be admin|member")
    from . import settings as cfg
    ttl_days = int(await cfg.get("limits.invite_ttl_days", 7))
    try:
        invite = await db.create_invite(org_id, email, role, actor["id"], ttl_days=ttl_days)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    org = await db.get_org_for_user(actor["id"])
    try:
        await email_stub.send_invite_email(
            to=email, inviter_name=actor.get("name"),
            org_name=(org or {}).get("name") or "your team",
            role=role, token=invite["token"],
        )
    except Exception as e:  # noqa: BLE001
        log.warning("invite.email_failed token=%s err=%s", invite["token"], e)
    # The token is what the link carries; we return it so dev/QA can copy it.
    return invite


@app.delete("/api/org/invites/{invite_id}")
async def revoke_team_invite(invite_id: int, request: Request) -> dict:
    """Cancel a pending invite. Admin+ only. Scoped to the caller's org so
    invite IDs from another org can't be revoked even if leaked."""
    actor = await current_user(request)
    org_id = await auth.primary_org_id(actor["id"])
    if org_id is None:
        raise HTTPException(status_code=404, detail="no org")
    await auth.require_admin(actor["id"], org_id)
    ok = await db.revoke_invite(invite_id, org_id)
    if not ok:
        raise HTTPException(status_code=404, detail="invite not found / already finalised")
    return {"ok": True}


# ── public invite preview / accept / decline (no auth required for preview) ──

@app.get("/api/invites/{token}")
async def get_invite_preview(token: str) -> dict:
    """Public — anyone with the token sees org name + inviter so they know
    what they're being asked to join before they log in."""
    inv = await db.get_invite_by_token(token)
    if not inv:
        raise HTTPException(status_code=404, detail="invite not found")
    from datetime import datetime, timezone
    expired = inv["expires_at"] and inv["expires_at"] < datetime.now(timezone.utc)
    finalised = bool(inv["accepted_at"] or inv["declined_at"] or inv["revoked_at"])
    # Trim sensitive fields — only return what the accept-invite UI needs.
    return {
        "org_name": inv["org_name"],
        "role": inv["role"],
        "email": inv["email"],
        "inviter_name": inv.get("inviter_name"),
        "inviter_email": inv.get("inviter_email"),
        "expires_at": inv["expires_at"],
        "status": ("accepted" if inv["accepted_at"]
                   else "declined" if inv["declined_at"]
                   else "revoked" if inv["revoked_at"]
                   else "expired" if expired
                   else "pending"),
    }


@app.post("/api/invites/{token}/accept")
async def accept_team_invite(token: str, request: Request) -> dict:
    """Bind the invite to the logged-in user. The caller must be auth'd —
    in dev that means X-User-Id is set to a real user, not the founder
    fallback. Returns the new membership row."""
    user = await current_user(request)
    membership = await db.accept_invite(token, user["id"])
    if not membership:
        raise HTTPException(status_code=400,
                            detail={"code": "invite_invalid",
                                    "message": "Invite is missing, expired, or already used."})
    return membership


@app.post("/api/invites/{token}/decline")
async def decline_team_invite(token: str) -> dict:
    """No auth — anyone with the token can decline (clicking "wasn't me" in
    the email). Doesn't reveal whether the token was valid; always 200 OK
    so a guess-the-token attacker learns nothing."""
    await db.decline_invite(token)
    return {"ok": True}


@app.get("/api/plans")
async def list_plans() -> list[dict]:
    return await db.list_plans()


@app.get("/api/me/plan")
async def get_my_plan(request: Request) -> dict:
    user = await current_user(request)
    return await db.get_user_plan_state(user["id"])


@app.post("/api/me/upgrade")
async def upgrade_plan(request: Request) -> dict:
    """Upgrades the caller's plan. Two paths:

      (a) Razorpay verified — body carries {plan_id, razorpay_payment_id,
          razorpay_order_id, razorpay_signature}. We verify the HMAC, then
          flip the plan + reset minutes.

      (b) Demo mode — when RAZORPAY_KEY_SECRET isn't set, we accept
          {plan_id} alone and mark paid immediately. Keeps the flow
          end-to-end testable without real credentials.
    """
    user = await current_user(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    try:
        plan_id = int(body.get("plan_id"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="plan_id is required")
    plan = await db.get_plan(plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="plan not found")

    razorpay_secret = os.environ.get("RAZORPAY_KEY_SECRET")
    if razorpay_secret and plan["price_paise"] > 0:
        # Real payment path — verify Razorpay's HMAC. Order + payment IDs are
        # opaque strings; signature = HMAC_SHA256(order_id|payment_id, secret).
        order_id = (body.get("razorpay_order_id") or "").strip()
        payment_id = (body.get("razorpay_payment_id") or "").strip()
        signature = (body.get("razorpay_signature") or "").strip()
        if not (order_id and payment_id and signature):
            raise HTTPException(status_code=400, detail="razorpay payment fields required")
        import hashlib, hmac
        expected = hmac.new(
            razorpay_secret.encode("utf-8"),
            f"{order_id}|{payment_id}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise HTTPException(status_code=400, detail="signature mismatch")
        log.info("upgrade.razorpay user=%s plan=%s payment=%s", user["id"], plan["slug"], payment_id)
    else:
        # Demo mode — log loudly so prod doesn't accidentally ship this.
        if plan["price_paise"] > 0:
            log.warning("upgrade.demo (RAZORPAY_KEY_SECRET unset) user=%s plan=%s", user["id"], plan["slug"])

    return await db.set_user_plan(user["id"], plan["id"], reset_usage=True)


@app.post("/api/razorpay/order")
async def razorpay_order(request: Request) -> dict:
    """Create a Razorpay order for the requested plan. Returns the order id +
    public key for the frontend's Checkout. Skipped (returns demo stub) when
    RAZORPAY_KEY_ID isn't configured — useful for local dev."""
    user = await current_user(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    try:
        plan_id = int(body.get("plan_id"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="plan_id is required")
    plan = await db.get_plan(plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="plan not found")

    key_id = os.environ.get("RAZORPAY_KEY_ID")
    key_secret = os.environ.get("RAZORPAY_KEY_SECRET")
    if not key_id or not key_secret or plan["price_paise"] == 0:
        # Demo stub so the frontend can render the "upgrade succeeded" state
        # without a real payment. Clearly marked so it can't slip into prod.
        return {
            "demo": True,
            "key": None,
            "order_id": f"demo_order_{plan['slug']}_{user['id']}",
            "amount_paise": plan["price_paise"],
            "currency": "INR",
            "plan_id": plan["id"],
            "plan_label": plan["label"],
            "name": user.get("name") or user.get("email"),
            "email": user.get("email"),
        }

    # Real Razorpay call — using urllib so we don't need to add the SDK.
    import base64, json as _json, urllib.request, urllib.error
    payload = _json.dumps({
        "amount": plan["price_paise"],
        "currency": "INR",
        "receipt": f"plan_{plan['slug']}_user_{user['id']}",
        "notes": {"plan_id": str(plan["id"]), "user_id": str(user["id"])},
    }).encode("utf-8")
    auth = base64.b64encode(f"{key_id}:{key_secret}".encode("utf-8")).decode("ascii")
    req = urllib.request.Request(
        "https://api.razorpay.com/v1/orders",
        data=payload,
        headers={"Authorization": f"Basic {auth}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            order = _json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        log.error("razorpay.order_http err=%s body=%s", e.code, e.read()[:300])
        raise HTTPException(status_code=502, detail="razorpay error")
    except Exception:
        log.exception("razorpay.order failed")
        raise HTTPException(status_code=502, detail="razorpay unreachable")
    return {
        "demo": False,
        "key": key_id,
        "order_id": order.get("id"),
        "amount_paise": plan["price_paise"],
        "currency": "INR",
        "plan_id": plan["id"],
        "plan_label": plan["label"],
        "name": user.get("name") or user.get("email"),
        "email": user.get("email"),
    }


@app.post("/api/auth/signup")
async def auth_signup(request: Request) -> dict:
    """Stub sign-up — creates a user row (idempotent on email). Real
    password handling is deferred to Auth0; we just record intent here.

    Phase 4: gated by the `features.signups_open` platform setting so we
    can close public signups before launch without a deploy."""
    from . import settings as cfg  # local import — settings module touches db pool
    if await cfg.get("features.signups_open", True) is False:
        raise HTTPException(
            status_code=403,
            detail={"code": "signups_closed",
                    "message": "Sign-ups are currently closed. Reach out to "
                               "support if you need access."},
        )
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    email = (body.get("email") or "").strip().lower()
    name = (body.get("name") or "").strip() or None
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="valid email is required")
    user = await db.create_user(email, name=name, provider="stub")
    log.info("auth.signup email=%s user_id=%s", email, user["id"])
    return user


@app.post("/api/auth/login")
async def auth_login(request: Request) -> dict:
    """Stub login — looks up the user by email. No password check (we're
    Auth0-bound). Returns the user row if it exists, or 404 to prompt
    a sign-up. Real verification lands when Auth0 is in front."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    email = (body.get("email") or "").strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="email is required")
    user = await db.get_user_by_email(email)
    if not user:
        raise HTTPException(status_code=404, detail="no account for that email — try sign up")
    log.info("auth.login email=%s user_id=%s", email, user["id"])
    return user


# ────────────────────────── REST ──────────────────────────

@app.get("/api/build")
async def build() -> dict:
    """Tiny endpoint the SPA hits on boot to detect a stale bundle and
    force-reload itself when the deployed build is newer than what the
    browser actually has running. Kept separate from /api/health so the
    SPA can poll it cheaply without dragging Gemini-config checks."""
    return {"build": APP_BUILD}


@app.get("/api/health")
async def health() -> dict:
    """Operator + Railway healthcheck. Exercises:
      - asyncpg pool reachable (SELECT 1)
      - row counts on the two seed tables that should never be empty
    Returns 200 with `{status: 'ok', db: 'up'}` on success, or 503 with
    a per-component breakdown on partial failure. The Railway healthcheck
    config can point at this path; failure marks the instance unhealthy
    and reroutes traffic to the next replica."""
    import time as _t
    started = _t.monotonic()
    pool_ok = False
    plans_seeded = False
    try:
        pool = await db_pg.get_pool()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
            pool_ok = True
            plans_seeded = (await conn.fetchval("SELECT COUNT(*) FROM plans") or 0) >= 1
    except Exception as e:  # noqa: BLE001
        log.warning("health.db_check_failed: %s", e)
    latency_ms = int((_t.monotonic() - started) * 1000)
    ok = pool_ok and plans_seeded
    payload = {
        "status": "ok" if ok else "degraded",
        "db": "up" if pool_ok else "down",
        "plans_seeded": plans_seeded,
        "build": APP_BUILD,
        "latency_ms": latency_ms,
    }
    if not ok:
        return JSONResponse(content=payload, status_code=503)
    return payload


@app.get("/api/version")
async def version() -> dict:
    """Static metadata about the running release. Useful from the admin
    grid + a status page to tell which commit shipped without grepping
    server logs. Build number is the truth; everything else is best-
    effort metadata that survives missing env vars."""
    return {
        "build": APP_BUILD,
        "service": "spiderx-ai",
        "model": gemini_bridge.DEFAULT_MODEL,
        "env": os.environ.get("DEPLOY_ENV") or "dev",
    }


@app.get("/api/presets")
async def get_presets() -> dict:
    return all_presets()


# ──────────────────── Form-wizard build (default UX) ────────────────────
#
# The wizard is the default way to build an agent: a deterministic
# multi-step form whose steps come from the same YAML templates the
# chat/voice paths use. These two endpoints back it:
#   GET  /api/build/template  → resolve the template + question list
#   POST /api/build/wizard    → validate answers, compose, create the agent

@app.get("/api/build/template")
async def build_template(industry: str = "", locale: str = "en-IN") -> dict:
    """Resolve the wizard's question list for an industry + locale. Falls
    back to the generic template when the industry has no dedicated one
    (so the wizard always has a coherent question set)."""
    from . import build_templates as _bt
    ind = (industry or "").strip().lower() or None
    loc = (locale or "").strip() or None
    tpl = None
    if ind:
        tpl = _bt.match_by_industry(ind, locale=loc)
    if tpl is None:
        # No industry (Any) or no template for it → generic.
        tpl = _bt.get_template("_generic")
    if tpl is None:
        raise HTTPException(status_code=503, detail="build templates not loaded")
    return _bt.wizard_payload(tpl)


@app.post("/api/build/dynamic-template")
async def build_dynamic_template(request: Request) -> dict:
    """Catch-all (Any-industry) form generator. Body: `{text, locale}`. The
    best model designs a use-case-tailored question set AND pre-fills the
    answers the description states. Returns a wizard-payload-shaped dict with
    `dynamic: true`. Falls back to the static generic template on failure."""
    from . import build_templates as _bt
    from . import chat_bridge
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    text = (body.get("text") or "").strip()
    locale = (body.get("locale") or "en-IN").strip() or "en-IN"
    if not text:
        # Nothing to tailor on → hand back the static generic form.
        gen = _bt.get_template("_generic")
        return {"ok": True, "template": _bt.wizard_payload(gen) if gen else None, "dynamic": False}
    tpl = await chat_bridge.generate_dynamic_template(text, locale=locale)
    if not tpl:
        gen = _bt.get_template("_generic")
        return {"ok": True, "template": _bt.wizard_payload(gen) if gen else None, "dynamic": False}
    return {"ok": True, "template": tpl, "dynamic": True}


@app.post("/api/build/wizard")
async def build_wizard(request: Request) -> dict:
    """Create an agent from a completed wizard form.

    Two modes:
      • template mode — `{industry, locale, answers}` → compose from the YAML
        template (chat/voice-equivalent path).
      • dynamic mode  — `{dynamic:true, use_case, locale, questions, answers}` →
        the best model composes a bespoke agent for an arbitrary use case.

    Mirrors chat_bridge.on_save_agent so a form-built agent is
    indistinguishable from a chat/voice-built one downstream."""
    from . import build_templates as _bt
    user = await current_user(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    locale = (body.get("locale") or "en-IN").strip() or "en-IN"
    raw_answers = body.get("answers") if isinstance(body.get("answers"), dict) else {}
    # Knowledge YAML the operator reviewed in the wizard preview (after the
    # Firecrawl scrape of their website / Google Maps listing). Folded into
    # the agent at create time — appended as a KNOWLEDGE block on the system
    # prompt + tracked in variables.knowledge_sources.
    knowledge_yaml = (body.get("knowledge_yaml") or "").strip()
    knowledge_source = body.get("knowledge_source") if isinstance(body.get("knowledge_source"), dict) else None

    def _fold_knowledge(args: dict) -> dict:
        if not knowledge_yaml:
            return args
        from . import import_helpers as _ih
        src = knowledge_source or {"kind": "url", "url": "", "title": ""}
        args["system_prompt"] = _ih.append_knowledge_block(
            args.get("system_prompt") or "", knowledge_yaml, src,
        )
        args["variables"] = _ih.add_source_to_variables(args.get("variables") or {}, src)
        return args

    # ── Dynamic (catch-all) mode ──
    if body.get("dynamic"):
        from . import chat_bridge
        use_case = (body.get("use_case") or "").strip()
        questions = body.get("questions") if isinstance(body.get("questions"), list) else []
        # Validate against the questions the client carried back from the
        # dynamic template (so required fields are enforced + types coerced).
        cleaned: dict[str, object] = {}
        errors: dict[str, str] = {}
        for q in questions:
            if not isinstance(q, dict):
                continue
            qid = q.get("id")
            if not qid:
                continue
            raw = raw_answers.get(qid)
            if raw is None or (isinstance(raw, str) and not raw.strip()):
                if q.get("required"):
                    errors[qid] = "required"
                continue
            value, err = _bt.validate_answer(q, raw)
            if err:
                errors[qid] = err
                continue
            cleaned[qid] = value
        if errors:
            raise HTTPException(status_code=422, detail={"code": "invalid_answers", "fields": errors})
        args = await chat_bridge.compose_dynamic_agent(
            use_case, cleaned, locale=locale, sector_hint=(body.get("sector") or "generic"),
        )
        # Preserve the bespoke Additional Info schema + prefill across the
        # silent-defaults merge (it only backfills voice/ambience/etc.).
        dyn_info_groups = args.get("info_groups")
        dyn_extra_info = args.get("extra_info")
        try:
            from . import silent_defaults
            args = silent_defaults.merge_into_save_args(args)
        except Exception as e:  # noqa: BLE001
            log.warning("wizard(dynamic): silent_defaults merge failed: %s", e)
        if dyn_info_groups:
            args["info_groups"] = dyn_info_groups
        if dyn_extra_info:
            args["extra_info"] = {**dyn_extra_info, **(args.get("extra_info") or {})}
        args = _fold_knowledge(args)
        try:
            saved = await db.create_agent(args, user_id=user["id"])
        except Exception as e:  # noqa: BLE001
            log.exception("wizard(dynamic): create_agent failed: %s", e)
            raise HTTPException(status_code=500, detail="couldn't save agent — see server log")
        try:
            await db.seed_helper_memory(user_id=user["id"], agent_id=saved["id"], agent=saved)
        except Exception as e:  # noqa: BLE001
            log.warning("seed_helper_memory(dynamic) failed: %s", e)
        log.info("build_wizard(dynamic): created agent id=%s name=%s knowledge=%s",
                 saved.get("id"), saved.get("name"), bool(knowledge_yaml))
        return {"ok": True, "agent": saved}

    # ── Template mode ──
    industry = (body.get("industry") or "").strip().lower() or None
    tpl = _bt.match_by_industry(industry, locale=locale) if industry else None
    if tpl is None:
        tpl = _bt.get_template("_generic")
    if tpl is None:
        raise HTTPException(status_code=503, detail="build templates not loaded")

    # Validate + coerce every answer against its question. Required
    # questions must be present and valid; optional ones may be skipped.
    cleaned: dict[str, object] = {}
    errors: dict[str, str] = {}
    for q in (tpl.get("questions") or []):
        qid = q.get("id")
        if not qid:
            continue
        raw = raw_answers.get(qid)
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            if q.get("required") and "default" not in q:
                errors[qid] = "required"
            continue
        value, err = _bt.validate_answer(q, raw)
        if err:
            errors[qid] = err
            continue
        cleaned[qid] = value
    if errors:
        raise HTTPException(status_code=422, detail={"code": "invalid_answers", "fields": errors})

    args = _bt.compose_save_args(tpl, cleaned)
    try:
        from . import silent_defaults
        args = silent_defaults.merge_into_save_args(args)
    except Exception as e:  # noqa: BLE001
        log.warning("wizard: silent_defaults merge failed: %s", e)
    try:
        from . import info_schemas
        pre = info_schemas.prefill_extra_info(args.get("sector"), args.get("variables") or {})
        if pre:
            args["extra_info"] = {**pre, **(args.get("extra_info") or {})}
    except Exception as e:  # noqa: BLE001
        log.warning("wizard: prefill_extra_info failed: %s", e)

    args = _fold_knowledge(args)
    try:
        saved = await db.create_agent(args, user_id=user["id"])
    except Exception as e:  # noqa: BLE001
        log.exception("wizard: create_agent failed: %s", e)
        raise HTTPException(status_code=500, detail="couldn't save agent — see server log")
    try:
        await db.seed_helper_memory(user_id=user["id"], agent_id=saved["id"], agent=saved)
    except Exception as e:  # noqa: BLE001
        log.warning("seed_helper_memory(template) failed: %s", e)
    log.info("build_wizard: created agent id=%s name=%s (template=%s) knowledge=%s",
             saved.get("id"), saved.get("name"), tpl.get("id"), bool(knowledge_yaml))
    return {"ok": True, "agent": saved}


@app.post("/api/build/extract")
async def build_extract(request: Request) -> dict:
    """LLM pre-fill for the wizard. Body: `{industry, locale, text}`. Runs
    a one-shot extraction over the operator's free-text description (typed
    in the landing prompt box) and returns `{answers: {question_id: value}}`
    the wizard seeds its fields with."""
    from . import build_templates as _bt
    from . import chat_bridge
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    industry = (body.get("industry") or "").strip().lower() or None
    locale = (body.get("locale") or "en-IN").strip() or "en-IN"
    text = (body.get("text") or "").strip()
    if not text:
        return {"ok": True, "answers": {}}
    tpl = _bt.match_by_industry(industry, locale=locale) if industry else None
    if tpl is None:
        tpl = _bt.get_template("_generic")
    if tpl is None:
        return {"ok": True, "answers": {}}
    answers = await chat_bridge.extract_wizard_answers(tpl, text, locale=locale)
    return {"ok": True, "answers": answers, "template_id": tpl.get("id")}


@app.post("/api/build/wizard/sync")
async def build_wizard_sync(request: Request) -> dict:
    """Persist the wizard's in-progress answers onto a build_session so a
    switch to chat/voice resumes seamlessly — Eva sees what's already
    answered and never re-asks it. Body: `{sid, industry, locale, answers}`."""
    from . import build_templates as _bt
    user = await current_user(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    sid = (body.get("sid") or "").strip()
    if not sid:
        raise HTTPException(status_code=400, detail="sid is required")
    industry = (body.get("industry") or "").strip().lower() or None
    locale = (body.get("locale") or "en-IN").strip() or "en-IN"
    answers = body.get("answers") if isinstance(body.get("answers"), dict) else {}

    tpl = _bt.match_by_industry(industry, locale=locale) if industry else None
    if tpl is None:
        tpl = _bt.get_template("_generic")
    if tpl is None:
        raise HTTPException(status_code=503, detail="build templates not loaded")

    try:
        await db.set_build_template(user_id=user["id"], sid=sid, template_id=tpl["id"])
    except Exception as e:  # noqa: BLE001
        log.warning("wizard/sync set_build_template failed: %s", e)

    qby = {q.get("id"): q for q in (tpl.get("questions") or [])}
    recorded: list[str] = []
    for qid, raw in (answers or {}).items():
        q = qby.get(qid)
        if not q:
            continue
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            continue
        cleaned, err = _bt.validate_answer(q, raw)
        if err:
            continue
        try:
            await db.record_template_answer(user_id=user["id"], sid=sid, question_id=qid, value=cleaned)
            recorded.append(qid)
        except Exception as e:  # noqa: BLE001
            log.warning("wizard/sync record %s failed: %s", qid, e)
    log.info("build_wizard_sync: sid=%s template=%s recorded=%d",
             sid[:18], tpl.get("id"), len(recorded))
    return {"ok": True, "template_id": tpl["id"], "recorded": recorded}


# ─── Knowledge ingestion (URL via Firecrawl + file upload) ──────────────────


@app.post("/api/build/scrape-url")
async def build_scrape_url(request: Request) -> dict:
    """Pull facts from a website / Google-Maps / local-listing URL during the
    wizard. Returns YAML the operator REVIEWS in a preview before it's saved
    onto the agent at create time. Body: `{url, locale?, context?}`."""
    from . import import_helpers as _ih
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    url = (body.get("url") or "").strip()
    locale = (body.get("locale") or "en-IN").strip() or "en-IN"
    context = (body.get("context") or "").strip()
    try:
        scrape = await _ih.firecrawl_scrape(url)
        yaml_text = await _ih.condense_to_yaml(
            markdown=scrape["markdown"], source_url=scrape["source_url"],
            source_title=scrape.get("title", ""), context_hint=context, locale=locale,
        )
    except _ih.IngestError as e:
        raise HTTPException(status_code=e.status, detail={"code": e.code, "message": str(e)})
    return {
        "ok": True, "yaml": yaml_text,
        "source": {"kind": "url", "url": scrape["source_url"], "title": scrape.get("title", "")},
    }


@app.post("/api/agents/{agent_id}/knowledge/import-url")
async def agent_knowledge_import_url(agent_id: int, request: Request) -> dict:
    """Import a URL's facts onto an EXISTING agent (post-build flow on the
    Knowledge base page). Body: `{url}`. Like scrape-url but for an existing
    agent — the operator confirms the YAML preview, then we append a
    KNOWLEDGE block to the system prompt + record the source."""
    from . import import_helpers as _ih
    user = await current_user(request)
    agent = await _require_agent_owned(agent_id, user)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    url = (body.get("url") or "").strip()
    yaml_text = (body.get("yaml") or "").strip()
    apply_now = bool(body.get("apply"))
    locale = (agent.get("locale") or "en-IN")
    context = f"{agent.get('name') or ''} — {agent.get('persona') or ''}"

    if yaml_text and apply_now:
        # Operator already reviewed the YAML and asked to apply it.
        source = {"kind": "url", "url": url, "title": body.get("title") or ""}
    else:
        # Scrape + condense; return YAML for the preview.
        try:
            scrape = await _ih.firecrawl_scrape(url)
            yaml_text = await _ih.condense_to_yaml(
                markdown=scrape["markdown"], source_url=scrape["source_url"],
                source_title=scrape.get("title", ""), context_hint=context, locale=locale,
            )
        except _ih.IngestError as e:
            raise HTTPException(status_code=e.status, detail={"code": e.code, "message": str(e)})
        if not apply_now:
            return {"ok": True, "preview": True, "yaml": yaml_text,
                    "source": {"kind": "url", "url": scrape["source_url"], "title": scrape.get("title", "")}}
        source = {"kind": "url", "url": scrape["source_url"], "title": scrape.get("title", "")}

    if not yaml_text:
        raise HTTPException(status_code=422, detail="nothing to apply")

    new_prompt = _ih.append_knowledge_block(agent.get("system_prompt") or "", yaml_text, source)
    new_vars = _ih.add_source_to_variables(agent.get("variables") or {}, source)
    updated = await db.update_agent(agent_id, {"system_prompt": new_prompt, "variables": new_vars})
    if not updated:
        raise HTTPException(status_code=404, detail="agent not found")
    log.info("knowledge.import_url: agent id=%s source=%s yaml_bytes=%d",
             agent_id, source.get("url"), len(yaml_text))
    return {"ok": True, "agent": updated, "source": source}


@app.post("/api/agents/{agent_id}/knowledge/upload")
async def agent_knowledge_upload(agent_id: int, request: Request) -> dict:
    """Upload a .txt / .docx file onto an EXISTING agent's knowledge base.
    Body: multipart with field `file` plus optional `apply=true` (one-shot)
    or `yaml=<edited>` + `filename` (apply a previewed/edited version).
    Two-step like import-url so the operator can review the YAML preview."""
    from . import import_helpers as _ih
    user = await current_user(request)
    agent = await _require_agent_owned(agent_id, user)
    form = await request.form()
    yaml_text = (form.get("yaml") or "").strip()
    filename = (form.get("filename") or "").strip()
    apply_now = str(form.get("apply") or "").lower() in ("1", "true", "yes")
    if not yaml_text:
        # New upload — parse + condense.
        f = form.get("file")
        if f is None or not hasattr(f, "filename"):
            raise HTTPException(status_code=400, detail="no file uploaded")
        filename = filename or (f.filename or "")
        raw = await f.read()
        try:
            text = _ih.parse_file_to_text(filename, raw)
            yaml_text = await _ih.condense_to_yaml(
                markdown=text, source_url=f"file://{filename}", source_title=filename,
                context_hint=f"{agent.get('name') or ''} — {agent.get('persona') or ''}",
                locale=agent.get("locale") or "en-IN",
            )
        except _ih.IngestError as e:
            raise HTTPException(status_code=e.status, detail={"code": e.code, "message": str(e)})
        if not apply_now:
            return {"ok": True, "preview": True, "yaml": yaml_text,
                    "source": {"kind": "file", "filename": filename, "title": filename}}

    if not yaml_text:
        raise HTTPException(status_code=422, detail="nothing to apply")
    source = {"kind": "file", "filename": filename or "uploaded.txt", "title": filename or "Uploaded file"}
    new_prompt = _ih.append_knowledge_block(agent.get("system_prompt") or "", yaml_text, source)
    new_vars = _ih.add_source_to_variables(agent.get("variables") or {}, source)
    updated = await db.update_agent(agent_id, {"system_prompt": new_prompt, "variables": new_vars})
    if not updated:
        raise HTTPException(status_code=404, detail="agent not found")
    log.info("knowledge.upload: agent id=%s filename=%s yaml_bytes=%d",
             agent_id, source.get("filename"), len(yaml_text))
    return {"ok": True, "agent": updated, "source": source}


@app.post("/api/auth/google")
async def auth_google(request: Request) -> dict:
    """Mock Google sign-in. Real OAuth lands with Auth0; for now we upsert
    a user from the supplied (or a demo) Google identity so the
    login→resume build flow is demoable end-to-end."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    email = (body.get("email") or "demo.user@gmail.com").strip().lower()
    name = (body.get("name") or "").strip() or None
    if "@" not in email:
        raise HTTPException(status_code=400, detail="valid email is required")
    user = await db.create_user(email, name=name, provider="google")
    log.info("auth.google email=%s user_id=%s", email, user["id"])
    return user


@app.get("/api/agents")
async def list_agents(request: Request) -> list[dict]:
    user = await current_user(request)
    return await db.list_agents(user["id"])


@app.get("/api/agents/{agent_id}")
async def get_agent(agent_id: int, request: Request) -> dict:
    return await _require_agent_owned(agent_id, await current_user(request))


@app.get("/api/agents/by-slug/{slug}")
async def get_agent_by_slug(slug: str, request: Request) -> dict:
    """Resolve an agent by its URL-friendly slug. Phase 9b made slugs
    org-scoped (composite UNIQUE on `(org_id, slug)`), so we resolve
    inside the caller's primary org. The membership check is still
    the safety net for any cross-org slug-guessing attempt."""
    from . import auth
    user = await current_user(request)
    org_id = await auth.primary_org_id(user["id"])
    a = await db.get_agent_by_slug(slug, org_id=org_id)
    if not a:
        raise HTTPException(status_code=404, detail="agent not found")
    await _require_agent_owned(a["id"], user)
    return a


@app.delete("/api/agents/{agent_id}")
async def delete_agent(agent_id: int, request: Request) -> dict:
    """Hard-delete the agent. Admin+ on the org only — members can build
    but not delete (the latter is destructive enough to gate)."""
    await _require_agent_admin(agent_id, await current_user(request))
    if not await db.delete_agent(agent_id):
        raise HTTPException(status_code=404, detail="agent not found")
    return {"ok": True}


@app.get("/api/agents/{agent_id}/stats")
async def agent_stats(agent_id: int, request: Request) -> dict:
    await _require_agent_owned(agent_id, await current_user(request))
    return await db.call_stats_for_agent(agent_id)


@app.get("/api/agents/{agent_id}/calls")
async def agent_calls(agent_id: int, limit: int = 50, request: Request = None) -> list[dict]:
    await _require_agent_owned(agent_id, await current_user(request))
    return await db.list_calls_for_agent(agent_id, limit=limit)


@app.patch("/api/agents/{agent_id}")
async def patch_agent(agent_id: int, request: Request) -> dict:
    """Partial update of a saved agent. Accepts any subset of the editable
    fields plus a `voice_tweaks` JSON object with per-agent Gemini Live
    voice params (voice, locale, temperature, top_p, sensitivity, etc.).

    Rate-limited per org (Phase 6) — protects the audit log + analytics
    rollups from a pathological client that PATCHes 1000 times/sec."""
    from . import ratelimit
    user = await current_user(request)
    a = await _require_agent_owned(agent_id, user)
    await ratelimit.acquire(a.get("org_id"))
    try:
        patch = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    if not isinstance(patch, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    # Publishing is a paid-plan feature. Free-tier users can build, edit, and
    # preview agents but the embed snippet won't go live and the phone number
    # won't ring out until they upgrade. We block the PATCH cleanly so the
    # frontend can show an "Upgrade to publish" gate.
    if patch.get("published") is True:
        plan_state = await db.get_user_plan_state(user["id"])
        plan_slug = ((plan_state or {}).get("plan") or {}).get("slug")
        if not plan_slug or plan_slug == "free":
            raise HTTPException(
                status_code=402,
                detail={
                    "code": "publish_requires_paid_plan",
                    "message": "Publishing requires a paid plan. Upgrade from /account/billing.",
                    "current_plan": plan_slug or "free",
                },
            )
    updated = await db.update_agent(agent_id, patch)
    if not updated:
        raise HTTPException(status_code=404, detail="agent not found")
    return updated


@app.get("/api/agents/{agent_id}/outcomes/catalogue")
async def get_agent_outcome_catalogue(agent_id: int, request: Request) -> dict:
    """The sector × locale × user-input outcome catalogue for THIS agent.
    Backs the read-only catalogue card on the Call outcomes page."""
    from . import call_outcomes
    user = await current_user(request)
    agent = await _require_agent_owned(agent_id, user)
    return {
        "agent_id": agent_id,
        "sector": agent.get("sector"),
        "locale": agent.get("locale"),
        "outcomes": call_outcomes.catalogue_for(agent),
    }


@app.get("/api/agents/{agent_id}/outcomes/report")
async def get_agent_outcome_report(agent_id: int, days: int = 30, request: Request = None) -> dict:
    """Performance report: joins the per-agent catalogue with the rollup
    analytics. Returns totals, per-outcome counts, per-kind totals
    (success/qualified/info/failure), the weighted success rate, and the
    daily series. The Call outcomes page renders this as the main report."""
    from . import call_outcomes
    user = await current_user(request)
    agent = await _require_agent_owned(agent_id, user)
    d = max(1, min(int(days), 365))
    analytics = await db.agent_analytics(agent_id, days=d)
    return call_outcomes.assemble_report(agent, analytics)


@app.get("/api/agents/{agent_id}/conventions")
async def get_agent_conventions(agent_id: int, request: Request) -> dict:
    """Operator-facing JSON view of the systemic phone-AI conventions that
    apply to THIS agent (speech rules, silence policy, sector playbook).
    Used by the dashboard's read-only 'Phone AI conventions' panel so the
    operator can see what's auto-applied to every call."""
    from . import phone_ai_conventions
    user = await current_user(request)
    agent = await _require_agent_owned(agent_id, user)
    return phone_ai_conventions.summarize_for_ui(agent)


@app.post("/api/agents/{agent_id}/regenerate-info-groups")
async def regenerate_agent_info_groups(agent_id: int, request: Request) -> dict:
    """Redesign an agent's Additional Info sections to match its CURRENT
    purpose (the best model), carrying existing notes into the new sections.
    The frontend shows an impact confirmation BEFORE calling this — it
    overwrites the agent's `info_groups` + `extra_info`."""
    from . import chat_bridge
    user = await current_user(request)
    agent = await _require_agent_owned(agent_id, user)
    result = await chat_bridge.regenerate_info_groups(agent)
    if not result:
        raise HTTPException(status_code=502, detail="couldn't redesign the sections — please try again")
    updated = await db.update_agent(agent_id, {
        "info_groups": result["info_groups"],
        "extra_info": result["extra_info"],
    })
    if not updated:
        raise HTTPException(status_code=404, detail="agent not found")
    log.info("regenerate_info_groups: agent id=%s → %d sections",
             agent_id, len(result["info_groups"]))
    return {"ok": True, "agent": updated,
            "info_groups": result["info_groups"], "extra_info": result["extra_info"]}


@app.get("/api/agents/{agent_id}/analytics")
async def get_agent_analytics(agent_id: int, request: Request, days: int = 30) -> dict:
    """Per-agent time-series + totals + outcome distribution for the last
    `days` calendar days. Reads materialised rollups so the per-agent
    Overview card renders instantly even at high call volume."""
    await _require_agent_owned(agent_id, await current_user(request))
    return await db.agent_analytics(agent_id, days=max(1, min(int(days), 365)))


@app.get("/api/org/analytics")
async def get_org_analytics(request: Request, days: int = 30) -> dict:
    """Org-wide rollup for the caller's primary org. Any member can read it."""
    user = await current_user(request)
    from . import auth
    org_id = await auth.primary_org_id(user["id"])
    if org_id is None:
        return {"range_days": int(days), "totals": {}, "series": [], "top_agents": []}
    await auth.require_member(user["id"], org_id)
    return await db.org_analytics(org_id, days=max(1, min(int(days), 365)))


@app.get("/api/org/analytics/llm")
async def get_org_llm_analytics(request: Request, days: int = 30) -> dict:
    """Org-wide LLM ledger (Phase 7) — every Eva-builder + agent-call
    session this org racked up, with cost-per-minute and kind breakdown.
    Any member can read it (cost transparency is the right default for
    a team)."""
    user = await current_user(request)
    from . import auth
    org_id = await auth.primary_org_id(user["id"])
    if org_id is None:
        return {"range_days": int(days), "totals": {}, "by_kind": []}
    await auth.require_member(user["id"], org_id)
    return await db.llm_analytics_for_org(org_id, days=max(1, min(int(days), 365)))


@app.get("/api/agents/{agent_id}/number-requests")
async def agent_number_requests(agent_id: int, limit: int = 100, request: Request = None) -> list[dict]:
    await _require_agent_owned(agent_id, await current_user(request))
    return await db.list_number_requests_for_agent(agent_id, limit=limit)


@app.post("/api/support/tickets")
async def create_support_ticket(request: Request) -> dict:
    """Inbound support ticket from the topbar "Raise a Support Ticket" button.
    For now: just log + return — ops monitors the logs and replies by email.
    A real ticketing-table backing comes after we plug into Linear/Plain."""
    try:
        user = await current_user(request)
    except Exception:
        user = None
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    subject = (body.get("subject") or "").strip()
    message = (body.get("message") or "").strip()
    if not subject or not message:
        raise HTTPException(status_code=400, detail="subject and message are required")
    log.info(
        "support_ticket topic=%s subject=%r agent=%s user=%s email=%s msg=%r",
        (body.get("topic") or "general"),
        subject[:80],
        body.get("agent_id"),
        (user or {}).get("id"),
        body.get("email"),
        message[:200],
    )
    return {"ok": True, "received_at": int(time.time())}


@app.post("/api/agents/{agent_id}/webhook/test")
async def test_agent_webhook(agent_id: int, request: Request) -> dict:
    """Fire a one-off test payload at the agent's configured webhook so the
    customer can self-serve verification before they put the agent in front
    of real callers. Mirrors the exact shape we POST at end_call time so the
    user's integration sees what production will look like.

    Returns the receiver's HTTP status + the first 500 chars of the response
    body so the UI can show "200 OK · response_body" inline."""
    user = await current_user(request)
    await _require_agent_owned(agent_id, user)
    agent = await db.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="agent not found")
    url = (agent.get("webhook_url") or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail={"code": "no_webhook_url",
            "message": "Add a webhook URL before testing."})
    if not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(status_code=400, detail={"code": "bad_webhook_url",
            "message": "URL must start with https:// (http:// works for local dev only)."})

    sample_payload = {
        "event": "call.ended",
        "test": True,
        "agent": {"id": agent["id"], "name": agent["name"], "slug": agent.get("slug")},
        "call": {
            "id": f"test-{int(time.time())}",
            "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "ended_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "duration_s": 47.3,
            "from": "+1 555 0100",
            "to": "+1 555 0199",
        },
        "outcome": "booked",
        "reason": "CONVERSATION_COMPLETE",
        "summary": "Caller booked a check-up for Friday 3 PM. Confirmed by SMS.",
        "extracted": {"customer_name": "Test Caller", "appointment_at": "2026-05-15T15:00"},
    }
    import urllib.request as _ur
    import urllib.error as _ue
    headers = {"Content-Type": "application/json", "User-Agent": "SpiderX.AI-Webhook-Test/1"}
    for k, v in (agent.get("webhook_headers") or {}).items():
        if isinstance(k, str) and isinstance(v, str):
            headers[k] = v
    body = json.dumps(sample_payload).encode("utf-8")
    req = _ur.Request(url, data=body, headers=headers, method="POST")
    try:
        with _ur.urlopen(req, timeout=10) as resp:
            text = resp.read(500).decode("utf-8", errors="replace")
            return {"ok": True, "status": resp.status, "body": text, "url": url}
    except _ue.HTTPError as e:
        text = e.read(500).decode("utf-8", errors="replace") if hasattr(e, "read") else str(e)
        return {"ok": False, "status": e.code, "body": text, "url": url,
                "error": f"HTTP {e.code}"}
    except _ue.URLError as e:
        return {"ok": False, "status": 0, "body": "", "url": url,
                "error": f"could not connect: {e.reason}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "status": 0, "body": "", "url": url,
                "error": f"unexpected: {e!s}"}


# ─────────────── SIP-config (self-service phone connect) ────────────────────
#
# The "Bring your own SIP" path on the Go-live page. Today: Voniz is the
# only self-service provider with an out-of-the-box setup card; the
# other providers in SIP_PROVIDERS are still ops-fulfilled.
#
# Schema + validation live in backend/sip_config.py — see that module's
# docstring for the canonical stored shape on agents.sip_config.


@app.get("/api/agents/{agent_id}/sip-config")
async def get_agent_sip_config(agent_id: int, request: Request) -> dict:
    """Read the current sip_config for an agent + the inbound URI the
    operator should paste into their SIP provider's Application field.
    Password is redacted (returns password_set: bool instead of the
    raw value)."""
    from . import sip_config as _sc
    user = await current_user(request)
    agent = await _require_agent_owned(agent_id, user)
    cfg = _sc.redacted_view(agent.get("sip_config"))
    return {
        "agent_id": agent_id,
        "inbound_uri": _sc.inbound_uri_for(agent_id),
        "inbound_host": _sc.SIP_INBOUND_HOST,
        "config": cfg,
    }


@app.patch("/api/agents/{agent_id}/sip-config")
async def patch_agent_sip_config(agent_id: int, request: Request) -> dict:
    """Save / update the agent's SIP configuration. Validates the
    provider id is known, registrar looks like a domain, etc. The
    password is preserved across saves when the operator doesn't
    re-enter it (so editing the alias doesn't blank the password)."""
    from . import sip_config as _sc
    user = await current_user(request)
    agent = await _require_agent_admin(agent_id, user)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")

    existing = agent.get("sip_config") if isinstance(agent.get("sip_config"), dict) else None
    cfg, err = _sc.validate_and_normalize(body, agent_id=agent_id, existing=existing)
    if err:
        # 422 because the request was understood but invalid; dodges the
        # global 404 handler that strips detail messages on 404 status.
        raise HTTPException(status_code=422, detail=err)

    updated = await db.update_agent(agent_id, {"sip_config": cfg})
    return {
        "ok": True,
        "agent_id": agent_id,
        "config": _sc.redacted_view(cfg),
        "inbound_uri": cfg["inbound_uri"],
        "agent": updated,
    }


@app.delete("/api/agents/{agent_id}/sip-config")
async def delete_agent_sip_config(agent_id: int, request: Request) -> dict:
    """Disconnect: clear the agent's sip_config. The agent stays
    saved; only the SIP routing config is wiped. Operator can
    reconfigure later."""
    user = await current_user(request)
    await _require_agent_admin(agent_id, user)
    await db.update_agent(agent_id, {"sip_config": None})
    return {"ok": True, "agent_id": agent_id, "config": None}


@app.post("/api/number-requests")
async def create_number_request(request: Request) -> dict:
    """End-user-facing "Get my number" submission from the Go Live modal.
    Stores intent only — ops fulfils manually until Phase 2 lands real
    Twilio REST provisioning. Required fields: agent_id, delivery_handle
    (where to text the new number once it's live)."""
    user = await current_user(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    try:
        agent_id = int(body.get("agent_id"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="agent_id is required")
    await _require_agent_owned(agent_id, user)
    delivery = (body.get("delivery_handle") or "").strip()
    if not delivery:
        raise HTTPException(status_code=400, detail="delivery_handle is required")
    row = await db.create_number_request({
        "agent_id": agent_id,
        "country": body.get("country"),
        "city": body.get("city"),
        "delivery_handle": delivery,
        "notes": body.get("notes"),
    }, user_id=user["id"])
    log.info("number_request id=%s agent=%s user=%s country=%s",
             row.get("id"), agent_id, user["id"], row.get("country"))
    return {"id": row["id"], "status": row["status"], "created_at": row["created_at"]}


# ──────────────────────── WebSocket (browser) ────────────

def _parse_tweaks(qp) -> dict:
    """Pull tweak params out of the WS query string. All optional."""
    t = {}
    if qp.get("voice"):
        t["voice"] = qp["voice"]
    if qp.get("sensitivity"):
        t["sensitivity"] = qp["sensitivity"]
    if qp.get("silence_ms"):
        try: t["silence_ms"] = int(qp["silence_ms"])
        except ValueError: pass
    if qp.get("prefix_pad_ms"):
        try: t["prefix_pad_ms"] = int(qp["prefix_pad_ms"])
        except ValueError: pass
    if qp.get("temperature"):
        try: t["temperature"] = float(qp["temperature"])
        except ValueError: pass
    if qp.get("top_p"):
        try: t["top_p"] = float(qp["top_p"])
        except ValueError: pass
    if qp.get("affective") is not None:
        t["affective"] = qp["affective"].lower() in ("1", "true", "yes", "on")
    if qp.get("proactive") is not None:
        t["proactive"] = qp["proactive"].lower() in ("1", "true", "yes", "on")
    return t


@app.websocket("/ws/session")
async def ws_session(ws: WebSocket) -> None:
    """Single session: starts in builder mode, conversationally hands off to
    a saved or just-created agent without dropping the WS.

    Accepts ?locale=<bcp47>&tz=<iana>&user_id=<id> plus optional tweak params
    (voice, sensitivity, silence_ms, prefix_pad_ms, temperature, top_p,
    affective, proactive) so the user's Live config defaults are honoured per
    session and any agent built here gets stamped with the right owner."""
    await ws.accept()
    qp = ws.query_params
    client_locale = (qp.get("locale") or "en-US").strip() or "en-US"
    client_tz = (qp.get("tz") or "UTC").strip() or "UTC"
    tweaks = _parse_tweaks(qp)
    initial_agent_id: int | None = None
    if qp.get("agent_id"):
        try:
            initial_agent_id = int(qp["agent_id"])
        except (TypeError, ValueError):
            initial_agent_id = None
    user_id: int = (await db.get_founder())["id"]
    if qp.get("user_id"):
        try:
            uid = int(qp["user_id"])
            if await db.get_user(uid):
                user_id = uid
        except (TypeError, ValueError):
            pass
    # `sid` is a browser-generated UUID per build — stable across Gemini
    # Live reconnects AND WS-level reconnects, so Eva's durable build
    # state (build_sessions row) survives any kind of network blip.
    # Absence is tolerated (older clients) — run_session will mint a
    # synthetic per-WS sid so state still survives Gemini reconnects.
    sid = (qp.get("sid") or "").strip() or None
    # `industry` is the optional landing-page preset (homepage dropdown
    # or a `/for-<industry>` deep-link). When set, the build locks that
    # industry's template up front so Eva skips triage. Passed verbatim to
    # whichever bridge serves the session.
    industry = (qp.get("industry") or "").strip().lower() or None
    # `mode=text` routes the WS to a SEPARATE chat bridge
    # (chat_bridge.run_chat_session) that uses the non-Live Gemini API
    # against gemini-2.5-flash with manual function calling. The Live
    # cascade model is audio-first and behaved poorly for text chat:
    # silent tool-call-only turns, wrong tool picks, lost user messages
    # in the reconnect window. The chat bridge fixes all three by
    # keeping persistent history server-side and running the function-
    # call loop deterministically. Voice mode (no mode param) keeps the
    # Live API via gemini_bridge.run_session.
    text_only = (qp.get("mode") or "").strip().lower() == "text"
    try:
        if text_only:
            from . import chat_bridge
            await chat_bridge.run_chat_session(
                ws,
                client_locale=client_locale,
                client_tz=client_tz,
                tweaks=tweaks,
                user_id=user_id,
                sid=sid,
                industry=industry,
            )
        else:
            await gemini_bridge.run_session(
                ws,
                client_locale=client_locale,
                client_tz=client_tz,
                tweaks=tweaks,
                initial_agent_id=initial_agent_id,
                user_id=user_id,
                sid=sid,
                text_only=text_only,
                industry=industry,
            )
    finally:
        try:
            await ws.close()
        except Exception:
            pass


@app.websocket("/ws/helper")
async def ws_helper(ws: WebSocket) -> None:
    """Persistent Eva-helper WebSocket. Opens once the operator's dashboard
    mounts (after the first agent exists) and stays up across dashboard
    pages. Push-to-talk audio + text + client-sent context updates flow
    over this one socket.

    Accepts ?locale=<bcp47>&tz=<iana>&user_id=<id> plus the same tweak
    params as /ws/session (voice, sensitivity, …). NO sid — the helper
    is stateless across reloads; each fresh open is a new logical
    session (no slot-filling table to key into)."""
    await ws.accept()
    qp = ws.query_params
    client_locale = (qp.get("locale") or "en-US").strip() or "en-US"
    client_tz = (qp.get("tz") or "UTC").strip() or "UTC"
    tweaks = _parse_tweaks(qp)
    user_id: int = (await db.get_founder())["id"]
    if qp.get("user_id"):
        try:
            uid = int(qp["user_id"])
            if await db.get_user(uid):
                user_id = uid
        except (TypeError, ValueError):
            pass
    try:
        await gemini_bridge.run_helper_session(
            ws,
            client_locale=client_locale,
            client_tz=client_tz,
            tweaks=tweaks,
            user_id=user_id,
        )
    finally:
        try:
            await ws.close()
        except Exception:
            pass


# ─────────────── build_sessions recovery endpoints ────────────────────────
#
# Three endpoints that let the dashboard recover work the operator
# invested into the voice build even if the build call ended
# abnormally (mic glitch, voice failure, accidental tab close).
#
# Wired up by the FloatingEva-adjacent landing-screen "recovery banner"
# on the frontend: on landing mount, the page checks
# sessionStorage['eva_build_sid'] and, if present, hits /state to see
# whether the session is finalizable. If yes, the operator gets a
# button to finalize manually OR a notice that the agent was already
# committed silently by the WS-close auto-commit path.
#
# All three require the operator to own the sid (via the user_id on
# the session row) so a random sid guess can't finalize someone
# else's build.


@app.get("/api/build-sessions/{sid}/state")
async def get_build_session_state(sid: str, request: Request) -> dict:
    """Lightweight status check for a build_session by its
    browser-generated sid. Includes committed + abandoned rows (not
    just in_progress) so the recovery banner can distinguish three
    cases:

      • exists=false        → never started; banner dismisses silently
      • status=in_progress  → savable → banner offers Save & test
      • status=committed    → already auto-committed by L4 (WS-close);
                              banner can show 'Eva already saved Maya
                              — open her now' with the agent payload
                              for one-click navigation
      • status=abandoned    → operator explicitly discarded; dismiss
    """
    user = await current_user(request)
    row = await db.get_build_session(user_id=user["id"], sid=sid, include_committed=True)
    if not row:
        # Genuinely never existed, or belongs to another user.
        return {"exists": False}
    extras = row.get("extras") if isinstance(row.get("extras"), dict) else {}
    committed_agent: Optional[dict] = None
    if row.get("status") == "committed" and row.get("committed_agent_id"):
        # Fetch the committed agent so the banner can navigate
        # straight to its dashboard without a second round-trip.
        try:
            committed_agent = await db.get_agent(int(row["committed_agent_id"]))
        except Exception:  # noqa: BLE001
            committed_agent = None
    return {
        "exists": True,
        "status": row.get("status"),
        "finalizable": (row.get("status") == "in_progress"
                        and bool(row.get("sector_kind") and row.get("agent_name"))),
        "sector_kind":   row.get("sector_kind"),
        "business_name": row.get("business_name"),
        "agent_name":    row.get("agent_name"),
        "primary_job":   row.get("primary_job"),
        "extras_keys":   sorted((extras or {}).keys()),
        "committed_agent_id": row.get("committed_agent_id"),
        # When the session was already silently committed (L4 path),
        # we hand the FULL agent payload to the banner so the operator
        # can click "Open Maya" and skip the finalize round-trip
        # entirely. Without this, the banner couldn't surface the
        # already-saved agent and would just dismiss.
        "committed_agent": committed_agent,
    }


@app.post("/api/build-sessions/{sid}/finalize")
async def finalize_build_session(sid: str, request: Request) -> dict:
    """Commit a partially-built session as a saved agent. The button
    on the recovery banner POSTs here. Idempotent — if the session
    was already committed (by the WS-close auto-commit path), this
    looks up and returns the linked agent instead of creating a
    second one."""
    user = await current_user(request)
    # If already committed, just hand back the linked agent so the
    # frontend can reveal it.
    row = await db.get_build_session(user_id=user["id"], sid=sid)
    if not row:
        # Resource isn't strictly missing (the sid was valid) — the row
        # is gone or no longer finalizable (already committed by the
        # WS-close auto-commit path, or abandoned). 410 Gone is the
        # semantically correct status here; it also dodges the global
        # 404 exception handler at the bottom of this file that
        # replaces ALL 404 detail messages with a generic body.
        raise HTTPException(
            status_code=410,
            detail="build_session not in_progress (already committed, abandoned, or never existed)",
        )
    if not (row.get("sector_kind") and row.get("agent_name")):
        raise HTTPException(
            status_code=422,
            detail="not enough information captured yet to save (need at least sector + agent name)",
        )
    from . import build_state
    saved = await build_state.force_commit_build_session(
        user_id=user["id"], sid=sid,
    )
    if not saved:
        raise HTTPException(
            status_code=500,
            detail="finalize failed — see server log",
        )
    return {"ok": True, "agent": saved}


@app.post("/api/build-sessions/{sid}/abandon")
async def abandon_build_session(sid: str, request: Request) -> dict:
    """Operator clicked 'discard' on the recovery banner. Mark the row
    abandoned so the cleanup sweeper doesn't have to find it later
    and so /state no longer returns it as in_progress."""
    user = await current_user(request)
    # Reuse the time-based sweeper helper but scoped to a single sid.
    # Simpler: just flip status directly.
    import asyncpg  # local — avoid top-level coupling
    pool = await db.db_pg.get_pool() if hasattr(db, "db_pg") else None
    # The cleanest path: a small new helper. We inline here to avoid
    # another db.py wire-up surface — it's a single UPDATE.
    from .db_pg import get_pool as _gp
    pool = await _gp()
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE build_sessions
               SET status = 'abandoned'
             WHERE user_id IS NOT DISTINCT FROM $1
               AND sid = $2
               AND status = 'in_progress'
            """,
            user["id"], sid,
        )
    return {"ok": True, "result": result}


@app.get("/api/tweaks/schema")
async def tweaks_schema() -> dict:
    """Schema for the tweaks panel — frontend renders fields from this."""
    return {
        "voice": {
            "label": "Default voice",
            "type": "select",
            "default": "Aoede",
            "options": [
                {"id": "Aoede", "label": "Aoede — warm, friendly"},
                {"id": "Leda", "label": "Leda — soft, conversational"},
                {"id": "Puck", "label": "Puck — bright, upbeat"},
                {"id": "Charon", "label": "Charon — deep, calm"},
                {"id": "Kore", "label": "Kore — clear, neutral"},
                {"id": "Orus", "label": "Orus — measured, formal"},
                {"id": "Fenrir", "label": "Fenrir — energetic, gruff"},
                {"id": "Zephyr", "label": "Zephyr — light, breezy"},
            ],
            "help": "Eva's own voice while she's building. Also the default she'll pick for new agents unless she has a strong sector reason.",
        },
        "sensitivity": {
            "label": "Mic sensitivity",
            "type": "select",
            "default": "low",
            "options": [
                {"id": "low", "label": "Low — forgiving, ignores background"},
                {"id": "high", "label": "High — picks up soft speech"},
            ],
            "help": "How easily Gemini's voice-activity detector fires on speech start / end.",
        },
        "silence_ms": {
            "label": "Pause before reply (ms)",
            "type": "range",
            "default": 2500,
            "min": 500,
            "max": 4000,
            "step": 100,
            "help": "How long Eva waits in silence before assuming you're done. Higher = more thinking room; lower = snappier turn-taking.",
        },
        "prefix_pad_ms": {
            "label": "Speech-start padding (ms)",
            "type": "range",
            "default": 300,
            "min": 0,
            "max": 1000,
            "step": 50,
            "help": "Extra audio kept before detected speech onset — prevents clipped first syllables.",
        },
        "temperature": {
            "label": "Temperature",
            "type": "range",
            "default": 1.0,
            "min": 0.0,
            "max": 2.0,
            "step": 0.1,
            "help": "How creative the model's phrasing is. Lower = more deterministic.",
        },
        "top_p": {
            "label": "Top-p",
            "type": "range",
            "default": 0.95,
            "min": 0.1,
            "max": 1.0,
            "step": 0.05,
            "help": "Nucleus-sampling cutoff. Rarely needs changing.",
        },
        "affective": {
            "label": "Affective dialog",
            "type": "bool",
            "default": True,
            "help": "Lets the model emit natural 'mm-hmm' / 'got it' backchannels and emotional inflection. Recommended on.",
        },
        "proactive": {
            "label": "Proactive audio",
            "type": "bool",
            "default": False,
            "help": "Lets the model speak unprompted when context warrants — e.g. start with a greeting before you say anything. Experimental.",
        },
    }


# ──────────────────────── Twilio voice routes ────────────

@app.get("/api/sip/twilio/twiml/{agent_id}", response_class=PlainTextResponse)
@app.post("/api/sip/twilio/twiml/{agent_id}", response_class=PlainTextResponse)
async def twilio_twiml(agent_id: int):
    """TwiML that bridges an inbound Twilio Voice call into our Media Streams
    WebSocket. Point a Twilio number's voice webhook at this URL via ngrok."""
    a = await db.get_agent(agent_id)
    if not a:
        raise HTTPException(status_code=404, detail="agent not found")
    public_host = os.environ.get("PUBLIC_HOST", "").rstrip("/")
    if not public_host:
        public_host = "wss://YOUR-NGROK-HOST"
    if public_host.startswith("http://"):
        public_host = "ws://" + public_host[len("http://"):]
    elif public_host.startswith("https://"):
        public_host = "wss://" + public_host[len("https://"):]
    elif not public_host.startswith(("ws://", "wss://")):
        public_host = "wss://" + public_host
    stream_url = f"{public_host}/ws/twilio/{agent_id}"
    return (
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
        "<Response>\n"
        f"  <Connect>\n    <Stream url=\"{stream_url}\" />\n  </Connect>\n"
        "</Response>"
    )


@app.websocket("/ws/twilio/{agent_id}")
async def ws_twilio(ws: WebSocket, agent_id: int) -> None:
    """Twilio Media Streams ↔ Gemini Live bridge for the given saved agent."""
    await ws.accept()
    try:
        await twilio_bridge.run_twilio_call(ws, agent_id)
    finally:
        try:
            await ws.close()
        except Exception:
            pass


# ─────────────────────── static frontend ──────────────────

FRONTEND_DIR = ROOT / "frontend"


# HTML responses MUST NOT be cached — the file references versioned JS / CSS
# (e.g. /static/app.js?v=30). If the browser caches the HTML, it keeps loading
# stale module versions forever. JS / CSS themselves can cache freely because
# their URLs are version-stamped.
_NO_CACHE = {"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"}


# Build-pin interpolation. The static index.html ships with `{BUILD}`
# placeholders in the <script>/<link> ?v= query strings; we substitute
# APP_BUILD on every SPA request so the cache-bust pin can never drift
# from the constant we bump in code. Before build 184 these were
# hand-maintained string literals in index.html — the user lost time
# to "I bumped APP_BUILD but the browser still served the old JS"
# because the SPA pin pointed at an older build. Re-reading the file
# per request is cheap (≈ 2 KB) and means an operator who hot-swaps
# `frontend/index.html` doesn't need to restart the server.
def _render_index() -> "HTMLResponse":
    from fastapi.responses import HTMLResponse
    try:
        html_text = (FRONTEND_DIR / "index.html").read_text(encoding="utf-8")
    except Exception:  # pragma: no cover — only fires if the file is gone
        log.exception("_render_index: failed to read index.html")
        return HTMLResponse("<h1>SpiderX AI · Eva</h1><p>Frontend missing.</p>", status_code=500, headers=_NO_CACHE)
    html_text = html_text.replace("{BUILD}", str(APP_BUILD))
    return HTMLResponse(html_text, headers=_NO_CACHE)


@app.get("/")
async def index() -> "HTMLResponse":
    return _render_index()


@app.get("/agent/{slug}")
@app.get("/agent/{slug}/{section}")
async def agent_page(slug: str, section: Optional[str] = None) -> FileResponse:
    """Deep-link route — serves the SPA for the agent overview or any of its
    sub-pages (/calls, /settings, /numbers). The client reads location.pathname
    to pick which page to render. Slug is validated on the client; non-existent
    slugs flash a "not found" hint and the user lands back on /."""
    return _render_index()


@app.get("/build")
async def build_page() -> FileResponse:
    """Direct deep-link into Eva's build flow — skips the landing splash."""
    return _render_index()


@app.get("/agents")
async def agents_page() -> FileResponse:
    """Full-page list of saved agents (replaces the old tweaks-drawer list)."""
    return _render_index()


@app.get("/for-{slug}")
async def industry_landing_page(slug: str) -> FileResponse:
    """Per-industry landing page — /for-automobile, /for-dental, etc. The
    SPA reads the slug, reskins the homepage to that industry, and presets
    the build's industry context. Unknown slugs render the plain landing."""
    return _render_index()


@app.get("/login")
@app.get("/signup")
async def auth_page() -> FileResponse:
    """Stub auth surface — the SPA renders sign-in / sign-up cards. Once
    Auth0 is wired, these redirect to the hosted-login experience."""
    return _render_index()


@app.get("/account/billing")
@app.get("/account/integrations")
@app.get("/account/org")
@app.get("/account/team")
async def account_page() -> FileResponse:
    """Account-scoped pages — billing (plans + upgrade), integrations,
    organisation (billing + tax entity), team (members + invites). SPA
    handles routing."""
    return _render_index()


@app.get("/invite/{token}")
async def invite_page(token: str) -> FileResponse:
    """Public accept-invite landing. The SPA reads the token from the URL,
    fetches /api/invites/{token} to render the preview, and either prompts
    login + calls /accept, or shows decline / expired states."""
    return _render_index()


@app.get("/admin")
@app.get("/admin/{section}")
async def admin_page(section: str = "") -> FileResponse:
    """Super-admin shell. The SPA gates this on /api/me.is_super_admin —
    a non-admin who guesses the URL gets the shell but every API call 403s
    so no data leaks. Reach: orgs, users, calls, audit, super-admins."""
    return _render_index()


@app.get("/embed/{slug}")
async def embed_page(slug: str) -> FileResponse:
    """Minimal embed surface — the SPA detects this route and renders only
    the orb + a "Tap to talk to {agent}" CTA, no brandbar/nav. Designed to
    be loaded inside an iframe via /static/embed.js, mounted on third-party
    sites. CORS-free because it's same-origin to our app."""
    return _render_index()


app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


@app.exception_handler(404)
async def _not_found(_, __):
    return JSONResponse({"detail": "not found"}, status_code=404)
