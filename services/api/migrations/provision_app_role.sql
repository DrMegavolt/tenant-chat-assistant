\set ON_ERROR_STOP on

-- Invoke with:
--   psql "$DATABASE_MIGRATION_URL" \
--     --set=app_role=tenantchat_app \
--     --file=services/api/migrations/provision_app_role.sql
--
-- The login and its password are provisioned by the platform/secret manager.
-- This script grants only runtime data access; it intentionally cannot create
-- or alter schema objects. Identifier quoting is handled by format('%I', ...).

SELECT format('GRANT CONNECT ON DATABASE %I TO %I', current_database(), :'app_role') \gexec
SELECT format('GRANT USAGE ON SCHEMA public TO %I', :'app_role') \gexec
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
SELECT format('REVOKE CREATE ON SCHEMA public FROM %I', :'app_role') \gexec
SELECT format(
    'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO %I',
    :'app_role'
) \gexec
SELECT format(
    'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO %I',
    :'app_role'
) \gexec
SELECT format(
    'REVOKE INSERT, UPDATE, DELETE ON TABLE public.alembic_version FROM %I',
    :'app_role'
) \gexec
SELECT format(
    'REVOKE UPDATE, DELETE ON TABLE public.audit_events FROM %I',
    :'app_role'
) \gexec
SELECT format(
    'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public '
    'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO %I',
    current_user,
    :'app_role'
) \gexec
SELECT format(
    'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public '
    'GRANT USAGE, SELECT ON SEQUENCES TO %I',
    current_user,
    :'app_role'
) \gexec
