"""Permission shim for the team era.

Centralises every "can this user act on this org/agent" check. Routes call
into here instead of inline-checking role strings, so the rules live in
one place and audit-logging (Phase 3) has a single hook to wrap.

Role hierarchy:
    owner  > admin > member

  * owner — can do everything (manage members, change plan, delete org)
  * admin — can manage agents + invite members; can't change roles or delete org
  * member — can build/edit agents; can't manage other members

For unauthenticated dev (no X-User-Id header), current_user falls back to
the founder. The founder is automatically owner of every org they're in,
so dev calls work without any seam.
"""
from __future__ import annotations

from typing import Optional

from fastapi import HTTPException

from . import db


_ROLE_RANK = {"member": 1, "admin": 2, "owner": 3}


async def require_member(user_id: int, org_id: int) -> str:
    """Caller must be a member of this org (any role). Returns their role.
    Raises 403 otherwise."""
    role = await db.get_member_role(org_id, user_id)
    if not role:
        raise HTTPException(status_code=403,
                            detail={"code": "not_org_member",
                                    "message": "You aren't a member of this organisation."})
    return role


async def require_admin(user_id: int, org_id: int) -> str:
    """Caller must be admin or owner."""
    role = await require_member(user_id, org_id)
    if _ROLE_RANK[role] < _ROLE_RANK["admin"]:
        raise HTTPException(status_code=403,
                            detail={"code": "requires_admin",
                                    "message": "This action requires admin or owner role."})
    return role


async def require_owner(user_id: int, org_id: int) -> str:
    """Caller must be owner."""
    role = await require_member(user_id, org_id)
    if role != "owner":
        raise HTTPException(status_code=403,
                            detail={"code": "requires_owner",
                                    "message": "This action requires owner role."})
    return role


async def require_agent_member(user_id: int, agent_id: int) -> dict:
    """Loads the agent and asserts the caller is a member of its org. Returns
    the agent dict on success (so the route doesn't need to fetch twice)."""
    a = await db.get_agent(agent_id)
    if not a:
        raise HTTPException(status_code=404, detail="agent not found")
    org_id = a.get("org_id")
    if org_id is None:
        # Pre-Phase-2 agent. Fall back to user_id ownership check so legacy
        # rows don't 500. Once every agent has org_id set this branch dies.
        owner = a.get("user_id")
        if owner is not None and owner != user_id:
            raise HTTPException(status_code=403, detail="not your agent")
        return a
    await require_member(user_id, org_id)
    return a


async def require_agent_admin(user_id: int, agent_id: int) -> dict:
    """Edit / delete actions on an agent require admin+ on its org."""
    a = await require_agent_member(user_id, agent_id)
    org_id = a.get("org_id")
    if org_id is not None:
        await require_admin(user_id, org_id)
    return a


async def primary_org_id(user_id: int) -> Optional[int]:
    """The user's "active" org for the current single-org UX. Reads the
    legacy `users.org_id` pointer; multi-org switching becomes a Phase 3
    concern (the topbar will let users hop between orgs they're a member
    of)."""
    org = await db.get_org_for_user(user_id)
    return org["id"] if org else None


# ─── Phase 3 — super admin shim ──────────────────────────────────────────

async def require_super_admin(user_id: int) -> None:
    """Gate every /api/admin/* route. Raises 403 if the caller isn't in
    `super_admins`. Cheap query (PK lookup) so each request can re-check
    instead of caching — revocation takes effect immediately."""
    if not await db.is_super_admin(user_id):
        raise HTTPException(
            status_code=403,
            detail={"code": "requires_super_admin",
                    "message": "Platform-admin access only."},
        )
