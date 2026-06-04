"""Per-call audio recording — writer + retention helpers.

What this module owns:
  - The filesystem layout under `data/recordings/<agent_id>/<call_id>/`
  - The wave-format writers for the two raw PCM streams Gemini Live
    gives us:
      caller.wav  — 16-kHz mono int16 (incoming mic, native rate)
      agent.wav   — 24-kHz mono int16 (outgoing TTS, Gemini's native rate)
  - The 180-day retention timedelta (env-overridable via
    `RECORDING_RETENTION_DAYS`)
  - The DB-driven purge pass that the daily scheduler invokes

Why two separate WAVs instead of one mixed-down stereo file:

  The inbound mic is 16 kHz; the model's TTS is 24 kHz. Mixing them
  into a single file forces a resample, which without scipy/numpy on
  the hot path is either lossy (sample-skip) or expensive (linear
  interpolation per sample). The two-stream layout sidesteps that
  entirely and gives a downstream QA tool perfect raw inputs to do
  whatever it wants — diarised transcript alignment, sentiment per
  channel, etc. Cost: two files per call instead of one. Trivial.

Design rules:

  - Best-effort. EVERY method here catches Exception. A failed write
    must NEVER crash the call — the caller's experience is the
    product; the recording is a side-channel.

  - No threads, no asyncio. Writes are synchronous to the OS via the
    stdlib `wave` module — pushing them to a writer thread is
    premature optimisation at our current call volume.

  - The writer is opened ONCE per call (in the gemini_bridge session
    setup) and finalized ONCE on session close. State is captured
    in the `RecordingWriter` instance, not a global registry.
"""
from __future__ import annotations

import logging
import os
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger("eva.recordings")


# ─── Config ──────────────────────────────────────────────────────────────

# Retention window. 180 days is the default — long enough for QA review
# cycles, audit / dispute windows, and most India / EU compliance
# regimes; short enough that we're not silently piling up audio
# forever. Overridable via env so an enterprise customer can dial it
# up to 365 (or down to 30 for a strict-privacy use-case).
RECORDING_RETENTION_DAYS = int(os.environ.get("RECORDING_RETENTION_DAYS", "180"))

# Where files land. Default to a repo-local `data/recordings` dir so a
# dev box just works; production wires `RECORDING_DIR` to a mounted
# volume (Railway volume, EBS, etc.).
RECORDING_ROOT = Path(os.environ.get("RECORDING_DIR", "data/recordings"))

# Native sample rates Gemini Live cascade speaks. Hard-coded because
# changing them silently would corrupt every WAV header we've ever
# written; safer to fail loudly if Gemini's contract changes.
CALLER_RATE_HZ = 16_000
AGENT_RATE_HZ  = 24_000


def expires_at_for(started_at: datetime) -> datetime:
    """When does a recording captured at `started_at` expire?

    Centralised so the scheduler's purge query and the insert-call
    write path can never drift apart on the retention math."""
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    return started_at + timedelta(days=RECORDING_RETENTION_DAYS)


def call_dir(agent_id: int, call_id: int | str) -> Path:
    """Canonical on-disk directory for one call's recordings."""
    return RECORDING_ROOT / str(int(agent_id)) / str(call_id)


def relative_path_for(agent_id: int, call_id: int | str) -> str:
    """Stored value for `calls.recording_path` — relative to the
    recordings root, NOT an absolute path. Keeps DB rows portable
    across deploy environments (a dev dump on a different machine
    won't carry `/var/eva/...` paths)."""
    return f"{int(agent_id)}/{call_id}"


# ─── Writer ──────────────────────────────────────────────────────────────


class RecordingWriter:
    """Append-only WAV writer for one call, two streams.

    The PCM frames Gemini Live hands us are little-endian int16, mono.
    The stdlib `wave` module gives us standards-compliant RIFF/WAVE
    output for free — no third-party deps, plays in every browser /
    DAW out of the box.

    Lifecycle:
      w = RecordingWriter(call_id_token, agent_id)
      w.open()                  # creates the directory + WAV headers
      w.write_caller(pcm_bytes) # for every inbound mic chunk
      w.write_agent(pcm_bytes)  # for every outbound TTS chunk
      meta = w.finalize()       # closes the files, returns dict for DB

    `call_id_token` is the temporary id we generate at session start
    (we don't have a `calls.id` yet — that comes from insert_call).
    The directory gets renamed in finalize() if a real call_id is
    supplied; otherwise it keeps the token form.
    """

    def __init__(self, call_id_token: str, agent_id: int):
        self.call_id_token = str(call_id_token)
        self.agent_id = int(agent_id)
        self._caller_wave: Optional[wave.Wave_write] = None
        self._agent_wave: Optional[wave.Wave_write] = None
        self._caller_bytes = 0
        self._agent_bytes = 0
        self._started_at: Optional[datetime] = None
        self._closed = False
        self._dir = call_dir(agent_id, self.call_id_token)

    def open(self) -> bool:
        """Create directory + open both WAV writers. Returns True on
        success, False if anything went wrong — callers should treat
        a False return as "recording is off for this call" and not
        retry per-chunk."""
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            self._caller_wave = wave.open(str(self._dir / "caller.wav"), "wb")
            self._caller_wave.setnchannels(1)
            self._caller_wave.setsampwidth(2)  # int16
            self._caller_wave.setframerate(CALLER_RATE_HZ)
            self._agent_wave = wave.open(str(self._dir / "agent.wav"), "wb")
            self._agent_wave.setnchannels(1)
            self._agent_wave.setsampwidth(2)
            self._agent_wave.setframerate(AGENT_RATE_HZ)
            self._started_at = datetime.now(timezone.utc)
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("recordings.open_failed agent=%s call=%s err=%s",
                        self.agent_id, self.call_id_token, e)
            self._safe_close()
            return False

    def write_caller(self, chunk: bytes) -> None:
        """Append one inbound-mic PCM chunk. Best-effort — exceptions
        are swallowed and the writer's state stays consistent (we
        just won't count those bytes)."""
        if self._closed or not self._caller_wave or not chunk:
            return
        try:
            self._caller_wave.writeframesraw(chunk)
            self._caller_bytes += len(chunk)
        except Exception as e:  # noqa: BLE001
            log.warning("recordings.write_caller_failed err=%s", e)

    def write_agent(self, chunk: bytes) -> None:
        """Append one outbound-TTS PCM chunk."""
        if self._closed or not self._agent_wave or not chunk:
            return
        try:
            self._agent_wave.writeframesraw(chunk)
            self._agent_bytes += len(chunk)
        except Exception as e:  # noqa: BLE001
            log.warning("recordings.write_agent_failed err=%s", e)

    def finalize(self, call_id: Optional[int] = None) -> dict:
        """Close both WAV files, optionally rename the directory from
        the temporary token to the real `calls.id`, and return the
        metadata block for `insert_call`.

        Even on partial failure we return a dict; the caller stamps
        what it can on the calls row."""
        if self._closed:
            return {}
        self._closed = True
        self._safe_close()
        # Rename token-dir → call_id-dir if we got a real call_id back.
        final_dir = self._dir
        if call_id is not None:
            final = call_dir(self.agent_id, call_id)
            try:
                if not final.exists():
                    self._dir.rename(final)
                    final_dir = final
            except Exception as e:  # noqa: BLE001
                log.warning("recordings.rename_failed err=%s", e)
        total_bytes = self._caller_bytes + self._agent_bytes
        # If nothing was written, drop the directory rather than leaving
        # an empty pair of 44-byte WAV headers on disk forever.
        if total_bytes == 0:
            try:
                for f in final_dir.glob("*.wav"):
                    f.unlink(missing_ok=True)
                final_dir.rmdir()
            except Exception:  # noqa: BLE001
                pass
            return {
                "recording_path": None,
                "recording_size_bytes": 0,
                "recording_format": None,
                "recording_started_at": self._started_at,
            }
        rel = relative_path_for(self.agent_id, call_id) if call_id else relative_path_for(self.agent_id, self.call_id_token)
        return {
            "recording_path": rel,
            "recording_size_bytes": total_bytes,
            "recording_format": "wav-2ch",  # marker: two single-channel WAVs
            "recording_started_at": self._started_at,
        }

    def _safe_close(self) -> None:
        for w in (self._caller_wave, self._agent_wave):
            if w is None:
                continue
            try:
                w.close()
            except Exception:  # noqa: BLE001
                pass


# ─── Purge ───────────────────────────────────────────────────────────────


async def purge_expired() -> dict:
    """Delete recordings whose `recording_expires_at` has passed.

    Walks every non-purged row with `recording_path IS NOT NULL` and
    `recording_expires_at < NOW()`, unlinks the on-disk files, and
    sets `recording_purged_at = NOW()`. Idempotent: a second run finds
    no rows.

    Emits `recording.purged` (info) per-batch, and `recording.purge.error`
    (warning) per row that fails to unlink — so the audit-trail
    shows BOTH the success rate AND the failures (a permission /
    mounted-volume issue won't go silent).
    """
    from . import db_pg as _db, events as _ev
    purged_n = 0
    failed_n = 0
    freed_bytes = 0
    pool = await _db.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, agent_id, recording_path, recording_size_bytes "
            "FROM calls "
            "WHERE recording_purged_at IS NULL "
            "  AND recording_path IS NOT NULL "
            "  AND recording_expires_at < NOW()"
        )
        for r in rows:
            rel = r["recording_path"]
            target = RECORDING_ROOT / rel
            try:
                if target.exists() and target.is_dir():
                    for f in target.glob("*.wav"):
                        f.unlink(missing_ok=True)
                    target.rmdir()
                # Mark purged whether the files were there or not.
                # A missing file means somebody (cleanup script, disk
                # rebuild) got there first — still flip the column so
                # we stop re-trying.
                await conn.execute(
                    "UPDATE calls SET recording_purged_at = NOW() WHERE id = $1",
                    int(r["id"]),
                )
                purged_n += 1
                freed_bytes += int(r["recording_size_bytes"] or 0)
            except Exception as e:  # noqa: BLE001
                failed_n += 1
                log.warning("recordings.purge_failed call=%s err=%s", r["id"], e)
                try:
                    await _ev.emit(
                        "recording.purge.error", severity="warning",
                        source="scheduler",
                        title=f"Recording purge failed for call #{r['id']}",
                        message=str(e)[:400],
                        agent_id=int(r["agent_id"]) if r["agent_id"] else None,
                        payload={"call_id": int(r["id"]), "path": rel},
                    )
                except Exception:  # noqa: BLE001
                    pass
    if purged_n > 0 or failed_n > 0:
        try:
            await _ev.emit(
                "recording.purged", severity="info", source="scheduler",
                title=f"Purged {purged_n} expired recording(s)",
                payload={
                    "purged_n": purged_n,
                    "failed_n": failed_n,
                    "freed_bytes": freed_bytes,
                    "retention_days": RECORDING_RETENTION_DAYS,
                },
            )
        except Exception:  # noqa: BLE001
            pass
    return {"purged_n": purged_n, "failed_n": failed_n, "freed_bytes": freed_bytes}


async def run_daily_recording_purge() -> None:
    """Scheduler entry point — matches the (no-args) shape `register()`
    in backend/scheduler.py expects."""
    await purge_expired()
