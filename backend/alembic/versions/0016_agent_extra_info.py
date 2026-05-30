"""agents.extra_info — industry-adaptive Additional Info.

Each agent gets an `extra_info` JSONB map of {group_id: free_text},
where the group ids come from info_schemas.groups_for(sector). The
dashboard's Additional Info page edits it; the live-call prompt builder
folds the filled groups into a REFERENCE INFO section so the agent
answers callers with this business knowledge.

JSONB NOT NULL DEFAULT '{}' so readers never handle null and the
update path can merge with jsonb_set safely.

Revision ID: 0016_agent_extra_info
Revises: 0015_build_session_template
Create Date: 2026-05-22
"""
from __future__ import annotations

from alembic import op


revision = "0016_agent_extra_info"
down_revision = "0015_build_session_template"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE agents "
        "ADD COLUMN extra_info JSONB NOT NULL DEFAULT '{}'::jsonb"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS extra_info")
