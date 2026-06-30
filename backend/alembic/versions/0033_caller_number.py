"""calls.caller_number — the other party's phone number (Build 309).

For an inbound phone call this is the carrier-reported `From` (the customer who
rang in); captured at the Answer webhook and threaded onto the media WS so it
lands on the call row. NULL for web/embed test calls (no PSTN leg) and for any
call where the carrier didn't supply it — surfaced in the Call log + CSV so the
operator can see who called and what each call cost.

Revision ID: 0033_caller_number
Revises: 0032_visitor_key
Create Date: 2026-06-30
"""
from __future__ import annotations

from alembic import op


revision = "0033_caller_number"
down_revision = "0032_visitor_key"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE calls ADD COLUMN IF NOT EXISTS caller_number TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE calls DROP COLUMN IF EXISTS caller_number")
