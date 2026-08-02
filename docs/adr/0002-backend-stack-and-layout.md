# 0002 — Backend stack and repository layout

- **Status:** Accepted
- **Date:** 2026-07-31
- **Affects:** `QA-001`, `API-001`, `DATA-001`, `DATA-002`, `DEP-001`

## Context

The prototype backend is a single 1,549-line module built on the standard
library's `ThreadingHTTPServer`, holding sessions, leads, and bookings in module
globals. It has no dependency manifest beyond one pinned package, no tests, no
type checking, and no schema migrations. Postgres persistence exists but the
container image never installs its driver, so that path cannot run as built.

None of this is defensible for a system that takes customer contact details and
books appointments. The question is what to replace it with, and how to keep the
replacement from drifting back.

## Decision

**Stack**

| Concern | Choice | Why this one |
|---|---|---|
| Packaging | uv workspace, committed `uv.lock` | Fast, and the lockfile carries hashes, which `DEP-001` requires for reproducible images |
| API | FastAPI + Pydantic v2 | Typed request/response models generate the OpenAPI contract `API-001` calls for, rather than documenting it separately |
| Persistence | SQLAlchemy 2.0 (async) + Alembic | Explicit migrations replace schema creation during request-server startup |
| Database | PostgreSQL 16 | Already required for LangGraph checkpoints (ADR-0001); adding a second store would be gratuitous |
| Tests | pytest | — |
| Lint/format | ruff | One tool covering both, fast enough to run on every save |
| Types | mypy, `strict` | — |

**Layout**

```
packages/core/      # Domain model. Zero runtime dependencies.
services/api/       # FastAPI app: routers, schemas, adapters.
services/ingestion/ # Knowledge ingestion worker.
services/embedding/ # Embedding model server.
tests/              # Cross-cutting tests, including architecture invariants.
```

The load-bearing rule is that **`packages/core` declares no runtime
dependencies**. Domain rules define `Protocol` ports; adapters live in the service
that owns the I/O. FastAPI, SQLAlchemy, and LangGraph are structurally unable to
appear in a domain type.

Services stay as separate deployables rather than collapsing into one process.

## Consequences

**Gained.** Domain rules are testable without a database, an HTTP client, or a
model: the test suite runs in well under a second, which is what makes it get run.
The dependency-free constraint is verified by `tests/test_architecture_invariants.py`,
so the boundary degrades loudly instead of silently.

**Cost.** Ports and adapters mean more indirection than calling the ORM from a
route handler. For CRUD this is overhead. It pays for itself at the points where
business rules and idempotency live, which is exactly where correctness matters
here.

**Cost of separate services.** Three deployables mean three Dockerfiles, a wider
CI matrix, and network calls where a function call would do. The shared domain
package is what keeps this from becoming three copies of drifting business logic
— without it, separate services would be actively worse than a monolith.

**Migration.** `server.py` stays runnable until `services/api` serves the same
endpoints, then is deleted rather than kept as a fallback. No characterization
tests were written against it: it is a prototype with no users, and several of its
behaviors are bugs worth losing (two-way substring service matching, collision-prone
booking IDs, client-supplied session identifiers).

## Alternatives considered

**Keep `ThreadingHTTPServer`.** Rejected. No async I/O, no request validation, no
generated contract, and a thread per connection. The prototype's own backlog
identifies replacing it as a release blocker.

**Django + DRF.** Rejected. The ORM and admin are genuine strengths, but the
framework wants to own the domain model, which is the opposite of the boundary
ADR-0001 depends on. Async support remains less mature than FastAPI's.

**Litestar.** A close call — arguably a better-designed framework. Rejected on
ecosystem size: FastAPI has more integration surface and is far more likely to be
familiar to whoever reads this code next.

**Collapse into a modular monolith.** Considered and not taken. It would be
simpler to run and deploy, and at this scale the separation buys little
operationally. Kept separate as a deliberate product decision: the embedding
service has genuinely different memory and warmup characteristics, and ingestion
is bursty work that should not share a request path with chat.

**Poetry or PDM instead of uv.** Rejected on speed and on `uv.lock` carrying
hashes natively.
