"""Engine-aware cost ledger: stamp the voice engine per call (Build 379).

A call now runs on one of two voice engines — "Standard" (Gemini native audio)
or "Pro" (Gemini brain + Fish TTS). The choice lived only in
`agents.voice_tweaks` (mutable, live-read at call time) and never reached the
call/ledger rows, so the super-admin ledger couldn't tell Standard spend from
Pro spend after the fact. This adds a point-in-time `voice_provider` stamp on
both `calls` and `llm_calls` so the LLM ledger can segment by engine.

Cost is unchanged: Pro's full Gemini compute is already metered (it runs a real
Gemini Live session; only the outbound audio is swapped for Fish), and Fish's
live model is the free `s2.1-pro-free` tier (₹0). We also seed a ₹0 Fish row in
`pricing_versions` so there's a dimension to attribute — and to roll a real rate
forward against — when the free tier ends (2026-08-31). Detect-only: this seeds
a zero baseline; any future non-zero Fish rate goes through the audited Pricing
tab roll-forward, and historical `cost_paise` is never re-priced.

Revision ID: 0034_voice_provider
Revises: 0033_caller_number
Create Date: 2026-08-11
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0034_voice_provider"
down_revision = "0033_caller_number"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Point-in-time engine stamp. NULL = legacy/unknown (pre-379 rows, and
    # web-voice test calls that don't run the telephony Fish path).
    op.execute("ALTER TABLE calls ADD COLUMN IF NOT EXISTS voice_provider TEXT")
    op.execute("ALTER TABLE llm_calls ADD COLUMN IF NOT EXISTS voice_provider TEXT")
    # Index the ledger column so the by-engine aggregation stays cheap.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_llm_calls_engine "
        "ON llm_calls(voice_provider) WHERE voice_provider IS NOT NULL"
    )

    # ─── Seed a ₹0 Fish (Pro voice) pricing dimension ────────────────────
    # Free tier today; a real rate is rolled forward via the Pricing tab when
    # the free window closes. effective_from far in the past so any historical
    # lookup resolves it. Guarded so re-running the migration is idempotent.
    conn = op.get_bind()
    exists = conn.execute(sa.text(
        "SELECT 1 FROM pricing_versions "
        "WHERE provider = 'fish' AND rate_kind = 'tts.pro.voice' "
        "AND effective_to IS NULL LIMIT 1"
    )).first()
    if not exists:
        conn.execute(sa.text(
            "INSERT INTO pricing_versions "
            "(provider, rate_kind, model_id, unit, usd_per_unit, "
            " inr_per_unit, effective_from, note) "
            "VALUES ('fish', 'tts.pro.voice', 's2.1-pro-free', 'per_1m_chars', "
            " 0, 0, :ef, :n)"
        ), {
            "ef": "2024-01-01 00:00:00+00",
            "n": "Fish Pro voice — free s2.1-pro-free tier (₹0) through "
                 "2026-08-31; roll a real rate forward via the Pricing tab "
                 "when the free window ends.",
        })


def downgrade() -> None:
    op.execute("DELETE FROM pricing_versions "
               "WHERE provider = 'fish' AND rate_kind = 'tts.pro.voice'")
    op.execute("DROP INDEX IF EXISTS idx_llm_calls_engine")
    op.execute("ALTER TABLE llm_calls DROP COLUMN IF EXISTS voice_provider")
    op.execute("ALTER TABLE calls DROP COLUMN IF EXISTS voice_provider")
