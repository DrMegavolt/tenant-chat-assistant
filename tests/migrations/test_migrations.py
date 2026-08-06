"""Migration specifications against a real, disposable PostgreSQL 16 database."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import psycopg
import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from psycopg import errors, sql

REPO_ROOT = Path(__file__).resolve().parents[2]
DOMAIN_TABLES = {
    "tenants",
    "chat_sessions",
    "messages",
    "tool_executions",
    "leads",
    "bookings",
    "handoffs",
    "idempotency_keys",
    "audit_events",
    "tenant_memberships",
    "availability_slots",
    "knowledge_sources",
    "knowledge_documents",
    "knowledge_document_versions",
    "consent_records",
    "privacy_requests",
    "background_jobs",
    "background_job_events",
    "turn_records",
    "turn_record_projections",
    "trace_access_grants",
    "knowledge_index_generations",
    "knowledge_index_findings",
    "routing_decisions",
    "agent_workflows",
    "workflow_events",
}
TENANT_QUERY_TABLES = DOMAIN_TABLES - {"tenants"}


def psycopg_url(sqlalchemy_url: str) -> str:
    return sqlalchemy_url.replace("postgresql+psycopg://", "postgresql://", 1)


def alembic_config(database_url: str) -> Config:
    """Point the checked-in Alembic environment at one disposable database."""
    os.environ["DATABASE_MIGRATION_URL"] = database_url
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "services/api/migrations"))
    return config


def upgrade_head(database_url: str) -> None:
    command.upgrade(alembic_config(database_url), "head")


@pytest.mark.integration
def test_zero_to_head_and_rerun_are_safe(migration_database_url: str) -> None:
    """A clean database reaches exactly head; invoking the same release step is a no-op."""
    upgrade_head(migration_database_url)
    # Kubernetes Secrets commonly carry the standard URL spelling. The image
    # installs psycopg 3 (not psycopg2), so the migration boundary must select
    # that driver explicitly instead of relying on SQLAlchemy's legacy default.
    upgrade_head(psycopg_url(migration_database_url))

    engine = sa.create_engine(migration_database_url)
    inspector = sa.inspect(engine)
    assert set(inspector.get_table_names()) >= DOMAIN_TABLES

    with engine.connect() as connection:
        revision = connection.execute(
            sa.text("SELECT version_num FROM alembic_version")
        ).scalar_one()
        enum_names = set(
            connection.execute(sa.text("SELECT typname FROM pg_type WHERE typtype = 'e'")).scalars()
        )
    assert revision == "0012_agent_routing"
    assert {
        "tenant_status",
        "chat_session_status",
        "message_role",
        "tool_execution_status",
        "lead_status",
        "booking_status",
        "handoff_status",
        "idempotency_status",
        "audit_actor_type",
        "knowledge_source_kind",
        "knowledge_version_state",
        "knowledge_indexing_state",
        "knowledge_visibility",
        "consent_purpose",
        "consent_status",
        "privacy_request_status",
        "background_job_status",
        "knowledge_generation_status",
        "routing_outcome",
        "workflow_status",
    } <= enum_names

    for table in TENANT_QUERY_TABLES:
        indexes = inspector.get_indexes(table)
        unique_constraints = inspector.get_unique_constraints(table)
        supporting_columns = [tuple(index["column_names"] or ()) for index in indexes] + [
            tuple(constraint["column_names"] or ()) for constraint in unique_constraints
        ]
        assert any(columns and columns[0] == "tenant_id" for columns in supporting_columns), table

        foreign_keys = inspector.get_foreign_keys(table)
        assert any(
            foreign_key["referred_table"] == "tenants"
            or "tenant_id" in (foreign_key["constrained_columns"] or ())
            for foreign_key in foreign_keys
        ), table
    engine.dispose()


@pytest.mark.integration
def test_composite_foreign_keys_reject_cross_tenant_records(
    migration_database_url: str,
) -> None:
    """A valid UUID from tenant A cannot be attached to a tenant B child row."""
    upgrade_head(migration_database_url)
    session_id = uuid.uuid4()
    message_id = uuid.uuid4()

    with psycopg.connect(psycopg_url(migration_database_url)) as connection:
        connection.execute(
            "INSERT INTO tenants (id, display_name) VALUES (%s, %s), (%s, %s)",
            ("tenant-a", "Tenant A", "tenant-b", "Tenant B"),
        )
        connection.execute(
            "INSERT INTO chat_sessions (id, tenant_id) VALUES (%s, %s)",
            (session_id, "tenant-a"),
        )
        with pytest.raises(errors.ForeignKeyViolation):
            connection.execute(
                """
                INSERT INTO messages
                    (id, tenant_id, chat_session_id, sequence_number, role, content)
                VALUES (%s, %s, %s, 1, 'visitor', 'hello')
                """,
                (message_id, "tenant-b", session_id),
            )
        connection.rollback()
        # A consent grant is attached to the same composite key, so a grant
        # recorded under tenant B against tenant A's session must fail too.
        with pytest.raises(errors.ForeignKeyViolation):
            connection.execute(
                """
                INSERT INTO consent_records
                    (id, tenant_id, chat_session_id, purpose, statement)
                VALUES (%s, %s, %s, 'booking', 'I agree.')
                """,
                (uuid.uuid4(), "tenant-b", session_id),
            )
        connection.rollback()


def seed_knowledge(connection: psycopg.Connection, tenant_id: str) -> tuple[uuid.UUID, uuid.UUID]:
    """One tenant, one financing source, one document, ready for versions."""
    source_id = uuid.uuid4()
    document_id = uuid.uuid4()
    connection.execute(
        """
        INSERT INTO knowledge_sources (id, tenant_id, domain, kind, display_name)
        VALUES (%s, %s, 'financing', 'upload', 'Partner brochures')
        """,
        (source_id, tenant_id),
    )
    connection.execute(
        """
        INSERT INTO knowledge_documents
            (id, tenant_id, domain, source_id, external_key, title)
        VALUES (%s, %s, 'financing', %s, 'plan-terms.pdf', 'Plan terms')
        """,
        (document_id, tenant_id, source_id),
    )
    return source_id, document_id


def insert_version(
    connection: psycopg.Connection,
    *,
    tenant_id: str,
    document_id: uuid.UUID,
    revision: int,
    checksum: str,
    domain: str = "financing",
    state: str = "published",
) -> uuid.UUID:
    version_id = uuid.uuid4()
    connection.execute(
        """
        INSERT INTO knowledge_document_versions
            (id, tenant_id, domain, document_id, revision, state, checksum,
             byte_size, media_type, storage_key, approved_at, approved_by,
             published_at, effective_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, 1024, 'text/markdown', 'objects/v.md',
                now(), 'ops@example', now(), now())
        """,
        (version_id, tenant_id, domain, document_id, revision, state, checksum),
    )
    return version_id


@pytest.mark.integration
def test_only_one_knowledge_version_can_be_published_per_document(
    migration_database_url: str,
) -> None:
    """The schema, not just the domain, is what makes a publish atomic."""
    upgrade_head(migration_database_url)

    with psycopg.connect(psycopg_url(migration_database_url)) as connection:
        connection.execute(
            "INSERT INTO tenants (id, display_name) VALUES (%s, %s)", ("tenant-a", "Tenant A")
        )
        _, document_id = seed_knowledge(connection, "tenant-a")
        insert_version(
            connection, tenant_id="tenant-a", document_id=document_id, revision=1, checksum="a" * 64
        )

        with pytest.raises(errors.UniqueViolation):
            insert_version(
                connection,
                tenant_id="tenant-a",
                document_id=document_id,
                revision=2,
                checksum="b" * 64,
            )
        connection.rollback()


@pytest.mark.integration
def test_identical_content_cannot_become_a_second_revision(migration_database_url: str) -> None:
    """Idempotent re-ingestion survives a racing worker, not just a checked read."""
    upgrade_head(migration_database_url)

    with psycopg.connect(psycopg_url(migration_database_url)) as connection:
        connection.execute(
            "INSERT INTO tenants (id, display_name) VALUES (%s, %s)", ("tenant-a", "Tenant A")
        )
        _, document_id = seed_knowledge(connection, "tenant-a")
        insert_version(
            connection,
            tenant_id="tenant-a",
            document_id=document_id,
            revision=1,
            checksum="c" * 64,
            state="draft",
        )

        with pytest.raises(errors.UniqueViolation):
            insert_version(
                connection,
                tenant_id="tenant-a",
                document_id=document_id,
                revision=2,
                checksum="c" * 64,
                state="draft",
            )
        connection.rollback()


@pytest.mark.integration
def test_a_knowledge_version_cannot_disagree_with_its_document_domain(
    migration_database_url: str,
) -> None:
    """The denormalized domain is pinned by a composite key, not by convention.

    Retrieval filters on tenant and domain, so a version carrying a domain its
    document does not have would answer under the wrong filter.
    """
    upgrade_head(migration_database_url)

    with psycopg.connect(psycopg_url(migration_database_url)) as connection:
        connection.execute(
            "INSERT INTO tenants (id, display_name) VALUES (%s, %s)", ("tenant-a", "Tenant A")
        )
        _, document_id = seed_knowledge(connection, "tenant-a")

        with pytest.raises(errors.ForeignKeyViolation):
            insert_version(
                connection,
                tenant_id="tenant-a",
                document_id=document_id,
                revision=1,
                checksum="d" * 64,
                domain="services",
                state="draft",
            )
        connection.rollback()


@pytest.mark.integration
def test_a_document_cannot_reference_another_tenants_source(
    migration_database_url: str,
) -> None:
    upgrade_head(migration_database_url)

    with psycopg.connect(psycopg_url(migration_database_url)) as connection:
        connection.execute(
            "INSERT INTO tenants (id, display_name) VALUES (%s, %s), (%s, %s)",
            ("tenant-a", "Tenant A", "tenant-b", "Tenant B"),
        )
        source_id, _ = seed_knowledge(connection, "tenant-a")

        with pytest.raises(errors.ForeignKeyViolation):
            connection.execute(
                """
                INSERT INTO knowledge_documents
                    (id, tenant_id, domain, source_id, external_key, title)
                VALUES (%s, 'tenant-b', 'financing', %s, 'stolen.pdf', 'Stolen')
                """,
                (uuid.uuid4(), source_id),
            )
        connection.rollback()


@pytest.mark.integration
def test_assigned_handoff_can_be_cancelled_without_erasing_assignment(
    migration_database_url: str,
) -> None:
    """Cancellation preserves which staff principal held the abandoned handoff."""
    upgrade_head(migration_database_url)
    session_id = uuid.uuid4()
    handoff_id = uuid.uuid4()

    with psycopg.connect(psycopg_url(migration_database_url)) as connection:
        connection.execute(
            "INSERT INTO tenants (id, display_name) VALUES (%s, %s)",
            ("tenant-a", "Tenant A"),
        )
        connection.execute(
            "INSERT INTO chat_sessions (id, tenant_id) VALUES (%s, %s)",
            (session_id, "tenant-a"),
        )
        connection.execute(
            """
            INSERT INTO handoffs
                (id, tenant_id, chat_session_id, status, reason,
                 assigned_principal_id, assigned_at)
            VALUES (%s, %s, %s, 'assigned', %s, %s, now())
            """,
            (handoff_id, "tenant-a", session_id, "visitor requested staff", "staff-1"),
        )
        connection.execute(
            "UPDATE handoffs SET status = 'cancelled', updated_at = now() WHERE id = %s",
            (handoff_id,),
        )
        row = connection.execute(
            "SELECT status, assigned_principal_id, assigned_at FROM handoffs WHERE id = %s",
            (handoff_id,),
        ).fetchone()

    assert row is not None
    assert row[0] == "cancelled"
    assert row[1] == "staff-1"
    assert row[2] is not None


@pytest.mark.integration
def test_a_handoff_summary_is_optional_but_never_blank(migration_database_url: str) -> None:
    """The graph always writes one; every handoff predating `ARCH-001` has none.

    Nullable and non-blank rather than ``NOT NULL DEFAULT ''``: a staff member
    seeing no summary knows to open the transcript, while an empty string reads
    as an assistant that had nothing to say.
    """
    upgrade_head(migration_database_url)
    session_id = uuid.uuid4()

    with psycopg.connect(psycopg_url(migration_database_url)) as connection:
        connection.execute(
            "INSERT INTO tenants (id, display_name) VALUES (%s, %s)", ("tenant-a", "Tenant A")
        )
        connection.execute(
            "INSERT INTO chat_sessions (id, tenant_id) VALUES (%s, %s)", (session_id, "tenant-a")
        )
        connection.execute(
            """
            INSERT INTO handoffs (id, tenant_id, chat_session_id, reason, summary)
            VALUES (%s, %s, %s, 'customer_request', NULL)
            """,
            (uuid.uuid4(), "tenant-a", session_id),
        )

        with pytest.raises(errors.CheckViolation):
            connection.execute(
                """
                INSERT INTO handoffs (id, tenant_id, chat_session_id, reason, summary)
                VALUES (%s, %s, %s, 'customer_request', '   ')
                """,
                (uuid.uuid4(), "tenant-a", session_id),
            )
        connection.rollback()


@pytest.mark.integration
def test_downgrade_to_base_and_restore_head(migration_database_url: str) -> None:
    """Development downgrade removes revision-owned objects and can be upgraded again."""
    config = alembic_config(migration_database_url)
    command.upgrade(config, "head")
    command.downgrade(config, "base")

    engine = sa.create_engine(migration_database_url)
    assert not (DOMAIN_TABLES & set(sa.inspect(engine).get_table_names()))
    engine.dispose()

    command.upgrade(config, "head")
    engine = sa.create_engine(migration_database_url)
    assert set(sa.inspect(engine).get_table_names()) >= DOMAIN_TABLES
    engine.dispose()


@pytest.mark.integration
def test_legacy_snapshot_table_stops_without_data_loss(migration_database_url: str) -> None:
    """The reset decision is operator-controlled; migration never drops prototype rows."""
    with psycopg.connect(psycopg_url(migration_database_url)) as connection:
        connection.execute(
            "CREATE TABLE chat_sessions (session_id text PRIMARY KEY, payload jsonb NOT NULL)"
        )
        connection.execute(
            "INSERT INTO chat_sessions (session_id, payload) VALUES ('legacy', '{\"safe\": true}')"
        )

    with pytest.raises(RuntimeError, match="pre-Alembic chat_sessions"):
        upgrade_head(migration_database_url)

    with psycopg.connect(psycopg_url(migration_database_url)) as connection:
        payload = connection.execute(
            "SELECT payload FROM chat_sessions WHERE session_id = 'legacy'"
        ).fetchone()
    assert payload == ({"safe": True},)


@pytest.mark.integration
def test_application_role_can_write_rows_but_cannot_create_schema(
    migration_database_url: str,
) -> None:
    """Normal runtime DML works without inheriting schema-owner privileges."""
    upgrade_head(migration_database_url)
    role_name = f"app_{uuid.uuid4().hex}"

    with psycopg.connect(psycopg_url(migration_database_url), autocommit=True) as owner:
        identifier = sql.Identifier(role_name)
        database_row = owner.execute("SELECT current_database()").fetchone()
        assert database_row is not None
        database_name = database_row[0]
        owner.execute(sql.SQL("CREATE ROLE {} NOLOGIN NOINHERIT").format(identifier))
        owner.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(database_name), identifier
            )
        )
        owner.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(identifier))
        owner.execute(
            sql.SQL(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {}"
            ).format(identifier)
        )
        owner.execute(
            sql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {}").format(
                identifier
            )
        )
        owner.execute(
            sql.SQL("REVOKE INSERT, UPDATE, DELETE ON TABLE public.alembic_version FROM {}").format(
                identifier
            )
        )
        owner.execute(
            sql.SQL("REVOKE UPDATE, DELETE ON TABLE public.audit_events FROM {}").format(identifier)
        )
        owner.execute(
            sql.SQL("REVOKE UPDATE, DELETE ON TABLE public.messages FROM {}").format(identifier)
        )
        owner.execute(
            sql.SQL("REVOKE DELETE ON TABLE public.chat_sessions FROM {}").format(identifier)
        )
        owner.execute(
            sql.SQL("REVOKE DELETE ON TABLE public.background_jobs FROM {}").format(identifier)
        )
        owner.execute(
            sql.SQL("REVOKE UPDATE, DELETE ON TABLE public.background_job_events FROM {}").format(
                identifier
            )
        )
        owner.execute(
            sql.SQL(
                "REVOKE DELETE ON TABLE public.knowledge_documents, "
                "public.knowledge_document_versions FROM {}"
            ).format(identifier)
        )
        owner.execute(
            sql.SQL(
                "REVOKE UPDATE, DELETE ON TABLE public.turn_records, "
                "public.turn_record_projections FROM {}"
            ).format(identifier)
        )
        owner.execute(sql.SQL("REVOKE CREATE ON SCHEMA public FROM {}").format(identifier))

    with psycopg.connect(psycopg_url(migration_database_url)) as application:
        application.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role_name)))
        application.execute(
            "INSERT INTO tenants (id, display_name) VALUES (%s, %s)",
            ("runtime-tenant", "Runtime Tenant"),
        )
        application.commit()

    with psycopg.connect(psycopg_url(migration_database_url)) as application:
        application.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role_name)))
        with pytest.raises(errors.InsufficientPrivilege):
            application.execute("CREATE TABLE forbidden_runtime_ddl (id integer)")
        application.rollback()

    job_id = uuid.uuid4()
    with psycopg.connect(psycopg_url(migration_database_url)) as application:
        application.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role_name)))
        application.execute(
            """
            INSERT INTO background_jobs
                (id, tenant_id, kind, payload, payload_hash, idempotency_key)
            VALUES (%s, %s, 'webhook', '{}', %s, 'role-test-job')
            """,
            (job_id, "runtime-tenant", "a" * 64),
        )
        application.execute(
            """
            INSERT INTO background_job_events (tenant_id, job_id, event, actor_type)
            VALUES (%s, %s, 'enqueued', 'service')
            """,
            ("runtime-tenant", job_id),
        )
        application.execute(
            "UPDATE background_jobs SET attempt_count = 1 WHERE tenant_id = %s AND id = %s",
            ("runtime-tenant", job_id),
        )
        application.commit()

    with psycopg.connect(psycopg_url(migration_database_url)) as application:
        application.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role_name)))
        with pytest.raises(errors.InsufficientPrivilege):
            application.execute(
                "UPDATE background_job_events SET event = 'succeeded' WHERE job_id = %s",
                (job_id,),
            )
        application.rollback()

    with psycopg.connect(psycopg_url(migration_database_url)) as application:
        application.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role_name)))
        with pytest.raises(errors.InsufficientPrivilege):
            application.execute("DELETE FROM background_jobs WHERE id = %s", (job_id,))
        application.rollback()

    with psycopg.connect(psycopg_url(migration_database_url)) as application:
        application.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role_name)))
        with pytest.raises(errors.InsufficientPrivilege):
            application.execute("UPDATE alembic_version SET version_num = 'owned'")
        application.rollback()

    with psycopg.connect(psycopg_url(migration_database_url)) as application:
        application.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role_name)))
        with pytest.raises(errors.InsufficientPrivilege):
            application.execute("DELETE FROM audit_events")
        application.rollback()

    with psycopg.connect(psycopg_url(migration_database_url)) as owner:
        session_id = uuid.uuid4()
        message_id = uuid.uuid4()
        owner.execute(
            "INSERT INTO chat_sessions (id, tenant_id) VALUES (%s, %s)",
            (session_id, "runtime-tenant"),
        )
        owner.execute(
            """
            INSERT INTO messages
                (id, tenant_id, chat_session_id, sequence_number, role, content)
            VALUES (%s, %s, %s, 1, 'staff', 'committed answer')
            """,
            (message_id, "runtime-tenant", session_id),
        )

    with psycopg.connect(psycopg_url(migration_database_url)) as application:
        application.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role_name)))
        with pytest.raises(errors.InsufficientPrivilege):
            application.execute(
                "UPDATE messages SET content = 'replaced' WHERE tenant_id = %s AND id = %s",
                ("runtime-tenant", message_id),
            )
        application.rollback()

    with psycopg.connect(psycopg_url(migration_database_url)) as application:
        application.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role_name)))
        with pytest.raises(errors.InsufficientPrivilege):
            application.execute(
                "DELETE FROM chat_sessions WHERE tenant_id = %s AND id = %s",
                ("runtime-tenant", session_id),
            )
        application.rollback()

    with psycopg.connect(psycopg_url(migration_database_url)) as owner:
        _, knowledge_document_id = seed_knowledge(owner, "runtime-tenant")

    with psycopg.connect(psycopg_url(migration_database_url)) as application:
        application.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role_name)))
        with pytest.raises(errors.InsufficientPrivilege):
            application.execute(
                "DELETE FROM knowledge_documents WHERE tenant_id = %s AND id = %s",
                ("runtime-tenant", knowledge_document_id),
            )
        application.rollback()

    with psycopg.connect(psycopg_url(migration_database_url)) as application:
        application.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role_name)))
        # The API assigns, re-assigns (upsert), and revokes membership rows
        # (SEC-001), so the runtime role owns that DML like any domain row.
        application.execute(
            """
            INSERT INTO tenant_memberships (tenant_id, principal_subject, role)
            VALUES (%s, 'operator-1', 'viewer')
            """,
            ("runtime-tenant",),
        )
        application.execute(
            """
            UPDATE tenant_memberships SET role = 'support_agent', updated_at = now()
            WHERE tenant_id = %s AND principal_subject = 'operator-1'
            """,
            ("runtime-tenant",),
        )
        application.execute(
            """
            DELETE FROM tenant_memberships
            WHERE tenant_id = %s AND principal_subject = 'operator-1'
            """,
            ("runtime-tenant",),
        )
        application.commit()

    with psycopg.connect(psycopg_url(migration_database_url)) as application:
        application.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role_name)))
        # audit_events is append-only: recording is INSERT, rewriting is refused.
        application.execute(
            """
            INSERT INTO audit_events
                (tenant_id, actor_type, principal_id, action, resource_type, request_id)
            VALUES (%s, 'staff', 'operator-1', 'membership_assigned', 'tenant_membership', 'req-1')
            """,
            ("runtime-tenant",),
        )
        application.commit()

    with psycopg.connect(psycopg_url(migration_database_url)) as application:
        application.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role_name)))
        with pytest.raises(errors.InsufficientPrivilege):
            application.execute(
                "UPDATE audit_events SET details = '{}' WHERE tenant_id = %s",
                ("runtime-tenant",),
            )
        application.rollback()

    # PRIV-002: the inference plane is append-only for the application role.
    # The API writes and reads turn records, but must not be able to rewrite
    # or erase evidence of what produced an answer; DELETE lives with the
    # erasure role, and projections are erased by cascading off their turn.
    # Each refusal gets a fresh connection: relation ACLs are cached per
    # session, so reusing one connection after a refused statement can see a
    # stale snapshot (the established pattern in this test).
    with psycopg.connect(psycopg_url(migration_database_url)) as owner:
        turn_id = uuid.uuid4()
        owner.execute(
            "INSERT INTO turn_records (id, tenant_id, chat_session_id, content, recorded_at) "
            "VALUES (%s, 'runtime-tenant', %s, '{}', now())",
            (turn_id, session_id),
        )
        owner.execute(
            "INSERT INTO turn_record_projections (id, tenant_id, turn_record_id, kind) "
            "VALUES (%s, 'runtime-tenant', %s, 'eval_dataset')",
            (uuid.uuid4(), turn_id),
        )

    with psycopg.connect(psycopg_url(migration_database_url)) as application:
        application.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role_name)))
        with pytest.raises(errors.InsufficientPrivilege):
            application.execute(
                "DELETE FROM turn_records WHERE tenant_id = %s AND id = %s",
                ("runtime-tenant", turn_id),
            )

    with psycopg.connect(psycopg_url(migration_database_url)) as application:
        application.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role_name)))
        with pytest.raises(errors.InsufficientPrivilege):
            application.execute(
                "UPDATE turn_records SET content = '{}' WHERE tenant_id = %s AND id = %s",
                ("runtime-tenant", turn_id),
            )

    with psycopg.connect(psycopg_url(migration_database_url)) as application:
        application.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role_name)))
        with pytest.raises(errors.InsufficientPrivilege):
            application.execute(
                "DELETE FROM turn_record_projections WHERE tenant_id = %s AND turn_record_id = %s",
                ("runtime-tenant", turn_id),
            )
