"""Alembic env wiring for SpiderX.AI.

We pull the DB URL from `PG_URL` env (or .env), not from alembic.ini, so the
same secret is used by app code and migrations. Alembic uses the sync
`psycopg` driver for DDL — `asyncpg` is the runtime driver in db_pg.py.
"""
from __future__ import annotations

import os
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool
from alembic import context

# Load .env so `alembic upgrade head` works from the CLI without an explicit export.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))
except Exception:
    pass

config = context.config

# Pull DB URL from env, overriding whatever's in alembic.ini.
_pg_url = os.environ.get("PG_URL") or os.environ.get("DATABASE_URL")
if _pg_url:
    # Alembic needs a sync URL. psycopg3 is the modern default; coerce known prefixes.
    if _pg_url.startswith("postgres://"):
        _pg_url = "postgresql+psycopg://" + _pg_url[len("postgres://"):]
    elif _pg_url.startswith("postgresql://"):
        _pg_url = "postgresql+psycopg://" + _pg_url[len("postgresql://"):]
    elif _pg_url.startswith("postgresql+asyncpg://"):
        _pg_url = "postgresql+psycopg://" + _pg_url[len("postgresql+asyncpg://"):]
    config.set_main_option("sqlalchemy.url", _pg_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# We hand-write migrations, no autogenerate, so target_metadata is None.
target_metadata = None


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
