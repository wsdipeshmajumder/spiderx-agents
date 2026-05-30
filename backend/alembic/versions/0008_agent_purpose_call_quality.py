"""agent purpose + per-call emotional & lead quality

What this adds:

  * `agents.purpose JSONB` — structured "what does this agent actually
    do" record that Eva captures at build time and the user can edit
    from the agent dashboard. Schema:
        {
          "summary":   "One-line description in the user's words",
          "answers":   ["Available models", "Test drive availability", …],
          "actions":   [{"id":"callback_request","label":"…"}, …],
          "post_call": {"email": true, "sms": true}
        }
    The library of `actions` is the same across every service sector
    (callback request, appointment booking, quote request, inquiry
    capture, complaint intake, order status, support ticket, emergency
    routing). The agent's purpose picks 2-4 from that library.

  * `calls.sentiment TEXT` — 'positive' | 'neutral' | 'negative' | 'mixed'
  * `calls.lead_quality TEXT` — 'hot' | 'warm' | 'cold' | 'na'
  * `calls.lead_signals TEXT` — short note ("Asked about pricing, urgent
    timeline" / "Information-only, not a buyer")

    The agent's system prompt is updated (in code, not schema) to
    instruct the model to assess these before calling end_call. The
    end_call connector accepts them as params and stamps the row.
    Visible on the call-log table so operators can prioritise follow-ups.

Indexes:
  - calls(lead_quality) so the "hot leads in last 24h" filter on the
    agent dashboard stays fast as call volume grows.

Revision ID: 0008_agent_purpose_call_quality
Revises: 0007_llm_ledger
Create Date: 2026-05-14
"""
from __future__ import annotations

from alembic import op


revision = "0008_agent_purpose_call_quality"
down_revision = "0007_llm_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE agents ADD COLUMN purpose JSONB NOT NULL DEFAULT '{}'::jsonb"
    )
    op.execute("ALTER TABLE calls ADD COLUMN sentiment TEXT")
    op.execute("ALTER TABLE calls ADD COLUMN lead_quality TEXT")
    op.execute("ALTER TABLE calls ADD COLUMN lead_signals TEXT")
    op.execute("CREATE INDEX idx_calls_lead_quality ON calls(lead_quality) WHERE lead_quality IS NOT NULL")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_calls_lead_quality")
    op.execute("ALTER TABLE calls DROP COLUMN IF EXISTS lead_signals")
    op.execute("ALTER TABLE calls DROP COLUMN IF EXISTS lead_quality")
    op.execute("ALTER TABLE calls DROP COLUMN IF EXISTS sentiment")
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS purpose")
