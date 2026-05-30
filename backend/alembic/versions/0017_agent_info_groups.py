"""agents.info_groups — per-agent Additional Info schema (catch-all).

Template agents resolve their Additional Info field groups statically from
info_schemas.INFO_GROUPS_BY_SECTOR (keyed by sector). Catch-all / dynamic
agents (built for an arbitrary use case via the best model) instead carry
their OWN generated group schema here, so the dashboard editor and the
live-call REFERENCE INFO section adapt to ANY use case.

Nullable on purpose: NULL means "fall back to the sector's static groups"
(every template/legacy agent), so this is fully backward compatible. When
present it's a JSON array of {id, label, emoji, desc, info_only} groups —
the same shape info_schemas._g() produces.

Revision ID: 0017_agent_info_groups
Revises: 0016_agent_extra_info
Create Date: 2026-05-29
"""
from __future__ import annotations

from alembic import op


revision = "0017_agent_info_groups"
down_revision = "0016_agent_extra_info"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE agents ADD COLUMN info_groups JSONB")


def downgrade() -> None:
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS info_groups")
