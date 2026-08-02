# Database migrations

Alembic owns the authoritative Postgres schema. Run it as a release step with a
schema-owner connection, never from API startup:

```bash
DATABASE_MIGRATION_URL='postgresql+psycopg://schema_owner:...@postgres/tenantchat' make migrate
```

The API uses a different login with only `CONNECT`, schema `USAGE`, table DML,
and sequence usage. See `docs/runbooks/database-migrations.md` for provisioning,
prototype reset, rollout, downgrade, and restore procedures.
