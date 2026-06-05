"""features.eva_assist — feature flag for the Ask-Eva floating helper.

When the platform_settings row for `features.eva_assist` is set to
false, the SPA hides the floating Eva bubble + card on every page.
Default is true so behaviour for fresh installs is unchanged.

Revision ID: 0027_eva_assist_flag
Revises: 0026_healthcheck_settings
Create Date: 2026-06-05
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0027_eva_assist_flag"
down_revision = "0026_healthcheck_settings"
branch_labels = None
depends_on = None


_KEY = "features.eva_assist"
_LABEL = "Ask Eva — in-app helper"
_DESCRIPTION = (
    "Toggles the floating Eva assistant bubble that overlays the dashboard. "
    "Disable for tenants that don't want the helper. Default ON. "
    "Changes take effect on the next page load."
)


def upgrade() -> None:
    bind = op.get_bind()
    stmt = sa.text(
        "INSERT INTO platform_settings (key, value, category, label, description) "
        "VALUES (:key, CAST(:value AS jsonb), :category, :label, :description) "
        "ON CONFLICT (key) DO NOTHING"
    )
    bind.execute(stmt, {
        "key": _KEY,
        "value": "true",
        "category": "features",
        "label": _LABEL,
        "description": _DESCRIPTION,
    })


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("DELETE FROM platform_settings WHERE key = :key"), {"key": _KEY})
