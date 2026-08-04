"""Create LangGraph's checkpoint tables under the schema-owner role.

Separate from ``alembic upgrade`` because the DDL is not ours: LangGraph owns
these tables and their shape changes with the library, so hand-transcribing them
into a migration would fork a schema this repository does not control.

It is *run like* a migration for the reason that matters — the application role
holds no ``CREATE`` on ``public``, which is what stops a compromised API pod from
altering the schema. ``AsyncPostgresSaver.setup()`` is idempotent, so re-running
is a no-op.

    DATABASE_MIGRATION_URL=postgresql://owner@host/db make migrate-checkpoints
"""

from __future__ import annotations

import asyncio
import os
import sys

from tenantchat.orchestration.checkpoints import postgres_checkpointer

_URL_VARIABLE = "DATABASE_MIGRATION_URL"


async def _setup(database_url: str) -> None:
    async with postgres_checkpointer(database_url) as saver:
        await saver.setup()


def main() -> int:
    database_url = os.environ.get(_URL_VARIABLE, "").strip()
    if not database_url:
        sys.stderr.write(
            f"{_URL_VARIABLE} is required, and must name the schema owner rather than "
            "the application role.\n"
        )
        return 2

    asyncio.run(_setup(database_url))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
