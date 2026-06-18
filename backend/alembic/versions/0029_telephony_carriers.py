"""telephony_carriers — per-carrier auto-setup config (Twilio + Plivo side by side).

Previously the carrier auto-setup feature stored a SINGLE config on
`agents.sip_config` (+ a single `telephony_secret_enc` blob), so an agent
could only have ONE carrier configured at a time and the saved number
bled across both the Twilio and Plivo tabs. It also shared the column
with the Voniz SIP-trunk feature, which risked field collisions.

This adds `agents.telephony_carriers` JSONB — a map keyed by provider:

    {
      "twilio": {
        "number": "+1...", "alias": "...", "app_id": "...",
        "setup_mode": "auto"|"manual",
        "configured_at": "...", "last_verified_at": "...",
        "secret_enc": "<fernet-or-PLAIN: string>"   # per-carrier creds
      },
      "plivo": { ... }
    }

Each carrier is independent: both can be configured at once, and inbound
calls already route by the `/api/sip/{provider}/...` URL path, so both
numbers can be live simultaneously (the failover groundwork in CLAUDE.md).

Backfill: lift any existing TELEPHONY auto-setup config out of
`sip_config` into `telephony_carriers[provider]`, folding the
`telephony_secret_enc` bytes in as the per-carrier `secret_enc` string.
SIP-trunk configs (Voniz/Exotel — discriminated by `registrar`/`username`/
`inbound_uri`) are left untouched on `sip_config`. Rows that were purely
telephony auto-setup get their `sip_config` cleared so nothing stale lingers.

`telephony_secret_enc` is intentionally left in place (now unread by the
app) to keep this migration reversible; a later migration can drop it.

Revision ID: 0029_telephony_carriers
Revises: 0028_telephony_secret
Create Date: 2026-06-18
"""
from __future__ import annotations

from alembic import op


revision = "0029_telephony_carriers"
down_revision = "0028_telephony_secret"
branch_labels = None
depends_on = None


# Predicate identifying a row whose sip_config is a TELEPHONY auto-setup
# config (Twilio/Plivo) and NOT a SIP-trunk config.
_IS_TELEPHONY = """
    sip_config IS NOT NULL
    AND lower(sip_config->>'provider') IN ('twilio', 'plivo')
    AND (sip_config ? 'setup_mode' OR sip_config ? 'number' OR sip_config ? 'app_id')
    AND NOT (sip_config ? 'registrar' OR sip_config ? 'username' OR sip_config ? 'inbound_uri')
"""


def upgrade() -> None:
    op.execute(
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS "
        "telephony_carriers JSONB NOT NULL DEFAULT '{}'::jsonb"
    )

    # Lift telephony auto-setup config + creds into the per-carrier map.
    op.execute(f"""
        UPDATE agents
        SET telephony_carriers = jsonb_build_object(
            lower(sip_config->>'provider'),
            jsonb_strip_nulls(jsonb_build_object(
                'number',           sip_config->>'number',
                'alias',            sip_config->>'alias',
                'app_id',           sip_config->>'app_id',
                'setup_mode',       sip_config->>'setup_mode',
                'configured_at',    sip_config->>'configured_at',
                'last_verified_at', sip_config->>'last_verified_at'
            ))
            || CASE
                 WHEN telephony_secret_enc IS NOT NULL
                 THEN jsonb_build_object('secret_enc', convert_from(telephony_secret_enc, 'UTF8'))
                 ELSE '{{}}'::jsonb
               END
        )
        WHERE {_IS_TELEPHONY}
    """)

    # Clear sip_config for rows that were purely telephony auto-setup
    # (the predicate already excludes SIP-trunk rows).
    op.execute(f"UPDATE agents SET sip_config = NULL WHERE {_IS_TELEPHONY}")


def downgrade() -> None:
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS telephony_carriers")
