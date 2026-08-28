"""PostgreSQL adapters for the PRIV-002 inference plane.

``PostgresTurnRecordStore`` is the envelope `OBS-004` populates: it writes
the opaque content object and reads it back for the trace viewer, and it
serves the content-free attribution query surface (`OBS-004`) over the
derived columns — outcome, component-manifest hash, and diagnosis causes.
The erasure and retention of these rows deliberately stay in
:mod:`tenantchat.api.persistence.privacy`, which runs under the erasure
role — the application role holds no ``DELETE`` on ``turn_records`` or
``turn_record_projections`` (see ``provision_app_role.sql``).

``PostgresTraceAccessStore`` is the dedicated read role, a grant table rather
than a membership role: it is tenant-qualified, audited on grant and revoke by
the calling route, and orthogonal to transcript memberships.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Mapping
from datetime import UTC, datetime

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from tenantchat.api.persistence.tenancy import require_active_tenant
from tenantchat.api.store import TraceAccessGrant, TurnRecord, TurnRecordProjection
from tenantchat.core.errors import NotFoundError

_MAX_TRACE_SEARCH_LIMIT = 200

_TRACE_COLUMNS = (
    "id",
    "tenant_id",
    "chat_session_id",
    "trace_id",
    "content",
    "recorded_at",
    "outcome",
    "component_manifest_hash",
    "diagnosis_causes",
    "diagnosis_statuses",
    "turn_index",
    "trace_schema_version",
    "source_generation_ids",
)

# A module constant, never a format site: every read selects the same columns.
_TRACE_SELECT = "SELECT " + ", ".join(_TRACE_COLUMNS)


def _turn_record(row: object) -> TurnRecord:
    mapping = row._mapping  # type: ignore[attr-defined]
    return TurnRecord(
        turn_id=mapping["id"],
        tenant_id=mapping["tenant_id"],
        session_id=mapping["chat_session_id"],
        trace_id=mapping["trace_id"],
        content=dict(mapping["content"]),
        recorded_at=mapping["recorded_at"],
        outcome=mapping["outcome"],
        component_manifest_hash=mapping["component_manifest_hash"],
        diagnosis_causes=tuple(mapping["diagnosis_causes"]),
        diagnosis_statuses=tuple(mapping["diagnosis_statuses"]),
        turn_index=mapping["turn_index"],
        trace_schema_version=mapping["trace_schema_version"],
        source_generation_ids=tuple(mapping["source_generation_ids"]),
    )


def _grant(row: object) -> TraceAccessGrant:
    mapping = row._mapping  # type: ignore[attr-defined]
    return TraceAccessGrant(
        tenant_id=mapping["tenant_id"],
        principal_subject=mapping["principal_subject"],
        granted_at=mapping["granted_at"],
        granted_by=mapping["granted_by"],
    )


def _projection(row: object) -> TurnRecordProjection:
    mapping = row._mapping  # type: ignore[attr-defined]
    return TurnRecordProjection(
        projection_id=mapping["id"],
        tenant_id=mapping["tenant_id"],
        turn_record_id=mapping["turn_record_id"],
        kind=mapping["kind"],
        created_at=mapping["created_at"],
        payload=dict(mapping["payload"]),
    )


class PostgresTurnRecordStore:
    """The turn-record envelope over the application role's engine."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def record(
        self,
        tenant_id: str,
        session_id: uuid.UUID,
        *,
        content: dict[str, object],
        trace_id: str | None = None,
        recorded_at: datetime | None = None,
        outcome: str = "unknown",
        component_manifest_hash: str = "",
        diagnosis_causes: tuple[str, ...] = (),
        diagnosis_statuses: tuple[str, ...] = (),
        turn_index: int = 0,
        trace_schema_version: str = "1",
        source_generation_ids: tuple[uuid.UUID, ...] = (),
    ) -> TurnRecord:
        try:
            async with self._engine.begin() as connection:
                # The session row must exist and belong to the tenant; the
                # composite foreign key is the second half of that proof.
                await require_active_tenant(connection, tenant_id)
                result = await connection.execute(
                    text(
                        """
                        INSERT INTO turn_records
                            (id, tenant_id, chat_session_id, trace_id, content, recorded_at,
                             outcome, component_manifest_hash, diagnosis_causes,
                             diagnosis_statuses, turn_index, trace_schema_version,
                             source_generation_ids)
                        VALUES
                            (:id, :tenant_id, :session_id, :trace_id, :content, :recorded_at,
                             :outcome, :manifest_hash, :diagnosis_causes, :diagnosis_statuses,
                             :turn_index, :schema_version, :source_generation_ids)
                        RETURNING id, tenant_id, chat_session_id, trace_id, content, recorded_at,
                                  outcome, component_manifest_hash, diagnosis_causes,
                                  diagnosis_statuses, turn_index, trace_schema_version,
                                  source_generation_ids
                        """
                    ).bindparams(bindparam("content", type_=JSONB)),
                    {
                        "id": uuid.uuid4(),
                        "tenant_id": tenant_id,
                        "session_id": session_id,
                        "trace_id": trace_id,
                        "content": content,
                        "recorded_at": recorded_at or datetime.now(UTC),
                        "outcome": outcome,
                        "manifest_hash": component_manifest_hash,
                        "diagnosis_causes": list(diagnosis_causes),
                        "diagnosis_statuses": list(diagnosis_statuses),
                        "turn_index": turn_index,
                        "schema_version": trace_schema_version,
                        "source_generation_ids": list(source_generation_ids),
                    },
                )
                return _turn_record(result.one())
        except IntegrityError as exc:
            # Only the session foreign key is an authorization boundary: a
            # session that belongs to another tenant is indistinguishable from
            # one that never existed, and the SQL DETAIL must not become an
            # exception message anyone logs. Any other integrity failure (a
            # CHECK this build drifted from, for example) is a server bug and
            # must surface as such instead of being relabelled a session 404.
            diag = getattr(exc.orig, "diag", None) if exc.orig is not None else None
            if getattr(diag, "constraint_name", None) != "fk_turn_records_session":
                raise
            raise NotFoundError(detail="session absent or outside tenant") from exc

    async def get(self, tenant_id: str, turn_id: uuid.UUID) -> TurnRecord:
        async with self._engine.begin() as connection:
            result = await connection.execute(
                text(
                    _TRACE_SELECT
                    + " FROM turn_records WHERE tenant_id = :tenant_id AND id = :turn_id"
                ),
                {"tenant_id": tenant_id, "turn_id": turn_id},
            )
            row = result.first()
        if row is None:
            raise NotFoundError(detail="turn record absent or outside tenant")
        return _turn_record(row)

    async def for_session(
        self, tenant_id: str, session_id: uuid.UUID, *, limit: int
    ) -> tuple[TurnRecord, ...]:
        bounded = min(limit, _MAX_TRACE_SEARCH_LIMIT)
        async with self._engine.begin() as connection:
            result = await connection.execute(
                text(
                    _TRACE_SELECT + " FROM turn_records WHERE tenant_id = :tenant_id AND "
                    "chat_session_id = :session_id ORDER BY recorded_at, id LIMIT :limit"
                ),
                {"tenant_id": tenant_id, "session_id": session_id, "limit": bounded},
            )
            return tuple(_turn_record(row) for row in result.all())

    async def for_turn_ids(
        self, tenant_id: str, turn_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, TurnRecord]:
        """Batch the queue's turn fetches into one `id = ANY(...)` query.

        The review queue names up to 200 turns per page; fetching them one
        transaction each made the list view quadratic. Missing ids are absent
        from the result, never an error: a queue row whose turn was purged
        must not take the whole page down.
        """
        if not turn_ids:
            return {}
        async with self._engine.begin() as connection:
            result = await connection.execute(
                text(
                    _TRACE_SELECT
                    + " FROM turn_records WHERE tenant_id = :tenant_id AND id = ANY(:turn_ids)"
                ),
                {"tenant_id": tenant_id, "turn_ids": list(turn_ids)},
            )
            records = (_turn_record(row) for row in result.all())
            return {record.turn_id: record for record in records}

    async def for_trace_id(self, tenant_id: str, trace_id: str) -> TurnRecord:
        async with self._engine.begin() as connection:
            result = await connection.execute(
                text(
                    _TRACE_SELECT
                    + " FROM turn_records WHERE tenant_id = :tenant_id AND trace_id = :trace_id "
                    "ORDER BY recorded_at DESC LIMIT 1"
                ),
                {"tenant_id": tenant_id, "trace_id": trace_id},
            )
            row = result.first()
        if row is None:
            raise NotFoundError(detail="turn record absent or outside tenant")
        return _turn_record(row)

    async def search(
        self,
        tenant_id: str,
        *,
        manifest_hash: str | None = None,
        causes: tuple[str, ...] = (),
        statuses: tuple[str, ...] = (),
        outcome: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
        generation_ids: tuple[uuid.UUID, ...] = (),
    ) -> tuple[TurnRecord, ...]:
        clauses = ["tenant_id = :tenant_id"]
        params: dict[str, object] = {"tenant_id": tenant_id}
        if manifest_hash is not None:
            clauses.append("component_manifest_hash = :manifest_hash")
            params["manifest_hash"] = manifest_hash
        if causes:
            clauses.append("diagnosis_causes @> :causes")
            params["causes"] = list(causes)
        if statuses:
            clauses.append("diagnosis_statuses @> :statuses")
            params["statuses"] = list(statuses)
        if outcome is not None:
            clauses.append("outcome = :outcome")
            params["outcome"] = outcome
        if since is not None:
            clauses.append("recorded_at >= :since")
            params["since"] = since
        if until is not None:
            clauses.append("recorded_at <= :until")
            params["until"] = until
        if generation_ids:
            clauses.append("source_generation_ids @> :generation_ids")
            params["generation_ids"] = list(generation_ids)
        bounded = min(limit, _MAX_TRACE_SEARCH_LIMIT)
        # The WHERE clause is built from a fixed clause list and bound
        # parameters only; no caller text ever reaches the statement.
        async with self._engine.begin() as connection:
            result = await connection.execute(
                text(
                    _TRACE_SELECT
                    + " FROM turn_records WHERE "
                    + " AND ".join(clauses)
                    + " ORDER BY recorded_at DESC, id LIMIT :limit"
                ),
                {**params, "limit": bounded},
            )
            return tuple(_turn_record(row) for row in result.all())

    async def projections_for_turn(
        self, tenant_id: str, turn_id: uuid.UUID
    ) -> tuple[TurnRecordProjection, ...]:
        async with self._engine.begin() as connection:
            result = await connection.execute(
                text(
                    """
                    SELECT id, tenant_id, turn_record_id, kind, created_at, payload
                    FROM turn_record_projections
                    WHERE tenant_id = :tenant_id AND turn_record_id = :turn_id
                    ORDER BY created_at, id
                    """
                ),
                {"tenant_id": tenant_id, "turn_id": turn_id},
            )
            return tuple(_projection(row) for row in result.all())

    async def create_projection(
        self,
        tenant_id: str,
        turn_id: uuid.UUID,
        *,
        kind: str,
        payload: Mapping[str, object],
    ) -> TurnRecordProjection:
        """Pin a derived dataset (an `FEAT-008` evaluation case) to a turn.

        The composite foreign key proves the turn exists and belongs to the
        tenant; its violation is a plain 404, like every other absent-or-
        outside-tenant read.
        """
        try:
            async with self._engine.begin() as connection:
                await require_active_tenant(connection, tenant_id)
                result = await connection.execute(
                    text(
                        """
                        INSERT INTO turn_record_projections
                            (id, tenant_id, turn_record_id, kind, payload)
                        VALUES
                            (:id, :tenant_id, :turn_id, :kind, :payload)
                        RETURNING id, tenant_id, turn_record_id, kind, created_at, payload
                        """
                    ).bindparams(bindparam("payload", type_=JSONB)),
                    {
                        "id": uuid.uuid4(),
                        "tenant_id": tenant_id,
                        "turn_id": turn_id,
                        "kind": kind,
                        "payload": dict(payload),
                    },
                )
                return _projection(result.one())
        except IntegrityError as exc:
            raise NotFoundError(detail="turn record absent or outside tenant") from exc


class PostgresTraceAccessStore:
    """The dedicated trace-read grant, tenant-qualified like a membership."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def grant(self, tenant_id: str, subject: str, *, granted_by: str) -> TraceAccessGrant:
        async with self._engine.begin() as connection:
            await require_active_tenant(connection, tenant_id)
            result = await connection.execute(
                text(
                    """
                    INSERT INTO trace_access_grants
                        (tenant_id, principal_subject, granted_by)
                    VALUES
                        (:tenant_id, :subject, :granted_by)
                    ON CONFLICT (tenant_id, principal_subject)
                    DO UPDATE SET granted_by = EXCLUDED.granted_by
                    RETURNING tenant_id, principal_subject, granted_at, granted_by
                    """
                ),
                {"tenant_id": tenant_id, "subject": subject, "granted_by": granted_by},
            )
            return _grant(result.one())

    async def revoke(self, tenant_id: str, subject: str) -> bool:
        async with self._engine.begin() as connection:
            result = await connection.execute(
                text(
                    """
                    DELETE FROM trace_access_grants
                    WHERE tenant_id = :tenant_id AND principal_subject = :subject
                    """
                ),
                {"tenant_id": tenant_id, "subject": subject},
            )
            return (result.rowcount or 0) > 0

    async def has_access(self, tenant_id: str, subject: str) -> bool:
        async with self._engine.begin() as connection:
            result = await connection.execute(
                text(
                    """
                    SELECT 1 FROM trace_access_grants
                    WHERE tenant_id = :tenant_id AND principal_subject = :subject
                    """
                ),
                {"tenant_id": tenant_id, "subject": subject},
            )
            return result.first() is not None

    async def for_tenant(self, tenant_id: str) -> tuple[TraceAccessGrant, ...]:
        async with self._engine.begin() as connection:
            result = await connection.execute(
                text(
                    """
                    SELECT tenant_id, principal_subject, granted_at, granted_by
                    FROM trace_access_grants
                    WHERE tenant_id = :tenant_id
                    ORDER BY principal_subject
                    """
                ),
                {"tenant_id": tenant_id},
            )
            return tuple(_grant(row) for row in result.all())
