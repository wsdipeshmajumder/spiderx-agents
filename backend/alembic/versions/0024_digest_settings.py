"""agents.digest_settings — per-agent outcome digest schedule.

Today the EOD digest is one-size-fits-all: every agent that had calls
gets included in the org's 19:00 IST email, covering the last 24 h.
Operators told us that's the wrong default for half their agents — a
high-volume car dealer wants a daily 24 h email, but the SaaS-support
agent doing 3 calls/week wants a weekly 7-day rollup, and the back-
office voicemail catcher wants no digest at all.

This migration lets each agent carry its own schedule. JSONB blob
shape (all keys optional; effective_settings() in code fills defaults):

  {
    "cadence":      "daily" | "weekly" | "monthly" | "off",
    "window_days":  1 | 7 | 30,             // what range to summarise
    "day_of_week":  0..6 (Mon=0),           // for weekly
    "day_of_month": 1..28                    // for monthly
  }

Recipients are NOT in the blob (yet) — for v1 we use the org owners,
matching today's behaviour. A future build can add `extra_recipients`
when the demand for it is concrete.

Revision ID: 0024_digest_settings
Revises: 0023_outcome_overrides
Create Date: 2026-06-04
"""
from __future__ import annotations

from alembic import op


revision = "0024_digest_settings"
down_revision = "0023_outcome_overrides"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE agents ADD COLUMN digest_settings JSONB NOT NULL DEFAULT '{}'::jsonb"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS digest_settings")
