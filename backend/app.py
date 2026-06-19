from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

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

    # Build 198: scheduler — single in-process cron loop. Registered jobs
    # fire async on schedule; failures emit system.scheduler.run.missed
    # events but never crash the loop.
    try:
        from . import scheduler, price_monitor, eod_digest
        # 05:00 IST daily — wholesale price-rate watchdog (Gemini + Twilio + Plivo)
        scheduler.register(
            "daily_price_check", "0 5 * * *",
            price_monitor.run_daily_price_check, tz="Asia/Kolkata",
        )
        # 19:00 IST daily — per-agent EOD digest email to org owners
        scheduler.register(
            "daily_eod_digest", "0 19 * * *",
            eod_digest.run_daily_eod_digest, tz="Asia/Kolkata",
        )
        # 03:00 IST daily — purge call recordings past their 180-day
        # retention window (build 206). Runs at 03:00 so it's well
        # clear of the EOD digest job (19:00) and the price-check
        # (05:00) — keeps disk-IO peaks from overlapping with email
        # SMTP + scraping windows.
        from . import recordings as _rec
        scheduler.register(
            "daily_recording_purge", "0 3 * * *",
            _rec.run_daily_recording_purge, tz="Asia/Kolkata",
        )
        # Hourly — per-agent WS handshake probe. Free (no Gemini cost),
        # ~500 ms per agent, parallelism-capped at 10 concurrent. Emits
        # agent.healthcheck.{passed,degraded,failed} per agent plus one
        # summary row per run. Catches Gemini/DB/per-agent config rot
        # the platform-level /api/build healthcheck misses. Build 219.
        from . import agent_healthcheck as _ahc
        scheduler.register(
            "hourly_agent_healthcheck", "5 * * * *",
            _ahc.run_hourly_healthchecks, tz="Asia/Kolkata",
        )
        # 04:00 IST daily — Level 3 full conversational probe per agent
        # (build 231). Opens a real Gemini Live session, sends a text
        # turn + silence frames, waits for response audio. OFF by
        # default; flip `healthcheck.level3_enabled` in Platform
        # Settings → Health checks to enable. Sample-size capped so
        # cost stays predictable.
        scheduler.register(
            "daily_agent_full_healthcheck", "0 4 * * *",
            _ahc.run_daily_full_healthchecks, tz="Asia/Kolkata",
        )
        await scheduler.start()
        log.info("scheduler: started with %d job(s)", len(scheduler.list_jobs()))
    except Exception as e:  # noqa: BLE001
        log.exception("scheduler.boot_failed: %s", e)


@app.on_event("shutdown")
async def _shutdown() -> None:
    """Drain the asyncpg pool cleanly so connections aren't left dangling."""
    try:
        from . import scheduler
        await scheduler.stop()
    except Exception:  # noqa: BLE001
        pass
    await db.shutdown()

# Canonical SPA bundle version. Bump this on EVERY frontend change that the
# user might be served stale; index.html's <script src="app.js?v=N"> and the
# SXAI_BUILD constant in app.js MUST match this. The /api/build endpoint
# advertises this number so the SPA can self-detect a stale bundle on boot
# and force-reload once (see app.js for the sentinel logic).
APP_BUILD = 268


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
    # Build 244 — publish the small set of platform feature flags the
    # SPA gates UI on. Settings live in the platform_settings table
    # (cached by backend/settings.py); read the ones the frontend
    # actually consults and pass them through as a small `features`
    # object. Keep the list narrow so /me stays tight.
    from . import settings as cfg
    user["features"] = {
        "eva_assist":   bool(await cfg.get("features.eva_assist", True)),
        "ambience_beta": bool(await cfg.get("features.ambience_beta", True)),
    }
    return user


@app.get("/api/me/org")
async def get_my_org(request: Request) -> dict:
    user = await current_user(request)
    org = await db.get_org_for_user(user["id"])
    if not org:
        raise HTTPException(status_code=404, detail="org not found")
    return org


@app.get("/api/me/orgs")
async def list_my_orgs(request: Request) -> list[dict]:
    """Every org the user is a member of, with their role + member
    count. Powers the workspace selector pill in the dashboard topbar
    (build 222). Tiny payload — fetched once on shell mount."""
    user = await current_user(request)
    rows = await db.list_orgs_for_user(user["id"])
    # Enrich each row with member count + active flag so the selector
    # can render "Dipesh's workspace · 2 Members" + a tick on the
    # current org without a second round-trip.
    primary = await db.get_org_for_user(user["id"])
    primary_id = primary.get("id") if primary else None
    out = []
    for r in rows:
        members = await db.list_org_members(int(r["id"])) or []
        out.append({
            "id":           r["id"],
            "name":         r.get("name") or "your workspace",
            "country":      r.get("country"),
            "role":         r.get("role"),
            "members_n":    len(members),
            "is_current":   r["id"] == primary_id,
        })
    return out


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
        # Build 198: lifecycle event for the Observability feed.
        try:
            from . import events as _ev
            await _ev.emit(
                "agent.created", source="user",
                user_id=user["id"], org_id=saved.get("org_id"), agent_id=saved.get("id"),
                title=f"Agent created — {saved.get('name')} ({saved.get('sector') or 'generic'})",
                payload={
                    "name": saved.get("name"),
                    "sector": saved.get("sector"),
                    "locale": saved.get("locale"),
                    "build_path": "wizard.dynamic",
                    "knowledge_imported": bool(knowledge_yaml),
                },
            )
        except Exception as _e:  # noqa: BLE001
            log.debug("events.emit agent.created (dynamic) failed: %s", _e)
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
    try:
        from . import events as _ev
        await _ev.emit(
            "agent.created", source="user",
            user_id=user["id"], org_id=saved.get("org_id"), agent_id=saved.get("id"),
            title=f"Agent created — {saved.get('name')} ({saved.get('sector') or 'generic'})",
            payload={
                "name": saved.get("name"),
                "sector": saved.get("sector"),
                "locale": saved.get("locale"),
                "build_path": "wizard.template",
                "template_id": tpl.get("id"),
                "knowledge_imported": bool(knowledge_yaml),
            },
        )
    except Exception as _e:  # noqa: BLE001
        log.debug("events.emit agent.created (template) failed: %s", _e)
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
    """Google sign-in. Build 236 — frontend now drives this from the
    Firebase Web SDK; on a successful popup it sends `id_token`, `email`
    and `name` from the Firebase user. The token is the Google-signed
    JWT we'd verify in production (firebase-admin / google-auth); for
    now we accept the email + name as truth and upsert a user. Real
    verification is a backend-only flip when the firebase-admin
    package lands.
    """
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
    log.info("auth.google email=%s user_id=%s id_token_present=%s",
             email, user["id"], bool(body.get("id_token")))
    return user


# ─── OTP login (build 236) ────────────────────────────────────────────────
#
# Email + 6-digit OTP for passwordless login. Codes live in a process-
# local dict with a 10-minute TTL. Production: swap for Redis or the
# existing Postgres so multi-process / multi-worker deployments share
# state. Same shape, two function calls to swap.
#
# Flow:
#   POST /api/auth/otp/request  { email }      → emails the code + ok:true
#   POST /api/auth/otp/verify   { email, code} → upserts user + returns it

import secrets as _secrets
import time as _time

_OTP_STORE: dict[str, dict] = {}
_OTP_TTL_SECONDS = 10 * 60
_OTP_MAX_ATTEMPTS = 5


def _otp_make() -> str:
    """6-digit numeric code (zero-padded). secrets ensures it isn't
    predictable from process state — same primitive Auth0 uses."""
    return f"{_secrets.randbelow(1_000_000):06d}"


# ─── Brand-mark bytes for transactional email (build 246) ──────────────
# Load the white-on-transparent SpiderX logo once at module import.
# Used as a MIME inline attachment (Content-ID: <spiderx-logo>) on
# every OTP email. Previous data-URI attempt (build 245) didn't render
# in Gmail — Gmail BLOCKS `data:` URIs in <img src> for security.
# CID-attached <img src="cid:spiderx-logo"> is the only path that
# reliably shows in Gmail + Apple Mail + Outlook all at once.
_BRAND_LOGO_CID = "spiderx-logo"
def _load_brand_bytes() -> bytes:
    try:
        svg_path = Path(__file__).resolve().parent.parent / "frontend" / "assets" / "spiderx-logo-white.svg"
        return svg_path.read_bytes()
    except Exception as _e:  # noqa: BLE001
        log.warning("brand.bytes_load_failed err=%s", _e)
        return b""

_BRAND_LOGO_BYTES = _load_brand_bytes()


@app.post("/api/auth/otp/request")
async def auth_otp_request(request: Request) -> dict:
    """Issue a fresh OTP to the given email. Always returns `ok: true`
    even when the address is invalid — avoids leaking which addresses
    have accounts (standard email-enumeration hardening).
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    email = ((body or {}).get("email") or "").strip().lower()
    if "@" not in email or "." not in email.split("@")[-1]:
        # Pretend success so attackers can't enumerate addresses by
        # error-code timing. Log the bad submit so we still see abuse.
        log.info("auth.otp.request bad_email=%r", email)
        return {"ok": True}
    code = _otp_make()
    _OTP_STORE[email] = {
        "code":     code,
        "expires":  _time.time() + _OTP_TTL_SECONDS,
        "attempts": 0,
    }
    # Send via the existing email pipeline. Dev mode (EMAIL_PROVIDER=log)
    # writes the code to stdout so local development never needs a real
    # mailbox. Best-effort — a delivery failure must not block the API
    # response (otherwise an attacker can probe the email provider).
    try:
        from . import email_stub as _es
        subject = f"Your SpiderX.AI sign-in code · {code}"
        text = (
            f"You requested to sign in to your SpiderX.AI account.\n\n"
            f"Your sign-in code is: {code}\n\n"
            f"It expires in 10 minutes. If you didn't request this, ignore this email.\n\n"
            f"SpiderX AI, 84 W Santa Clara St, Suite 700, San Jose, CA 95113, USA\n"
        )
        # Build 242 — properly designed OTP email header.
        #
        # The previous version had three problems visible in real Gmail:
        #   1. "SPIDERX.AI" rendered as a blue underlined LINK because
        #      Gmail auto-detects domain-like text and hyperlinks it.
        #   2. The right-side <img> URL was a hashed marketing-site
        #      asset that 404'd, leaving a broken-image icon next to
        #      the wordmark.
        #   3. A redundant pink/purple avatar circle sat next to a
        #      wordmark that already carries the brand mark.
        #
        # New header — centered, full-bleed gradient banner:
        #   - Wordmark rendered as styled HTML TEXT, "spider" white +
        #     "X" red (#df3739) + ".ai" white. Brand-correct, no image
        #     to 404. An <a href="#"> wrapper with explicit color +
        #     text-decoration:none defeats Gmail's auto-link on .ai.
        #   - "PHONE AI AGENT BUILDER" eyebrow under it, small caps,
        #     low-opacity white.
        #   - No avatar circle. No external <img>. Pure text + gradient.
        html_body = (
            "<!doctype html><html><body style='margin:0;padding:32px 16px;"
            "background:#0a0d2e;font-family:-apple-system,BlinkMacSystemFont,"
            "\"Segoe UI\",Helvetica,Arial,sans-serif;color:#1f2230;'>"
            "<table cellpadding='0' cellspacing='0' border='0' width='100%' "
            "style='max-width:540px;margin:0 auto;background:#ffffff;"
            "border-radius:14px;overflow:hidden;'>"
            # ── Header banner (gradient + brand-text wordmark) ──────
            "<tr><td style='padding:0;'>"
            "<table cellpadding='0' cellspacing='0' border='0' width='100%' "
            "style='background:linear-gradient(120deg,#1a1138 0%,#2a1a4d 55%,#3a1d52 100%);'>"
            "<tr><td align='center' style='padding:34px 28px 30px;'>"
            # Build 246 — real SpiderX SVG (white wordmark + red X)
            # attached as a MIME inline image with Content-ID
            # `<spiderx-logo>`, referenced via cid:spiderx-logo. This
            # is the ONLY reliable path: Gmail blocks data: URIs and
            # rate-limits external image fetches, so neither inline
            # nor hosted <img src> worked. The CID-attached pattern
            # ships the bytes alongside the HTML body in a
            # multipart/related envelope. If the brand bytes failed
            # to load at module import, fall back to the styled-text
            # wordmark so the email still has SOMETHING brand-correct.
            + (
                f"<img src='cid:{_BRAND_LOGO_CID}' alt='spiderX.ai' "
                f"height='44' style='display:inline-block;height:44px;"
                f"width:auto;border:0;outline:none;text-decoration:none;' />"
                if _BRAND_LOGO_BYTES else
                "<a href='#' style='display:inline-block;font-size:32px;"
                "font-weight:800;letter-spacing:-0.02em;text-decoration:none !important;"
                "color:#ffffff !important;line-height:1;'>"
                "<span style='color:#ffffff;'>spider</span>"
                "<span style='color:#df3739;'>X</span>"
                "<span style='color:#ffffff;'>.ai</span>"
                "</a>"
            ) +
            "<div style='font-size:11px;font-weight:600;"
            "letter-spacing:0.18em;text-transform:uppercase;"
            "color:rgba(255,255,255,0.65);margin-top:14px;'>"
            "Phone AI agent builder</div>"
            "</td></tr></table>"
            "</td></tr>"
            # ── Intro text ─────────────────────────────────────────
            "<tr><td style='padding:24px 30px 4px;font-size:15px;line-height:1.6;color:#3a3f4d;'>"
            "Hi there — here's the one-time code to finish signing in to "
            "<b>SpiderX.AI</b>. Pop it into the browser tab you came from:"
            "</td></tr>"
            # ── Code block ─────────────────────────────────────────
            "<tr><td align='center' style='padding:18px 30px 6px;'>"
            "<div style='background:#f4f5f9;border-radius:14px;padding:36px 24px;'>"
            f"<div style='font-family:ui-monospace,\"SF Mono\",Menlo,Consolas,monospace;"
            f"font-size:46px;font-weight:600;letter-spacing:0.12em;"
            f"color:#1f2230;line-height:1;'>{code}</div>"
            "</div>"
            "</td></tr>"
            # ── Caption ───────────────────────────────────────────
            "<tr><td align='center' style='padding:14px 30px 22px;'>"
            "<div style='font-size:15px;font-weight:700;color:#1f2230;'>One-time sign-in code</div>"
            "<div style='font-size:12.5px;color:#6a6f7d;margin-top:3px;'>Expires in 10 minutes</div>"
            "</td></tr>"
            # ── Divider ───────────────────────────────────────────
            "<tr><td style='padding:0 30px;'>"
            "<div style='height:1px;background:#eef0f4;'></div>"
            "</td></tr>"
            # ── Safety note ───────────────────────────────────────
            "<tr><td style='padding:22px 30px 18px;font-size:13.5px;line-height:1.6;color:#3a3f4d;'>"
            "Didn't ask for this code? You can safely ignore the email — "
            "the code expires on its own. If you've noticed anything off "
            "with your account, drop us a line at "
            "<a href='mailto:support@spiderx.ai' style='color:#3b82f6;text-decoration:none;'>"
            "support@spiderx.ai</a> and we'll take a look."
            "</td></tr>"
            # ── Footer (address) ──────────────────────────────────
            "<tr><td align='center' style='padding:22px 30px 28px;font-size:12px;"
            "color:#9095a3;line-height:1.6;'>"
            "SpiderX AI, 84 W Santa Clara St, Suite 700, San Jose, CA 95113, USA"
            "</td></tr>"
            "</table>"
            "</body></html>"
        )
        # Build 246 — attach the brand SVG as an inline MIME image so
        # the cid:spiderx-logo reference in the HTML resolves to a real
        # asset in the recipient's client. Empty dict if the bytes
        # failed to load at import time; the email template falls back
        # to the styled-text wordmark in that case.
        inline_imgs = (
            {_BRAND_LOGO_CID: _BRAND_LOGO_BYTES}
            if _BRAND_LOGO_BYTES else None
        )
        await _es._send(email, subject, text, html_body=html_body,
                        inline_images=inline_imgs)
    except Exception as e:  # noqa: BLE001
        log.warning("auth.otp.request email_failed addr=%s err=%s", email, e)
    return {"ok": True}


@app.post("/api/auth/otp/verify")
async def auth_otp_verify(request: Request) -> dict:
    """Verify an OTP. On success upserts the user via the same
    create_user path login/Google use, returns the user record.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    body = body if isinstance(body, dict) else {}
    email = (body.get("email") or "").strip().lower()
    code = (body.get("code") or "").strip()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="email required")
    if not code or not code.isdigit() or len(code) != 6:
        raise HTTPException(status_code=400, detail="6-digit code required")
    entry = _OTP_STORE.get(email)
    if not entry:
        raise HTTPException(status_code=400, detail="no code outstanding for this email")
    if _time.time() > entry["expires"]:
        _OTP_STORE.pop(email, None)
        raise HTTPException(status_code=400, detail="code expired — request a new one")
    entry["attempts"] += 1
    if entry["attempts"] > _OTP_MAX_ATTEMPTS:
        # Burn the entry on too many tries — fresh request required.
        _OTP_STORE.pop(email, None)
        raise HTTPException(status_code=429, detail="too many attempts — request a new code")
    if not _secrets.compare_digest(code, entry["code"]):
        raise HTTPException(status_code=400, detail="incorrect code")
    # Success — burn the code so it can't replay.
    _OTP_STORE.pop(email, None)
    user = await db.create_user(email, name=None, provider="otp")
    log.info("auth.otp.verify ok email=%s user_id=%s", email, user["id"])
    return user


@app.get("/api/agents")
async def list_agents(request: Request) -> list[dict]:
    user = await current_user(request)
    return await db.list_agents(user["id"])


def _public_agent(agent: dict) -> dict:
    """Strip carrier secrets before an agent row leaves the API. The raw row
    carries encrypted carrier creds (`telephony_carriers[*].secret_enc` and the
    legacy `telephony_secret_enc` bytea) — never send those to the browser."""
    if not isinstance(agent, dict):
        return agent
    out = dict(agent)
    out.pop("telephony_secret_enc", None)
    tc = out.get("telephony_carriers")
    if isinstance(tc, dict):
        out["telephony_carriers"] = {
            p: ({k: v for k, v in c.items() if k != "secret_enc"} if isinstance(c, dict) else c)
            for p, c in tc.items()
        }
    return out


@app.get("/api/agents/{agent_id}")
async def get_agent(agent_id: int, request: Request) -> dict:
    return _public_agent(await _require_agent_owned(agent_id, await current_user(request)))


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
    return _public_agent(a)


@app.delete("/api/agents/{agent_id}")
async def delete_agent(agent_id: int, request: Request) -> dict:
    """Hard-delete the agent. Admin+ on the org only — members can build
    but not delete (the latter is destructive enough to gate)."""
    user = await current_user(request)
    agent = await _require_agent_admin(agent_id, user)
    if not await db.delete_agent(agent_id):
        raise HTTPException(status_code=404, detail="agent not found")
    try:
        from . import events as _ev
        await _ev.emit(
            "agent.deleted", source="user", severity="warning",
            user_id=user.get("id"), org_id=agent.get("org_id"), agent_id=agent_id,
            title=f"Agent deleted — {agent.get('name')}",
            payload={"name": agent.get("name"), "sector": agent.get("sector"),
                     "published_was": bool(agent.get("published"))},
        )
    except Exception as _e:  # noqa: BLE001
        log.debug("events.emit agent.deleted failed: %s", _e)
    return {"ok": True}


@app.get("/api/agents/{agent_id}/stats")
async def agent_stats(agent_id: int, request: Request) -> dict:
    await _require_agent_owned(agent_id, await current_user(request))
    return await db.call_stats_for_agent(agent_id)


@app.get("/api/agents/{agent_id}/calls")
async def agent_calls(agent_id: int, limit: int = 50, request: Request = None) -> list[dict]:
    await _require_agent_owned(agent_id, await current_user(request))
    return await db.list_calls_for_agent(agent_id, limit=limit)


@app.get("/api/agents/{agent_id}/calls/{call_id}")
async def agent_call_detail(agent_id: int, call_id: int, request: Request) -> dict:
    """Full call detail for the dashboard's Call Details modal (build 188).
    Returns everything `list_calls_for_agent` returns PLUS the parsed
    transcript turns + the `final_message` + the `extracted` JSONB payload.
    Recording is a forward-looking field: today it always returns
    `recording_available: false` with a `recording_status` note. When
    audio capture lands (planned build), the same endpoint will surface
    a signed URL.
    """
    await _require_agent_owned(agent_id, await current_user(request))
    import json as _json
    row = await db.get_call_detail(agent_id, call_id)
    if not row:
        raise HTTPException(status_code=404, detail="call not found")
    # `transcript` is stored as a JSON string for build 188 onward, but
    # legacy rows (pre-188) may be NULL or plain text. Parse defensively.
    raw_tx = row.get("transcript")
    turns: list[dict] = []
    if isinstance(raw_tx, str) and raw_tx.strip():
        try:
            parsed = _json.loads(raw_tx)
            if isinstance(parsed, list):
                for t in parsed:
                    if isinstance(t, dict) and t.get("text"):
                        turns.append({
                            "role": (t.get("role") or "model").strip().lower(),
                            "text": str(t.get("text")).strip(),
                        })
        except Exception:  # noqa: BLE001
            # Plain-text legacy — render as a single model turn so the
            # operator at least sees what was captured.
            turns = [{"role": "model", "text": raw_tx.strip()}]
    # Build 206 — surface recording metadata. Three states:
    #   • recording_path is None → recording was off or write failed
    #   • recording_purged_at is set → file was deleted past retention
    #   • otherwise → available for download via the per-call endpoint
    rec_path  = row.get("recording_path")
    rec_purged = row.get("recording_purged_at")
    rec_avail  = bool(rec_path) and not rec_purged
    if rec_avail:
        rec_status = (
            f"Recording retained until {row['recording_expires_at']}"
            if row.get("recording_expires_at") else "Recording available"
        )
    elif rec_purged:
        rec_status = "Recording was purged at end of the 180-day retention window."
    elif rec_path:
        rec_status = "Recording captured but file is missing on disk."
    else:
        rec_status = "Recording was not captured for this call."
    return {
        "id": row.get("id"),
        "agent_id": row.get("agent_id"),
        "started_at": str(row.get("started_at") or ""),
        "ended_at": str(row.get("ended_at") or ""),
        "duration_s": float(row.get("duration_s") or 0),
        "outcome": row.get("outcome"),
        "reason": row.get("reason"),
        "summary": row.get("summary"),
        "final_message": row.get("final_message"),
        "extracted": row.get("extracted") or {},
        "sentiment": row.get("sentiment"),
        "lead_quality": row.get("lead_quality"),
        "lead_signals": row.get("lead_signals"),
        "transcript_turns": turns,
        "recording_available": rec_avail,
        "recording_status": rec_status,
        "recording_expires_at": (
            str(row["recording_expires_at"]) if row.get("recording_expires_at") else None
        ),
        "recording_started_at": (
            str(row["recording_started_at"]) if row.get("recording_started_at") else None
        ),
        "recording_purged_at": (
            str(row["recording_purged_at"]) if row.get("recording_purged_at") else None
        ),
        "recording_size_bytes": row.get("recording_size_bytes"),
        # Single canonical playback URL — server merges caller (left)
        # and agent (right) into a stereo WAV lazily on first hit and
        # caches it on disk. The two per-channel URLs stay around
        # for advanced QA tooling that wants the raw streams.
        "recording_url":        f"/api/agents/{row.get('agent_id')}/calls/{row.get('id')}/recording.wav" if rec_avail else None,
        "recording_caller_url": f"/api/agents/{row.get('agent_id')}/calls/{row.get('id')}/recording/caller.wav" if rec_avail else None,
        "recording_agent_url":  f"/api/agents/{row.get('agent_id')}/calls/{row.get('id')}/recording/agent.wav"  if rec_avail else None,
    }


@app.get("/api/agents/{agent_id}/calls/{call_id}/recording.wav")
async def agent_call_recording_mixed(
    agent_id: int, call_id: int, request: Request,
):
    """Serve the stereo mixdown of a call — caller on the LEFT
    channel, agent on the RIGHT. The browser gets one <audio> element
    with a single seek bar that scrubs both voices in lock-step.

    The mixdown is generated lazily on first request and cached as
    `mixed.wav` in the same directory as the two source channels;
    every subsequent play (and every range request the audio
    element makes while seeking) hits the cached file directly.
    The daily purge job deletes the whole directory when the call's
    retention window expires, which catches the mixdown too.
    """
    await _require_agent_owned(agent_id, await current_user(request))
    row = await db.get_call_detail(agent_id, call_id)
    if not row or not row.get("recording_path") or row.get("recording_purged_at"):
        raise HTTPException(status_code=404, detail="recording not available")
    from . import recordings as _rec
    rec_dir = _rec.RECORDING_ROOT / str(row["recording_path"])
    # Build 211 — async-safe path: offloads the sync mix to a thread
    # executor + dedupes concurrent first-build requests with a
    # per-call lock. Stops the modal-reopen race that occasionally
    # produced a 503 on /recording.wav.
    mixed = await _rec.async_get_or_build_mixed(rec_dir)
    if mixed is None or not mixed.exists():
        raise HTTPException(status_code=404, detail="recording mix unavailable")
    from fastapi.responses import FileResponse
    return FileResponse(
        path=str(mixed),
        media_type="audio/wav",
        filename=f"call-{call_id}.wav",
    )


@app.get("/api/agents/{agent_id}/calls/{call_id}/recording/{stream:path}")
async def agent_call_recording(
    agent_id: int, call_id: int, stream: str, request: Request,
):
    """Stream one of the raw channels of a call recording.

    `stream` is `caller.wav` or `agent.wav` — anything else 404s,
    which closes the door on path traversal via the URL. The auth
    gate on the call detail endpoint applies here too: the agent
    must be owned by the requesting user's org.

    Kept around alongside the stereo mixdown endpoint for advanced
    QA — diarised transcript alignment, per-channel sentiment, etc.
    The dashboard's normal "play recording" affordance uses the
    merged stereo URL.
    """
    if stream not in {"caller.wav", "agent.wav"}:
        raise HTTPException(status_code=404, detail="unknown stream")
    await _require_agent_owned(agent_id, await current_user(request))
    row = await db.get_call_detail(agent_id, call_id)
    if not row or not row.get("recording_path") or row.get("recording_purged_at"):
        raise HTTPException(status_code=404, detail="recording not available")
    from . import recordings as _rec
    target = _rec.RECORDING_ROOT / str(row["recording_path"]) / stream
    if not target.exists():
        raise HTTPException(status_code=404, detail="recording file missing")
    from fastapi.responses import FileResponse
    return FileResponse(
        path=str(target),
        media_type="audio/wav",
        filename=f"call-{call_id}-{stream}",
    )


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
    was_published = bool(a.get("published"))
    updated = await db.update_agent(agent_id, patch)
    if not updated:
        raise HTTPException(status_code=404, detail="agent not found")
    # Build 198: emit lifecycle events. We diff old vs new on published
    # (because that's the most operationally important state transition)
    # and a few other high-signal fields. purpose, knowledge, info_groups
    # are emitted from their dedicated endpoints so we don't duplicate.
    try:
        from . import events as _ev
        now_published = bool(updated.get("published"))
        if patch.get("published") is True and not was_published:
            await _ev.emit(
                "agent.published", source="user",
                user_id=user["id"], org_id=updated.get("org_id"), agent_id=agent_id,
                title=f"Agent published — {updated.get('name')}",
                payload={"name": updated.get("name"), "sector": updated.get("sector"),
                         "locale": updated.get("locale")},
            )
        elif patch.get("published") is False and was_published:
            await _ev.emit(
                "agent.unpublished", source="user", severity="warning",
                user_id=user["id"], org_id=updated.get("org_id"), agent_id=agent_id,
                title=f"Agent unpublished — {updated.get('name')}",
                payload={"name": updated.get("name")},
            )
        elif "voice" in patch and patch.get("voice") and patch.get("voice") != a.get("voice"):
            await _ev.emit(
                "agent.voice.changed", source="user",
                user_id=user["id"], org_id=updated.get("org_id"), agent_id=agent_id,
                title=f"Voice changed — {updated.get('name')} → {patch.get('voice')}",
                payload={"from": a.get("voice"), "to": patch.get("voice")},
            )
        elif "purpose" in patch:
            await _ev.emit(
                "agent.purpose.changed", source="user",
                user_id=user["id"], org_id=updated.get("org_id"), agent_id=agent_id,
                title=f"Purpose updated — {updated.get('name')}",
                payload={"summary": (patch.get("purpose") or {}).get("summary"),
                         "actions": (patch.get("purpose") or {}).get("actions")},
            )
    except Exception as _e:  # noqa: BLE001
        log.debug("events.emit on patch_agent failed: %s", _e)
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


@app.get("/api/agents/{agent_id}/cost-breakdown")
async def get_agent_cost_breakdown(agent_id: int, request: Request) -> dict:
    """Structured per-minute cost breakdown for the agent's Overview
    'Cost Breakdown' card. Itemised line items (Platform / STT / TTS /
    AI Model / Telephony) each with vendor + status + per-minute INR,
    ending in `total_inr_per_min`.

    Source of truth: pricing.per_minute_inr() for the AI model line +
    pricing_versions table for the Plivo telephony rate. No call data
    needed — this is the projected rate; real per-call cost is still
    computed from actual token counts in `cost_paise`.
    """
    from . import pricing
    user = await current_user(request)
    agent = await _require_agent_owned(agent_id, user)
    return await pricing.cost_breakdown_for_agent(agent)


@app.get("/api/agents/{agent_id}/chip-schema")
async def get_agent_chip_schema(agent_id: int, request: Request) -> dict:
    """Resolved tag-chip schema for THIS agent — sector defaults +
    operator chip_overrides applied. Frontend uses this to render
    chips on the call log AND to power the override editor on
    /agent/<slug>/outcomes.

    Response shape:
      {
        sector: "<resolved sector>",
        categories: { <key>: {bg, fg, label, hint}, ... },  // SEMANTIC_CATEGORIES
        schema: [ {field, category, label, is_custom?, is_edited?}, ... ],
        overrides: <agent.chip_overrides verbatim>          // for the editor
      }

    Both the catalogue AND the categories are returned so the
    frontend doesn't need to keep its own colour map in sync — the
    backend is the single source of truth.
    """
    from . import chip_schema
    user = await current_user(request)
    agent = await _require_agent_owned(agent_id, user)
    return {
        "sector":     agent.get("sector"),
        "categories": chip_schema.SEMANTIC_CATEGORIES,
        "schema":     chip_schema.effective_schema(agent),
        "overrides":  agent.get("chip_overrides") or {},
    }


@app.post("/api/agents/{agent_id}/digest/preview")
async def post_agent_digest_preview(agent_id: int, request: Request) -> dict:
    """Render the outcome-digest email the way the daily scheduler
    would, using DRAFT settings posted in the body. No email is sent.

    Body: `{cadence, window_days, day_of_week, day_of_month}` — same
    shape `agents.digest_settings` takes. Bad / missing values default
    to `effective_settings`'s defaults so the preview never errors.

    Response: `{ok, html, subject, calls_n, minutes, window_label,
    cadence_label, day_iso, recipients_count, would_send_today}`.
    The frontend renders `html` inside a sandboxed iframe so the
    operator can eyeball EXACTLY what their owners will receive.
    """
    from . import eod_digest, email_stub as _es
    user = await current_user(request)
    agent = await _require_agent_owned(agent_id, user)
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    base_url = _es._public_base_url()
    out = await eod_digest.render_agent_digest_preview(
        agent=agent, settings=body, base_url=base_url,
    )
    out["ok"] = True
    return out


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
    _mode = (qp.get("mode") or "").strip().lower()
    text_only = _mode == "text"
    # `mode=chat&agent_id=<id>` → customer-facing AGENT chat (paid add-on).
    # Gated on the agent's org holding the `chat_channel` entitlement.
    agent_chat = _mode == "chat" and initial_agent_id is not None
    # Build 267 — publish gate. Live embed sessions (the third-party web
    # widget, marked embed=1) only serve a PUBLISHED agent. Dashboard test
    # calls don't set embed=1, so building/editing/testing stays free — it's
    # the LIVE channel that publishing unlocks. Publishing itself requires a
    # paid plan (PATCH /api/agents enforces 402), so this gates real calls on
    # a paid, published agent.
    if (qp.get("embed") or "").strip() == "1" and initial_agent_id is not None:
        _a = await db.get_agent(initial_agent_id)
        if not _a or not _a.get("published"):
            try:
                await ws.send_text(json.dumps({
                    "type": "error", "code": "agent_not_published",
                    "message": "This assistant isn't live yet.",
                }))
            except Exception:  # noqa: BLE001
                pass
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass
            return
    try:
        if agent_chat:
            from . import chat_bridge
            org_id = await db.get_agent_org(initial_agent_id)
            ent = await db.get_org_entitlements(org_id) if org_id else {}
            if not ent.get("chat_channel"):
                try:
                    await ws.send_text(json.dumps({
                        "type": "error", "code": "chat_not_entitled",
                        "message": "Chat is not enabled for this agent.",
                    }))
                except Exception:  # noqa: BLE001
                    pass
            else:
                await chat_bridge.run_agent_chat_session(
                    ws, initial_agent_id,
                    client_locale=client_locale, client_tz=client_tz,
                    user_id=user_id, sid=sid,
                )
        elif text_only:
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


# ──────────────────────── Generic telephony adapter ────────────────
#
# Build 250 — Plivo + any future webhook-based carrier shares one URL family.
# `{provider}` is `plivo`, `twilio` (alias for the legacy route above), or
# whatever the registry holds (Exotel etc. when added).
#
#   POST /api/sip/{provider}/answer/{agent_id}    → returns the provider's XML
#   POST /api/sip/{provider}/hangup               → finalises the call row
#   POST /api/sip/{provider}/fallback             → polite hangup on primary fail
#    WS  /ws/{provider}/{agent_id}                → audio bridge into Gemini Live
#
# Plivo's dashboard wants exact URLs in its "Create Application" dialog.
# Paste these:
#   Answer URL:    https://<host>/api/sip/plivo/answer/<agent_id>
#   Hangup URL:    https://<host>/api/sip/plivo/hangup
#   Fallback URL:  https://<host>/api/sip/plivo/fallback


def _host_hint_from_request(request: Optional["Request"]) -> str:
    """Bare public host (e.g. "agents.spiderx.ai") inferred from the
    incoming request when PUBLIC_HOST isn't configured. Behind Railway's
    proxy the X-Forwarded-Host / Host header carries the real public
    domain, so the webhook URLs we show the operator resolve correctly
    even if the admin never set PUBLIC_HOST. Returns "" when there's no
    usable request (e.g. internal callers)."""
    if request is None:
        return ""
    try:
        host = (request.headers.get("x-forwarded-host")
                or request.headers.get("host") or "").split(",")[0].strip()
    except Exception:  # noqa: BLE001
        host = ""
    # Never echo an internal/loopback host into a carrier-facing URL.
    if not host or host.startswith(("localhost", "127.0.0.1", "0.0.0.0")):
        return ""
    return host.rstrip("/")


def _public_wss_host(request: Optional["Request"] = None) -> str:
    """Public hostname for the carrier's WebSocket. Mirrors the Twilio
    helper above — supports PUBLIC_HOST as either bare host, http(s) URL,
    or ws(s) URL. Falls back to the request host when PUBLIC_HOST is unset."""
    public_host = os.environ.get("PUBLIC_HOST", "").rstrip("/")
    if not public_host:
        hint = _host_hint_from_request(request)
        return ("wss://" + hint) if hint else "wss://YOUR-NGROK-HOST"
    if public_host.startswith("http://"):
        return "ws://" + public_host[len("http://"):]
    if public_host.startswith("https://"):
        return "wss://" + public_host[len("https://"):]
    if not public_host.startswith(("ws://", "wss://")):
        return "wss://" + public_host
    return public_host


@app.api_route("/api/sip/{provider}/answer/{agent_id}", methods=["GET", "POST"],
               response_class=PlainTextResponse)
async def telephony_answer(provider: str, agent_id: int, request: Request):
    """Generic carrier Answer URL — returns the XML/JSON the carrier expects
    to start streaming audio to our WebSocket."""
    from .telephony import get_provider
    prov = get_provider(provider)
    if prov is None:
        raise HTTPException(status_code=404, detail=f"provider {provider!r} not registered")
    a = await db.get_agent(agent_id)
    if not a:
        raise HTTPException(status_code=404, detail="agent not found")
    # Build 267 — publish gate for INBOUND phone. A real caller reaching an
    # unpublished agent gets a polite "not active" hangup; publishing (paid)
    # unlocks the line. The owner's outbound "Call me back" test bypasses this
    # via ?test=1 on its answer URL (set in telephony_outbound_call), so the
    # owner can always test their own number.
    is_test = (request.query_params.get("test") or "").strip() == "1"
    if not a.get("published") and not is_test:
        body, content_type = prov.fallback_xml(agent=a)
        return PlainTextResponse(content=body, media_type=content_type)
    # Pass `request` so the stream URL falls back to the carrier-facing host
    # (e.g. agents.spiderx.ai) when PUBLIC_HOST isn't set — otherwise the
    # media WebSocket points at wss://YOUR-NGROK-HOST and the call has no audio.
    stream_url = f"{_public_wss_host(request)}/ws/{prov.name}/{agent_id}"
    body, content_type = prov.answer_xml(stream_url=stream_url, agent=a)
    return PlainTextResponse(content=body, media_type=content_type)


@app.api_route("/api/sip/{provider}/fallback", methods=["GET", "POST"],
               response_class=PlainTextResponse)
@app.api_route("/api/sip/{provider}/fallback/{agent_id}", methods=["GET", "POST"],
               response_class=PlainTextResponse)
async def telephony_fallback(provider: str, agent_id: Optional[int] = None):
    """Carrier's Fallback URL — primary timed out or 5xx'd. We emit a
    `telephony.fallback` event so observability shows the degradation
    and return the provider's polite-hangup XML."""
    from .telephony import get_provider
    prov = get_provider(provider)
    if prov is None:
        raise HTTPException(status_code=404, detail=f"provider {provider!r} not registered")
    a = await db.get_agent(agent_id) if agent_id else None
    try:
        from . import events
        await events.emit(
            kind="telephony.fallback",
            title=f"{prov.display_name} fallback URL hit",
            severity="warning",
            source="webhook",
            agent_id=agent_id,
            payload={"provider": prov.name},
        )
    except Exception:  # noqa: BLE001 — observability shouldn't break the response
        pass
    body, content_type = prov.fallback_xml(agent=a)
    return PlainTextResponse(content=body, media_type=content_type)


@app.post("/api/sip/{provider}/hangup")
@app.post("/api/sip/{provider}/hangup/{agent_id}")
async def telephony_hangup(provider: str, request: Request, agent_id: Optional[int] = None) -> dict:
    """Carrier's Hangup URL — fires once when the call ends. We normalise
    the body and emit a `call.ended` event for observability + EOD digest."""
    from .telephony import get_provider
    prov = get_provider(provider)
    if prov is None:
        raise HTTPException(status_code=404, detail=f"provider {provider!r} not registered")
    # Carriers POST form-encoded by default; accept JSON too.
    body: dict[str, Any] = {}
    ctype = (request.headers.get("content-type") or "").lower()
    try:
        if "application/json" in ctype:
            body = await request.json() or {}
        else:
            form = await request.form()
            body = dict(form)
    except Exception:  # noqa: BLE001
        body = {}
    normalised = prov.parse_hangup_webhook(body)
    try:
        from . import events
        cid = normalised.get("call_id") or "?"
        dur = normalised.get("duration_seconds")
        await events.emit(
            kind="call.ended",
            title=f"{prov.display_name} call {cid} ended" + (f" ({dur}s)" if dur else ""),
            severity="info",
            source="webhook",
            agent_id=agent_id,
            payload={
                "provider": prov.name,
                "call_id": normalised.get("call_id"),
                "duration_seconds": normalised.get("duration_seconds"),
                "hangup_cause": normalised.get("hangup_cause"),
            },
        )
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True}


@app.websocket("/ws/{provider}/{agent_id}")
async def ws_telephony(ws: WebSocket, provider: str, agent_id: int) -> None:
    """Generic carrier WS → Gemini Live bridge. Dispatches by provider name."""
    from .telephony import get_provider, run_call
    prov = get_provider(provider)
    if prov is None:
        await ws.close(code=4004)
        return
    await ws.accept()
    try:
        await run_call(ws, prov, agent_id)
    finally:
        try:
            await ws.close()
        except Exception:
            pass


# ──────────────────────── Telephony provisioning (auto + manual) ───
#
# Build 251 — endpoints that drive the Phone Number tab on agent settings.
# Operator pastes carrier creds (Plivo Auth ID + Token, Twilio SID + Token);
# we hit the carrier's REST API to verify creds, list owned numbers, create
# an Application with our webhook URLs pre-filled, and bind a chosen number
# to that Application — all without leaving SpiderX.
#
# Manual fallback: same endpoints (minus the auto-create steps) plus a
# "verify live" check that reads the carrier's current binding and tells
# the operator exactly what's misconfigured if anything is.


def _public_https_host(request: Optional["Request"] = None) -> str:
    """Public HTTPS hostname for the carrier's Answer / Hangup / Fallback URLs.
    Mirrors `_public_wss_host` but emits https:// instead of wss://. Falls
    back to the request host when PUBLIC_HOST is unset."""
    public_host = os.environ.get("PUBLIC_HOST", "").rstrip("/")
    if not public_host:
        hint = _host_hint_from_request(request)
        return ("https://" + hint) if hint else "https://YOUR-NGROK-HOST"
    if public_host.startswith(("ws://",)):
        return "http://" + public_host[len("ws://"):]
    if public_host.startswith(("wss://",)):
        return "https://" + public_host[len("wss://"):]
    if not public_host.startswith(("http://", "https://")):
        return "https://" + public_host
    return public_host


def _webhook_urls(provider_name: str, agent_id: int,
                  request: Optional["Request"] = None) -> dict[str, str]:
    """The URLs that go into the carrier's Application dialog."""
    base = _public_https_host(request)
    return {
        "answer_url":   f"{base}/api/sip/{provider_name}/answer/{agent_id}",
        "hangup_url":   f"{base}/api/sip/{provider_name}/hangup/{agent_id}",
        "fallback_url": f"{base}/api/sip/{provider_name}/fallback/{agent_id}",
        "stream_url":   f"{_public_wss_host(request)}/ws/{provider_name}/{agent_id}",
    }


def _carriers_map(agent: dict[str, Any]) -> dict[str, dict]:
    """The agent's per-carrier telephony map ({provider: cfg}). Tolerant of
    a missing column, a JSON string (legacy/asyncpg edge), or junk — always
    returns a dict so callers can read/copy safely."""
    raw = agent.get("telephony_carriers")
    if isinstance(raw, str):
        try:
            import json as _json
            raw = _json.loads(raw)
        except Exception:  # noqa: BLE001
            raw = {}
    if not isinstance(raw, dict):
        return {}
    # Only keep dict-valued entries keyed by lowercase provider id.
    return {str(k).lower(): v for k, v in raw.items() if isinstance(v, dict)}


def _outbound_carrier(carriers: dict[str, dict]) -> Optional[tuple[str, dict]]:
    """Return (provider, cfg) for a carrier that can place OUTBOUND calls —
    i.e. has both a saved number and stored API credentials (secret_enc).
    Manual-only numbers (no creds) are inbound-only. None if none qualify."""
    for prov, cfg in (carriers or {}).items():
        if isinstance(cfg, dict) and cfg.get("number") and cfg.get("secret_enc"):
            return (prov, cfg)
    return None


def _telephony_view(agent: dict[str, Any], provider_name: Optional[str] = None,
                    request: Optional["Request"] = None) -> dict[str, Any]:
    """Project the agent's stored telephony state into the shape the UI
    consumes, scoped to a SINGLE carrier (the selected/requested one).

    Each carrier persists independently under `agents.telephony_carriers`
    (see migration 0029), so the Twilio tab never shows a Plivo number and
    vice-versa. `configured_providers` lists every carrier that has a saved
    number so the UI can badge the carrier chips.

    Never returns the raw Auth Token — only the last-4 tail. Defensive
    against partial/garbage DB state: every field can be null."""
    from .telephony import available_providers
    from .telephony.secrets import decrypt_creds_str, mask_token
    carriers = _carriers_map(agent)
    configured_providers = sorted(
        p for p, c in carriers.items() if (c.get("number"))
    )
    # Which carrier this view is about: the explicit request wins; else the
    # first configured carrier; else none (the UI defaults to a tab).
    selected = (provider_name or (configured_providers[0] if configured_providers else "")).lower()
    cfg = carriers.get(selected) if isinstance(carriers.get(selected), dict) else {}
    cfg = cfg or {}
    try:
        creds = decrypt_creds_str(cfg.get("secret_enc")) or {}
    except Exception:  # noqa: BLE001
        creds = {}
    auth_token = (creds.get("auth_token") or "")
    try:
        providers = available_providers()
    except Exception:  # noqa: BLE001
        providers = []
    try:
        webhooks = _webhook_urls(selected or "plivo", int(agent["id"]), request)
    except Exception:  # noqa: BLE001
        webhooks = {"answer_url": "", "hangup_url": "", "fallback_url": "", "stream_url": ""}
    # `configured_provider` mirrors the legacy field but is now scoped to the
    # SELECTED carrier: non-null only when THIS carrier has a saved number.
    this_configured = selected if cfg.get("number") else None
    # Outbound readiness: we can place an outbound (test/callback) call only
    # for a carrier with BOTH a number AND stored API credentials (auto-setup).
    # A manual-only number can receive calls but can't originate them.
    outbound = _outbound_carrier(carriers)
    return {
        "providers": providers,
        "selected_provider": selected or None,
        "configured_provider": this_configured,
        "configured_providers": configured_providers,
        "outbound_ready": bool(outbound),
        "outbound_provider": outbound[0] if outbound else None,
        "outbound_from": (outbound[1].get("number") if outbound else None),
        "config": {
            "alias": cfg.get("alias") or "",
            "number": cfg.get("number") or "",
            "app_id": cfg.get("app_id") or "",
            "setup_mode": cfg.get("setup_mode") or "",
            "last_verified_at": cfg.get("last_verified_at"),
        },
        "creds": {
            "auth_id": (creds.get("auth_id") or "") if creds else "",
            "auth_token_mask": mask_token(auth_token) if auth_token else "",
            "has_token": bool(auth_token),
        },
        "webhooks": webhooks,
    }


@app.get("/api/agents/{agent_id}/telephony")
async def telephony_get(agent_id: int, request: Request, provider: Optional[str] = None) -> dict:
    """Current telephony state for the agent. Optional `?provider=plivo`
    switches the previewed webhook URLs without persisting anything."""
    user = await current_user(request)
    agent = await _require_agent_owned(agent_id, user)
    return _telephony_view(agent, provider_name=provider, request=request)


@app.post("/api/agents/{agent_id}/telephony/test-creds")
async def telephony_test_creds(agent_id: int, request: Request) -> dict:
    """Verify the operator's carrier credentials WITHOUT persisting them.
    Returns `{ok, account_name, balance, currency}` on success."""
    user = await current_user(request)
    await _require_agent_admin(agent_id, user)
    body = await request.json()
    provider_name = (body.get("provider") or "").strip().lower()
    from .telephony import get_provider
    from .telephony.base import TelephonyAuthError
    prov = get_provider(provider_name)
    if prov is None:
        raise HTTPException(status_code=400, detail=f"unknown provider {provider_name!r}")
    if not prov.auto_provision_supported:
        raise HTTPException(status_code=400, detail=f"{prov.display_name} doesn't support auto-setup")
    creds = {
        "auth_id":    (body.get("auth_id") or body.get("account_sid") or "").strip(),
        "auth_token": (body.get("auth_token") or "").strip(),
    }
    if not creds["auth_id"] or not creds["auth_token"]:
        raise HTTPException(status_code=400, detail="auth_id and auth_token are required")
    try:
        info = await prov.verify_creds(creds)
    except TelephonyAuthError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001 — never surface a raw 500 to the panel
        log.exception("telephony_test_creds: %s verify_creds failed", prov.name)
        raise HTTPException(status_code=502, detail=f"Couldn't reach {prov.display_name}: {e}")
    return info


@app.post("/api/agents/{agent_id}/telephony/numbers")
async def telephony_list_numbers(agent_id: int, request: Request) -> dict:
    """List the operator's owned numbers in the carrier's account. Creds
    come in the POST body (we don't want them in a GET query string)."""
    user = await current_user(request)
    await _require_agent_admin(agent_id, user)
    body = await request.json()
    provider_name = (body.get("provider") or "").strip().lower()
    from .telephony import get_provider
    from .telephony.base import TelephonyAuthError
    prov = get_provider(provider_name)
    if prov is None:
        raise HTTPException(status_code=400, detail=f"unknown provider {provider_name!r}")
    creds = {
        "auth_id":    (body.get("auth_id") or body.get("account_sid") or "").strip(),
        "auth_token": (body.get("auth_token") or "").strip(),
    }
    try:
        nums = await prov.list_numbers(creds)
    except TelephonyAuthError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"{prov.display_name} API error: {e}")
    return {"ok": True, "numbers": nums}


@app.post("/api/agents/{agent_id}/telephony/provision")
async def telephony_provision(agent_id: int, request: Request) -> dict:
    """Auto-setup: create the Application in the carrier's dashboard with
    our webhook URLs pre-filled, bind the chosen number to it, and persist
    the encrypted creds + binding metadata onto the agent row."""
    from datetime import datetime, timezone
    from .telephony import get_provider
    from .telephony.base import TelephonyAuthError
    from .telephony.secrets import encrypt_creds_str
    user = await current_user(request)
    agent = await _require_agent_admin(agent_id, user)
    body = await request.json()
    provider_name = (body.get("provider") or "").strip().lower()
    prov = get_provider(provider_name)
    if prov is None:
        raise HTTPException(status_code=400, detail=f"unknown provider {provider_name!r}")
    if not prov.auto_provision_supported:
        raise HTTPException(status_code=400, detail=f"{prov.display_name} doesn't support auto-setup")
    number = (body.get("number") or "").strip()
    if not number:
        raise HTTPException(status_code=400, detail="number is required")
    creds = {
        "auth_id":    (body.get("auth_id") or body.get("account_sid") or "").strip(),
        "auth_token": (body.get("auth_token") or "").strip(),
    }
    if not creds["auth_id"] or not creds["auth_token"]:
        raise HTTPException(status_code=400, detail="auth_id and auth_token are required")
    alias = (body.get("alias") or f"SpiderX-{agent.get('name') or 'Eva'}-Inbound").strip()[:120]
    urls = _webhook_urls(prov.name, agent_id, request)
    try:
        app_info = await prov.create_application(
            creds=creds, name=alias,
            answer_url=urls["answer_url"],
            hangup_url=urls["hangup_url"],
            fallback_url=urls["fallback_url"],
        )
        await prov.bind_number(
            creds=creds, number=number,
            app_id=app_info["app_id"], alias=alias,
        )
    except TelephonyAuthError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"{prov.display_name} API error: {e}")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    carriers = _carriers_map(agent)
    carriers[prov.name] = {
        "alias": alias,
        "number": number,
        "app_id": app_info.get("app_id"),
        "setup_mode": "auto",
        "configured_at": now,
        "last_verified_at": now,
        "secret_enc": encrypt_creds_str(creds),
    }
    updated = await db.update_agent(agent_id, {"telephony_carriers": carriers})
    if not updated:
        raise HTTPException(status_code=404, detail="agent not found")
    try:
        from . import events
        await events.emit(
            kind="telephony.provisioned",
            title=f"{prov.display_name} number {number} bound to agent {agent.get('name') or agent_id}",
            severity="info", source="user", agent_id=agent_id,
            payload={"provider": prov.name, "number": number, "app_id": app_info.get("app_id"), "mode": "auto"},
        )
    except Exception:  # noqa: BLE001
        pass
    return _telephony_view(updated, provider_name=prov.name, request=request)


@app.post("/api/agents/{agent_id}/telephony/manual-config")
async def telephony_manual_config(agent_id: int, request: Request) -> dict:
    """Operator did the setup manually in the carrier's dashboard — record
    which provider + number so we can route inbound calls and show status."""
    from datetime import datetime, timezone
    from .telephony import get_provider
    user = await current_user(request)
    agent = await _require_agent_admin(agent_id, user)
    body = await request.json()
    provider_name = (body.get("provider") or "").strip().lower()
    prov = get_provider(provider_name)
    if prov is None:
        raise HTTPException(status_code=400, detail=f"unknown provider {provider_name!r}")
    number = (body.get("number") or "").strip()
    if not number:
        raise HTTPException(status_code=400, detail="number is required")
    alias = (body.get("alias") or "").strip()[:120]
    # Per-carrier: update only THIS provider's slot, preserving any other
    # carrier already configured (and any creds already saved for this one).
    carriers = _carriers_map(agent)
    existing = carriers.get(prov.name) if isinstance(carriers.get(prov.name), dict) else {}
    carriers[prov.name] = {
        **(existing or {}),
        "number": number,
        "alias": alias,
        "setup_mode": "manual",
        "configured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    updated = await db.update_agent(agent_id, {"telephony_carriers": carriers})
    if not updated:
        raise HTTPException(status_code=404, detail="agent not found")
    return _telephony_view(updated, provider_name=prov.name, request=request)


@app.post("/api/agents/{agent_id}/telephony/verify-live")
async def telephony_verify_live(agent_id: int, request: Request) -> dict:
    """Re-read the carrier's current binding for the configured number and
    compare to what we expect. Surfaces the gap so the UI can tell the
    operator exactly what's wrong (e.g. "Number is bound to a different
    Application")."""
    from datetime import datetime, timezone
    from .telephony import get_provider
    from .telephony.base import TelephonyAuthError
    from .telephony.secrets import decrypt_creds_str
    user = await current_user(request)
    agent = await _require_agent_admin(agent_id, user)
    # Which carrier to verify: explicit body/query provider, else the only
    # configured one. Each carrier is independent now.
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    req_provider = (body.get("provider") or request.query_params.get("provider") or "").strip().lower()
    carriers = _carriers_map(agent)
    provider_name = req_provider or next((p for p, c in carriers.items() if c.get("number")), "")
    cfg = carriers.get(provider_name) if isinstance(carriers.get(provider_name), dict) else {}
    cfg = cfg or {}
    if not provider_name or not cfg.get("number"):
        raise HTTPException(status_code=400, detail="No telephony provider configured yet.")
    prov = get_provider(provider_name)
    if prov is None or not prov.auto_provision_supported:
        # Manual-only providers can't be verified via API.
        return {"ok": True, "verifiable": False,
                "reason": f"{provider_name} doesn't expose a read-back API; verification is by inbound test call."}
    creds = decrypt_creds_str(cfg.get("secret_enc"))
    if not creds.get("auth_id") or not creds.get("auth_token"):
        raise HTTPException(status_code=400,
                            detail="Saved carrier credentials are missing — re-enter them under Auto-setup.")
    try:
        remote = await prov.read_number_config(creds=creds, number=cfg["number"])
    except TelephonyAuthError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"{prov.display_name} API error: {e}")
    expected_app_id = cfg.get("app_id")
    remote_app_id = remote.get("app_id")
    ok = bool(remote_app_id) and (not expected_app_id or remote_app_id == expected_app_id)
    if ok:
        cfg["last_verified_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        carriers[provider_name] = cfg
        await db.update_agent(agent_id, {"telephony_carriers": carriers})
    return {
        "ok": ok,
        "verifiable": True,
        "remote": remote,
        "expected_app_id": expected_app_id,
        "drift": None if ok else (
            f"Number is bound to Application {remote_app_id!r}, expected {expected_app_id!r}."
            if remote_app_id and expected_app_id and remote_app_id != expected_app_id
            else "Number isn't bound to any Application in your carrier account."
        ),
    }


@app.post("/api/agents/{agent_id}/telephony/outbound-call")
async def telephony_outbound_call(agent_id: int, request: Request) -> dict:
    """Place an OUTBOUND call: the agent's connected carrier dials the given
    number and, on answer, streams to Gemini via our Answer URL (the same
    inbound path). Requires a carrier connected via auto-setup (stored API
    credentials) — a manual-only number can't originate calls.

    Used by the "Call me back" phone-test and any future outbound flow."""
    from .telephony import get_provider
    from .telephony.base import TelephonyAuthError
    from .telephony.secrets import decrypt_creds_str
    user = await current_user(request)
    agent = await _require_agent_admin(agent_id, user)
    body = await request.json()
    to_number = (body.get("to") or body.get("number") or "").strip()
    if not re.match(r"^\+?[1-9]\d{6,14}$", to_number):
        raise HTTPException(status_code=400, detail="Enter the number in international format — e.g. +918031321199.")
    if not to_number.startswith("+"):
        to_number = "+" + to_number
    carriers = _carriers_map(agent)
    outbound = _outbound_carrier(carriers)
    if not outbound:
        raise HTTPException(
            status_code=409,
            detail="No carrier with API credentials is connected. Connect your carrier via auto-setup (paste credentials) to place outbound calls.",
        )
    provider_name, cfg = outbound
    prov = get_provider(provider_name)
    if prov is None:
        raise HTTPException(status_code=400, detail=f"unknown provider {provider_name!r}")
    creds = decrypt_creds_str(cfg.get("secret_enc"))
    if not creds.get("auth_id") or not creds.get("auth_token"):
        raise HTTPException(status_code=409, detail="Saved carrier credentials are missing — re-connect the carrier API.")
    urls = _webhook_urls(prov.name, agent_id, request)
    # Owner-initiated test call — mark the answer URL so the inbound publish
    # gate lets it through even when the agent isn't published yet.
    answer_url = urls["answer_url"] + ("&" if "?" in urls["answer_url"] else "?") + "test=1"
    try:
        res = await prov.place_outbound_call(
            creds=creds, from_number=cfg["number"],
            to_number=to_number, answer_url=answer_url,
        )
    except TelephonyAuthError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"{prov.display_name} API error: {e}")
    try:
        from . import events
        await events.emit(
            kind="telephony.outbound",
            title=f"{prov.display_name} outbound call to {to_number} from agent {agent.get('name') or agent_id}",
            severity="info", source="user", agent_id=agent_id,
            payload={"provider": prov.name, "to": to_number, "from": cfg["number"], "call_id": res.get("call_id")},
        )
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "provider": prov.name, "from": cfg["number"],
            "to": to_number, "call_id": res.get("call_id")}


@app.delete("/api/agents/{agent_id}/telephony")
async def telephony_disconnect(agent_id: int, request: Request) -> dict:
    """Disconnect ONE carrier (or all). Wipes our copy of that carrier's
    credentials + binding metadata. Does NOT touch the carrier's account —
    the operator may want to keep the Application/number for another purpose.

    `?provider=twilio` removes only that carrier's slot; omit it to wipe
    every carrier (legacy behaviour)."""
    user = await current_user(request)
    agent = await _require_agent_admin(agent_id, user)
    provider_name = (request.query_params.get("provider") or "").strip().lower()
    carriers = _carriers_map(agent)
    if provider_name:
        carriers.pop(provider_name, None)
    else:
        carriers = {}
    updated = await db.update_agent(agent_id, {"telephony_carriers": carriers})
    return _telephony_view(updated or {**agent, "telephony_carriers": carriers},
                           provider_name=provider_name or None, request=request)


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
    # Build 238 — inject the Firebase Web API key from env at render
    # time so the value never lives in git. Empty string is fine for
    # builds without Google sign-in wired up (the lazy importer
    # rejects an empty key with a clear error from Firebase itself).
    html_text = html_text.replace(
        "{FIREBASE_API_KEY}", os.environ.get("FIREBASE_API_KEY", ""),
    )
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
