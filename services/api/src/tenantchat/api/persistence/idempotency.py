"""The record of which effects have already been attempted.

The unique index on ``(tenant_id, scope, key_hash)`` is what makes this correct,
not the read that precedes the write. Two workers replaying the same graph node
at the same moment both find nothing and both try to insert; exactly one wins,
and the loser is told the attempt is already in flight. A check-then-insert
without the index would let both through.

Keys are stored hashed. The derived keys are already digests, but a
caller-supplied one is not, and a table holding raw keys becomes a table that
can be read to replay someone else's action.

In-flight claims are not forever. A worker that crashes between claiming and
completing leaves a row whose answer will never arrive; :data:`RETENTION` bounds
how long such a row blocks its key. The store reclaims an expired in-flight
claim lazily on the next :meth:`PostgresIdempotencyStore.begin`, and
:meth:`PostgresIdempotencyStore.sweep` deletes finished rows past retention so
the table stays a working set. The composition root runs the sweep periodically;
there is no separate sweeper process.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncEngine

from tenantchat.api.persistence.tenancy import require_active_tenant
from tenantchat.core.errors import ConflictError, NotFoundError
from tenantchat.core.ports import IdempotencyKey

# How long a key keeps its answer, and how long a crashed in-flight claim keeps
# blocking its key. Long enough to cover any retry a resumed conversation could
# make, short enough that the table stays a working set rather than an archive.
# The store itself enforces both: lazily on claim, and through the periodic
# sweep the composition root runs.
RETENTION: Final = timedelta(days=7)


def _hashed(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class PostgresIdempotencyStore:
    """Claim-then-complete over the ``idempotency_keys`` table."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def begin(
        self,
        tenant_id: str,
        *,
        scope: str,
        key: IdempotencyKey,
        fingerprint: str,
    ) -> dict[str, object] | None:
        """Claim the key, or return the completed response it already carries.

        A crashed attempt's claim is reclaimable once it expires, so the caller
        may be handed the key again instead of being conflict-blocked forever.

        Raises:
            NotFoundError: the tenant does not exist or is not active.
            ConflictError: a live attempt with this key is still in flight, or
                the key was used for a materially different request.
        """
        key_hash = _hashed(key.value)
        async with self._engine.begin() as connection:
            await require_active_tenant(connection, tenant_id)
            claimed = await connection.execute(
                text(
                    """
                    INSERT INTO idempotency_keys
                        (id, tenant_id, scope, key_hash, request_hash, status, expires_at)
                    VALUES
                        (:id, :tenant_id, :scope, :key_hash, :request_hash,
                         'in_progress', :expires_at)
                    ON CONFLICT (tenant_id, scope, key_hash) DO NOTHING
                    RETURNING id
                    """
                ),
                {
                    "id": uuid.uuid4(),
                    "tenant_id": tenant_id,
                    "scope": scope,
                    "key_hash": key_hash,
                    "request_hash": fingerprint,
                    "expires_at": datetime.now(UTC) + RETENTION,
                },
            )
            if claimed.scalar_one_or_none() is not None:
                return None

            existing = await connection.execute(
                text(
                    """
                    SELECT request_hash, status, response, expires_at
                    FROM idempotency_keys
                    WHERE tenant_id = :tenant_id AND scope = :scope AND key_hash = :key_hash
                    """
                ),
                {"tenant_id": tenant_id, "scope": scope, "key_hash": key_hash},
            )
            row = existing.one()

            if row.request_hash != fingerprint:
                raise ConflictError(detail=f"idempotency key reused for a different {scope}")
            if row.status == "completed" and row.response is not None:
                return dict(row.response)

            # An expired in-flight row is a crashed attempt: reclaim it
            # atomically, so a race between two claimants still admits exactly
            # one. The conditional update is the arbiter, not the read above.
            if row.status == "in_progress" and row.expires_at <= datetime.now(UTC):
                reclaimed = await connection.execute(
                    text(
                        """
                        UPDATE idempotency_keys
                        SET request_hash = :request_hash, status = 'in_progress',
                            response = NULL, completed_at = NULL,
                            expires_at = :expires_at
                        WHERE tenant_id = :tenant_id AND scope = :scope
                          AND key_hash = :key_hash
                          AND status = 'in_progress' AND expires_at <= now()
                        RETURNING id
                        """
                    ),
                    {
                        "tenant_id": tenant_id,
                        "scope": scope,
                        "key_hash": key_hash,
                        "request_hash": fingerprint,
                        "expires_at": datetime.now(UTC) + RETENTION,
                    },
                )
                if reclaimed.scalar_one_or_none() is not None:
                    return None

        raise ConflictError(detail=f"an earlier {scope} attempt is still in flight")

    async def complete(
        self,
        tenant_id: str,
        *,
        scope: str,
        key: IdempotencyKey,
        response: dict[str, object],
    ) -> None:
        """Record what the committed action produced.

        Raises:
            NotFoundError: the key was never claimed, so there is nothing this
                response belongs to.
        """
        statement = text(
            """
            UPDATE idempotency_keys
            SET status = 'completed', response = :response, completed_at = now()
            WHERE tenant_id = :tenant_id AND scope = :scope AND key_hash = :key_hash
              AND status = 'in_progress'
            """
        ).bindparams(bindparam("response", type_=JSONB))

        async with self._engine.begin() as connection:
            updated = await connection.execute(
                statement,
                {
                    "tenant_id": tenant_id,
                    "scope": scope,
                    "key_hash": _hashed(key.value),
                    "response": dict(response),
                },
            )
            if updated.rowcount != 1:
                raise NotFoundError(detail=f"no claimed {scope} attempt for this key")

    async def sweep(self) -> int:
        """Delete rows past retention; returns how many were removed.

        A completed row's answer has served its retries once its window closes,
        and an in-flight row that outlived its window belongs to a crashed
        attempt whose key :meth:`begin` can then re-claim fresh. Deletes rather
        than updates: the table is a working set, not an archive.
        """
        async with self._engine.begin() as connection:
            result = await connection.execute(
                text(
                    """
                    DELETE FROM idempotency_keys
                    WHERE expires_at <= now()
                    """
                )
            )
            return int(result.rowcount)
