# 0001 — Single LangGraph agent runtime over a framework-free domain

- **Status:** Accepted
- **Date:** 2026-07-31
- **Amended:** 2026-07-31 — restated the framework restriction as a per-layer
  dependency-direction policy after review found the original wording banned
  LangGraph from "repositories" without exempting the checkpoint adapter, which
  cannot be written without it.
- **Amended:** 2026-08-03 — recorded the framework surface actually adopted, the
  measured single-turn overhead, and where the checkpoint schema comes from,
  once `ARCH-001` implemented this record.
- **Supersedes:** `ADR-001` in `BACKLOG.md`
- **Affects:** `ARCH-001`, `AGENT-001`, `AI-001`, `AI-003`, `RAG-006`, `FEAT-004`

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

| Layer | Where it lives | LangGraph | Responsibility |
|---|---|---|---|
| `packages/core` | `tenantchat.core` | No | Entities, value objects, policies, business ports |
| Application services | `tenantchat.api.actions` | Not in public contracts | Commands, transactions, authorization, idempotency |
| API schemas and routes | `tenantchat.api.schemas`, `.routers` | Not in contracts | HTTP validation, RFC 9457 error mapping |
| Graph orchestration | `tenantchat.orchestration` | **Yes** | Graph state, nodes, interrupts, routing |
| Business repository adapters | `tenantchat.api.persistence` | No | SQLAlchemy and Elasticsearch implementations of core ports |
| Checkpoint adapter | `tenantchat.orchestration.checkpoints` | **Yes** | LangGraph's own execution-state persistence |
| Composition root | `tenantchat.api.agent`, `.app` | **Yes** | Wiring implementations together at startup |

The distinction the original wording missed: a *business repository* persists
domain aggregates and must not know the framework exists, while the *checkpoint
adapter* persists the framework's own execution state and necessarily imports it.
Both talk to Postgres; only one is a domain concern.

The chat routes reach the runtime the same way they reach anything else the
domain does not own: through a `Protocol`. `tenantchat.core.ports.ConversationRuntime`
takes a message and returns an `AssistantTurn` — a frozen domain value carrying
the answer, what was committed, what the runtime stopped to ask, and the
component versions `OBS-004` attributes the answer to. `tenantchat.api.agent`
holds the one adapter from a LangGraph turn to that value. This is not a second
runtime abstraction: there is one implementation, replacing LangGraph would mean
rewriting it rather than adding another, and the port exists so the HTTP layer
stays inside the scan rather than joining the composition root's exemption list.

### What is enforced today

`tests/test_architecture_invariants.py` scans `packages/core` for framework,
driver, transport, and model-SDK imports, and checks its declared dependencies
against a narrow approved list.

Core has no runtime dependencies today, but "zero dependencies" is a property, not
the rule. The rule is the category ban. A deterministic, I/O-free library encoding
a genuine domain concern may be added to `APPROVED_DOMAIN_LIBRARIES` with a note
here; `phonenumbers` is the standing candidate if service areas ever extend beyond
the North American numbering plan, where a hand-written regex becomes a liability.

`services/api` joined the scan with `ARCH-001`. Every module in it is checked for
LangChain, LangGraph, `tenantchat.orchestration`, and provider SDK imports, with
one exemption: `tenantchat/api/agent.py` and `tenantchat/api/app.py`, the
composition root. The exemption list is itself tested — an entry naming a file
that no longer exists, or one that has stopped composing the runtime, fails the
build rather than quietly widening the policy.

Orchestration and the checkpoint adapter are never scanned, because the framework
belongs there.

## Framework surface

**LangGraph, and the parts of it that are used.** `StateGraph` with a `TypedDict`
state, `interrupt`/`Command` for the booking confirmation, and the Postgres
checkpointer. Nothing else. The graph is six nodes.

**`langchain-core` is a declared dependency; LangChain is not used.** One name is
imported from it — `RunnableConfig`, the type of the config LangGraph threads
through a run. It arrives transitively with LangGraph regardless, so declaring it
records a fact rather than adding a dependency.

**`langchain-classic` is excluded.** It is the compatibility package for the
pre-v1 chain and agent APIs, and every one of the abstractions it carries —
`LLMChain`, `AgentExecutor`, the memory classes — is a place where prompt
assembly, tool dispatch, and control flow are decided by a library instead of by
code in this repository. `AI-003` versions prompts and `OBS-004` reconstructs a
turn from its record; both need those decisions to be ours and to be legible in a
diff.

**LangChain v1 `create_agent` is worth reaching for inside a node**, and not
above one. It is a good fit where a sub-task is genuinely a bounded tool loop
whose intermediate steps nobody needs to resume into — a research or
summarization step within one node, say. It is the wrong tool for the dispatcher
loop itself, because the interrupt, the round budget, and the replay-safe commit
are exactly the parts that must not be delegated. If one is added, it lives
behind a node boundary and its own tool list, and the node stays responsible for
what it commits.

**Reconsidering the OpenAI Agents SDK** would need two things to change, both
external to this codebase. `ADR-0004` would have to stop targeting a local
OpenAI-compatible server as the default, since that SDK's value is in its
provider-side features rather than in the wire format everyone implements; and
its durable-execution story would have to reach parity with a checkpointer that
survives process restart and supports interrupt-and-resume. Neither is close. The
domain layer is unaffected either way, which is the point of the layer policy.

## Measured cost

A single-turn question that calls no tools and commits nothing crosses two nodes
and costs **7 checkpoint writes** and roughly **1.2 ms** of in-process overhead
with the model held at zero. The budgets enforced in
`tests/agent_runtime/test_runtime_overhead.py` are 8 writes and a 25 ms median,
the latter loose enough to survive shared CI hardware while still catching a step
change such as a third node on the hot path.

This is the constraint the record accepted, now with a number on it.

## Operating the checkpoint store

LangGraph owns the checkpoint schema, so it is created by the library — `make
migrate-checkpoints`, which calls `AsyncPostgresSaver.setup()` under
`DATABASE_MIGRATION_URL` — rather than transcribed into an Alembic revision that
would fork a schema this repository does not control. It runs as a migration
because the application role holds no `CREATE` on `public`, which is what stops a
compromised API pod from altering the schema.

The checkpointer opens its own psycopg pool rather than sharing the SQLAlchemy
engine that serves domain queries. Checkpoint traffic is frequent and
individually worthless; letting it exhaust the pool that commits bookings would
trade a business write for a resume point.

Truncating every checkpoint table is a supported operation. It costs in-flight
conversations their resume point and costs the business nothing.

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
