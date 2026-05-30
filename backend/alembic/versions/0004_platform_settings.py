"""platform_settings

Phase 4 — runtime-editable config. Replaces hardcoded constants scattered
across the codebase with a single key/value store that super-admins can
edit from the UI without a deploy.

The table is intentionally narrow:
  - `key`         dotted-path identifier ('models.builder_model_id')
  - `value`       JSONB — strings, numbers, booleans, arrays all work
  - `category`    UI grouping ('models' | 'limits' | 'features' | 'branding')
  - `label`       human-readable name shown in the settings panel
  - `description` one-line help text
  - `updated_*`   audit trail; the audit_log table catches the diff

We seed sensible defaults so a fresh deploy works without any
super-admin pre-config. Callers in app code read via `settings.get(key,
default)` — the default is the safety net if a key gets deleted.

Revision ID: 0004_platform_settings
Revises: 0003_super_admin
Create Date: 2026-05-14
"""
from __future__ import annotations

from alembic import op


revision = "0004_platform_settings"
down_revision = "0003_super_admin"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE platform_settings (
      key         TEXT PRIMARY KEY,
      value       JSONB NOT NULL,
      category    TEXT NOT NULL,
      label       TEXT NOT NULL,
      description TEXT,
      updated_by  BIGINT REFERENCES users(id) ON DELETE SET NULL,
      updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """)
    op.execute("CREATE INDEX idx_platform_settings_category ON platform_settings(category)")

    # ── Seed sensible defaults ─────────────────────────────────────────
    # Values are JSONB so quoted strings stay strings, numbers stay numbers.
    # Keep in sync with `settings._DEFAULTS` in backend/settings.py — that
    # table is the runtime safety net if a row is ever deleted.
    op.execute("""
    INSERT INTO platform_settings (key, value, category, label, description) VALUES
      -- models
      ('models.builder_model_id',
       '"gemini-3.1-flash-live-preview"'::jsonb,
       'models', 'Eva builder model',
       'Gemini Live model used for the Eva builder conversation.'),
      ('models.agent_model_id',
       '"gemini-3.1-flash-live-preview"'::jsonb,
       'models', 'Saved-agent model',
       'Gemini Live model used for live calls with a saved phone-AI agent.'),
      ('models.tts_preview_model',
       '"gemini-2.5-flash-preview-tts"'::jsonb,
       'models', 'TTS preview model',
       'One-shot model for the Voice Picker preview clips.'),
      -- limits
      ('limits.free_minutes_per_month',
       '30'::jsonb,
       'limits', 'Free-plan minutes / month',
       'Total voice minutes allotted to the Free tier each month.'),
      ('limits.max_agents_free',
       '3'::jsonb,
       'limits', 'Free-plan agent cap',
       'Maximum number of draft agents a Free-plan user can build.'),
      ('limits.max_kb_chars',
       '100000'::jsonb,
       'limits', 'Knowledge base size cap',
       'Hard cap on the system_prompt length for any single agent.'),
      ('limits.invite_ttl_days',
       '7'::jsonb,
       'limits', 'Invite expiry (days)',
       'How long a team invite remains valid after creation.'),
      -- features (boolean flags)
      ('features.ambience_beta',
       'true'::jsonb,
       'features', 'Ambience beta',
       'Show the office-chatter background-audio toggle on the voice page.'),
      ('features.knowledge_url_fetch',
       'false'::jsonb,
       'features', 'Knowledge URL fetch',
       'Allow the KB editor to fetch & ingest content from external URLs.'),
      ('features.signups_open',
       'true'::jsonb,
       'features', 'Public signups',
       'When false, /api/auth/signup returns 403 — useful before launch.'),
      -- branding / contact
      ('branding.support_email',
       '"support@spiderx.ai"'::jsonb,
       'branding', 'Support email',
       'Email shown in the topbar Support Ticket modal and on invite emails.'),
      ('branding.brand_palette',
       '{"primary":"#a78bfa","accent":"#2563eb"}'::jsonb,
       'branding', 'Brand palette',
       'CSS variables for the topbar / brand surfaces. JSON object of colours.')
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS platform_settings CASCADE")
