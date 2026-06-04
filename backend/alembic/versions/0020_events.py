"""events — uniform event ledger for platform observability.

Every noteworthy thing that happens on the platform writes a row here:
agent lifecycle (created / published / deleted), call outcomes,
scheduler runs, anomaly detections, pricing observations + drift,
delivery audit (email / WhatsApp / Slack), system health. The single
write path is `backend.events.emit()`; the single read path is the
`/api/admin/events` endpoint, surfaced on the Observability page.

Severity ladder (string column, not enum — adding a new level later
shouldn't require a migration):
  - info     — normal lifecycle. Faded out after a week in the UI.
  - warning  — soft anomaly. Stays sticky until resolved.
  - error    — degraded service. Page-level banner.
  - critical — wholesale change or hard threshold crossed. Email blast.

`dedupe_key` lets a job fire on every wake but only persist the first
distinct logical occurrence (e.g. one "drift detected for Gemini today"
row instead of 24 hourly ones).

Indices are partial on the not-null filters because most queries are
"open warnings/errors" or "events for this agent" — partial indices
keep the working set tight even at hundreds of thousands of rows.

Revision ID: 0020_events
Revises: 0019_agent_helper_memory
Create Date: 2026-06-04
"""
from __future__ import annotations

from alembic import op


revision = "0020_events"
down_revision = "0019_agent_helper_memory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE events (
        id           BIGSERIAL PRIMARY KEY,
        kind         TEXT    NOT NULL,
        severity     TEXT    NOT NULL DEFAULT 'info',
        source       TEXT    NOT NULL DEFAULT 'system',
        org_id       BIGINT  REFERENCES orgs(id)   ON DELETE SET NULL,
        agent_id     BIGINT  REFERENCES agents(id) ON DELETE SET NULL,
        user_id      BIGINT  REFERENCES users(id)  ON DELETE SET NULL,
        title        TEXT    NOT NULL,
        message      TEXT,
        payload      JSONB   NOT NULL DEFAULT '{}'::jsonb,
        dedupe_key   TEXT    UNIQUE,
        created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        resolved_at  TIMESTAMPTZ,
        resolved_by  BIGINT  REFERENCES users(id) ON DELETE SET NULL
    )
    """)
    op.execute("CREATE INDEX idx_events_kind_created ON events(kind, created_at DESC)")
    op.execute(
        "CREATE INDEX idx_events_severity_open ON events(severity, created_at DESC) "
        "WHERE resolved_at IS NULL"
    )
    op.execute(
        "CREATE INDEX idx_events_org_created ON events(org_id, created_at DESC) "
        "WHERE org_id IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX idx_events_agent_created ON events(agent_id, created_at DESC) "
        "WHERE agent_id IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS events")
