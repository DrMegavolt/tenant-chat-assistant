# 0001 — Single LangGraph agent runtime over a framework-free domain

- **Status:** Accepted
- **Date:** 2026-07-31
- **Amended:** 2026-07-31 — restated the framework restriction as a per-layer
  dependency-direction policy after review found the original wording banned
  LangGraph from "repositories" without exempting the checkpoint adapter, which
  cannot be written without it.
- **Supersedes:** `ADR-001` in `BACKLOG.md`
- **Affects:** `ARCH-001`, `AGENT-001`, `AI-001`, `RAG-006`, `FEAT-004`

## Context

The prototype runs a hand-written tool loop against an OpenAI-compatible endpoint
(`server.py`): bounded rounds, tool calls dispatched server-side, no persistence
between turns. It is small and easy to follow, and for single-turn question
answering it is entirely adequate.

It cannot express the workflows this product actually needs. A dispatcher
assistant has to collect booking details across several turns, pause for a human
to approve or take over, survive a deployment mid-conversation, and resume
without re-executing a booking it already committed. Encoding that as ad-hoc
state in a request handler means reinventing a state machine, a checkpoint store,
and a replay guard.

The superseded `ADR-001` proposed a framework-neutral `AgentRuntime` protocol
with two adapters — the existing loop and LangGraph — plus a documented third
option for the OpenAI Agents SDK.

## Decision

**Use LangGraph v1 with the Postgres checkpointer as the only agent runtime.
Do not build an abstraction layer over agent frameworks.**

Retain, unchanged, the part of the superseded record that carries the real
weight:

1. Authentication, authorization, validation, transactions, and idempotency live
   in deterministic domain services, never in graph nodes.
2. Every side-effecting node calls an idempotent domain service with an explicit
   idempotency key, and tolerates replay.
3. LangGraph checkpoints hold resumable execution state. Postgres domain tables
   remain the system of record for conversations, bookings, leads, and handoffs.
   Deleting every checkpoint must lose no business record.
4. Framework types flow in one direction only, per the layer policy below.

### Layer policy

The rule is **dependency direction**, not a repository-wide import ban. LangGraph
is expected and correct in the layers that orchestrate; it is excluded from the
layers that define meaning.

| Layer | LangGraph | Responsibility |
|---|---|---|
| `packages/core` | No | Entities, value objects, policies, business ports |
| Application services | Not in public contracts | Commands, transactions, authorization, idempotency |
| API schemas and routes | Not in contracts | HTTP validation, RFC 9457 error mapping |
| Graph orchestration | **Yes** | Graph state, nodes, interrupts, routing |
| Business repository adapters | No | SQLAlchemy and Elasticsearch implementations of core ports |
| Checkpoint adapter | **Yes** | LangGraph's own execution-state persistence |
| Composition root | **Yes** | Wiring implementations together at startup |

The distinction the original wording missed: a *business repository* persists
domain aggregates and must not know the framework exists, while the *checkpoint
adapter* persists the framework's own execution state and necessarily imports it.
Both talk to Postgres; only one is a domain concern.

### What is enforced today

`tests/test_architecture_invariants.py` scans `packages/core` — the only layer
that currently exists — for framework, driver, transport, and model-SDK imports,
and checks its declared dependencies against a narrow approved list.

Core has no runtime dependencies today, but "zero dependencies" is a property, not
the rule. The rule is the category ban. A deterministic, I/O-free library encoding
a genuine domain concern may be added to `APPROVED_DOMAIN_LIBRARIES` with a note
here; `phonenumbers` is the standing candidate if service areas ever extend beyond
the North American numbering plan, where a hand-written regex becomes a liability.

The scanned set grows as layers land: API request/response schemas and application
public contracts join it with `services/api`. Orchestration, checkpoint adapters,
and the composition root are never scanned, because the framework belongs there.

## Consequences

**Gained.** Interrupt-and-resume comes from the framework rather than from
bespoke state handling, which is what makes human approval and staff takeover
(`FEAT-004`) tractable. Durable execution across process restart is a checkpointer
configuration rather than a subsystem. One runtime means one set of tests.

**Lost.** LangGraph is now a hard dependency of the orchestration layer, and
migrating away later means rewriting graph definitions. This is an accepted risk:
the domain layer — where the business value and the hard-won correctness live —
is unaffected by that migration, because it cannot reference the framework at all.

**Cost.** LangGraph brings a substantial transitive dependency tree, and its
debugging story is worse than a plain function call: a failure surfaces inside
framework machinery rather than at the call site.

**Constraint accepted.** Simple one-turn chat now pays graph and checkpoint
overhead it does not need. Measured against the cost of maintaining two runtimes
that must stay behaviorally identical, this is the cheaper trade.

## Alternatives considered

**Keep the custom loop.** Rejected. Every workflow beyond single-turn Q&A would
require hand-building checkpointing, interrupts, and replay guards. That is the
part of an agent framework that is genuinely hard to get right, and writing it
twice is not a portfolio argument.

**The `AgentRuntime` protocol with two adapters, as originally accepted.**
Rejected. An abstraction across two agent frameworks has to expose the
intersection of their capabilities, so it either leaks the LangGraph model or
forbids the features that motivated adopting LangGraph. Two adapters must be kept
behaviorally identical under test, doubling the surface. The portability it buys
is speculative; the maintenance is immediate. The genuine insight in the original
record was the *domain/framework* boundary, not the *framework/framework* one —
and that boundary is kept, enforced by test.

**OpenAI Agents SDK.** Rejected as the primary path. It assumes an OpenAI-shaped
provider, while ADR-0004 targets a local OpenAI-compatible server with hosted
providers as a configuration change. Its durable-execution story is also less
developed than LangGraph's checkpointer.

**Temporal, DBOS, or Restate for durability.** Deferred, not rejected. These are
stronger than a checkpointer for workflows that wait hours or days. Nothing in
the current scope waits longer than a staff member's response time, so adopting a
workflow engine now would add an operational component with no present use.
Revisit if scheduled follow-ups or multi-day escalations enter scope.
