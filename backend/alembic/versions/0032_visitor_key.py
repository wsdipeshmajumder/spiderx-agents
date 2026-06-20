"""calls.visitor_key — cross-session recall key (Build 284).

A deterministic per-person key derived from a call's captured contact detail
(normalised phone last-10 or lowercased email), so a later chat/call for the
SAME person can be linked WITHOUT any anonymous tracking — the link only exists
once the visitor identifies themselves. Used by identifier-based recall
("welcome back, we already have your details"). Spans channels: phone calls get
a key too, so a returning caller is recognised in chat and vice-versa.

NULL when no contact detail was captured. Partial index for the recall lookup.

Revision ID: 0032_visitor_key
Revises: 0031_chat_settings
Create Date: 2026-06-19
"""
from __future__ import annotations

from alembic import op


revision = "0032_visitor_key"
down_revision = "0031_chat_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE calls ADD COLUMN IF NOT EXISTS visitor_key TEXT")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_calls_visitor "
        "ON calls(agent_id, visitor_key) WHERE visitor_key IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_calls_visitor")
    op.execute("ALTER TABLE calls DROP COLUMN IF EXISTS visitor_key")
