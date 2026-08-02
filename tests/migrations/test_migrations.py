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
    upgrade_head(migration_database_url)

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
    assert revision == "0001_normalized"
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
