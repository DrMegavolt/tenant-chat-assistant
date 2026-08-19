# 0001 — LangGraph runtime over a framework-free domain

- **Status:** Accepted
- **Date:** 2026-07-31
- **Supersedes:** the inline agent-framework decision in `BACKLOG.md`

## Context

The assistant has multi-turn booking and lead workflows, human handoff, durable
resume, and replay-sensitive effects. A custom tool loop would need to recreate a
state machine, checkpoint store, interrupts, and replay guards.

The framework must not own business rules. Authentication, tenant isolation,
validation, transactions, and idempotency need to remain deterministic and
testable without an agent runtime.

## Decision

Use LangGraph v1 and its PostgreSQL checkpointer as the only agent runtime. Do
not add a portability abstraction over multiple agent frameworks.

- Graph nodes orchestrate work; domain services decide and commit it.
- Every side effect uses an explicit idempotency key and tolerates replay.
- Checkpoints contain resumable execution state, never authoritative business
  records. Deleting them may interrupt a conversation but must not delete a
  booking, lead, handoff, or message.
- `packages/core` cannot import LangGraph, transport libraries, database
  libraries, or model SDKs.
- The API depends on the runtime through domain-facing ports. Framework imports
  are confined to orchestration, checkpointing, and composition modules.

These boundaries are enforced by architecture tests. LangGraph's schema is
created by its own migration command, and its connection pool is separate from
the pool used for business transactions.

## Consequences

Interrupt-and-resume and durable graph execution come from a maintained
framework. There is one runtime and one behavioral test surface.

The orchestration layer is coupled to LangGraph, simple turns pay checkpoint
overhead, and framework failures are less direct to debug than plain function
calls. Replacing LangGraph would require rewriting orchestration, but not the
domain model or business services.

## Alternatives considered

- **Keep the custom loop:** rejected because durable multi-turn workflows would
  require rebuilding framework features.
- **Support multiple runtimes:** rejected because the common abstraction would
  either leak framework details or exclude the features that motivated the
  framework.
- **Use a general workflow engine:** deferred until workflows need scheduled or
  multi-day execution.
