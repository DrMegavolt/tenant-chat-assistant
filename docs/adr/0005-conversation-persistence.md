# 0005 — Tenant-qualified append-only conversation persistence

- **Status:** Accepted
- **Date:** 2026-08-02
- **Affects:** `DATA-002`, `SEC-002`, `DATA-003`

## Context

Composite foreign keys prevent a tenant B child row from referencing tenant A's
session, but they do not prevent an adapter from reading or updating a known UUID
without a tenant predicate. The prototype also accepted whole client transcript
snapshots and kept leads and bookings in process memory, so replicas disagreed
and concurrent turns could overwrite one another.

The API needs deterministic ordering across processes without moving SQLAlchemy
or transaction concerns into `packages/core`. Booking slot reservation and
idempotency remain a separate business transaction in `DATA-003`.

## Decision

Use async SQLAlchemy adapters over psycopg with one bounded pool per API process.
Every read, lock, update, and delete predicate begins with the protected tenant
ID as well as the record ID. A missing record and a wrong-tenant record produce
the same domain `NotFoundError`.

Conversation IDs and message IDs are server-generated UUIDs. The public
repository contract can create a conversation, append one message, or read the
committed transcript; it has no operation that accepts, replaces, reorders, or
deletes prior messages. An append runs in one explicit transaction:

1. `SELECT` the tenant/session row `FOR UPDATE`.
2. Use its monotonic `version` as the next message sequence.
3. Insert the message under the composite tenant/session foreign key.
4. Increment the same tenant-qualified session version and commit.

The row lock serializes only writers to one conversation. Different sessions and
tenants remain independent, and the unique tenant/session/sequence constraint is
the final invariant if adapter logic regresses.

Current lead and static-slot booking API writes use the same database and
explicit transactions. Their client session field remains an untrusted,
write-only correlation label; it never authorizes reads. `SEC-002` replaces that
label with a server-issued visitor credential. Static booking labels are stored
durably, but no calendar slot is reserved and no retry is deduplicated here.

Production composition requires `DATABASE_URL` and constructs only PostgreSQL
stores. In-memory stores remain explicit injected test doubles. Startup performs
DML-only synchronization of the two code-owned tenant seeds until `FEAT-006`
moves configuration ownership into Postgres; it never creates schema.

## Consequences

**Gained.** Replicas share committed state, restarts lose nothing, cross-tenant
UUID knowledge is insufficient to read or mutate records, and concurrent writers
produce a gap-free server order without retry loops.

**Cost.** A busy single conversation is deliberately serialized. Pool capacity
is configured per process, so deployment sizing must multiply it by replica and
worker counts when setting Postgres connection limits.

**Boundary.** The adapter records today's booking result but does not claim
transactional availability. Provider slot IDs, reservation conflicts,
idempotency keys, and timeout replay behavior remain exclusively `DATA-003`.

## Alternatives considered

**Optimistic version updates with retries.** Viable, but every collision would
roll back and retry an insert. Conversations are naturally sequential and short,
so a row lock is simpler and gives the same per-session serialization directly.

**A database sequence per conversation.** Rejected because provisioning and
dropping one sequence per session adds schema-object churn and makes tenant
retention harder. The locked session version already supplies the order.

**Advisory locks.** Rejected because their keys are easier to derive incorrectly
and have no foreign-key relationship to the row being protected.
