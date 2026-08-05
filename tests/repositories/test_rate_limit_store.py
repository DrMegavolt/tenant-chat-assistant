"""The shared rate-limit counter, as it behaves across processes and windows.

This is the correctness claim `SEC-003` makes about the guard: a budget counted
here is counted once for the whole fleet, atomically, and the table stays
bounded because each hit sweeps the windows behind it.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ProcessPoolExecutor

import psycopg
import pytest

from tenantchat.api.persistence import Database, DatabasePoolSettings
from tenantchat.api.persistence.rate_limits import PostgresRateLimitStore

POOL = DatabasePoolSettings(size=2, max_overflow=1, timeout_seconds=2)

WINDOW = 1_700_000_000
STALE_WINDOW = WINDOW - 120


def _psycopg_url(sqlalchemy_url: str) -> str:
    return sqlalchemy_url.replace("postgresql+psycopg://", "postgresql://", 1)


def _hit_worker(database_url: str, key: str, window: int, times: int) -> int:
    async def hits() -> int:
        database = Database.connect(
            database_url,
            DatabasePoolSettings(size=1, max_overflow=0, timeout_seconds=5),
        )
        try:
            store = PostgresRateLimitStore(database.engine)
            count = 0
            for _ in range(times):
                count = await store.hit(key, window)
            return count
        finally:
            await database.dispose()

    return asyncio.run(hits())


@pytest.mark.integration
def test_hits_count_per_key_and_window(repository_database_url: str) -> None:
    async def scenario() -> None:
        database = Database.connect(repository_database_url, POOL)
        try:
            store = PostgresRateLimitStore(database.engine)
            assert await store.hit("ip:10.0.0.1", WINDOW) == 1
            assert await store.hit("ip:10.0.0.1", WINDOW) == 2
            assert await store.hit("tenant:apex", WINDOW) == 1
            # A new window starts the count over for the same key.
            assert await store.hit("ip:10.0.0.1", WINDOW + 60) == 1
        finally:
            await database.dispose()

    asyncio.run(scenario())


@pytest.mark.integration
def test_each_hit_sweeps_windows_behind_it(repository_database_url: str) -> None:
    """A stale row is gone as soon as a newer window is counted, so the table
    cannot accumulate one row per abandoned window."""

    async def scenario() -> None:
        database = Database.connect(repository_database_url, POOL)
        try:
            store = PostgresRateLimitStore(database.engine)
            await store.hit("ip:10.0.0.1", STALE_WINDOW)
            await store.hit("ip:10.0.0.1", STALE_WINDOW)
            await store.hit("ip:10.0.0.1", WINDOW)
        finally:
            await database.dispose()

    asyncio.run(scenario())

    with psycopg.connect(_psycopg_url(repository_database_url)) as connection:
        rows = connection.execute(
            "SELECT scope_key, window_start, count FROM rate_limit_counters"
        ).fetchall()

    assert rows == [("ip:10.0.0.1", WINDOW, 1)]


@pytest.mark.integration
def test_concurrent_hits_across_processes_are_counted_once(repository_database_url: str) -> None:
    """The upsert serializes on the row, so N workers cannot double-count a hit."""
    key = "session:shared-budget"
    workers = 3
    hits_per_worker = 25

    with ProcessPoolExecutor(max_workers=workers) as executor:
        observed = list(
            executor.map(
                _hit_worker,
                [repository_database_url] * workers,
                [key] * workers,
                [WINDOW] * workers,
                [hits_per_worker] * workers,
            )
        )

    # A worker returns the count its own last hit observed, which races the
    # other workers' final hits; the invariant is that no increment is lost, so
    # the settled table holds the grand total and no worker saw more than it.
    with psycopg.connect(_psycopg_url(repository_database_url)) as connection:
        row = connection.execute(
            "SELECT count FROM rate_limit_counters WHERE scope_key = %s",
            (key,),
        ).fetchone()
    assert row is not None
    settled = row[0]
    assert settled == workers * hits_per_worker
    assert all(count <= settled for count in observed)
