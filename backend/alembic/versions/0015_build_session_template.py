"""build_sessions — template_id + template_answers for the deterministic
interview flow.

Background: Eva builds were probabilistic — the LLM decided what to ask
based on a 30-KB system prompt. New flow loads a per-(industry × locale
× city) YAML template from backend/build_templates/ and walks its
question list deterministically. We need to persist two new bits of
state per build_session so a Gemini drop / WS-level reload / force-
commit watchdog can resume the interview without losing position:

  • template_id        — the resolved template (e.g.
                          'automotive.dealership.en-IN.kolkata'). NULL
                          for builds that fell back to the probabilistic
                          flow (no template matched).
  • template_answers   — JSONB map of {question_id: answered_value}.
                          Populated incrementally as Eva walks the
                          template. On reconnect we read this back to
                          know which question to ask NEXT (the first
                          question whose id isn't a key here).

Revision ID: 0015_build_session_template
Revises: 0014_llm_kind_helper
Create Date: 2026-05-20
"""
from __future__ import annotations

from alembic import op


revision = "0015_build_session_template"
down_revision = "0014_llm_kind_helper"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE build_sessions "
        "ADD COLUMN template_id TEXT NULL"
    )
    # JSONB so the existing build_session read path naturally returns
    # a python dict. NOT NULL with empty-object default means readers
    # never need to handle null.
    op.execute(
        "ALTER TABLE build_sessions "
        "ADD COLUMN template_answers JSONB NOT NULL DEFAULT '{}'::jsonb"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE build_sessions DROP COLUMN IF EXISTS template_answers")
    op.execute("ALTER TABLE build_sessions DROP COLUMN IF EXISTS template_id")
