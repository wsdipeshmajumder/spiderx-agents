"""Phase 9b — DB-architect audit deeper fixes (B1, B2, slug scope, denorms)

This migration ships the audit items that need batched data migrations +
coordinated app-code changes. Carries five concerns into one revision so
they land atomically (any partial state would mean app calls referencing
half-migrated schema).

What this migrates:

  1. `agents.user_id` → `created_by` semantics
       - drop NOT NULL
       - change FK to ON DELETE SET NULL
     Deleting a user who left the team no longer cascades-nukes the org's
     work. (Audit B1.)

  2. `calls.org_id` — immutable per-call tenant stamp
       - add column nullable
       - backfill from agents.org_id
       - SET NOT NULL after backfill
       - FK ON DELETE CASCADE
       - BEFORE INSERT trigger to autofill + assert consistency on every
         row going forward. (Audit B2.)
       - Index on (org_id, started_at DESC) for cross-tenant analytics

  3. Composite `UNIQUE (org_id, slug)` on agents, drop global `UNIQUE(slug)`
     Two orgs can now both have a "support-bot". (Audit High concern.)

  4. Denormalised `agents.last_call_at` and `agents.calls_count`
     The Phase-9a correlated subqueries in `list_agents` are O(agents) at
     scale. Two STORED columns + insert-time maintenance kill them.
     (Audit High concern.)

  5. Backfill agents.{last_call_at, calls_count} from existing calls.

Why one migration: the trigger (#2) + the new agents columns (#4) need to
exist before the application drops the old query patterns; backfills run
once across populated tables. Splitting would mean either app downtime or
mixed schema during the window.

Revision ID: 0010_db_audit_phase9b
Revises: 0009_db_audit_phase9a
Create Date: 2026-05-14
"""
from __future__ import annotations

from alembic import op


revision = "0010_db_audit_phase9b"
down_revision = "0009_db_audit_phase9a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. agents.user_id → created_by semantics ─────────────────────
    # Goal: deleting a user nullifies user_id on their agents (the org's
    # agents survive), instead of cascade-deleting.
    op.execute("ALTER TABLE agents ALTER COLUMN user_id DROP NOT NULL")
    op.execute("ALTER TABLE agents DROP CONSTRAINT IF EXISTS agents_user_id_fkey")
    op.execute("""
    ALTER TABLE agents
      ADD CONSTRAINT agents_user_id_fkey
      FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
    """)

    # ── 2. calls.org_id — immutable tenant stamp ─────────────────────
    # Step 1: add column nullable so the backfill can run.
    op.execute("ALTER TABLE calls ADD COLUMN org_id BIGINT")
    # Step 2: backfill from agents. Single statement on populated table —
    # at the 12-month target this is ~10–30s of UPDATE; lock-friendly
    # because it touches each row once.
    op.execute("""
    UPDATE calls SET org_id = a.org_id
      FROM agents a
     WHERE calls.agent_id = a.id
       AND calls.org_id IS NULL
    """)
    # Step 3: SET NOT NULL now that every existing row has a value.
    op.execute("ALTER TABLE calls ALTER COLUMN org_id SET NOT NULL")
    # Step 4: FK + index. CASCADE because deleting an org wipes its calls
    # the same way it wipes its agents (existing behaviour for agents at
    # 0002, kept symmetric here).
    op.execute("""
    ALTER TABLE calls
      ADD CONSTRAINT calls_org_id_fkey
      FOREIGN KEY (org_id) REFERENCES orgs(id) ON DELETE CASCADE
    """)
    op.execute("CREATE INDEX idx_calls_org_started ON calls(org_id, started_at DESC)")

    # Step 5: BEFORE INSERT trigger so future rows can either supply
    # org_id explicitly (and we verify it matches the agent) or omit it
    # entirely (we fill it from agents). Same trigger guards against
    # UPDATEs that try to change org_id away from the agent's org —
    # immutability is the whole point.
    op.execute("""
    CREATE OR REPLACE FUNCTION calls_org_stamp() RETURNS trigger AS $$
    DECLARE
      a_org BIGINT;
    BEGIN
      SELECT org_id INTO a_org FROM agents WHERE id = NEW.agent_id;
      IF a_org IS NULL THEN
        RAISE EXCEPTION 'calls.agent_id=% references missing or detached agent', NEW.agent_id;
      END IF;
      IF TG_OP = 'INSERT' THEN
        IF NEW.org_id IS NULL THEN
          NEW.org_id := a_org;
        ELSIF NEW.org_id <> a_org THEN
          RAISE EXCEPTION 'calls.org_id=% does not match agents.org_id=% for agent_id=%',
            NEW.org_id, a_org, NEW.agent_id;
        END IF;
      ELSIF TG_OP = 'UPDATE' THEN
        IF NEW.org_id <> a_org THEN
          RAISE EXCEPTION 'calls.org_id is immutable; tried to change to % (agent org = %)',
            NEW.org_id, a_org;
        END IF;
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql
    """)
    op.execute("""
    CREATE TRIGGER calls_org_stamp_trigger
      BEFORE INSERT OR UPDATE OF org_id ON calls
      FOR EACH ROW EXECUTE FUNCTION calls_org_stamp()
    """)

    # ── 3. Composite UNIQUE (org_id, slug) on agents ─────────────────
    # Currently `agents.slug` is globally unique — org A creates
    # "support-bot", org B can't. Make slugs org-scoped.
    op.execute("ALTER TABLE agents DROP CONSTRAINT IF EXISTS agents_slug_key")
    op.execute("DROP INDEX IF EXISTS idx_agents_slug")
    op.execute("CREATE UNIQUE INDEX agents_org_slug_uniq ON agents(org_id, slug)")

    # ── 4. Denormalised agents.last_call_at + agents.calls_count ─────
    # Phase-9a flagged the correlated subqueries in `list_agents` as
    # 200–400ms at scale. Two STORED columns + insert-time maintenance
    # in `insert_call` (db_pg.py) make the listing a flat index scan.
    op.execute("ALTER TABLE agents ADD COLUMN last_call_at TIMESTAMPTZ")
    op.execute("ALTER TABLE agents ADD COLUMN calls_count BIGINT NOT NULL DEFAULT 0")

    # ── 5. Backfill agents.{last_call_at, calls_count} ───────────────
    # One pass over calls, GROUP BY agent_id, into a join-update on agents.
    # At the 12-month target this is a single scan of `calls` ordered by
    # `idx_calls_agent_started` — bounded cost.
    op.execute("""
    UPDATE agents a SET
      last_call_at = sub.last_at,
      calls_count  = sub.cnt
    FROM (
      SELECT agent_id,
             MAX(ended_at) AS last_at,
             COUNT(*)::bigint AS cnt
      FROM calls
      GROUP BY agent_id
    ) sub
    WHERE sub.agent_id = a.id
    """)


def downgrade() -> None:
    # Reverse order. The trigger + function must drop before the column
    # they reference.
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS calls_count")
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS last_call_at")
    op.execute("DROP INDEX IF EXISTS agents_org_slug_uniq")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_agents_slug ON agents(slug)")
    op.execute("DROP TRIGGER IF EXISTS calls_org_stamp_trigger ON calls")
    op.execute("DROP FUNCTION IF EXISTS calls_org_stamp()")
    op.execute("DROP INDEX IF EXISTS idx_calls_org_started")
    op.execute("ALTER TABLE calls DROP CONSTRAINT IF EXISTS calls_org_id_fkey")
    op.execute("ALTER TABLE calls DROP COLUMN IF EXISTS org_id")
    op.execute("ALTER TABLE agents DROP CONSTRAINT IF EXISTS agents_user_id_fkey")
    op.execute("""
    ALTER TABLE agents
      ADD CONSTRAINT agents_user_id_fkey
      FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    """)
    # NB: re-asserting NOT NULL on a column that may now contain NULLs
    # from the SET NULL behaviour is destructive; downgrade leaves it
    # nullable. Intentional one-way ratchet for created_by semantics.
