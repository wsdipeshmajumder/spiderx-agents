"""agent-healthcheck platform settings rows.

set_platform_setting() only UPDATEs existing keys (design — operators
can't add new keys via the dashboard, prevents typos from creating
ghost rows). So new healthcheck config keys land via this migration.
The static defaults in backend/settings.py mirror these so the code
works even if this migration hasn't run yet (last-known-good).

Revision ID: 0026_healthcheck_settings
Revises: 0025_chip_overrides
Create Date: 2026-06-05
"""
from __future__ import annotations

import json
from alembic import op
import sqlalchemy as sa


revision = "0026_healthcheck_settings"
down_revision = "0025_chip_overrides"
branch_labels = None
depends_on = None


_SETTINGS = [
    # Level 2 — hourly handshake probe (shipped build 229).
    ("healthcheck.level2_enabled", "true", "healthcheck",
     "Hourly handshake probe",
     "Per-agent WS handshake every hour at :05. No Gemini cost. Catches DB / WS / agent-config breakage."),
    # Level 3 — daily full conversational probe (build 231).
    ("healthcheck.level3_enabled", "false", "healthcheck",
     "Daily full-conversational probe",
     "Opens a real Gemini Live session per agent, exercises greeting + caller utterance + response. ~$0.002 per probe. OFF by default — opt in once Gemini cost is acceptable."),
    ("healthcheck.level3_sample_size", "25", "healthcheck",
     "Daily probe sample size",
     "When > 0, randomly sample N published agents per daily run instead of probing every one. 0 = probe all. Caps total Gemini spend."),
    # Email-on-failure.
    ("healthcheck.email_on_failure", "true", "healthcheck",
     "Email on healthcheck failure",
     "Send an alert email when any probe (Level 2/3/4) fails. Uses REPORT_EMAIL_TO unless email_recipients is set."),
    ("healthcheck.email_recipients", "\"\"", "healthcheck",
     "Alert recipients (override)",
     "Comma-separated email addresses. Empty falls back to the REPORT_EMAIL_TO env var. Useful for routing healthcheck alerts to a different channel than call reports."),
    # Level 4 — real PSTN probe (placeholder — not yet implemented).
    ("healthcheck.level4_pstn_enabled", "false", "healthcheck",
     "Real PSTN probe (coming soon)",
     "Periodically place a real phone call from one number to another to verify the end-to-end telephony path. Disabled by default; the configuration fields below are wired but the probe itself is stubbed pending Twilio outbound integration."),
    ("healthcheck.level4_pstn_provider", "\"twilio\"", "healthcheck",
     "PSTN provider",
     "Which telephony provider executes the outbound test call. Today only 'twilio' is supported; Plivo follows once their outbound API is wired."),
    ("healthcheck.level4_pstn_from_number", "\"\"", "healthcheck",
     "Test FROM number (E.164)",
     "Owned number that places the test call. Must be enabled on the configured provider. E.g. +14155551234."),
    ("healthcheck.level4_pstn_to_number", "\"\"", "healthcheck",
     "Test TO number (E.164)",
     "The agent's PSTN-routed number to dial. The probe asserts the call connected and that the agent answered with audio."),
]


def upgrade() -> None:
    # Use UPSERT-ish pattern: insert if missing, skip if already there.
    # ON CONFLICT (key) DO NOTHING — an admin who already typed a value
    # before this migration ran keeps their value. Bind params instead
    # of f-string interpolation: descriptions contain colons (":05")
    # which SQLAlchemy would otherwise treat as parameter markers.
    bind = op.get_bind()
    stmt = sa.text(
        "INSERT INTO platform_settings (key, value, category, label, description) "
        "VALUES (:key, CAST(:value AS jsonb), :category, :label, :description) "
        "ON CONFLICT (key) DO NOTHING"
    )
    for key, value_json, category, label, description in _SETTINGS:
        bind.execute(stmt, {
            "key": key, "value": value_json, "category": category,
            "label": label, "description": description,
        })


def downgrade() -> None:
    bind = op.get_bind()
    stmt = sa.text("DELETE FROM platform_settings WHERE key = :key")
    for key, *_ in _SETTINGS:
        bind.execute(stmt, {"key": key})
