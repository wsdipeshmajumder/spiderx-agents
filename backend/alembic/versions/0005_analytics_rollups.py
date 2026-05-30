"""analytics rollups — agent_daily_stats + org_daily_stats

Phase 5. Materialized per-day rollups so the analytics dashboard reads
a single row per (agent|org, day) instead of scanning the calls table.

Why pre-aggregate:
  * Per-agent analytics card on Overview should render in <50ms even
    when the agent has thousands of calls. A SUM over calls.duration_s
    plus a GROUP BY day is fine at 100 rows but slow at 100k.
  * Rollups also let us project tokens + cost without re-reading the
    transcript blob, which keeps memory pressure low on the admin grid.

How the rollups stay fresh:
  * `insert_call` (db_pg) UPSERTs both tables in the same transaction
    as the calls insert. If `agent_daily_stats(agent_id, day)` doesn't
    exist, we INSERT; if it exists, we ADD the new call's contribution.
  * No batch / cron — fully synchronous, atomic, and self-healing if
    we ever backfill historic data via a one-shot.

Schema choices:
  * `day DATE` (not timestamp) — analytics groups by calendar day in
    the caller's locale; we don't need sub-day granularity yet.
  * `outcomes JSONB` for the per-agent table — encodes the outcome
    distribution as {"Lead": 3, "Demo booked": 1}. Cheap to JSON-merge
    on UPSERT, lets the agent's analytics card show outcome breakdown
    without a second query.
  * Token columns mirror calls.{input,output}_tokens so the same
    UPSERT pattern works for either side.

Revision ID: 0005_analytics_rollups
Revises: 0004_platform_settings
Create Date: 2026-05-14
"""
from __future__ import annotations

from alembic import op


revision = "0005_analytics_rollups"
down_revision = "0004_platform_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE agent_daily_stats (
      agent_id      BIGINT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
      day           DATE   NOT NULL,
      calls         INTEGER NOT NULL DEFAULT 0,
      minutes       NUMERIC(10,2) NOT NULL DEFAULT 0,
      input_tokens  BIGINT NOT NULL DEFAULT 0,
      output_tokens BIGINT NOT NULL DEFAULT 0,
      cost_paise    BIGINT NOT NULL DEFAULT 0,
      outcomes      JSONB  NOT NULL DEFAULT '{}'::jsonb,
      PRIMARY KEY (agent_id, day)
    )
    """)
    op.execute("CREATE INDEX idx_ads_day ON agent_daily_stats(day DESC)")

    op.execute("""
    CREATE TABLE org_daily_stats (
      org_id        BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
      day           DATE   NOT NULL,
      calls         INTEGER NOT NULL DEFAULT 0,
      minutes       NUMERIC(10,2) NOT NULL DEFAULT 0,
      input_tokens  BIGINT NOT NULL DEFAULT 0,
      output_tokens BIGINT NOT NULL DEFAULT 0,
      cost_paise    BIGINT NOT NULL DEFAULT 0,
      PRIMARY KEY (org_id, day)
    )
    """)
    op.execute("CREATE INDEX idx_ods_day ON org_daily_stats(day DESC)")

    # Backfill from any existing calls so the rollups don't start empty if
    # the DB already has call rows (currently 0, but pattern stays correct).
    op.execute("""
    INSERT INTO agent_daily_stats (agent_id, day, calls, minutes,
                                    input_tokens, output_tokens, cost_paise, outcomes)
    SELECT c.agent_id,
           (c.started_at AT TIME ZONE 'UTC')::date AS day,
           COUNT(*),
           COALESCE(SUM(c.duration_s)/60.0, 0),
           COALESCE(SUM(c.input_tokens), 0),
           COALESCE(SUM(c.output_tokens), 0),
           COALESCE(SUM(c.cost_paise), 0),
           COALESCE(jsonb_object_agg(coalesce(c.outcome, 'unknown'),
                                      cnt) FILTER (WHERE cnt IS NOT NULL),
                    '{}'::jsonb)
      FROM calls c
      LEFT JOIN LATERAL (
        SELECT COUNT(*) AS cnt FROM calls c2
         WHERE c2.agent_id = c.agent_id
           AND (c2.started_at AT TIME ZONE 'UTC')::date
               = (c.started_at AT TIME ZONE 'UTC')::date
           AND coalesce(c2.outcome,'unknown') = coalesce(c.outcome,'unknown')
      ) sub ON true
     GROUP BY c.agent_id, (c.started_at AT TIME ZONE 'UTC')::date
    """)

    op.execute("""
    INSERT INTO org_daily_stats (org_id, day, calls, minutes,
                                  input_tokens, output_tokens, cost_paise)
    SELECT a.org_id,
           (c.started_at AT TIME ZONE 'UTC')::date AS day,
           COUNT(*),
           COALESCE(SUM(c.duration_s)/60.0, 0),
           COALESCE(SUM(c.input_tokens), 0),
           COALESCE(SUM(c.output_tokens), 0),
           COALESCE(SUM(c.cost_paise), 0)
      FROM calls c
      JOIN agents a ON a.id = c.agent_id
     GROUP BY a.org_id, (c.started_at AT TIME ZONE 'UTC')::date
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS org_daily_stats CASCADE")
    op.execute("DROP TABLE IF EXISTS agent_daily_stats CASCADE")
