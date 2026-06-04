"""Minimal in-process scheduler for cron-style platform jobs.

Single asyncio task running at server startup. Wakes every minute,
checks each registered job against its cron-ish schedule, runs the
ones whose minute has arrived. Tolerates job failures (catches and
emits `system.scheduler.run.missed`); never crashes the loop.

Why not APScheduler / Celery / a real scheduler:
  - We have one process for now; a worker tier is premature
    optimisation.
  - APScheduler's persistence + clustering features are not free
    operationally — they add a moving part for a feature whose v1
    is "fire 3 jobs / day".
  - When we DO need durable scheduling, the migration path is to
    extract this module's register() into a thin adapter over
    APScheduler; consumers (price_monitor, etc.) don't change.

Schedule format is a simplified cron of 5 fields:
  "MIN HOUR DOM MON DOW"     each is either a number or "*"
A `tz` per-job is supported — defaults to IST (Asia/Kolkata) since
that's our primary customer market. UTC jobs pass tz="UTC".
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Callable, Awaitable, Optional
from zoneinfo import ZoneInfo

log = logging.getLogger("eva.scheduler")


_IST = ZoneInfo("Asia/Kolkata")
_JOBS: list[dict] = []
_TASK: Optional[asyncio.Task] = None
_LAST_RUN: dict[str, datetime] = {}


def register(
    name: str,
    cron: str,
    func: Callable[[], Awaitable[None]],
    *,
    tz: str = "Asia/Kolkata",
) -> None:
    """Register a job. `cron` is "MIN HOUR DOM MON DOW" (each int or '*').
    The job's `func` is an async callable taking no arguments — closure
    over needed state at registration time."""
    parts = cron.strip().split()
    if len(parts) != 5:
        raise ValueError(f"bad cron {cron!r} — need 5 fields")
    _JOBS.append({
        "name": name,
        "cron": cron,
        "parts": parts,
        "func": func,
        "tz": ZoneInfo(tz),
    })
    log.info("scheduler.registered name=%s cron=%r tz=%s", name, cron, tz)


def _matches(parts: list[str], now: datetime) -> bool:
    """Does the given 5-field cron match `now`? Each part is a number or
    "*". Day-of-week uses Python's Monday=0 convention."""
    fields = [now.minute, now.hour, now.day, now.month, now.weekday()]
    for spec, val in zip(parts, fields):
        if spec == "*":
            continue
        try:
            if int(spec) != val:
                return False
        except ValueError:
            return False
    return True


async def _loop() -> None:
    """The wake-every-minute supervisor. Sleeps to the next minute
    boundary so we don't drift across midnights."""
    from . import events as _ev
    while True:
        try:
            for job in _JOBS:
                now = datetime.now(job["tz"]).replace(second=0, microsecond=0)
                if not _matches(job["parts"], now):
                    continue
                # Same-minute idempotency — if the loop fires twice in
                # the same minute (e.g. clock wobble), don't double-run.
                last = _LAST_RUN.get(job["name"])
                if last and last == now:
                    continue
                _LAST_RUN[job["name"]] = now
                log.info("scheduler.run name=%s at=%s", job["name"], now.isoformat())
                try:
                    await job["func"]()
                except Exception as e:  # noqa: BLE001
                    log.exception("scheduler.run_failed name=%s", job["name"])
                    try:
                        await _ev.emit(
                            "system.scheduler.run.missed",
                            severity="error", source="scheduler",
                            title=f"Scheduled job failed: {job['name']}",
                            message=str(e)[:600],
                            payload={"name": job["name"], "cron": job["cron"],
                                     "fired_at": now.isoformat()},
                        )
                    except Exception:  # noqa: BLE001
                        pass
        except Exception:  # noqa: BLE001
            log.exception("scheduler.loop_iteration_failed")
        # Sleep to the next minute boundary
        now = datetime.now()
        nxt = (60 - now.second) + 1
        await asyncio.sleep(nxt)


async def start() -> None:
    """Boot the scheduler task. Called from FastAPI startup hook."""
    global _TASK
    if _TASK and not _TASK.done():
        return
    _TASK = asyncio.create_task(_loop(), name="eva.scheduler")
    log.info("scheduler.started")


async def stop() -> None:
    """Cancel the task on shutdown."""
    global _TASK
    if _TASK and not _TASK.done():
        _TASK.cancel()
        try:
            await _TASK
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _TASK = None


def list_jobs() -> list[dict]:
    """Snapshot of registered jobs + last-run timestamps for the
    Observability /admin/observability Schedulers tab."""
    return [
        {
            "name": j["name"],
            "cron": j["cron"],
            "tz": str(j["tz"]),
            "last_run": _LAST_RUN.get(j["name"]).isoformat() if _LAST_RUN.get(j["name"]) else None,
        }
        for j in _JOBS
    ]


async def run_now(name: str) -> bool:
    """Out-of-band "Run now" trigger from the Observability page.

    Build 209 — stamp `_LAST_RUN` here too so the Schedulers tab's
    "Last run" column (and the Pricing tab's "Last checked Nm ago"
    pill) update immediately after a manual trigger. Without this
    stamp the cron-loop path was the only thing that wrote
    _LAST_RUN, leaving manual runs invisible to the UI.
    """
    for j in _JOBS:
        if j["name"] == name:
            try:
                await j["func"]()
                _LAST_RUN[name] = datetime.now(j["tz"]).replace(second=0, microsecond=0)
                return True
            except Exception:  # noqa: BLE001
                log.exception("scheduler.run_now_failed name=%s", name)
                return False
    return False
