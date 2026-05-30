"""agents.small_talk — pre-loaded rapport phrases per saved agent

A new JSONB column on `agents` holding a short array of casual,
task-agnostic phrases the agent can lean on when a caller opens with
chitchat ("hi, how's your day?"). Distinct from the task-relevant
"Sample phrases" Eva already weaves into `system_prompt` — those are
business-specific ("Let me check that for you, one moment") whereas
small_talk is pure rapport ("How's your day going?", "Hope you're
keeping well.").

Why a dedicated column rather than nesting inside `policy` or
`variables`:
  • Independent PATCH surface on the dashboard ("Small talk" page),
    same shape as guardrails / outcomes / connectors.
  • Sector-defaulted at create time via silent_defaults.py — keeps
    the column rectangular and the runtime prompt builder simple.
  • Mirrors the pattern set by `purpose` (0008) and `outcomes` (baseline):
    JSONB NOT NULL with a structural default, so the read path never
    has to coalesce away a NULL.

Lifecycle:
  - Eva fills 2-4 phrases at save_agent time based on sector + locale.
  - Operator edits them on /agent/<slug>/small-talk (textarea, one per line).
  - Runtime prompt builder renders the list into the agent's
    A-STAR system prompt as "Small-talk phrases to lean on".

Revision ID: 0012_agent_small_talk
Revises: 0011_build_sessions
Create Date: 2026-05-14
"""
from __future__ import annotations

from alembic import op


revision = "0012_agent_small_talk"
down_revision = "0011_build_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE agents "
        "ADD COLUMN small_talk JSONB NOT NULL DEFAULT '[]'::jsonb"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS small_talk")
