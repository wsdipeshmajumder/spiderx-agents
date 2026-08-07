"""In-process pub/sub for live guardrail (agent.policy) push over SSE.

When an operator saves an agent's Do's & Don'ts, `app.patch_agent` calls
`publish(agent_id, payload)`; every dashboard client streaming
`GET /api/agents/{id}/policy-stream` receives it and updates without a reload —
so two operators on different devices stay in sync.

Scope: single-process (one uvicorn worker), which is the current deployment.
It is best-effort — a full queue drops the update rather than blocking the
save. If the service is ever scaled to multiple instances, swap the in-memory
fan-out for Redis pub/sub or Postgres LISTEN/NOTIFY behind this same API.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

# agent_id → set of subscriber queues (one per open SSE connection).
_subs: dict[int, set[asyncio.Queue]] = defaultdict(set)


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


def publish(agent_id: int, payload: dict[str, Any]) -> None:
    """Fan a payload out to every subscriber of this agent. Never raises and
    never blocks the caller (the save path) — a full queue just drops."""
    for q in list(_subs.get(agent_id, ())):
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:  # slow client — drop, it'll catch up on reconnect
            pass


def subscriber_count(agent_id: int) -> int:
    return len(_subs.get(agent_id, ()))
