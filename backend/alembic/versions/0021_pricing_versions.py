"""pricing_versions — audit-tracked rate history for LLM + telephony.

See module docstring for rationale; the table is the audit-trail layer
on top of the build-197 pricing.py constants + build-198 watchdog.

Revision ID: 0021_pricing_versions
Revises: 0020_events
Create Date: 2026-06-04
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0021_pricing_versions"
down_revision = "0020_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE pricing_versions (
        id              BIGSERIAL PRIMARY KEY,
        provider        TEXT NOT NULL,
        rate_kind       TEXT NOT NULL,
        model_id        TEXT,
        unit            TEXT NOT NULL,
        usd_per_unit    NUMERIC(14, 6),
        inr_per_unit    NUMERIC(14, 6),
        effective_from  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        effective_to    TIMESTAMPTZ,
        rolled_by       BIGINT REFERENCES users(id) ON DELETE SET NULL,
        note            TEXT,
        observed_event_id BIGINT REFERENCES events(id) ON DELETE SET NULL,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """)
    op.execute(
        "CREATE INDEX idx_pricing_versions_lookup "
        "ON pricing_versions(provider, rate_kind, effective_from DESC)"
    )
    op.execute(
        "CREATE INDEX idx_pricing_versions_current "
        "ON pricing_versions(provider, rate_kind) "
        "WHERE effective_to IS NULL"
    )

    # ─── Seed initial rates from pricing.py constants ─────────────────
    # Effective_from is set far in the past so any historical call lookup
    # at any call.started_at finds this row. Once a human rolls a new
    # rate forward, this row's effective_to fills in.
    FX = 83.5
    rows = [
        # Gemini Live audio (build 197 refresh)
        ("gemini", "llm.audio.in",  "gemini-3.1-flash-live-preview",        "per_1m_tokens", 3.00, None),
        ("gemini", "llm.audio.out", "gemini-3.1-flash-live-preview",        "per_1m_tokens", 12.00, None),
        ("gemini", "llm.audio.in",  "gemini-2.5-flash-native-audio-latest", "per_1m_tokens", 3.00, None),
        ("gemini", "llm.audio.out", "gemini-2.5-flash-native-audio-latest", "per_1m_tokens", 12.00, None),
        ("gemini", "llm.text.in",   "gemini-2.5-flash-preview-tts",         "per_1m_tokens", 0.075, None),
        ("gemini", "llm.text.out",  "gemini-2.5-flash-preview-tts",         "per_1m_tokens", 0.30, None),
        # Telephony (build 198 reference snapshot)
        ("plivo",  "pstn.outbound.mobile",   None, "per_min",   None, 0.60),
        ("plivo",  "pstn.inbound.local",     None, "per_min",   None, 0.60),
        ("plivo",  "did.local.monthly",      None, "per_month", None, 250.0),
        ("twilio", "pstn.outbound.mobile",   None, "per_min",   0.0496, None),
        ("twilio", "pstn.outbound.landline", None, "per_min",   0.0699, None),
        ("twilio", "did.intl.monthly",       None, "per_month", 1.15, None),
    ]
    conn = op.get_bind()
    sql = sa.text(
        "INSERT INTO pricing_versions "
        "(provider, rate_kind, model_id, unit, usd_per_unit, "
        " inr_per_unit, effective_from, note) "
        "VALUES (:p, :k, :m, :u, :usd, :inr, :ef, :n)"
    )
    for provider, kind, model, unit, usd, inr in rows:
        usd_final = usd if usd is not None else (inr / FX if inr else None)
        inr_final = inr if inr is not None else (usd * FX if usd else None)
        conn.execute(sql, {
            "p": provider, "k": kind, "m": model, "u": unit,
            "usd": usd_final, "inr": inr_final,
            "ef": "2024-01-01 00:00:00+00",
            "n": "Seeded from pricing.py constants at migration time",
        })


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS pricing_versions")
