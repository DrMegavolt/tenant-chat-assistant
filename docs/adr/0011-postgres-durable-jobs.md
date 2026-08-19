# 0011 — PostgreSQL outbox with at-least-once delivery

- **Status:** Accepted
- **Date:** 2026-08-04

## Context

Ingestion and privacy work must survive API and worker restarts. Publishing to a
separate broker cannot be committed atomically with the PostgreSQL record that
caused the work. No generic queue can guarantee exactly-once execution across a
database and an external receiver.

## Decision

Use PostgreSQL as the durable job and outbox store. Insert the domain mutation
and its job intent in one transaction.

Workers claim jobs with `FOR UPDATE SKIP LOCKED`, a bounded lease, and a stable
worker identity. Expired leases can be reclaimed, and long handlers renew their
lease. Retryable failures use capped exponential backoff; permanent or exhausted
failures enter a dead-letter state.

Delivery is at least once. Each job has a unique tenant, kind, and idempotency
key plus a payload hash. Reusing the key for different work is a conflict. Every
effect handler must pass the stable key to its receiver or enforce it in the
local transaction that records the effect.

Authorized operators can inspect safe metadata and event history, retry dead
letters, or cancel work that has not started. Job payloads are excluded from the
operator API and telemetry.

## Consequences

Committed work survives restarts, workers scale without double leasing, and
failures remain visible and recoverable. PostgreSQL remains the only durable
queue dependency.

Handlers must implement effect-level idempotency. Polling and lease renewal add
database traffic. “Exactly once” means one observable business effect, not one
handler invocation.

## Alternatives considered

- **Celery, Redis, or a managed broker:** rejected because a transactional
  outbox relay would still be required.
- **Advisory locks:** rejected because they do not persist ownership, expiry,
  retries, or operator history.
- **Exactly-once acknowledgements:** rejected because they cannot make an
  external receiver and PostgreSQL one atomic system.
