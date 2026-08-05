\set ON_ERROR_STOP on

-- Invoke with:
--   psql "$DATABASE_MIGRATION_URL" \
--     --set=privacy_role=tenantchat_privacy \
--     --file=services/api/migrations/provision_privacy_role.sql
--
-- The erasure and retention worker's login. Unlike the application role, it
-- holds DELETE on sessions, transcripts, and consent records, so the worker
-- can fulfill deletion requests and purge expired records — and it is the only
-- role that can. The API never connects with this role (PRIVACY_DATABASE_URL
-- names it, and only the worker reads that variable).
--
-- The role cannot create or alter schema objects; it owns no tables, so the
-- ``ALTER DEFAULT PRIVILEGES`` clauses are absent and new tables are not
-- silently granted to it. Identifier quoting is handled by format('%I', ...).

SELECT format('GRANT CONNECT ON DATABASE %I TO %I', current_database(), :'privacy_role') \gexec
SELECT format('GRANT USAGE ON SCHEMA public TO %I', :'privacy_role') \gexec
SELECT format(
    'GRANT SELECT ON ALL TABLES IN SCHEMA public TO %I',
    :'privacy_role'
) \gexec
-- DELETE where erasure needs it. The application role has these revoked; the
-- erasure role is the mirror image, and the two together are the whole story.
SELECT format(
    'GRANT DELETE ON TABLE public.tool_executions, public.messages, '
    'public.leads, public.bookings, public.handoffs, public.consent_records, '
    'public.chat_sessions TO %I',
    :'privacy_role'
) \gexec
-- PRIV-002: the worker purges expired turn records and fulfills erasure of a
-- subject's turn records; projections derived from a turn cascade with it.
SELECT format(
    'GRANT SELECT, DELETE ON TABLE public.turn_records, '
    'public.turn_record_projections TO %I',
    :'privacy_role'
) \gexec
SELECT format(
    'GRANT UPDATE ON TABLE public.privacy_requests TO %I',
    :'privacy_role'
) \gexec
-- The LangGraph checkpoint tables are created by the schema owner, so they
-- predate this script; grant them explicitly and defensively (they may not all
-- exist yet, hence the per-table statement).
SELECT format(
    'GRANT SELECT, DELETE ON TABLE public.checkpoints TO %I',
    :'privacy_role'
) \gexec
SELECT format(
    'GRANT SELECT, DELETE ON TABLE public.checkpoint_blobs TO %I',
    :'privacy_role'
) \gexec
SELECT format(
    'GRANT SELECT, DELETE ON TABLE public.checkpoint_writes TO %I',
    :'privacy_role'
) \gexec
-- The audit trail is append-only for every role.
SELECT format('REVOKE INSERT, UPDATE, DELETE ON TABLE public.audit_events FROM %I', :'privacy_role') \gexec
SELECT format('REVOKE UPDATE, DELETE ON TABLE public.idempotency_keys FROM %I', :'privacy_role') \gexec
