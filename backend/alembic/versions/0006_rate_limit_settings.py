"""Rate-limit settings + audit_log retention

Phase 6 ops knobs. Two more rows in platform_settings — capacity +
window for the per-org rate limiter (backend/ratelimit.py).

Idempotent: ON CONFLICT DO NOTHING so re-running on a populated DB
doesn't trip the PK.

Revision ID: 0006_rate_limit_settings
Revises: 0005_analytics_rollups
Create Date: 2026-05-14
"""
from __future__ import annotations

from alembic import op


revision = "0006_rate_limit_settings"
down_revision = "0005_analytics_rollups"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    INSERT INTO platform_settings (key, value, category, label, description) VALUES
      ('limits.rate_capacity',
       '60'::jsonb,
       'limits', 'Rate-limit capacity',
       'Requests an org can burst within one window before getting 429s.'),
      ('limits.rate_window_s',
       '60'::jsonb,
       'limits', 'Rate-limit window (seconds)',
       'Window over which the rate-limit capacity refills linearly.')
    ON CONFLICT (key) DO NOTHING
    """)


def downgrade() -> None:
    op.execute("DELETE FROM platform_settings WHERE key IN ('limits.rate_capacity','limits.rate_window_s')")
