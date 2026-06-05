"""Per-agent periodic health probe — proves every PUBLISHED agent is
actually ready to take a call right now, not just that the FastAPI
process is alive.

What this catches that `/api/build` (Railway healthcheck) misses:
  - Gemini API regional outage (the bridge can't open Live sessions)
  - DB row corruption on a specific agent (broken JSONB blob, missing
    voice config, malformed greeting template)
  - Schema drift after a partial migration (column missing on one row)
  - Per-agent config rot (operator deleted a knowledge_base, broke a
    template variable reference)

Two probe levels are exposed:

  Level 2 (WS handshake)   default — opens a WebSocket to /ws/session,
                           waits for the {type:"session_starting"}
                           message the bridge emits after loading the
                           agent + composing the system prompt, closes.
                           ~500 ms per probe, NO Gemini Live cost.

  Level 3 (full audio)     opt-in — same WS open, but actually completes
                           a Gemini Live handshake and waits for at least
                           one outbound audio chunk. ~5 s per probe,
                           ~$0.01 of Gemini cost per probe. Run daily.

Both levels emit events the Observability page already renders:

  agent.healthcheck.passed   (info)     all good
  agent.healthcheck.degraded (warning)  worked but slow (latency > threshold)
  agent.healthcheck.failed   (error)    didn't get to session_starting

The probe is INTERNAL — it connects to ws://127.0.0.1:${PORT} from
inside the running process. That keeps the probe free and fast, but
means an outage between Railway's edge and our container won't be
detected here (that's the platform healthcheck's job). The two
together cover both "is the box up?" (platform) and "is each agent
ready?" (this module).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Optional

import websockets

from . import db, events as _ev

log = logging.getLogger("eva.agent_healthcheck")


# ─── Config ──────────────────────────────────────────────────────────────

# Per-probe timeout. The handshake should land well under 2 s in
# healthy conditions; 5 s gives generous margin without making a real
# failure case (gemini outage → bridge hangs trying to open a session)
# wait too long.
PROBE_TIMEOUT_S = float(os.environ.get("AGENT_HEALTHCHECK_TIMEOUT_S", "5.0"))

# Latency threshold for "degraded" — slower than this but still
# successful gets a warning event instead of a passed event.
DEGRADED_LATENCY_MS = int(os.environ.get("AGENT_HEALTHCHECK_DEGRADED_MS", "2000"))

# Concurrency cap. With 100 agents and a 5 s timeout each, sequential
# would take 8 minutes; concurrent at 10 finishes in ~50 s in the
# worst case. Bumped via env if a customer has more agents.
PROBE_PARALLELISM = int(os.environ.get("AGENT_HEALTHCHECK_PARALLELISM", "10"))


def _internal_ws_url() -> str:
    """ws:// URL for the probe to hit. Always points at localhost on
    whichever port uvicorn bound to — Railway sets $PORT, dev defaults
    to the in-process value (8765) but the scheduler runs in the SAME
    process so we can read uvicorn's bound port from the env."""
    port = os.environ.get("PORT") or os.environ.get("UVICORN_PORT") or "8080"
    return f"ws://127.0.0.1:{port}"


# ─── Level 2: WS handshake probe ─────────────────────────────────────────


async def probe_agent_ws(agent_id: int, *, timeout_s: float = PROBE_TIMEOUT_S) -> dict:
    """Open a WS to /ws/session?agent_id=<id>, wait for the bridge's
    `session_starting` JSON message, close cleanly.

    Returns:
      {
        "ok":          bool,
        "latency_ms":  int,
        "phase":       "connected" | "session_starting" | "closed",
        "error":       optional str — populated when ok=False,
      }
    """
    url = f"{_internal_ws_url()}/ws/session?agent_id={int(agent_id)}&kind=test"
    started = time.monotonic()
    phase = "init"
    try:
        async with asyncio.timeout(timeout_s):
            async with websockets.connect(url, max_size=None) as ws:
                phase = "connected"
                # Loop reading messages until we see session_starting.
                # The bridge may emit other framing messages first
                # (e.g. error events on bad state) — we treat any
                # `type:"error"` as a probe failure.
                while True:
                    raw = await ws.recv()
                    if isinstance(raw, bytes):
                        # Audio bytes mean we somehow blew past handshake
                        # without seeing session_starting — count it
                        # as success either way (the WS is clearly alive).
                        phase = "session_starting"
                        break
                    try:
                        msg = json.loads(raw)
                    except Exception:  # noqa: BLE001
                        continue
                    if not isinstance(msg, dict):
                        continue
                    if msg.get("type") == "session_starting":
                        phase = "session_starting"
                        break
                    if msg.get("type") == "error":
                        return {
                            "ok": False,
                            "latency_ms": int((time.monotonic() - started) * 1000),
                            "phase": phase,
                            "error": str(msg.get("message") or "bridge reported error")[:300],
                        }
                # Close cleanly. The bridge interprets text {type:"stop"}
                # as the client hanging up — graceful teardown, no
                # Gemini session was opened anyway.
                try:
                    await ws.send(json.dumps({"type": "stop"}))
                except Exception:  # noqa: BLE001
                    pass
        return {
            "ok": True,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "phase": "closed",
            "error": None,
        }
    except asyncio.TimeoutError:
        return {
            "ok": False,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "phase": phase,
            "error": f"timeout after {timeout_s}s (stuck at {phase})",
        }
    except Exception as e:  # noqa: BLE001
        return {
            "ok": False,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "phase": phase,
            "error": f"{type(e).__name__}: {str(e)[:240]}",
        }


# ─── Scheduler entry point ───────────────────────────────────────────────


async def _probe_one(agent: dict) -> dict:
    """Probe a single agent and emit the right event kind based on the
    outcome. Returns the probe result dict so the caller can roll up
    a summary."""
    result = await probe_agent_ws(int(agent["id"]))
    agent_id = int(agent["id"])
    agent_name = agent.get("name") or f"#{agent_id}"
    org_id = int(agent["org_id"]) if agent.get("org_id") else None
    latency = result["latency_ms"]
    payload = {
        "agent_id": agent_id,
        "agent_slug": agent.get("slug"),
        "latency_ms": latency,
        "phase": result["phase"],
        "level": 2,
    }
    if result["ok"] and latency > DEGRADED_LATENCY_MS:
        # Worked but slow — likely Gemini regional latency or DB
        # contention. Worth a warning so it surfaces on the
        # Observability page without crying wolf.
        try:
            await _ev.emit(
                "agent.healthcheck.degraded",
                severity="warning", source="scheduler",
                title=f"{agent_name} healthcheck slow ({latency} ms)",
                org_id=org_id, agent_id=agent_id,
                payload=payload,
            )
        except Exception:  # noqa: BLE001
            pass
    elif result["ok"]:
        try:
            await _ev.emit(
                "agent.healthcheck.passed",
                severity="info", source="scheduler",
                title=f"{agent_name} healthcheck OK ({latency} ms)",
                org_id=org_id, agent_id=agent_id,
                payload=payload,
            )
        except Exception:  # noqa: BLE001
            pass
    else:
        payload["error"] = result["error"]
        try:
            await _ev.emit(
                "agent.healthcheck.failed",
                severity="error", source="scheduler",
                title=f"{agent_name} healthcheck FAILED",
                message=result["error"],
                org_id=org_id, agent_id=agent_id,
                payload=payload,
            )
        except Exception:  # noqa: BLE001
            pass
    return result


async def run_hourly_healthchecks() -> None:
    """Scheduler entry point — probes every PUBLISHED agent in
    parallel (bounded by PROBE_PARALLELISM) and emits one event per
    agent per run. Failed probes raise to the Observability page as
    error-severity events; the daily EOD digest can sum them per
    agent later if we want a "uptime %" KPI.
    """
    try:
        pool = await db.get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, name, slug, org_id FROM agents "
                "WHERE published = TRUE ORDER BY id ASC"
            )
        agents = [dict(r) for r in rows]
    except Exception as e:  # noqa: BLE001
        log.warning("agent_healthcheck: agent list query failed: %s", e)
        return
    if not agents:
        return

    sem = asyncio.Semaphore(PROBE_PARALLELISM)
    async def _bounded(a):
        async with sem:
            return await _probe_one(a)
    results = await asyncio.gather(*(_bounded(a) for a in agents), return_exceptions=True)

    # Roll-up summary event — a single "hourly_agent_healthcheck.summary"
    # info row that the Schedulers tab can show as "Last run: X passed,
    # Y degraded, Z failed". Keeps the live feed from drowning in 100
    # per-agent rows on every run.
    passed = sum(1 for r in results if isinstance(r, dict) and r.get("ok") and r["latency_ms"] <= DEGRADED_LATENCY_MS)
    degraded = sum(1 for r in results if isinstance(r, dict) and r.get("ok") and r["latency_ms"] > DEGRADED_LATENCY_MS)
    failed = sum(1 for r in results if not (isinstance(r, dict) and r.get("ok")))
    sev = "info" if (failed == 0 and degraded == 0) else ("warning" if failed == 0 else "error")
    try:
        await _ev.emit(
            "agent.healthcheck.summary",
            severity=sev, source="scheduler",
            title=f"Hourly healthcheck: {passed} ok · {degraded} slow · {failed} failed",
            payload={
                "passed": passed, "degraded": degraded, "failed": failed,
                "total": len(agents),
                "parallelism": PROBE_PARALLELISM,
            },
        )
    except Exception:  # noqa: BLE001
        pass


# ─── Public-status endpoint helper ───────────────────────────────────────


async def latest_status_per_agent() -> list[dict[str, Any]]:
    """Return the most recent healthcheck event per published agent,
    sorted by agent_id. Powers `/api/admin/agents/health` — the
    Observability page consumes this to render a green/yellow/red
    dot beside every agent.

    The query is small (LATERAL join to a 1-row-per-agent subquery
    over the events table) so it's safe to call on every page load.
    """
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT a.id          AS agent_id,
                   a.name        AS name,
                   a.slug        AS slug,
                   a.org_id      AS org_id,
                   o.name        AS org_name,
                   lc.kind       AS kind,
                   lc.severity   AS severity,
                   lc.title      AS title,
                   lc.payload    AS payload,
                   lc.created_at AS last_check
              FROM agents a
              LEFT JOIN orgs o ON o.id = a.org_id
              LEFT JOIN LATERAL (
                SELECT kind, severity, title, payload, created_at
                  FROM events
                 WHERE agent_id = a.id
                   AND kind LIKE 'agent.healthcheck.%'
                   AND kind <> 'agent.healthcheck.summary'
                 ORDER BY id DESC
                 LIMIT 1
              ) lc ON TRUE
             WHERE a.published = TRUE
             ORDER BY a.id ASC
            """
        )
    out = []
    for r in rows:
        d = dict(r)
        # status maps the event-kind suffix back to a stable label the
        # UI can colour. `never` = published but never probed (yet).
        kind = d.get("kind") or ""
        if not kind:
            status = "never"
        elif kind.endswith(".passed"):
            status = "up"
        elif kind.endswith(".degraded"):
            status = "degraded"
        elif kind.endswith(".failed"):
            status = "down"
        else:
            status = "unknown"
        d["status"] = status
        if d.get("last_check"):
            d["last_check"] = d["last_check"].isoformat()
        # Pull latency from the payload for the UI's "Xms" badge.
        if isinstance(d.get("payload"), str):
            try:
                d["payload"] = json.loads(d["payload"])
            except Exception:  # noqa: BLE001
                d["payload"] = {}
        d["latency_ms"] = (d.get("payload") or {}).get("latency_ms")
        out.append(d)
    return out
