# Working in this repository

Multi-tenant RAG and agent platform for home-services dispatch: an embeddable chat
widget that answers from tenant-approved knowledge, books appointments, captures
leads, and escalates to a human.

## Commands

```bash
make setup     # sync Python and frontend dependencies, seed .env
make check     # full quality gate: Python + JavaScript lint, format, types, tests, coverage
make up        # Postgres + Elasticsearch via docker compose
make test      # tests only
make help      # everything else
```

`make check` is the hermetic local quality gate and is what CI's quality job
runs. It uses fake browser API responses and a scripted chat model, so it needs
no LLM, database, search service, embedding model, or Kubernetes cluster. Migration, architecture,
container/security, and secret-history checks are separate CI jobs; run the
relevant local equivalents before proposing a change is complete.

Never invoke `pip` or a bare `python`. Everything goes through
`uv run --frozen`, so a hand-modified environment cannot change what is verified.
Dependency changes mean editing `pyproject.toml` and running `make lock`.

## Layout

```
packages/core/      Domain model. Zero runtime dependencies.
packages/orchestration/ LangGraph agent runtime: graph, nodes, checkpoint adapter.
services/           Deployable services: api, ingestion, embedding, financing-agent.
frontend/           Self-contained npm project: React 19 + TypeScript, built by Vite.
tests/              Cross-cutting tests, including architecture invariants.
docs/adr/           Architecture decision records. Start at docs/adr/README.md.
architecture/likec4/ Architecture-as-code model and generated diagrams.
BACKLOG.md          Full productionization plan with task IDs. Gate B is the target.
```

## Invariants

These are enforced by tests, not convention. Breaking one fails the build.

1. **`packages/core` imports no framework, driver, transport, or model SDK.**
   Domain rules define `Protocol` ports; adapters live in the service that owns
   the I/O. Enforced by `tests/test_architecture_invariants.py`. This is a
   dependency-*direction* policy, not a repo-wide ban — LangGraph belongs in
   orchestration, the checkpoint adapter, and the composition root. See the layer
   table in ADR-0001.
2. **Authorization, validation, transactions, and idempotency live in domain
   services**, never in agent-framework graph nodes. See ADR-0001. A graph node
   causes effects only through a `tenantchat.core.ports` Protocol carrying an
   `IdempotencyKey` derived from checkpointed values, because a node is replayed
   whenever a run resumes.
3. **LangGraph checkpoints are not the system of record.** Deleting every
   checkpoint must lose no conversation, booking, lead, or handoff.
4. **The domain carries no transport concerns.** Domain errors have a `code`, a
   safe `message`, and typed semantic fields — no HTTP status, no serializer. The
   API layer maps them onto RFC 9457 Problem Details responses.
5. **Public and internal data are separate types, not filtered dicts.**
   `TenantPolicy.public_view()` returns a `PublicTenantView`; a new private field
   cannot leak by omission.
6. **PII does not reach logs, traces, or metric labels.** `Contact.__str__` and
   `DomainError.__str__`/`__repr__` are safe by default, so an accidental
   f-string cannot leak one. Error `detail` is reachable only as an attribute,
   for structured logging that passes through redaction. Prompts, retrieved
   evidence, and model outputs are content and belong to the inference trace
   plane only — never to logs, metrics, or exported spans. See ADR-0010 for the
   two-plane split; the trace store is the one deliberate home for that content,
   governed by `PRIV-002`.

Not yet enforced, because the code it governs does not exist: **every answer is
reconstructible** from its turn record — router decision, retrieval candidate
set, assembled prompt, model parameters, and validator verdicts, each pinned to a
component version. `OBS-004` builds it and makes it testable. Design new
orchestration code so this stays achievable.

## Conventions

- Python 3.12, mypy `strict`, ruff for lint and format. Line length 100.
- Absolute imports only; relative imports are banned by lint.
- Exception classes end in `Error`.
- Prefer parsing over validating: return a closed type that cannot be invalid,
  rather than a bare `str` plus a separate `is_valid` check.
- Comments explain *why*. Do not narrate what the code already says.
- Tests read as specifications. Name them after the behavior being guaranteed,
  and give non-obvious cases a docstring explaining the failure being prevented.

## Context

The current `services/*/app.py` files are prototype code being replaced. They
are excluded from lint and type checking and will be deleted, not refactored —
several of their behaviors are bugs worth losing. The original `server.py`
prototype was removed in the `DEP-001` cutover; do not reintroduce it. Build in
`packages/core` and `services/api` instead.

`BACKLOG.md` is the full productionization plan and is written for both humans and
implementation agents. Task IDs referenced in comments (`SEC-003`, `RAG-004`) point
there. It is deliberately larger than current scope: it documents how the system
would be productionized, not a commitment to build all of it. `Gate B` is the
target; `Gate C` is documented and explicitly not committed.

The backlog's inline `ADR-001` section is **superseded** and retained only for
history. Where it and `docs/adr/` disagree, the ADR wins — the ADR index says so,
and the backlog section is labelled. Do not implement from it.
