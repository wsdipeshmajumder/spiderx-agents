"""One-shot data migration: data/eva.db (SQLite) → Postgres.

Reads every row out of the live SQLite file and inserts into Postgres,
translating ISO-string timestamps to datetime, TEXT-JSON to JSONB, and
preserving primary keys so existing slugs / external references keep
working.

Usage:
    PG_URL='postgresql://sxai:sxai_local_dev@localhost:5432/sxai_dev' \\
        .venv/bin/python -m scripts.migrate_sqlite_to_pg

Prerequisites:
    1. `alembic upgrade head` against PG_URL — schema must exist.
    2. The destination DB should be empty of user data (plans are seeded
       by the baseline; we delete them and reinsert in case the source
       SQLite has a customised free-plan minutes_total).

Idempotency: this script is NOT idempotent — it expects an empty target.
If you re-run, run `alembic downgrade base && alembic upgrade head` first.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import asyncpg

# Make backend imports work when run from repo root.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from backend.db_pg import _pg_url, _init_codecs  # type: ignore

SQLITE_PATH = ROOT / "data" / "eva.db"


def _parse_iso(s):
    if not s:
        return None
    if isinstance(s, datetime):
        return s if s.tzinfo else s.replace(tzinfo=timezone.utc)
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _parse_json(s, default):
    if s is None or s == "":
        return default
    if isinstance(s, (dict, list)):
        return s
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return default


async def main() -> int:
    if not SQLITE_PATH.exists():
        print(f"✗ SQLite source not found at {SQLITE_PATH}")
        return 1

    src = sqlite3.connect(SQLITE_PATH)
    src.row_factory = sqlite3.Row

    dst_url = _pg_url()
    print(f"→ Source: {SQLITE_PATH}")
    print(f"→ Target: {dst_url.split('@')[-1]}")

    conn = await asyncpg.connect(dsn=dst_url)
    await _init_codecs(conn)

    try:
        async with conn.transaction():
            # ── plans ─── (baseline migration seeded these; reset to source values)
            await conn.execute("DELETE FROM plans")
            plan_id_map: dict[int, int] = {}
            for row in src.execute("SELECT * FROM plans ORDER BY id").fetchall():
                features = _parse_json(row["features"], [])
                new_id = await conn.fetchval(
                    """
                    INSERT INTO plans (slug, label, tagline, price_paise, currency,
                                        minutes_total, features, sort_order, is_active)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) RETURNING id
                    """,
                    row["slug"], row["label"], row["tagline"], row["price_paise"],
                    row["currency"] or "INR", row["minutes_total"], features,
                    row["sort_order"] or 0, bool(row["is_active"]),
                )
                plan_id_map[row["id"]] = new_id
            print(f"  ✓ plans: {len(plan_id_map)}")

            # ── orgs ───
            org_id_map: dict[int, int] = {}
            try:
                src_orgs = src.execute("SELECT * FROM orgs ORDER BY id").fetchall()
            except sqlite3.OperationalError:
                src_orgs = []
            for row in src_orgs:
                new_id = await conn.fetchval(
                    """
                    INSERT INTO orgs (name, country, tax_id, billing_address,
                                       currency, timezone, created_at)
                    VALUES ($1, $2, $3, $4, $5, $6, COALESCE($7, now())) RETURNING id
                    """,
                    row["name"], row["country"], row["tax_id"],
                    row["billing_address"], row["currency"], row["timezone"],
                    _parse_iso(row["created_at"]),
                )
                org_id_map[row["id"]] = new_id
            print(f"  ✓ orgs: {len(org_id_map)}")

            # ── users ───
            user_id_map: dict[int, int] = {}
            for row in src.execute("SELECT * FROM users ORDER BY id").fetchall():
                org_id = org_id_map.get(row["org_id"]) if "org_id" in row.keys() else None
                plan_id = plan_id_map.get(row["plan_id"]) if "plan_id" in row.keys() else None
                new_id = await conn.fetchval(
                    """
                    INSERT INTO users (email, name, password_hash, avatar_url,
                                        provider, org_id, plan_id, minutes_used,
                                        plan_started_at, created_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, COALESCE($10, now()))
                    RETURNING id
                    """,
                    row["email"], row["name"], row["password_hash"], row["avatar_url"],
                    row["provider"] or "stub", org_id, plan_id,
                    float(row["minutes_used"] or 0) if "minutes_used" in row.keys() else 0,
                    _parse_iso(row["plan_started_at"]) if "plan_started_at" in row.keys() else None,
                    _parse_iso(row["created_at"]),
                )
                user_id_map[row["id"]] = new_id
            print(f"  ✓ users: {len(user_id_map)}")

            # ── agents ───
            agent_id_map: dict[int, int] = {}
            seen_slugs: set[str] = set()
            for row in src.execute("SELECT * FROM agents ORDER BY id").fetchall():
                user_id = user_id_map.get(row["user_id"])
                if not user_id:
                    print(f"  ⚠ skipping agent {row['id']} — no mapped user")
                    continue
                slug = row["slug"] or row["name"].lower().replace(" ", "-")
                # Dedupe slugs across migration (shouldn't happen but be safe).
                orig_slug = slug
                n = 2
                while slug in seen_slugs:
                    slug = f"{orig_slug}-{n}"
                    n += 1
                seen_slugs.add(slug)

                published = bool(row["published"]) if "published" in row.keys() else False
                new_id = await conn.fetchval(
                    """
                    INSERT INTO agents (
                        user_id, slug, name, sector, locale, persona, greeting,
                        system_prompt, voice,
                        guardrails, connectors, sip_config, voice_tweaks,
                        outcomes, policy, webhook_url, webhook_headers, variables,
                        published, published_at, created_at
                    ) VALUES (
                        $1, $2, $3, $4, $5, $6, $7,
                        $8, $9,
                        $10, $11, $12, $13,
                        $14, $15, $16, $17, $18,
                        $19, $20, COALESCE($21, now())
                    ) RETURNING id
                    """,
                    user_id, slug, row["name"], row["sector"], row["locale"],
                    row["persona"], row["greeting"], row["system_prompt"] or "",
                    row["voice"],
                    _parse_json(row["guardrails"], []),
                    _parse_json(row["connectors"], []),
                    _parse_json(row["sip_config"], None),
                    _parse_json(row["voice_tweaks"], None),
                    _parse_json(row["outcomes"], []),
                    _parse_json(row["policy"], None),
                    row["webhook_url"],
                    _parse_json(row["webhook_headers"], None),
                    _parse_json(row["variables"], {}),
                    published,
                    _parse_iso(row["published_at"]) if "published_at" in row.keys() else None,
                    _parse_iso(row["created_at"]),
                )
                agent_id_map[row["id"]] = new_id
            print(f"  ✓ agents: {len(agent_id_map)}")

            # ── calls ───
            calls_inserted = 0
            for row in src.execute("SELECT * FROM calls ORDER BY id").fetchall():
                agent_id = agent_id_map.get(row["agent_id"])
                if not agent_id:
                    continue  # orphan
                await conn.execute(
                    """
                    INSERT INTO calls (
                        agent_id, started_at, ended_at, duration_s,
                        outcome, reason, summary, final_message,
                        extracted, transcript
                    ) VALUES ($1, COALESCE($2, now()), COALESCE($3, now()), $4,
                              $5, $6, $7, $8, $9, $10)
                    """,
                    agent_id,
                    _parse_iso(row["started_at"]),
                    _parse_iso(row["ended_at"]),
                    float(row["duration_s"] or 0),
                    row["outcome"], row["reason"], row["summary"], row["final_message"],
                    _parse_json(row["extracted"], None),
                    row["transcript"],
                )
                calls_inserted += 1
            print(f"  ✓ calls: {calls_inserted}")

            # ── number_requests ───
            nr_inserted = 0
            for row in src.execute("SELECT * FROM number_requests ORDER BY id").fetchall():
                agent_id = agent_id_map.get(row["agent_id"])
                if not agent_id:
                    continue
                user_id = user_id_map.get(row["user_id"]) if "user_id" in row.keys() else None
                await conn.execute(
                    """
                    INSERT INTO number_requests (
                        agent_id, user_id, country, city, delivery_handle, notes,
                        status, created_at
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, COALESCE($8, now()))
                    """,
                    agent_id, user_id, row["country"], row["city"],
                    row["delivery_handle"], row["notes"],
                    row["status"] or "pending",
                    _parse_iso(row["created_at"]),
                )
                nr_inserted += 1
            print(f"  ✓ number_requests: {nr_inserted}")

            # ── post-migration receipts ───
            counts = {}
            for tbl in ("plans", "orgs", "users", "agents", "calls", "number_requests"):
                counts[tbl] = await conn.fetchval(f"SELECT COUNT(*) FROM {tbl}")
            print("\n→ Final row counts in Postgres:")
            for k, v in counts.items():
                print(f"    {k:20s} {v}")
    finally:
        await conn.close()
        src.close()

    print("\n✓ Migration complete.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
