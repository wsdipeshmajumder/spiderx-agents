"""build_sessions — transcript_log + extraction counters for the eavesdropper

The Eva-build interceptor (server-side fact extractor that runs on every
user turn_complete) needs two things from the build_sessions row:

  • a place to durably log the *full* user-side transcript so that even
    a WS-level drop (laptop sleep, wifi flap, page hide) can replay the
    whole dialogue — not just the four typed fact columns.
  • a small counter that records how many extraction passes have run.
    Useful for debugging "Eva is still re-asking X" reports — if the
    count is high and X is still missing, the extractor isn't catching
    the relevant phrasing; if the count is 0, the extractor never fired.

All the new "soft" slots (language, country, city, hours_text,
services_text, offers, email, website, escalation_phone, locale_hint,
voice_hint, ambience_hint, persona_hint, greeting_hint,
additional_jobs, mentioned_guardrails) live inside the existing
`extras` JSONB column rather than gaining typed columns. They're
transient (copied into `agents` at save_agent time) and the shape may
evolve; a rectangular table for transient slots would just churn.

Revision ID: 0013_build_session_eavesdrop
Revises: 0012_agent_small_talk
Create Date: 2026-05-18
"""
from __future__ import annotations

from alembic import op


revision = "0013_build_session_eavesdrop"
down_revision = "0012_agent_small_talk"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # transcript_log: ordered JSONB array of turn dicts. Shape:
    #   [{"role": "user"|"model", "text": "...", "ts": "ISO8601"}, ...]
    # Cap is enforced application-side (~80 turns); the DB column has no
    # length limit but we slice before persisting so the row stays small.
    op.execute(
        "ALTER TABLE build_sessions "
        "ADD COLUMN transcript_log JSONB NOT NULL DEFAULT '[]'::jsonb"
    )
    # Bumped on every successful extractor pass. Plain counter — no
    # need for a separate audit table.
    op.execute(
        "ALTER TABLE build_sessions "
        "ADD COLUMN extraction_count INTEGER NOT NULL DEFAULT 0"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE build_sessions DROP COLUMN IF EXISTS extraction_count")
    op.execute("ALTER TABLE build_sessions DROP COLUMN IF EXISTS transcript_log")
