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
        # Build 231 — email alert on Level 2 failure too. Same path
        # the Level 3 probe uses; honours `healthcheck.email_on_failure`
        # + `healthcheck.email_recipients` settings.
        await _maybe_email_alert(agent, result, level=2)
    return result


async def run_hourly_healthchecks() -> None:
    """Scheduler entry point — probes every PUBLISHED agent in
    parallel (bounded by PROBE_PARALLELISM) and emits one event per
    agent per run. Failed probes raise to the Observability page as
    error-severity events; the daily EOD digest can sum them per
    agent later if we want a "uptime %" KPI.
    """
    # Build 231 — admin-toggle gate. The hourly probe stays on by
    # default but operators can flip it off via Platform Settings
    # → Health checks without redeploying.
    from . import settings as cfg
    if not bool(await cfg.get("healthcheck.level2_enabled", True)):
        log.info("agent_healthcheck: level 2 disabled via settings, skipping run")
        return
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


# ─── Level 3: full conversational probe (build 231) ──────────────────────


# Sample-rate of the inbound mic frames the bridge expects. PCM int16 LE.
# 100 ms at 16 kHz = 1600 samples = 3200 bytes per "frame" we stream to
# the WS — matches the chunk size the browser audio engine produces.
_PROBE_FRAME_BYTES = 3200

# Total bytes of silence to stream as the synthetic caller utterance.
# 800 ms = 25 600 bytes. Long enough that Gemini's VAD fires; short
# enough to keep the probe under 5 s total.
_PROBE_SILENCE_BYTES = 25_600

# How long to wait for each phase of the probe before giving up. Tuned
# generously — a real Gemini Live session takes 1-2 s to open and 2-4 s
# to produce the first greeting chunk on a cold path.
_LEVEL3_TIMEOUT_GREETING_S = 12.0
_LEVEL3_TIMEOUT_RESPONSE_S = 15.0


async def probe_agent_full(agent_id: int) -> dict:
    """Level 3 — exercise the FULL voice round-trip for one agent.

    Phases (each timed; failure at any phase short-circuits + reports
    which phase died):
      1. connect           — WS handshake to /ws/session
      2. session_starting  — bridge loaded the agent
      3. greeting_audio    — Gemini Live opened + spoke first chunk
      4. response_audio    — synthetic caller frames in, response out
      5. closed            — clean teardown

    The "caller utterance" is a synthesised text turn over the WS's
    `{type:"text"}` channel (proves LLM round-trip + voice synthesis)
    + a brief silence buffer streamed as binary frames (proves the
    inbound audio pipe accepts frames). Together that's tight coverage
    on every leg of a real call EXCEPT the PSTN provider hop.

    Returns:
      {
        "ok":            bool,
        "latency_ms":    int — total wall time
        "phase_timings": {connect, session_starting, greeting_audio,
                          response_audio, closed} — each int ms or None
        "phase":         the phase reached (success or failure)
        "error":         optional str — populated when ok=False,
      }
    """
    url = f"{_internal_ws_url()}/ws/session?agent_id={int(agent_id)}&kind=test"
    started = time.monotonic()
    timings: dict[str, Optional[int]] = {
        "connect": None, "session_starting": None,
        "greeting_audio": None, "response_audio": None, "closed": None,
    }
    phase = "init"

    def _ms_since() -> int:
        return int((time.monotonic() - started) * 1000)

    try:
        async with asyncio.timeout(PROBE_TIMEOUT_S + _LEVEL3_TIMEOUT_GREETING_S + _LEVEL3_TIMEOUT_RESPONSE_S):
            async with websockets.connect(url, max_size=None) as ws:
                phase = "connect"; timings["connect"] = _ms_since()

                # Phase 2 — wait for session_starting
                async with asyncio.timeout(PROBE_TIMEOUT_S):
                    while True:
                        raw = await ws.recv()
                        if isinstance(raw, str):
                            try:
                                msg = json.loads(raw)
                            except Exception:  # noqa: BLE001
                                continue
                            if isinstance(msg, dict) and msg.get("type") == "session_starting":
                                phase = "session_starting"
                                timings["session_starting"] = _ms_since()
                                break
                            if isinstance(msg, dict) and msg.get("type") == "error":
                                return {
                                    "ok": False, "latency_ms": _ms_since(),
                                    "phase_timings": timings, "phase": "session_starting",
                                    "error": str(msg.get("message") or "bridge error at handshake")[:300],
                                }

                # Phase 3 — wait for first audio chunk OUT (greeting)
                async with asyncio.timeout(_LEVEL3_TIMEOUT_GREETING_S):
                    while True:
                        raw = await ws.recv()
                        if isinstance(raw, (bytes, bytearray)):
                            phase = "greeting_audio"
                            timings["greeting_audio"] = _ms_since()
                            break
                        # ignore non-audio messages during greeting wait

                # Phase 4 — send synthetic caller turn + silence frames,
                # wait for the next audio chunk out (the agent's response).
                greeting_at = time.monotonic()
                try:
                    await ws.send(json.dumps({
                        "type": "text",
                        "text": "Hello, this is an automated healthcheck. Please briefly acknowledge.",
                    }))
                except Exception as e:  # noqa: BLE001
                    return {
                        "ok": False, "latency_ms": _ms_since(),
                        "phase_timings": timings, "phase": "response_audio",
                        "error": f"text send failed: {e}",
                    }
                # Stream a short burst of silence as audio frames so the
                # inbound audio pipe is exercised even though the model
                # answers from the text turn above. Best-effort — frame
                # send failures don't fail the probe (the text path is
                # the primary signal).
                silence = b"\x00\x00" * (_PROBE_FRAME_BYTES // 2)
                bytes_sent = 0
                while bytes_sent < _PROBE_SILENCE_BYTES:
                    try:
                        await ws.send(silence)
                        bytes_sent += len(silence)
                        await asyncio.sleep(0.05)  # ~20 chunks/sec, mimics live mic
                    except Exception:  # noqa: BLE001
                        break

                # Now wait for the agent's RESPONSE audio. Skip the
                # greeting tail chunks by ignoring everything received
                # in the first ~1 s after the greeting kicked off.
                async with asyncio.timeout(_LEVEL3_TIMEOUT_RESPONSE_S):
                    while True:
                        raw = await ws.recv()
                        if isinstance(raw, (bytes, bytearray)):
                            # If we're still inside the greeting window,
                            # this is greeting-tail audio — keep waiting.
                            if time.monotonic() - greeting_at < 1.2:
                                continue
                            phase = "response_audio"
                            timings["response_audio"] = _ms_since()
                            break

                # Phase 5 — close cleanly.
                try:
                    await ws.send(json.dumps({"type": "stop"}))
                except Exception:  # noqa: BLE001
                    pass
                phase = "closed"
                timings["closed"] = _ms_since()
        return {
            "ok": True, "latency_ms": _ms_since(),
            "phase_timings": timings, "phase": "closed", "error": None,
        }
    except asyncio.TimeoutError:
        return {
            "ok": False, "latency_ms": _ms_since(),
            "phase_timings": timings, "phase": phase,
            "error": f"timeout at phase '{phase}'",
        }
    except Exception as e:  # noqa: BLE001
        return {
            "ok": False, "latency_ms": _ms_since(),
            "phase_timings": timings, "phase": phase,
            "error": f"{type(e).__name__}: {str(e)[:240]}",
        }


async def _probe_one_full(agent: dict) -> dict:
    """Run one Level-3 probe + emit the matching event. Returns the
    raw result so the caller can roll up + alert."""
    result = await probe_agent_full(int(agent["id"]))
    agent_id = int(agent["id"])
    agent_name = agent.get("name") or f"#{agent_id}"
    org_id = int(agent["org_id"]) if agent.get("org_id") else None
    payload = {
        "agent_id": agent_id,
        "agent_slug": agent.get("slug"),
        "latency_ms": result["latency_ms"],
        "phase": result["phase"],
        "phase_timings": result["phase_timings"],
        "level": 3,
    }
    if result["ok"] and result["latency_ms"] > DEGRADED_LATENCY_MS * 3:
        # Level 3 latency is naturally higher (full Gemini round-trip).
        # Treat 3× the Level 2 threshold as "degraded" so info-grade
        # operators aren't paged on every healthy 6-second probe.
        await _safe_emit("agent.healthcheck.full.degraded", "warning",
                         f"{agent_name} full probe slow ({result['latency_ms']} ms)",
                         org_id, agent_id, payload)
    elif result["ok"]:
        await _safe_emit("agent.healthcheck.full.passed", "info",
                         f"{agent_name} full probe OK ({result['latency_ms']} ms)",
                         org_id, agent_id, payload)
    else:
        payload["error"] = result["error"]
        await _safe_emit("agent.healthcheck.full.failed", "error",
                         f"{agent_name} FULL PROBE FAILED — {result['phase']}",
                         org_id, agent_id, payload, message=result["error"])
        # Email alert when configured. Failed-only — we don't spam on
        # passed runs because the summary event covers that.
        await _maybe_email_alert(agent, result, level=3)
    return result


async def run_daily_full_healthchecks() -> None:
    """Scheduler entry point — Level 3 probe across published agents.

    Sample-size capped via `healthcheck.level3_sample_size` to bound
    Gemini cost. When N agents > sample_size, pick N at random per run
    so over time every agent gets covered without paying for all of
    them daily.
    """
    from . import settings as cfg
    if not bool(await cfg.get("healthcheck.level3_enabled", False)):
        log.info("agent_healthcheck: level 3 disabled via settings, skipping run")
        return
    sample_size = int(await cfg.get("healthcheck.level3_sample_size", 25) or 0)
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
    # Stable but rotating sample — order by (id + day_of_year) mod len
    # so the same agent isn't always picked first. No random imports
    # needed; day-anchored rotation gives even coverage over a week.
    from datetime import datetime, timezone as _tz
    if sample_size > 0 and sample_size < len(agents):
        doy = datetime.now(_tz.utc).timetuple().tm_yday
        agents = sorted(agents, key=lambda a: (int(a["id"]) + doy) % len(agents))[:sample_size]

    # Lower parallelism than Level 2 — each probe holds a Gemini Live
    # session for ~5 s, and we don't want to spike concurrent sessions.
    sem = asyncio.Semaphore(max(1, PROBE_PARALLELISM // 2))
    async def _bounded(a):
        async with sem:
            return await _probe_one_full(a)
    results = await asyncio.gather(*(_bounded(a) for a in agents), return_exceptions=True)

    passed = sum(1 for r in results if isinstance(r, dict) and r.get("ok"))
    failed = sum(1 for r in results if not (isinstance(r, dict) and r.get("ok")))
    sev = "info" if failed == 0 else "error"
    await _safe_emit(
        "agent.healthcheck.full.summary", sev,
        f"Daily full probe: {passed} ok · {failed} failed (of {len(agents)} sampled)",
        None, None,
        {"passed": passed, "failed": failed, "total_sampled": len(agents)},
    )


# ─── Level 4: real PSTN probe (placeholder) ──────────────────────────────


async def run_pstn_healthcheck() -> dict:
    """Level 4 — place a real outbound call to verify the PSTN path.

    NOT YET IMPLEMENTED — the configuration plumbing is live (settings
    keys + admin form), but the actual call placement awaits the
    Twilio outbound integration. Returns a stub result so the admin
    "Run now" button can give a clear "not configured" message rather
    than crashing.
    """
    from . import settings as cfg
    if not bool(await cfg.get("healthcheck.level4_pstn_enabled", False)):
        return {"ok": False, "error": "Level 4 PSTN probe is disabled in settings"}
    provider = await cfg.get("healthcheck.level4_pstn_provider", "twilio")
    from_n = (await cfg.get("healthcheck.level4_pstn_from_number", "")).strip()
    to_n   = (await cfg.get("healthcheck.level4_pstn_to_number", "")).strip()
    if not (from_n and to_n):
        await _safe_emit(
            "agent.healthcheck.pstn.config_missing", "warning",
            "PSTN probe requested but FROM / TO number not configured",
            None, None,
            {"provider": provider, "from": from_n, "to": to_n},
        )
        return {"ok": False, "error": "from_number / to_number not set"}
    # Stub — emit an event explaining the wiring is partial.
    await _safe_emit(
        "agent.healthcheck.pstn.not_implemented", "info",
        "PSTN probe configured but outbound integration not wired yet",
        None, None,
        {"provider": provider, "from": from_n, "to": to_n},
    )
    return {"ok": False, "error": f"{provider} outbound integration not yet implemented"}


# ─── Helpers ─────────────────────────────────────────────────────────────


async def _safe_emit(
    kind: str, severity: str, title: str,
    org_id, agent_id, payload, *, message: Optional[str] = None,
) -> None:
    """Wrapper around events.emit that swallows failures + uniformly
    sets source='scheduler'. Keeps every emission site terse."""
    try:
        await _ev.emit(
            kind, severity=severity, source="scheduler",
            title=title, message=message,
            org_id=org_id, agent_id=agent_id, payload=payload,
        )
    except Exception:  # noqa: BLE001
        pass


async def _maybe_email_alert(agent: dict, result: dict, *, level: int) -> None:
    """When the operator has `healthcheck.email_on_failure` on, send
    a terse alert email with the failed phase + the latency + a link
    back to the agent's overview page so they can dig in immediately."""
    try:
        from . import settings as cfg, email_stub
        if not bool(await cfg.get("healthcheck.email_on_failure", True)):
            return
        recipients = (await cfg.get("healthcheck.email_recipients", "")).strip()
        to_list: list[str] = []
        if recipients:
            to_list = [a.strip() for a in recipients.split(",") if a.strip()]
        else:
            fallback = (os.environ.get("REPORT_EMAIL_TO") or "").strip()
            if fallback:
                to_list = [fallback]
        if not to_list:
            log.info("healthcheck.alert_email: no recipients configured, skipping")
            return
        subject = f"[SpiderX healthcheck] {agent.get('name') or agent.get('id')} — LEVEL {level} FAILED"
        base = (os.environ.get("PUBLIC_BASE_URL") or "http://localhost:8765").rstrip("/")
        agent_url = f"{base}/agent/{agent.get('slug') or agent.get('id')}"
        body = (
            f"Healthcheck probe FAILED for agent: {agent.get('name')}\n"
            f"  Probe level:      {level}\n"
            f"  Phase reached:    {result.get('phase')}\n"
            f"  Latency:          {result.get('latency_ms')} ms\n"
            f"  Error:            {result.get('error')}\n"
            f"  Phase timings:    {result.get('phase_timings')}\n"
            f"\nAgent: {agent_url}\n"
            f"Observability: {base}/admin/observability\n"
        )
        for to in to_list:
            try:
                await email_stub._send(to, subject, body, html_body=None)
            except Exception as e:  # noqa: BLE001
                log.warning("healthcheck.alert_email send to %s failed: %s", to, e)
    except Exception as e:  # noqa: BLE001
        log.warning("healthcheck.alert_email path failed: %s", e)
