"""LangGraph's execution-state persistence.

This is the one adapter in the repository that both talks to PostgreSQL and
imports the agent framework, and `ADR-0001` singles it out for exactly that
reason: a *business* repository persists domain aggregates and must not know
LangGraph exists, while this persists LangGraph's own resume points and cannot
be written without it. The distinction is what the layer table encodes.

Nothing here is authoritative. Conversations, bookings, leads, and handoffs are
written by the domain services; dropping every table this module touches costs
in-flight runs their resume point and costs the business nothing.

**Schema is not created at runtime.** ``AsyncPostgresSaver.setup()`` issues DDL,
and the application role deliberately holds no ``CREATE`` on ``public`` — see the
privilege specification in ``tests/migrations``. Run ``make migrate-checkpoints``
with ``DATABASE_MIGRATION_URL``, alongside the Alembic upgrade.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Checkpointer

__all__ = [
    "Checkpointer",
    "InMemorySaver",
    "checkpoint_connection_string",
    "postgres_checkpointer",
]

_SQLALCHEMY_SCHEME = re.compile(r"^postgres(?:ql)?(?:\+[A-Za-z0-9_]+)?://")


def checkpoint_connection_string(database_url: str) -> str:
    """Convert an application ``DATABASE_URL`` to the libpq form psycopg wants.

    The rest of the codebase carries SQLAlchemy URLs, which name a driver
    (``postgresql+psycopg://``). The checkpointer opens its own psycopg pool and
    rejects that prefix. Rewriting the scheme by hand, rather than round-tripping
    through a URL parser, keeps this package free of a database dependency it
    otherwise has no use for — and keeps the password out of a parsed object
    whose ``repr`` might be logged.

    Raises:
        ValueError: the URL does not name PostgreSQL.
    """
    if not _SQLALCHEMY_SCHEME.match(database_url):
        raise ValueError("the checkpoint store must be PostgreSQL")
    return _SQLALCHEMY_SCHEME.sub("postgresql://", database_url, count=1)


@asynccontextmanager
async def postgres_checkpointer(database_url: str) -> AsyncIterator[AsyncPostgresSaver]:
    """Open a checkpointer over its own connection pool.

    Separate from the SQLAlchemy engine that serves domain queries on purpose:
    checkpoint traffic is high-frequency and low-value, and letting it exhaust
    the pool that commits bookings would trade a business write for a resume
    point.
    """
    async with AsyncPostgresSaver.from_conn_string(
        checkpoint_connection_string(database_url)
    ) as saver:
        yield saver
