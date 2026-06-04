"""agents.outcome_overrides — per-agent edits to the resolved catalogue.

What this enables:

  • Operator renames "Test drive booked" → "Showroom appointment booked"
    because that's how their staff talks internally — no schema change.
  • Operator reclassifies "Quote sent" from `qualified` → `success`
    because in their world a sent quote IS the win.
  • Operator adds a totally custom outcome ("Insurance docs collected")
    that no sector catalogue ships out of the box.
  • Operator hides an outcome that doesn't apply to their business
    ("test_drive_booked" makes no sense for a dental clinic that
    inherited the wrong template).

Shape of the JSONB blob:

  {
    "edited": {
      "<outcome_id>": {
        "label":       "<new label>",
        "kind":        "success" | "qualified" | "info" | "failure",
        "description": "<new description>"
      }, ...
    },
    "added": [
      {
        "id":          "<custom_id>",   // unique, slug-shaped
        "label":       "<label>",
        "kind":        "success" | "qualified" | "info" | "failure",
        "description": "<description>"
      }, ...
    ],
    "removed": ["<outcome_id>", ...]
  }

Applied at read time by `call_outcomes.catalogue_for(agent)` —
`removed` filters first, then `edited` overlays per-field, then
`added` is appended. Idempotent; legacy agents without the column
default to `{}` and behave identically to build 212.

Revision ID: 0023_outcome_overrides
Revises: 0022_call_recordings
Create Date: 2026-06-04
"""
from __future__ import annotations

from alembic import op


revision = "0023_outcome_overrides"
down_revision = "0022_call_recordings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # JSONB rather than three separate columns — the three fields move
    # together (edit usually mentions removed siblings too, "added"
    # often takes a slot vacated by a "removed"). One blob, one PATCH.
    op.execute(
        "ALTER TABLE agents ADD COLUMN outcome_overrides JSONB NOT NULL DEFAULT '{}'::jsonb"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS outcome_overrides")
