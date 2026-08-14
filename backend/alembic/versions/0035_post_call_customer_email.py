"""Post-call email to customer organization (Build 422).

Add a configurable email address field to agents so post-call summaries can be
sent to the customer's organization (in addition to the internal operator
notifications). The email is customized per agent sector/industry and includes
extracted data, call recording link, sentiment, and next actions. CC'd to
devteam@spiderx.ai and dipesh.majumder@webspiders.com for ops visibility.

Revision ID: 0035_post_call_customer_email
Revises: 0034_voice_provider
Create Date: 2026-08-14
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0035_post_call_customer_email"
down_revision = "0034_voice_provider"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Customer organization email(s) where post-call summaries are sent.
    # Can be a single email or comma-separated list. NULL = feature disabled
    # for this agent (no email to customer org; internal notifications still
    # go to org owners as before).
    op.execute(
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS post_call_email_to TEXT"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS post_call_email_to")
