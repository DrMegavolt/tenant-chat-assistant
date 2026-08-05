"""Shared rate-limit accounting over the ``rate_limit_counters`` table.

One counter per key per window, in PostgreSQL rather than in memory, because the
API is deployed as several replicas and a per-process map would multiply every
budget by the replica count. The upsert is the atomicity: two workers hitting
the same key in the same window both execute the same statement, the row lock
serializes them, and each sees the other's increments — no read-then-write
window exists.

The table is bounded by the sweep in the same statement: rows older than the
request's own window are deleted as it counts, so at any moment the table holds
roughly the identities that were active in the last one to two windows. A key
is an opaque ``scope:value`` string; sessions and addresses never reach logs,
and the rows themselves expire within minutes.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


class PostgresRateLimitStore:
    """Sweep-and-count fixed-window accounting, atomic per key."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def hit(self, key: str, window: int) -> int:
        statement = text(
            """
            WITH sweep AS (
                DELETE FROM rate_limit_counters
                WHERE window_start < :window
            )
            INSERT INTO rate_limit_counters (scope_key, window_start, count)
            VALUES (:key, :window, 1)
            ON CONFLICT (scope_key, window_start) DO UPDATE
            SET count = rate_limit_counters.count + 1
            RETURNING count
            """
        )
        async with self._engine.begin() as connection:
            result = await connection.execute(statement, {"key": key, "window": window})
            return int(result.scalar_one())
