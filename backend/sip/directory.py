"""Directory — resolves an inbound SIP call to a SpiderX agent from the DB.

This is what makes the dashboard "Connect your phone system" card actually drive
sipd: the operator saves a native `sip_config` (DID + allowed IPs, and/or trunk
credentials) on their agent, and here we look calls up against it — no env vars,
no redeploy, so it's genuinely self-serve/multi-tenant.

Resolution order for an INVITE:
  1. by trunk username (if the request authenticated) — the username maps 1:1 to
     an agent, so it's the most precise key.
  2. by dialed DID — tolerant match (formats vary: +9133…, 9133…, national).

The pure matching helpers are unit-tested; the DB lookups need a live Postgres.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ResolvedAgent:
    agent_id: int
    allowed_ips: list = field(default_factory=list)
    trunk_username: Optional[str] = None
    trunk_password: Optional[str] = None


def normalize_did(s) -> str:
    """Reduce a phone number to bare digits for comparison (drop +, spaces,
    dashes, parens, and a leading 00 international prefix)."""
    if not s:
        return ""
    d = re.sub(r"\D", "", str(s))
    if d.startswith("00"):
        d = d[2:]
    return d


def did_matches(a, b) -> bool:
    """True if two numbers refer to the same DID across formatting differences.
    Exact on full digits, else a 10-digit suffix match (national number) — enough
    to disambiguate DIDs while tolerating +CC / 0-prefix / trunk rewrites."""
    na, nb = normalize_did(a), normalize_did(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    return len(na) >= 10 and len(nb) >= 10 and na[-10:] == nb[-10:]


def _native(cfg) -> bool:
    return isinstance(cfg, dict) and cfg.get("mode") == "native"


async def resolve_by_username(username: str) -> Optional[ResolvedAgent]:
    if not username:
        return None
    from ..db_pg import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        r = await conn.fetchrow(
            "SELECT id, sip_config FROM agents WHERE sip_config->>'trunk_username' = $1 LIMIT 1",
            username)
    if not r:
        return None
    cfg = r["sip_config"] or {}
    return ResolvedAgent(agent_id=r["id"], allowed_ips=cfg.get("allowed_ips") or [],
                         trunk_username=cfg.get("trunk_username"),
                         trunk_password=cfg.get("trunk_password"))


async def resolve_by_did(did: str) -> Optional[ResolvedAgent]:
    if not did:
        return None
    from ..db_pg import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, sip_config FROM agents "
            "WHERE sip_config->>'mode' = 'native' AND sip_config->>'did' IS NOT NULL")
    for r in rows:
        cfg = r["sip_config"] or {}
        if did_matches(cfg.get("did"), did):
            return ResolvedAgent(agent_id=r["id"], allowed_ips=cfg.get("allowed_ips") or [],
                                 trunk_username=cfg.get("trunk_username"),
                                 trunk_password=cfg.get("trunk_password"))
    return None
