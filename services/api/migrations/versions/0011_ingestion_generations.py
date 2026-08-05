"""Index generations and content-free index-integrity findings.

Revision ID: 0011_ingestion_generations
Revises: 0010_trace_privacy
Create Date: 2026-08-05
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0011_ingestion_generations"
down_revision: str | None = "0010_trace_privacy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Persist what an ingestion job produced and what integrity checks found.

    Two rules make the tables safe to rely on:

    - ``uq_generations_tenant_version`` — one generation per version, keyed by
      the deterministic identifier the ingestion worker derives, so a retried
      job rewrites its own record instead of creating rivals.
    - ``ck_findings_detail_content_free`` — the finding payload is bounded by
      construction: the application writes counts, model names, and thresholds
      through ``IndexIntegrityFinding``, and the column exists to carry that
      payload, never document text.
    """
    op.execute(
        """
        CREATE TYPE knowledge_generation_status AS ENUM (
            'in_progress', 'complete', 'failed'
        );

        CREATE TABLE knowledge_index_generations (
            id uuid PRIMARY KEY,
            tenant_id varchar(63) NOT NULL,
            document_id uuid NOT NULL,
            version_id uuid NOT NULL,
            -- Immutable component identifiers `OBS-004` pins a replay to.
            parser_version varchar(100) NOT NULL,
            chunker_version varchar(100) NOT NULL,
            embedding_model varchar(200) NOT NULL,
            status knowledge_generation_status NOT NULL DEFAULT 'in_progress',
            chunk_count integer NOT NULL DEFAULT 0,
            indexed_chunk_count integer NOT NULL DEFAULT 0,
            started_at timestamptz NOT NULL,
            completed_at timestamptz,
            CONSTRAINT fk_generations_document
                FOREIGN KEY (tenant_id, document_id)
                REFERENCES knowledge_documents (tenant_id, id) ON DELETE RESTRICT,
            CONSTRAINT fk_generations_version
                FOREIGN KEY (tenant_id, version_id)
                REFERENCES knowledge_document_versions (tenant_id, id) ON DELETE RESTRICT,
            CONSTRAINT uq_generations_tenant_id UNIQUE (tenant_id, id),
            CONSTRAINT uq_generations_tenant_version UNIQUE (tenant_id, version_id),
            CONSTRAINT ck_generations_counts CHECK
                (chunk_count >= 0 AND indexed_chunk_count BETWEEN 0 AND chunk_count),
            CONSTRAINT ck_generations_completed_state CHECK
                ((status = 'complete' OR status = 'failed')
                 = (completed_at IS NOT NULL))
        );

        CREATE TABLE knowledge_index_findings (
            id uuid PRIMARY KEY,
            tenant_id varchar(63) NOT NULL,
            document_id uuid NOT NULL,
            version_id uuid NOT NULL,
            generation_id uuid,
            code varchar(100) NOT NULL,
            detail jsonb NOT NULL DEFAULT '{}'::jsonb,
            detected_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT fk_findings_document
                FOREIGN KEY (tenant_id, document_id)
                REFERENCES knowledge_documents (tenant_id, id) ON DELETE RESTRICT,
            CONSTRAINT fk_findings_version
                FOREIGN KEY (tenant_id, version_id)
                REFERENCES knowledge_document_versions (tenant_id, id) ON DELETE RESTRICT,
            CONSTRAINT fk_findings_generation
                FOREIGN KEY (tenant_id, generation_id)
                REFERENCES knowledge_index_generations (tenant_id, id) ON DELETE SET NULL,
            CONSTRAINT uq_findings_tenant_version_code
                UNIQUE (tenant_id, version_id, code),
            CONSTRAINT ck_findings_code_format
                CHECK (code ~ '^[a-z][a-z0-9_.-]{0,99}$'),
            CONSTRAINT ck_findings_detail_object CHECK (jsonb_typeof(detail) = 'object')
        );

        CREATE INDEX ix_findings_tenant_detected
            ON knowledge_index_findings (tenant_id, detected_at DESC);
        """
    )


def downgrade() -> None:
    """Remove the ingestion lifecycle bookkeeping; chunks stay derived."""
    op.execute(
        """
        DROP TABLE knowledge_index_findings;
        DROP TABLE knowledge_index_generations;
        DROP TYPE knowledge_generation_status;
        """
    )
