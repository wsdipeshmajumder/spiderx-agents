"""llm_calls.kind — allow 'helper' alongside agent/builder/tts

The persistent Eva-helper sessions (run_helper_session) write rows with
kind='helper'. The CHECK constraint from 0007_llm_ledger only allowed
('agent', 'builder', 'tts') so every helper flush was failing the
INSERT silently — caught by the warn-only try/except in the bridge,
which meant helper-side LLM cost was invisible to the ledger.

This drops + re-adds the CHECK with 'helper' included. The other three
values are preserved. Dropping by name (we know it from 0007 — Postgres
named it llm_calls_kind_check by default).
"""
from __future__ import annotations

from alembic import op


revision = "0014_llm_kind_helper"
down_revision = "0013_build_session_eavesdrop"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Postgres names the table-level CHECK constraint
    # "<table>_<column>_check" by default. Defensive drop-by-pattern
    # (IF EXISTS) so an older build with a renamed constraint still
    # upgrades cleanly.
    op.execute("ALTER TABLE llm_calls DROP CONSTRAINT IF EXISTS llm_calls_kind_check")
    op.execute(
        "ALTER TABLE llm_calls "
        "ADD CONSTRAINT llm_calls_kind_check "
        "CHECK (kind IN ('agent', 'builder', 'tts', 'helper'))"
    )


def downgrade() -> None:
    # Reverting REQUIRES that no helper rows exist — otherwise the CHECK
    # add fails. We don't delete them; one-way ratchet.
    op.execute("ALTER TABLE llm_calls DROP CONSTRAINT IF EXISTS llm_calls_kind_check")
    op.execute(
        "ALTER TABLE llm_calls "
        "ADD CONSTRAINT llm_calls_kind_check "
        "CHECK (kind IN ('agent', 'builder', 'tts'))"
    )
