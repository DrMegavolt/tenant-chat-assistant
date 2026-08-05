# Durable background jobs

REL-003 runs background work from PostgreSQL. Start the local worker with
`make worker`; Kubernetes runs the same module in the `job-worker` Deployment.
The worker is ready only when both its application and privacy database roles
can connect. It has no liveness probe: a database outage makes it unready
without creating a restart loop.

## Delivery contract

Execution is at least once. Every handler receives the job's stable
`idempotency_key` and must use it at its effect boundary. After a process dies,
an expired lease is reclaimed and the handler may execute again; a receiver
that already committed the key must return the original result. Do not add a
handler for a provider that cannot deduplicate or reconcile effects.

Retryable failures use capped exponential backoff. Exhausted and permanent
failures enter `dead_lettered`. Error records contain bounded codes only, never
exception messages, provider bodies, contacts, or document content.

## Operator controls

All routes require authenticated, same-origin tenant-admin access. Mutations
also require the CSRF token issued by `GET /api/admin/csrf-token`.

- `GET /api/admin/jobs?tenant_id=<tenant>&status=<status>` lists safe metadata.
- `GET /api/admin/jobs/<id>?tenant_id=<tenant>` includes the immutable event
  trail. Payloads are intentionally unavailable.
- `POST /api/admin/jobs/<id>/retry` with `{"tenant_id":"<tenant>"}` replays a
  dead letter from attempt zero while preserving prior attempts and incrementing
  `replay_count`.
- `POST /api/admin/jobs/<id>/cancel` with the same body cancels pending or
  dead-lettered work. Running work is refused because its external effect may
  already be in flight.

Every retry and cancellation event records the operator subject and request ID.
A job UUID from another tenant returns the same authorization/not-found contract
as any other tenant-owned record.

## Verification and recovery

Run the real-Postgres specifications with:

```bash
make test-migrations
make test-repositories
```

`test_worker_restart_replays_delivery_with_exactly_one_business_effect` commits
to a fake idempotent receiver, destroys the first worker's database pool before
acknowledgement, expires the lease, and verifies the restarted worker produces
one receiver record and a succeeded job. The repository suite also races two
workers for one row and verifies only one lease is issued.

Before an operator retries a dead letter, inspect its error code and the target
receiver's idempotency receipt. If the receiver cannot establish whether the
effect committed, leave the job dead-lettered until the dependent integration's
reconciliation procedure can decide safely.
