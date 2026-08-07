"""Live guardrail (agent.policy) push over SSE — multi-instance safe.

When an operator saves an agent's Do's & Don'ts, `app.patch_agent` calls
`await publish(agent_id, payload)`, which fires a Postgres `NOTIFY`. EVERY app
instance runs a background `LISTEN` connection; each receives the notification
and fans it out to its own SSE subscribers (`GET /api/agents/{id}/policy-stream`).
So two operators on different devices — even served by different instances/
workers — stay in sync without a reload.

Design:
  • `publish()`  → `pg_notify('sxai_policy', <json>)` (reaches all instances).
  • `start_listener()` → one dedicated LISTEN connection per process, auto-
    reconnecting; its callback fans out to local subscriber queues.
  • Postgres caps a NOTIFY payload at 8000 bytes. The policy is normally tiny,
    but if a custom-rules blob pushes it over, we notify `{agent_id, origin,
    refetch:true}` and the listener re-reads the policy from the DB.
  • If NOTIFY ever fails, we fall back to a same-process fan-out so at least the
    saving instance's clients update.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from typing import Any, Optional

log = logging.getLogger("policy_stream")

_CHANNEL = "sxai_policy"
_NOTIFY_MAX = 7500          # stay safely under Postgres' 8000-byte NOTIFY cap

# agent_id → set of subscriber queues (one per open SSE connection, this process).
_subs: dict[int, set[asyncio.Queue]] = defaultdict(set)

_listener_conn = None       # dedicated asyncpg connection holding the LISTEN
_listener_started = False


# ─── local subscribers (SSE endpoint) ────────────────────────────────────────
def subscribe(agent_id: int) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=16)
    _subs[agent_id].add(q)
    return q


def unsubscribe(agent_id: int, q: asyncio.Queue) -> None:
    subs = _subs.get(agent_id)
    if subs:
        subs.discard(q)
        if not subs:
            _subs.pop(agent_id, None)


def subscriber_count(agent_id: int) -> int:
    return len(_subs.get(agent_id, ()))


def _fanout(agent_id: int, payload: dict[str, Any]) -> None:
    """Deliver a payload to this process's SSE subscribers. Never blocks."""
    for q in list(_subs.get(agent_id, ())):
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:  # slow client — drop; it recovers on reconnect
            pass


# ─── publish (cross-instance via NOTIFY) ──────────────────────────────────────
async def publish(agent_id: int, payload: dict[str, Any]) -> None:
    """Broadcast a policy change to every instance. Best-effort — never raises."""
    origin = payload.get("origin", "")
    body = {"agent_id": int(agent_id), "origin": origin, "policy": payload.get("policy")}
    s = json.dumps(body, default=str)
    if len(s.encode("utf-8")) > _NOTIFY_MAX:
        # Too big for a NOTIFY payload — tell listeners to re-read from the DB.
        s = json.dumps({"agent_id": int(agent_id), "origin": origin, "refetch": True})
    try:
        from . import db_pg
        pool = await db_pg.get_pool()
        async with pool.acquire() as conn:
            await conn.execute("SELECT pg_notify($1, $2)", _CHANNEL, s)
    except Exception as e:  # noqa: BLE001
        # DB unreachable / no LISTEN infra — at least update this instance.
        log.warning("policy NOTIFY failed (%s) — local fan-out only", e)
        _fanout(int(agent_id), {"policy": payload.get("policy"), "origin": origin})


# ─── listener (one per process) ───────────────────────────────────────────────
async def _handle_notify(agent_id: int, origin: str, policy: Any, refetch: bool) -> None:
    if refetch:
        try:
            from . import db as _db
            agent = await _db.get_agent(agent_id)
            policy = (agent or {}).get("policy")
        except Exception as e:  # noqa: BLE001
            log.debug("policy refetch failed for agent %s: %s", agent_id, e)
            policy = None
    _fanout(agent_id, {"policy": policy, "origin": origin})


def _on_notify(_conn, _pid, _channel, payload) -> None:
    """asyncpg listener callback (sync) — parse + schedule async fan-out."""
    try:
        d = json.loads(payload)
        aid = d.get("agent_id")
        if aid is None:
            return
        asyncio.get_running_loop().create_task(
            _handle_notify(int(aid), d.get("origin", ""), d.get("policy"), bool(d.get("refetch")))
        )
    except Exception as e:  # noqa: BLE001
        log.debug("policy NOTIFY parse failed: %s", e)


async def start_listener() -> None:
    """Keep a dedicated LISTEN connection alive, reconnecting on failure. Fire
    once at startup as a background task."""
    global _listener_conn, _listener_started
    if _listener_started:
        return
    _listener_started = True
    import asyncpg
    from . import db_pg
    while True:
        try:
            _listener_conn = await asyncpg.connect(dsn=db_pg._pg_url())
            await _listener_conn.add_listener(_CHANNEL, _on_notify)
            log.info("policy_stream: LISTEN %s established", _CHANNEL)
            while not _listener_conn.is_closed():
                await asyncio.sleep(30)
        except asyncio.CancelledError:
            break
        except Exception as e:  # noqa: BLE001
            log.warning("policy_stream listener error: %s — retrying in 5s", e)
        finally:
            try:
                if _listener_conn and not _listener_conn.is_closed():
                    await _listener_conn.close()
            except Exception:  # noqa: BLE001
                pass
            _listener_conn = None
        await asyncio.sleep(5)


async def stop_listener() -> None:
    global _listener_conn
    try:
        if _listener_conn and not _listener_conn.is_closed():
            await _listener_conn.close()
    except Exception:  # noqa: BLE001
        pass
    _listener_conn = None
