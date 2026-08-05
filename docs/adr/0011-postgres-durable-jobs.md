# 0011 — PostgreSQL outbox with leased at-least-once delivery

- **Status:** Accepted
- **Date:** 2026-08-04
- **Affects:** `REL-003`, `RAG-002`, `FEAT-003`, `FEAT-005`, `PRIV-001`

## Context

Ingestion, privacy erasure, CRM delivery, notifications, and webhooks must
survive API and worker restarts. A process-local task or broker acknowledgement
cannot commit atomically with the PostgreSQL domain record that caused it. The
system also needs tenant-qualified operator inspection and replay without
publishing job payloads, which can contain contact or document data.

An external call cannot be made atomically with a local database commit. A
worker can always die after the receiver commits and before the local queue is
acknowledged, so a generic claim of exactly-once execution would be false.

## Decision

Use PostgreSQL as the durable job/outbox store. A domain mutation and its job
intent are inserted in one database transaction. Workers claim due rows with
`FOR UPDATE SKIP LOCKED`, a bounded lease, and a stable worker ID. Expired leases
are eligible for another worker, so termination and cluster restart lose no
work. A heartbeat extends leases for long handlers.

Delivery is at least once. Each job has a unique `(tenant_id, kind,
idempotency_key)` identity and a hash of its payload. Re-enqueueing the same key
and payload returns the original row; reusing it for different work is a
conflict. Every effect handler must propagate that stable key to its external
receiver or use it in the transaction that records a local effect. The receiver
returns the original result for a repeated key. This is how duplicate execution
produces one business effect.

Retryable failures use capped exponential backoff. A non-retryable failure, or
a failure at the configured attempt limit, enters `dead_lettered`. Operators
with tenant-admin access can inspect safe metadata and an append-only event
trail, retry a dead letter from attempt zero, or cancel pending/dead-lettered
work. Running work cannot be cancelled because an external effect may already
be in flight. Payloads are deliberately absent from the operator API, logs, and
job events.

The first handler moves privacy deletion onto this mechanism. Later tasks add
ingestion, CRM, notification, and webhook handlers behind the same typed
contract; REL-003 reserves those kinds but does not implement their integrations.

## Consequences

**Gained.** Jobs and their audit trail survive restarts, workers scale without
double leasing, transient failures retry without hot loops, and failed work is
visible and controllable within its tenant boundary. The transactional privacy
outbox proves that a committed request cannot exist without runnable work.

**Cost.** Handler authors must implement effect-level idempotency. Queue polling
adds bounded Postgres traffic, and long handlers write lease-renewal events.
Postgres remains a single dependency for both domain state and delivery.

**Boundary.** “Exactly once” means one observable business effect, not one
handler invocation. A provider that offers no idempotency key requires a local
reconciliation design in the dependent integration task; REL-003 cannot create
an atomic transaction across two systems.

## Alternatives considered

**Celery/Redis or a managed broker.** Rejected for this stage because publishing
to the broker still races the domain commit and would require a PostgreSQL
outbox relay anyway. It also adds an operational dependency without improving
the demonstrated guarantee.

**PostgreSQL advisory locks.** Rejected because they disappear with the
connection and do not record ownership, expiry, retries, or operator history.

**Exactly-once worker acknowledgements.** Rejected as an impossible generic
promise across an external receiver and PostgreSQL. Receiver idempotency plus
at-least-once delivery states the real guarantee.
