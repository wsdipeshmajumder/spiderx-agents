"""agents.chip_overrides — per-agent customisation of the tag-chip schema.

Build 217 shipped a frontend-hardcoded SECTOR_CHIP_SCHEMA. Build 218
added value-based inference to make it adaptive. This build (219)
moves the schema to a backend registry — the SAME registry that the
chip UI reads from and that Eva's end_call extraction prompt reads
from — so the vocabulary the LLM captures consistently matches the
vocabulary the dashboard renders. Drift between calls disappears
because both sides quote the same schema.

`agents.chip_overrides` lets operators customise this per-agent:
  {
    "added":   [ { field, category, label, description? }, ... ],
    "edited":  { "<field>": { category?, label?, description? }, ... },
    "removed": [ "<field>", ... ]
  }

`added` — extra fields beyond the sector defaults the operator wants
captured + chipped (e.g. "loyalty_tier", "referral_source").
`edited` — rename the label or change the colour-category on a
sector default ("party_size" → "Group size").
`removed` — hide a sector default that doesn't apply ("baby_seat" on
a child-free restaurant).

Defaults `'{}'` so existing agents keep behaving exactly as before.

Revision ID: 0025_chip_overrides
Revises: 0024_digest_settings
Create Date: 2026-06-04
"""
from __future__ import annotations

from alembic import op


revision = "0025_chip_overrides"
down_revision = "0024_digest_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE agents ADD COLUMN chip_overrides JSONB NOT NULL DEFAULT '{}'::jsonb"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS chip_overrides")
