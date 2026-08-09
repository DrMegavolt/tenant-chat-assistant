"""Bounded async Postgres engine and database lifecycle."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


@dataclass(frozen=True, slots=True)
class DatabasePoolSettings:
    size: int = 5
    max_overflow: int = 5
    timeout_seconds: float = 5.0
    recycle_seconds: int = 1800

    def __post_init__(self) -> None:
        if self.size < 1:
            raise ValueError("database pool size must be positive")
        if self.max_overflow < 0:
            raise ValueError("database max overflow cannot be negative")
        if self.timeout_seconds <= 0:
            raise ValueError("database pool timeout must be positive")
        if self.recycle_seconds < 1:
            raise ValueError("database pool recycle interval must be positive")


def _async_psycopg_url(raw_url: str) -> str:
    url = make_url(raw_url)
    if url.drivername in {"postgres", "postgresql"}:
        url = url.set(drivername="postgresql+psycopg")
    if url.drivername != "postgresql+psycopg":
        raise ValueError("DATABASE_URL must use PostgreSQL with the psycopg driver")
    return url.render_as_string(hide_password=False)


class Database:
    """Own one bounded pool; it never creates or migrates schema."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    @classmethod
    def connect(cls, database_url: str, pool: DatabasePoolSettings) -> Database:
        engine = create_async_engine(
            _async_psycopg_url(database_url),
            pool_size=pool.size,
            max_overflow=pool.max_overflow,
            pool_timeout=pool.timeout_seconds,
            pool_recycle=pool.recycle_seconds,
            pool_pre_ping=True,
            # DBAPI exceptions otherwise include bound customer contact and
            # transcript values in their string representation.
            hide_parameters=True,
        )
        return cls(engine)

    async def synchronize_tenants(self, tenants: Iterable[tuple[str, str]]) -> None:
        """Persist the code-owned tenant seeds until FEAT-006 owns configuration."""
        statement = text(
            """
            INSERT INTO tenants (id, display_name)
            VALUES (:tenant_id, :display_name)
            ON CONFLICT (id) DO UPDATE
            SET display_name = EXCLUDED.display_name, updated_at = now()
            WHERE tenants.display_name IS DISTINCT FROM EXCLUDED.display_name
            """
        )
        async with self.engine.begin() as connection:
            for tenant_id, display_name in tenants:
                await connection.execute(
                    statement,
                    {"tenant_id": tenant_id, "display_name": display_name},
                )

    async def ready(self) -> None:
        """Prove the bounded pool can execute a trivial query."""
        async with self.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    async def dispose(self) -> None:
        await self.engine.dispose()
