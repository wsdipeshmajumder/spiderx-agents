"""agents.outcome_weights — operator-defined success weighting.

The Call outcomes report computes a weighted success rate using the
kind-level weights (success / qualified / info / failure). Defaults live in
backend/call_outcomes.py; this column lets a business override them so the
KPI matches THEIR definition of success (e.g. a sales-heavy team values
`qualified` at 0.7, a service desk wants `info` at 0.4).

Shape: { "success": 1.0, "qualified": 0.5, "info": 0.2, "failure": 0.0 }
Nullable on purpose: NULL = use the defaults, so every legacy agent stays
unchanged and the report still works.

Revision ID: 0018_agent_outcome_weights
Revises: 0017_agent_info_groups
Create Date: 2026-05-30
"""
from __future__ import annotations

from alembic import op


revision = "0018_agent_outcome_weights"
down_revision = "0017_agent_info_groups"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE agents ADD COLUMN outcome_weights JSONB")


def downgrade() -> None:
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS outcome_weights")
