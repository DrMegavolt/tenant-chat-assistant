"""Store versioned, tenant-owned knowledge as the system of record.

Revision ID: 0003_knowledge
Revises: 0002_repositories
Create Date: 2026-08-03
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003_knowledge"
down_revision: str | None = "0002_repositories"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ENUMS = (
    ("knowledge_source_kind", ("upload", "url", "manual")),
    (
        "knowledge_version_state",
        ("draft", "approved", "published", "superseded", "deleted"),
    ),
    ("knowledge_indexing_state", ("pending", "indexing", "indexed", "failed")),
    ("knowledge_visibility", ("public", "internal")),
)


def upgrade() -> None:
    """Create the RAG-001 knowledge tables.

    Two rules are enforced here rather than only in the domain, because the
    retrieval index is rebuilt from these rows and a violation would be published
    to visitors before any code path noticed:

    - ``uq_knowledge_versions_one_published_per_document`` — a partial unique
      index that makes "publish supersedes the current version" atomic. Two
      concurrent publishes cannot both win, so a rollback can never leave two
      versions answering the same question differently.
    - ``uq_knowledge_versions_tenant_document_checksum`` — identical content
      cannot become a second revision, which is what makes re-ingesting an
      unchanged document idempotent (`RAG-002`).

    ``domain`` is denormalized onto documents and versions and then pinned to its
    source by composite foreign keys, so the copies are provably equal rather
    than equal by convention. Retrieval filters on tenant and domain; a version
    whose domain silently disagreed with its source's would answer under the
    wrong filter.
    """
    for name, values in ENUMS:
        quoted = ", ".join(f"'{value}'" for value in values)
        op.execute(f"CREATE TYPE {name} AS ENUM ({quoted})")

    op.execute(
        """
        CREATE TABLE knowledge_sources (
            id uuid PRIMARY KEY,
            tenant_id varchar(63) NOT NULL,
            domain varchar(63) NOT NULL,
            kind knowledge_source_kind NOT NULL,
            display_name varchar(200) NOT NULL,
            enabled boolean NOT NULL DEFAULT true,
            -- An object-storage key or a crawl URL, written by the ingestion
            -- worker. Never a caller-supplied filesystem path: that is the
            -- prototype vulnerability RAG-002 exists to remove.
            external_reference varchar(1024),
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT fk_knowledge_sources_tenant FOREIGN KEY (tenant_id)
                REFERENCES tenants (id) ON DELETE RESTRICT,
            CONSTRAINT uq_knowledge_sources_tenant_id_id UNIQUE (tenant_id, id),
            CONSTRAINT uq_knowledge_sources_tenant_domain_id UNIQUE (tenant_id, domain, id),
            CONSTRAINT uq_knowledge_sources_tenant_domain_name
                UNIQUE (tenant_id, domain, display_name),
            CONSTRAINT ck_knowledge_sources_domain_format
                CHECK (domain ~ '^[a-z][a-z0-9-]{1,62}$'),
            CONSTRAINT ck_knowledge_sources_display_name_not_blank
                CHECK (btrim(display_name) <> ''),
            CONSTRAINT ck_knowledge_sources_timestamps CHECK (updated_at >= created_at)
        );

        CREATE TABLE knowledge_documents (
            id uuid PRIMARY KEY,
            tenant_id varchar(63) NOT NULL,
            domain varchar(63) NOT NULL,
            source_id uuid NOT NULL,
            -- Stable identity within the source: the upload filename or the
            -- crawled path. Re-ingesting the same key revises this document
            -- instead of creating a rival one.
            external_key varchar(512) NOT NULL,
            title varchar(300) NOT NULL,
            deleted_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT fk_knowledge_documents_source
                FOREIGN KEY (tenant_id, domain, source_id)
                REFERENCES knowledge_sources (tenant_id, domain, id) ON DELETE RESTRICT,
            CONSTRAINT uq_knowledge_documents_tenant_id_id UNIQUE (tenant_id, id),
            CONSTRAINT uq_knowledge_documents_tenant_domain_id UNIQUE (tenant_id, domain, id),
            CONSTRAINT uq_knowledge_documents_tenant_source_key
                UNIQUE (tenant_id, source_id, external_key),
            CONSTRAINT ck_knowledge_documents_external_key_not_blank
                CHECK (btrim(external_key) <> ''),
            CONSTRAINT ck_knowledge_documents_title_not_blank CHECK (btrim(title) <> ''),
            CONSTRAINT ck_knowledge_documents_timestamps CHECK (updated_at >= created_at)
        );

        CREATE TABLE knowledge_document_versions (
            id uuid PRIMARY KEY,
            tenant_id varchar(63) NOT NULL,
            domain varchar(63) NOT NULL,
            document_id uuid NOT NULL,
            revision integer NOT NULL,
            state knowledge_version_state NOT NULL DEFAULT 'draft',
            indexing_state knowledge_indexing_state NOT NULL DEFAULT 'pending',
            visibility knowledge_visibility NOT NULL DEFAULT 'public',
            checksum char(64) NOT NULL,
            byte_size bigint NOT NULL,
            media_type varchar(120) NOT NULL,
            storage_key varchar(1024) NOT NULL,
            effective_at timestamptz,
            expires_at timestamptz,
            approved_at timestamptz,
            approved_by varchar(200),
            published_at timestamptz,
            superseded_at timestamptz,
            deleted_at timestamptz,
            indexed_at timestamptz,
            index_error_code varchar(100),
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT fk_knowledge_versions_document
                FOREIGN KEY (tenant_id, domain, document_id)
                REFERENCES knowledge_documents (tenant_id, domain, id) ON DELETE RESTRICT,
            CONSTRAINT uq_knowledge_versions_tenant_id_id UNIQUE (tenant_id, id),
            CONSTRAINT uq_knowledge_versions_tenant_document_revision
                UNIQUE (tenant_id, document_id, revision),
            CONSTRAINT uq_knowledge_versions_tenant_document_checksum
                UNIQUE (tenant_id, document_id, checksum),
            CONSTRAINT ck_knowledge_versions_revision_positive CHECK (revision > 0),
            CONSTRAINT ck_knowledge_versions_byte_size_positive CHECK (byte_size > 0),
            CONSTRAINT ck_knowledge_versions_checksum_format CHECK (checksum ~ '^[0-9a-f]{64}$'),
            CONSTRAINT ck_knowledge_versions_media_type_not_blank
                CHECK (btrim(media_type) <> ''),
            CONSTRAINT ck_knowledge_versions_storage_key_not_blank
                CHECK (btrim(storage_key) <> ''),
            -- The effective window is half-open, so an equal pair would describe
            -- a version that is never retrievable.
            CONSTRAINT ck_knowledge_versions_window
                CHECK (expires_at IS NULL
                       OR (effective_at IS NOT NULL AND expires_at > effective_at)),
            CONSTRAINT ck_knowledge_versions_approved_state
                CHECK (state NOT IN ('approved', 'published', 'superseded')
                       OR (approved_at IS NOT NULL AND approved_by IS NOT NULL)),
            CONSTRAINT ck_knowledge_versions_published_state
                CHECK (state NOT IN ('published', 'superseded') OR published_at IS NOT NULL),
            CONSTRAINT ck_knowledge_versions_current_state
                CHECK (state <> 'published'
                       OR (effective_at IS NOT NULL AND superseded_at IS NULL
                           AND deleted_at IS NULL)),
            CONSTRAINT ck_knowledge_versions_superseded_state
                CHECK (state <> 'superseded' OR superseded_at IS NOT NULL),
            CONSTRAINT ck_knowledge_versions_deleted_state
                CHECK ((state = 'deleted') = (deleted_at IS NOT NULL)),
            CONSTRAINT ck_knowledge_versions_indexed_state
                CHECK ((indexing_state = 'indexed') = (indexed_at IS NOT NULL)),
            CONSTRAINT ck_knowledge_versions_index_error
                CHECK (index_error_code IS NULL OR indexing_state = 'failed'),
            CONSTRAINT ck_knowledge_versions_timestamps CHECK (updated_at >= created_at)
        );

        CREATE UNIQUE INDEX uq_knowledge_versions_one_published_per_document
            ON knowledge_document_versions (tenant_id, document_id)
            WHERE state = 'published';

        CREATE INDEX ix_knowledge_sources_tenant_domain
            ON knowledge_sources (tenant_id, domain, enabled);
        CREATE INDEX ix_knowledge_documents_tenant_source_updated
            ON knowledge_documents (tenant_id, source_id, updated_at DESC);
        CREATE INDEX ix_knowledge_versions_tenant_state_effective
            ON knowledge_document_versions (tenant_id, state, effective_at DESC);
        -- Drives the indexing worker's queue scan: pending and failed versions
        -- for one tenant, oldest first.
        CREATE INDEX ix_knowledge_versions_tenant_indexing
            ON knowledge_document_versions (tenant_id, indexing_state, updated_at);
        """
    )


def downgrade() -> None:
    """Remove this revision. Production recovery should restore a backup instead."""
    for table in (
        "knowledge_document_versions",
        "knowledge_documents",
        "knowledge_sources",
    ):
        op.execute(f"DROP TABLE {table}")
    for name, _values in reversed(ENUMS):
        op.execute(f"DROP TYPE {name}")
