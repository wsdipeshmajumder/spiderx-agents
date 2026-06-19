"""chat_settings — per-agent chat-widget appearance (Build 272).

Channel-specific PRESENTATION for the chat embed (the agent's behaviour/brain
stays shared across voice/phone/chat — only the look is per-channel). Shape:

    {
      "accent_color": "#4f46e5",     # bubbles + send button + avatar
      "avatar_url":   "https://…",   # header logo/avatar (falls back to initial)
      "launcher_text":"Chat with Kavya",
      "welcome_message":"Hi! Ask me anything about our cars."
    }

NOT NULL DEFAULT '{}' so the embed always has a value to read; empty → defaults.

Revision ID: 0031_chat_settings
Revises: 0030_chat_channel
Create Date: 2026-06-19
"""
from __future__ import annotations

from alembic import op


revision = "0031_chat_settings"
down_revision = "0030_chat_channel"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS "
        "chat_settings JSONB NOT NULL DEFAULT '{}'::jsonb"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS chat_settings")
