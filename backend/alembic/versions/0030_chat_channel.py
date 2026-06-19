"""Chat channel add-on — org entitlements + a call `channel` discriminator.

Two columns for the optional paid chat-embed channel (Build 265):

  • orgs.entitlements JSONB — per-org add-on flags, e.g. {"chat_channel": true}.
    No entitlement system existed before (gating was plan.slug == 'free').
    Org-scoped so it survives the per-user → per-org billing roadmap and can
    hold future add-ons (sso, white_label, …). Gated like publishing (402).

  • calls.channel TEXT — which surface a conversation came in on:
    'web_voice' (browser mic), 'phone' (PSTN carrier), 'web_chat' (text embed).
    NULL on historical rows → the UI treats NULL as 'web_voice'. Lets analytics
    + Call Logs separate voice from chat now that one agent has both.

Revision ID: 0030_chat_channel
Revises: 0029_telephony_carriers
Create Date: 2026-06-19
"""
from __future__ import annotations

from alembic import op


revision = "0030_chat_channel"
down_revision = "0029_telephony_carriers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE orgs ADD COLUMN IF NOT EXISTS "
        "entitlements JSONB NOT NULL DEFAULT '{}'::jsonb"
    )
    op.execute("ALTER TABLE calls ADD COLUMN IF NOT EXISTS channel TEXT")
    # Partial index for the common 'chat calls for this agent' filter.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_calls_channel "
        "ON calls(agent_id, channel) WHERE channel IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_calls_channel")
    op.execute("ALTER TABLE calls DROP COLUMN IF EXISTS channel")
    op.execute("ALTER TABLE orgs DROP COLUMN IF EXISTS entitlements")
