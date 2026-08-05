"""PostgreSQL repository for index generations and index-integrity findings.

Generations and findings are the durable half of `OBS-004`'s failure
attribution: a generation row is what a replay pins itself to, and a finding
row is what an operator console or a trace query reads without touching the
search index. Both tables are tenant-qualified and content-free by schema —
``detail`` is JSON but the application only ever writes bounded values through
:class:`~tenantchat.core.indexing.IndexIntegrityFinding`.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tenantchat.api.persistence.tenancy import require_active_tenant
from tenantchat.core.indexing import (
    GenerationStatus,
    IndexGeneration,
    IndexingFault,
    IndexIntegrityFinding,
)

_GENERATION_COLUMNS = """
    id, tenant_id, document_id, version_id, parser_version, chunker_version,
    embedding_model, status, chunk_count, indexed_chunk_count, started_at, completed_at
"""

_FINDING_COLUMNS = """
    id, tenant_id, document_id, version_id, generation_id, code, detail,
    detected_at
"""


def _generation(row: object) -> IndexGeneration:
    mapping = row._mapping  # type: ignore[attr-defined]
    return IndexGeneration(
        generation_id=mapping["id"],
        tenant_id=mapping["tenant_id"],
        document_id=mapping["document_id"],
        version_id=mapping["version_id"],
        parser_version=mapping["parser_version"],
        chunker_version=mapping["chunker_version"],
        embedding_model=mapping["embedding_model"],
        status=GenerationStatus(mapping["status"]),
        chunk_count=mapping["chunk_count"],
        indexed_chunk_count=mapping["indexed_chunk_count"],
        started_at=mapping["started_at"],
        completed_at=mapping["completed_at"],
    )


def _finding(row: object) -> IndexIntegrityFinding:
    mapping = row._mapping  # type: ignore[attr-defined]
    return IndexIntegrityFinding(
        code=IndexingFault(mapping["code"]),
        tenant_id=mapping["tenant_id"],
        document_id=mapping["document_id"],
        version_id=mapping["version_id"],
        generation_id=mapping["generation_id"],
        detected_at=mapping["detected_at"],
        detail=dict(mapping["detail"]),
    )


class PostgresIndexIntegrityStore:
    """Index generations and findings over the application database."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def begin_generation(self, generation: IndexGeneration) -> IndexGeneration:
        return await self._upsert_generation(generation)

    async def complete_generation(self, generation: IndexGeneration) -> IndexGeneration:
        return await self._upsert_generation(generation)

    async def fail_generation(self, generation: IndexGeneration) -> IndexGeneration:
        return await self._upsert_generation(generation)

    async def _upsert_generation(self, generation: IndexGeneration) -> IndexGeneration:
        statement = text(
            f"""
            INSERT INTO knowledge_index_generations
                (id, tenant_id, document_id, version_id, parser_version,
                 chunker_version, embedding_model, status, chunk_count,
                 indexed_chunk_count, started_at, completed_at)
            VALUES
                (:id, :tenant_id, :document_id, :version_id, :parser_version,
                 :chunker_version, :embedding_model, :status, :chunk_count,
                 :indexed_chunk_count, :started_at, :completed_at)
            ON CONFLICT (tenant_id, version_id) DO UPDATE SET
                parser_version = EXCLUDED.parser_version,
                chunker_version = EXCLUDED.chunker_version,
                embedding_model = EXCLUDED.embedding_model,
                status = EXCLUDED.status,
                chunk_count = EXCLUDED.chunk_count,
                indexed_chunk_count = EXCLUDED.indexed_chunk_count,
                completed_at = EXCLUDED.completed_at
            RETURNING {_GENERATION_COLUMNS}
            """
        )
        async with self._engine.begin() as connection:
            result = await connection.execute(
                statement,
                {
                    "id": generation.generation_id,
                    "tenant_id": generation.tenant_id,
                    "document_id": generation.document_id,
                    "version_id": generation.version_id,
                    "parser_version": generation.parser_version,
                    "chunker_version": generation.chunker_version,
                    "embedding_model": generation.embedding_model,
                    "status": generation.status.value,
                    "chunk_count": generation.chunk_count,
                    "indexed_chunk_count": generation.indexed_chunk_count,
                    "started_at": generation.started_at,
                    "completed_at": generation.completed_at,
                },
            )
            return _generation(result.one())

    async def generation(self, tenant_id: str, version_id: uuid.UUID) -> IndexGeneration | None:
        async with self._engine.begin() as connection:
            result = await connection.execute(
                text(
                    f"""
                    SELECT {_GENERATION_COLUMNS}
                    FROM knowledge_index_generations
                    WHERE tenant_id = :tenant_id AND version_id = :version_id
                    """  # noqa: S608 - _GENERATION_COLUMNS is a module constant
                ),
                {"tenant_id": tenant_id, "version_id": version_id},
            )
            row = result.one_or_none()
        return None if row is None else _generation(row)

    async def generations_for_tenant(self, tenant_id: str) -> tuple[IndexGeneration, ...]:
        async with self._engine.begin() as connection:
            await require_active_tenant(connection, tenant_id)
            result = await connection.execute(
                text(
                    f"""
                    SELECT {_GENERATION_COLUMNS}
                    FROM knowledge_index_generations
                    WHERE tenant_id = :tenant_id
                    ORDER BY started_at
                    """  # noqa: S608 - _GENERATION_COLUMNS is a module constant
                ),
                {"tenant_id": tenant_id},
            )
            return tuple(_generation(row) for row in result.all())

    async def sync_findings(
        self, tenant_id: str, findings: Sequence[IndexIntegrityFinding]
    ) -> None:
        """Replace the tenant's persisted findings with the detected set.

        One transaction: the set the detector saw is the truth, and any finding
        absent from it is resolved. Re-detected findings keep their first
        detection timestamp (``detected_at = MIN(existing, incoming)``) so a
        console can show how long a fault has been open.
        """
        async with self._engine.begin() as connection:
            await require_active_tenant(connection, tenant_id)
            for finding in findings:
                await self._insert_finding(connection, finding)
            if findings:
                tuples = ", ".join(
                    f"(CAST(:v{i} AS uuid), CAST(:c{i} AS text))" for i in range(len(findings))
                )
                parameters: dict[str, object] = {"tenant_id": tenant_id}
                for index, finding in enumerate(findings):
                    parameters[f"v{index}"] = finding.version_id
                    parameters[f"c{index}"] = finding.code.value
                await connection.execute(
                    text(
                        f"""
                        DELETE FROM knowledge_index_findings
                        WHERE tenant_id = :tenant_id
                          AND (version_id, code) NOT IN (
                              SELECT v, c FROM (VALUES {tuples}) AS detected(v, c)
                          )
                        """  # noqa: S608 - tuples is built from a bounded sequence
                    ),
                    parameters,
                )
            else:
                await connection.execute(
                    text("DELETE FROM knowledge_index_findings WHERE tenant_id = :tenant_id"),
                    {"tenant_id": tenant_id},
                )

    async def _insert_finding(
        self, connection: AsyncConnection, finding: IndexIntegrityFinding
    ) -> None:
        statement = text(
            """
            INSERT INTO knowledge_index_findings
                (id, tenant_id, document_id, version_id, generation_id, code,
                 detail, detected_at)
            VALUES
                (:id, :tenant_id, :document_id, :version_id, :generation_id,
                 :code, :detail, :detected_at)
            ON CONFLICT (tenant_id, version_id, code) DO UPDATE SET
                generation_id = EXCLUDED.generation_id,
                detail = EXCLUDED.detail,
                detected_at = LEAST(
                    knowledge_index_findings.detected_at, EXCLUDED.detected_at
                )
            """
        ).bindparams(bindparam("detail", type_=JSONB))
        await connection.execute(
            statement,
            {
                "id": uuid.uuid4(),
                "tenant_id": finding.tenant_id,
                "document_id": finding.document_id,
                "version_id": finding.version_id,
                "generation_id": finding.generation_id,
                "code": finding.code.value,
                "detail": dict(finding.detail),
                "detected_at": finding.detected_at,
            },
        )

    async def active_findings(self, tenant_id: str) -> tuple[IndexIntegrityFinding, ...]:
        async with self._engine.begin() as connection:
            await require_active_tenant(connection, tenant_id)
            result = await connection.execute(
                text(
                    f"""
                    SELECT {_FINDING_COLUMNS}
                    FROM knowledge_index_findings
                    WHERE tenant_id = :tenant_id
                    ORDER BY detected_at, version_id, code
                    """  # noqa: S608 - _FINDING_COLUMNS is a module constant
                ),
                {"tenant_id": tenant_id},
            )
            return tuple(_finding(row) for row in result.all())
