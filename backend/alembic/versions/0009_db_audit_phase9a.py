"""Phase 9a — DB-architect audit fixes (half-day pass)

Independent DB audit flagged five production-breakers + fourteen ranked
concerns. This migration ships the cheapest, highest-leverage half-day
slice — the things you do BEFORE you have scale problems:

  1. Drop the redundant case-sensitive `users_email_key` UNIQUE
     constraint. The case-insensitive `idx_users_email_lower` UNIQUE
     index already enforces email uniqueness; the case-sensitive one
     let `Foo@x.com` and `foo@x.com` coexist, silently violating the
     contract `get_user_by_email(lower(...))` relies on.

  2. Drop the dead `idx_agents_user` index. Post-Phase-2 (Teams),
     every hot query reads agents by `org_id`, not `user_id`. The
     index was costing write amplification for no read benefit.

  3. Add CHECK constraints on `calls.sentiment` and `calls.lead_quality`
     so the model can't slip a freeform string past the call-log
     dashboard. Outcome is intentionally NOT constrained — the
     outcomes vocabulary is per-agent, not global.

  4. Add size caps on the two freeform JSONB columns that any client
     can write to: `audit_log.diff` ≤ 64 KB, `agents.purpose` ≤ 16 KB.
     Prevents a buggy or hostile client from writing multi-MB blobs
     that TOAST + then need to be detoasted on every read.

The remaining audit items (B1 user_id semantics flip, B2 calls.org_id
stamp, partitioning, denormalised list reads, pool resizing as
runtime config) are intentionally deferred to Phase 9b/9c — they need
batched backfills + coordinated app-code changes. Pool size knobs are
a code change (db_pg.get_pool reads env vars), not a schema migration,
so they ship in this same commit but outside this file.

Revision ID: 0009_db_audit_phase9a
Revises: 0008_agent_purpose_call_quality
Create Date: 2026-05-14
"""
from __future__ import annotations

from alembic import op


revision = "0009_db_audit_phase9a"
down_revision = "0008_agent_purpose_call_quality"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. Drop the case-sensitive users.email UNIQUE ────────────────
    # The case-insensitive `idx_users_email_lower` UNIQUE index (created
    # in 0001_baseline) is the real enforcement. The duplicate constraint
    # from the inline `email TEXT NOT NULL UNIQUE` was a footgun.
    op.execute("ALTER TABLE users DROP CONSTRAINT IF EXISTS users_email_key")

    # ── 2. Drop the dead idx_agents_user index ───────────────────────
    # Post-Phase-2, `list_agents(user_id)` filters by org membership and
    # the planner uses `idx_agents_org`. `idx_agents_user` is pure write
    # amplification with no remaining read consumer.
    op.execute("DROP INDEX IF EXISTS idx_agents_user")

    # ── 3. Sentiment + lead_quality CHECK constraints ────────────────
    # The model is instructed (system prompt) to use these enums. CHECKs
    # turn instruction into enforcement. NULL is allowed (legacy rows
    # pre-Phase-8 + cases where the model genuinely couldn't assess).
    op.execute("""
    ALTER TABLE calls ADD CONSTRAINT calls_sentiment_check CHECK (
      sentiment IS NULL OR sentiment IN ('positive','neutral','negative','mixed')
    )
    """)
    op.execute("""
    ALTER TABLE calls ADD CONSTRAINT calls_lead_quality_check CHECK (
      lead_quality IS NULL OR lead_quality IN ('hot','warm','cold','na')
    )
    """)

    # ── 4. JSONB size caps ───────────────────────────────────────────
    # 64 KB is generous for an audit-log diff (typical = 200 B–4 KB).
    # 16 KB for agents.purpose is generous for a structured object that
    # shouldn't exceed ~2 KB in practice. Postgres CHECK on JSONB-cast-to-
    # text is a microsecond-cost per INSERT/UPDATE and zero cost on read.
    op.execute("""
    ALTER TABLE audit_log ADD CONSTRAINT audit_log_diff_size_check CHECK (
      diff IS NULL OR octet_length(diff::text) <= 65536
    )
    """)
    op.execute("""
    ALTER TABLE agents ADD CONSTRAINT agents_purpose_size_check CHECK (
      octet_length(purpose::text) <= 16384
    )
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE agents DROP CONSTRAINT IF EXISTS agents_purpose_size_check")
    op.execute("ALTER TABLE audit_log DROP CONSTRAINT IF EXISTS audit_log_diff_size_check")
    op.execute("ALTER TABLE calls DROP CONSTRAINT IF EXISTS calls_lead_quality_check")
    op.execute("ALTER TABLE calls DROP CONSTRAINT IF EXISTS calls_sentiment_check")
    op.execute("CREATE INDEX IF NOT EXISTS idx_agents_user ON agents(user_id)")
    # Recreating the case-sensitive UNIQUE is destructive (would fail if
    # mixed-case rows now exist), so downgrade leaves the case-insensitive
    # index as the sole enforcement. Intentional one-way ratchet.
