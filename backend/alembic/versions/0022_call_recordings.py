"""call recordings + 180-day retention + per-agent disclosure toggle.

Adds the columns the platform needs to PERSIST a call's audio to disk,
track WHEN it must be purged (180-day default retention) and remember
whether the agent VERBALLY disclosed the recording at call start. The
actual writer lives in `backend/recordings.py`; this migration is just
the schema scaffold + sane defaults.

Why one migration for both `calls` and `agents`:
  - The two columns on `agents` (`recording_enabled`, `recording_disclosed`)
    gate whether the writer fires + whether the system-prompt block
    injects the legally-mandated "this call may be recorded" notice.
  - We want them defaulted ON at row-creation so EVERY existing agent
    flips on recording the moment the feature ships — no per-agent
    backfill required.
  - The columns on `calls` are the audit-trail of the file (path,
    size, started/expires/purged timestamps, format). Stored even
    when the file is later purged so analytics can answer "did we
    ever have a recording for this call?".

Revision ID: 0022_call_recordings
Revises: 0021_pricing_versions
Create Date: 2026-06-04
"""
from __future__ import annotations

from alembic import op


revision = "0022_call_recordings"
down_revision = "0021_pricing_versions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ─── calls.recording_* — per-call audit trail of the audio file ───────
    # All nullable: a call can land WITHOUT a recording (recording_enabled
    # was off on the agent, the writer failed, etc.). The presence of a
    # non-NULL `recording_path` is the single source of truth for "we
    # have audio on disk". `recording_purged_at` flips to non-NULL once
    # the daily retention job has unlinked the file; `recording_path`
    # stays so we can still report "had a recording, purged on Y".
    op.execute("ALTER TABLE calls ADD COLUMN recording_path        TEXT")
    op.execute("ALTER TABLE calls ADD COLUMN recording_format      TEXT")
    op.execute("ALTER TABLE calls ADD COLUMN recording_size_bytes  BIGINT")
    op.execute("ALTER TABLE calls ADD COLUMN recording_started_at  TIMESTAMPTZ")
    op.execute("ALTER TABLE calls ADD COLUMN recording_expires_at  TIMESTAMPTZ")
    op.execute("ALTER TABLE calls ADD COLUMN recording_purged_at   TIMESTAMPTZ")

    # Index the cleanup-job's hot path. The daily purge scans for
    # `recording_purged_at IS NULL AND recording_expires_at < NOW()`.
    # Partial index is the right fit — once a row is purged it stops
    # being interesting to this query.
    op.execute(
        "CREATE INDEX idx_calls_recording_purge "
        "ON calls (recording_expires_at) "
        "WHERE recording_purged_at IS NULL AND recording_path IS NOT NULL"
    )

    # ─── agents.recording_enabled + agents.recording_disclosed ────────────
    # Default TRUE so the feature is on for every existing agent the
    # moment this lands. Operators can opt OUT per-agent via the
    # dashboard (Compliance card on the settings page).
    op.execute(
        "ALTER TABLE agents ADD COLUMN recording_enabled "
        "BOOLEAN NOT NULL DEFAULT TRUE"
    )
    op.execute(
        "ALTER TABLE agents ADD COLUMN recording_disclosed "
        "BOOLEAN NOT NULL DEFAULT TRUE"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_calls_recording_purge")
    op.execute("ALTER TABLE calls DROP COLUMN IF EXISTS recording_purged_at")
    op.execute("ALTER TABLE calls DROP COLUMN IF EXISTS recording_expires_at")
    op.execute("ALTER TABLE calls DROP COLUMN IF EXISTS recording_started_at")
    op.execute("ALTER TABLE calls DROP COLUMN IF EXISTS recording_size_bytes")
    op.execute("ALTER TABLE calls DROP COLUMN IF EXISTS recording_format")
    op.execute("ALTER TABLE calls DROP COLUMN IF EXISTS recording_path")
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS recording_disclosed")
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS recording_enabled")
