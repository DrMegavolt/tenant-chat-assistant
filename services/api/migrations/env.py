"""Alembic environment for the authoritative domain schema.

Migrations deliberately require a separate owner URL. The API settings do not
read this variable and application startup never imports this module, keeping
DDL privileges out of the normal request-serving process.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def database_url() -> str:
    """Return the schema-owner connection string or fail before issuing DDL."""
    url = os.environ.get("DATABASE_MIGRATION_URL")
    if not url:
        msg = "DATABASE_MIGRATION_URL is required for schema migrations"
        raise RuntimeError(msg)
    return url


def run_migrations_offline() -> None:
    """Render SQL without opening a database connection."""
    context.configure(
        url=database_url(),
        target_metadata=None,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in one transaction with no persistent connection pool."""
    engine = create_engine(database_url(), poolclass=NullPool)
    with engine.connect() as connection:
        context.configure(
            connection=connection, target_metadata=None, transaction_per_migration=True
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
