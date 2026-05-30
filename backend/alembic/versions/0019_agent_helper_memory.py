"""agent_helper_memory — persistent Eva-helper memory per (user, agent).

The floating Ask-Eva widget today rebuilds its system prompt from scratch on
every (re)connect. The operator has to re-explain context every time they
reopen, and the conversation from build-time is lost. This table stores a
running, capped log of Ask-Eva turns plus a condensed summary keyed on
(user, agent), so every reopen restores everything Eva already knows.

  • `turns`    — most-recent ~40 {role, text, ts} turns as JSONB. Older
                 turns get folded into `summary` by the helper bridge.
  • `summary`  — a running prose summary (best-model-condensed) of older
                 turns + the build-time context (sector / locale / purpose
                 / business name) that the agent was created with.

Rows are upserted on (user_id, agent_id). Per-user (not per-org) because
two operators in the same org may have distinct task histories with the
same agent.

Revision ID: 0019_agent_helper_memory
Revises: 0018_agent_outcome_weights
Create Date: 2026-05-30
"""
from __future__ import annotations

from alembic import op


revision = "0019_agent_helper_memory"
down_revision = "0018_agent_outcome_weights"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS agent_helper_memory (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            agent_id BIGINT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
            turns JSONB NOT NULL DEFAULT '[]'::jsonb,
            summary TEXT NOT NULL DEFAULT '',
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (user_id, agent_id)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_agent_helper_memory_agent ON agent_helper_memory (agent_id)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_agent_helper_memory_agent")
    op.execute("DROP TABLE IF EXISTS agent_helper_memory")
