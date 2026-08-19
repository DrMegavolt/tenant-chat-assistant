# 0002 — Backend stack and repository layout

- **Status:** Accepted
- **Date:** 2026-07-31

## Context

The original single-file HTTP server mixed transport, process-local state, and
business behavior. The replacement needed typed contracts, migrations, async
I/O, and boundaries that could be checked automatically.

## Decision

Use an uv workspace with a committed lockfile. Build the API with FastAPI and
Pydantic, persistence adapters with async SQLAlchemy and psycopg, migrations with
Alembic, and tests with pytest. Ruff and strict mypy are repository gates.

Keep these primary boundaries:

```text
packages/core/      Domain model and ports
packages/orchestration/ Agent graph and model adapters
services/api/       HTTP API, application services, and persistence adapters
services/embedding/ Embedding model service
frontend/           Visitor widget, operator console, and nginx gateway
```

`packages/core` has no runtime dependencies. It defines business meaning and
ports; services and orchestration provide I/O implementations. The API and
durable worker may share an image because they share application code. The
embedding service remains separate because its model runtime, memory use, and
startup behavior differ.

## Consequences

Domain rules run without a database, web server, or model, and architecture
tests make dependency drift visible. Ports and adapters introduce some
indirection, but keep transactions and framework code out of domain types.

The deployment still contains multiple images and a network boundary around
embeddings. That operational cost is accepted because the processes have
different resource profiles.

## Alternatives considered

- **Keep the standard-library HTTP server:** rejected for weak validation,
  contracts, async I/O, and migration support.
- **Let a full-stack framework own the domain model:** rejected because it would
  invert the intended dependency direction.
- **Run embeddings inside the API:** rejected because model serving has a
  different scaling and failure profile.
