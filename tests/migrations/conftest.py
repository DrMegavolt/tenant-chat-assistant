"""An isolated PostgreSQL 16 server and disposable database per migration test."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import psycopg
import pytest
from psycopg import sql
from testcontainers.postgres import PostgresContainer  # type: ignore[import-untyped]


def psycopg_url(sqlalchemy_url: str) -> str:
    return sqlalchemy_url.replace("postgresql+psycopg://", "postgresql://", 1)


@pytest.fixture(scope="session")
def postgres_server_url() -> Iterator[str]:
    """Start one pinned server; no developer database or ambient URL is consulted."""
    with PostgresContainer(
        "postgres:16-alpine",
        username="migration_owner",
        password="migration-test-only",
        dbname="postgres",
        driver="psycopg",
    ) as postgres:
        yield postgres.get_connection_url()


@pytest.fixture
def migration_database_url(postgres_server_url: str) -> Iterator[str]:
    """Give each test a fresh database and force-drop it after use."""
    database_name = f"migration_{uuid.uuid4().hex}"
    with psycopg.connect(psycopg_url(postgres_server_url), autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))

    database_url = postgres_server_url.rsplit("/", maxsplit=1)[0] + f"/{database_name}"
    try:
        yield database_url
    finally:
        with psycopg.connect(psycopg_url(postgres_server_url), autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database_name))
            )
