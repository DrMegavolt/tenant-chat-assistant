# SDD-001: atomic knowledge publication

Status: implemented in the September 2026 audit; human review pending.

## Problem and boundary

The publish route previously committed the version swap, then submitted ingestion
in a separate transaction. A process failure between those calls could leave a
published version with no delivery intent. This violates ADR-0011.

Publication must atomically commit the version swap, ingestion job, and enqueue
event in PostgreSQL. Elasticsearch indexing remains asynchronous. There is no
API shape change and no promise of exactly-once handler execution.

## Acceptance scenarios

1. Given an approved version, when publication commits and the API stops before
   its next statement, then a fresh repository can find one ingestion job with
   the same tenant and version. Repeating the route's enqueue returns that job
   and produces no duplicate enqueue event.
2. Given a published version and an approved replacement, when writing the new
   job fails, then the old version remains published, the replacement remains
   approved, and neither the replacement job nor its enqueue event persists.
3. Given a version owned by another tenant, when publication is attempted under
   the wrong tenant, then it returns not-found and creates no delivery intent.

Executable evidence: the `test_publication_*` tests in
[the PostgreSQL knowledge suite](../../tests/repositories/test_knowledge_repository.py).
Run `make test-repositories`; these tests require disposable Docker PostgreSQL.
The existing HTTP lifecycle and ingestion suites cover the unchanged transport
and asynchronous indexing behavior.

## Implementation and review

Reuse the job repository's insertion, payload-fingerprint validation, and event
logic through a connection-scoped helper. Call it inside the publication
transaction. Keep the route's idempotent submission for explicit in-memory
compositions. A second database connection or a compensating retry after commit
would leave the crash window open.

Review the transaction boundary and the unique `(tenant_id, kind, idempotency_key)`
constraint. Do not accept a fake-store test as proof of atomic database rollback.
Rollback of this code change restores the old failure window; existing job rows
need no migration or deletion.
