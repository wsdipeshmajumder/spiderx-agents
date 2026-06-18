"""telephony_secret_enc — Fernet-encrypted carrier credentials.

Adds a single BYTEA column on `agents` that stores Plivo / Twilio /
Exotel Auth Token (the bearer-style credential the carrier expects on
every API call). Encrypted at rest with a Fernet key sourced from the
TELEPHONY_CRED_KEY env var (see backend/telephony/secrets.py).

The unencrypted, non-secret half of the carrier config (Auth ID,
Application id, bound number, alias, etc.) continues to live on
`agents.sip_config` JSONB, discriminated by `provider`. Same column
that already holds Voniz SIP-trunk config — `provider` discriminates.

Revision ID: 0028_telephony_secret
Revises: 0027_eva_assist_flag
Create Date: 2026-06-11
"""
from __future__ import annotations

from alembic import op


revision = "0028_telephony_secret"
down_revision = "0027_eva_assist_flag"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS telephony_secret_enc BYTEA"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS telephony_secret_enc")
