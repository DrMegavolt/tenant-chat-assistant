"""A disposable PostgreSQL 16 database at the current Alembic head, per suite."""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql
from testcontainers.postgres import PostgresContainer  # type: ignore[import-untyped]

REPO_ROOT = Path(__file__).resolve().parents[2]


def _libpq(sqlalchemy_url: str) -> str:
    return sqlalchemy_url.replace("postgresql+psycopg://", "postgresql://", 1)


@pytest.fixture(scope="session")
def privacy_postgres_server_url() -> Iterator[str]:
    with PostgresContainer(
        "postgres:16.11-alpine3.23@sha256:4327b9fd295502f326f44153a1045a7170ddbfffed1c3829798328556cfd09e2",
        username="privacy_owner",
        password="privacy-test-only",
        dbname="postgres",
        driver="psycopg",
    ) as postgres:
        yield postgres.get_connection_url()


@pytest.fixture
def privacy_database_url(privacy_postgres_server_url: str) -> Iterator[str]:
    """A disposable database migrated to the current Alembic head."""
    server = privacy_postgres_server_url
    database_name = f"privacy_{uuid.uuid4().hex}"
    with psycopg.connect(_libpq(server), autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))

    database_url = server.rsplit("/", maxsplit=1)[0] + f"/{database_name}"
    os.environ["DATABASE_MIGRATION_URL"] = database_url
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "services/api/migrations"))
    command.upgrade(config, "head")
    # The chat route drives the agent runtime, which persists to the LangGraph
    # checkpoint tables created the deployed way (`make migrate-checkpoints`).
    from scripts.setup_checkpoints import _setup

    asyncio.run(_setup(database_url))
    try:
        yield database_url
    finally:
        with psycopg.connect(_libpq(server), autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database_name))
            )
