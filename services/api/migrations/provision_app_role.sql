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
    'REVOKE UPDATE, DELETE ON TABLE public.messages FROM %I',
    :'app_role'
) \gexec
SELECT format(
    'REVOKE DELETE ON TABLE public.chat_sessions FROM %I',
    :'app_role'
) \gexec
-- Withdrawing knowledge is a tombstone, never a row delete: the indexing worker
-- has to learn that chunks it wrote earlier are retracted, and an audit of what
-- the assistant used to answer with has to stay answerable.
SELECT format(
    'REVOKE DELETE ON TABLE public.knowledge_sources, public.knowledge_documents, '
    'public.knowledge_document_versions FROM %I',
    :'app_role'
) \gexec
-- PRIV-001: the deletion queue and the consent record are the evidence a
-- rights request was filed and answered. An operator who can delete them can
-- make an erasure unverifiable, so the application role cannot touch them; the
-- erasure role owns DELETE on the rows themselves, and privacy_requests is
-- updated only by the worker.
SELECT format(
    'REVOKE DELETE ON TABLE public.consent_records, public.privacy_requests FROM %I',
    :'app_role'
) \gexec
-- REL-003: jobs and their lifecycle events are durable evidence. Workers and
-- operator controls update job state, but no runtime principal can erase work
-- or rewrite the event trail.
SELECT format(
    'REVOKE DELETE ON TABLE public.background_jobs FROM %I',
    :'app_role'
) \gexec
SELECT format(
    'REVOKE UPDATE, DELETE ON TABLE public.background_job_events FROM %I',
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
