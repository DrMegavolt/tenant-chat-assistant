# Database migration runbook

## Ownership boundary

Alembic is the only schema writer. A release operator or one-shot migration Job
uses `DATABASE_MIGRATION_URL`, whose role owns the objects. The API uses
`DATABASE_URL`, whose distinct `NOINHERIT` login receives only `CONNECT`, schema
`USAGE`, table `SELECT/INSERT/UPDATE/DELETE`, and sequence `USAGE/SELECT`.
Audit events are further restricted to `SELECT/INSERT`, and the application role
cannot mutate Alembic's revision table. Authoritative messages are also
`SELECT/INSERT` only; retention and subject deletion use a separately authorized
privacy worker rather than granting transcript replacement to the API role.
The knowledge tables are `SELECT/INSERT/UPDATE` only for the same reason:
withdrawing a document is a tombstone the indexing worker has to observe, so the
API role cannot remove the row that records the withdrawal.

Provision the login and password through the platform secret manager, then run:

```sql
CREATE ROLE tenantchat_app LOGIN NOINHERIT PASSWORD '<from secret manager>';
```

```bash
psql "$DATABASE_MIGRATION_URL" \
  --set=app_role=tenantchat_app \
  --file=services/api/migrations/provision_app_role.sql
```

Do not give the API role membership in the owner role, `CREATE` on `public`, or
permission to update `alembic_version`. Application startup does not invoke
Alembic, call `MetaData.create_all`, or otherwise create schema.

## Release procedure

1. Take and verify a database backup. Record the current Alembic revision with
   `alembic current` using the owner URL.
2. Run `alembic upgrade head` and the idempotent LangGraph checkpoint setup in a
   one-shot release Job using the exact immutable API image that will be deployed.
   `k8s/api-migration-job.yaml` is the template; replace its image placeholder
   with the release digest before applying it.
3. Re-run `provision_app_role.sql`. Default privileges cover new objects created
   by the same owner, while the explicit grants also repair drift.
4. Roll out the API using only the application-role secret. Confirm the Job has
   completed before any new API pod becomes ready.

Alembic records each revision transactionally. Running `upgrade head` again is a
safe no-op once `alembic_version` is at the head revision.

## Prototype snapshot reset decision

The prototype's `chat_sessions(payload jsonb)` rows are not imported. They carry
client-supplied transcript state, mix leads/bookings into snapshots, and do not
contain the stable identifiers or trusted ordering required by the normalized
schema. Guessing those values during an automatic import would turn unsafe demo
state into authoritative production records.

The initial migration detects the legacy table and stops without modifying it.
For a database that still holds pre-cutover JSONB snapshots (the prototype image
that wrote them was deleted with the `API-001` cutover, so new writers are
impossible but the rows can remain):

1. Retain the `chats/` directory separately.
2. Export the table with `pg_dump --format=custom --table=public.chat_sessions`.
   Verify the dump with `pg_restore --list` and store it under the existing
   data-retention controls because it contains PII.
3. In a transaction, rename the legacy table to a dated quarantine name and
   remove access from the future API role:

   ```sql
   BEGIN;
   ALTER TABLE public.chat_sessions
     RENAME TO prototype_chat_session_snapshots_20260801;
   REVOKE ALL ON TABLE public.prototype_chat_session_snapshots_20260801
     FROM tenantchat_app;
   COMMIT;
   ```

4. Run the migration and seed tenants through a separately reviewed onboarding
   process.
5. Delete the quarantined table only after backup verification and the approved
   prototype retention period. This task never deletes it automatically.

## Downgrade and restore

`alembic downgrade -1` is supported for development and the empty-schema path is
tested head-to-base-to-head. DATA-002 rows with label-only booking slots or its
expanded lead urgencies deliberately block downgrade to DATA-001 rather than
fabricating timestamps or rewriting business meaning. Downgrading the RAG-001
revision drops every knowledge source, document, and version, which is the record
of what the assistant was authorized to answer from; the search index built from
them is derived and rebuildable, these rows are not. Downgrading the initial
revision drops every authoritative table, enum, and all contained data, so
none of these operations is a production rollback mechanism.

For a failed production release, prefer a forward fix when the schema remains
compatible. If data or schema must be rolled back, stop writers, restore the
pre-migration custom-format backup into a new database, run integrity checks,
and change the application secret to the restored database. Keep the failed
database read-only until the recovery is verified. Never downgrade a database
in place without an independently verified backup and an approved data-loss plan.
