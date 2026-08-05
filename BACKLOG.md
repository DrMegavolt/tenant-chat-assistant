---
project: tenant-chat-assistant
document_type: implementation-backlog
schema_version: 1
last_updated: 2026-08-04
source_of_truth: BACKLOG.md
---

# Production RAG Chatbot Backlog

## Product goal

Turn the current tenant chatbot prototype into a production-oriented demonstration of a secure, multi-tenant RAG and agent platform. The finished demo should show safe knowledge retrieval, reliable business actions, human escalation, measurable outcomes, and an operational deployment story.

This backlog is written for both humans and implementation agents. Every task has a stable ID, status, priority, dependencies, bounded scope, acceptance criteria, and expected verification.

### What the demo is meant to prove

Three claims, in order of how hard they are to fake:

1. **Grounded answers, with the receipts.** A tenant uploads a document; it is parsed, chunked, embedded, indexed, and retrieved under tenant and version filters. Answers cite the exact authorized source version, or abstain and offer a human. A citation the model invented is rejected mechanically, not discouraged by a prompt.
2. **Business actions that survive reality.** Booking, lead capture, and human handoff are transactional, idempotent, and authorized by deterministic domain services — so a retried request, a mid-conversation restart, or a replayed workflow node cannot double-book anyone.
3. **Answers you can debug.** Any answer opens in the provenance viewer: the routing decision and the alternatives it rejected, the candidate set collapsing through reranking, the exact assembled prompt with trusted and untrusted regions distinguished, and each claim linked to the chunk supporting it. When an answer is wrong, the record identifies the first observable stage that failed. Deterministic failures are detected mechanically; prompt, model, and other ambiguous quality failures carry evidence, uncertainty, and controlled-comparison results rather than an unsupported causal claim from one stochastic response.

Claim 3 is the differentiator and the reason `OBS-004` sits in `Gate B` rather than in operational polish. Retrieval-augmented chat is common; showing exactly why a given answer came out the way it did is not.

### Scope honesty

This document is deliberately larger than the committed scope. It describes how the system *would* be productionized so the engineering judgement is legible; it is not a promise to build every task. `Gate B` is the target. `Gate C` is documented, costed, and explicitly not committed — see the note under it.

## Status and priority vocabulary

Allowed statuses:

- `Done`: Implemented in the repository. A `Done` prototype entry does not imply production hardening.
- `Todo`: Defined and available after its dependencies are complete.
- `In Progress`: Assigned to exactly one owner or coordinated agent team.
- `Blocked`: Cannot progress; the blocking reason must be added to the task.
- `Cancelled`: Deliberately removed from scope; the reason must be recorded.

Priorities:

- `P0`: Blocks safe public exposure or can cause unauthorized access, data loss, cross-tenant leakage, unsafe actions, or unrecoverable operation.
- `P1`: Required for a credible production-ready RAG/agent demonstration.
- `P2`: Important product maturity, usability, or operational improvement.
- `P3`: Optional enhancement after the production demo is complete.

### What is not a separate task

Quality and operational work is a property of a feature, not a successor to it.
An entry whose whole content is testing, deploying, or operating what another
entry delivers is folded into that entry's definition of done, so coverage
lands with the behavior instead of accumulating as a separately prioritized
debt. Four kinds of work stay dedicated, because none of them is the byproduct
of one feature:

- The shared harness other tasks' tests run in — `QA-001`, complete.
- AI quality measurement — `RAG-009` and `RAG-008`. The scoreboard is what makes
  retrieval tunable at all; it is a product capability, not hygiene applied
  afterwards.
- Controls that own a security or privacy boundary rather than test one —
  `SEC-*` and `PRIV-*`.
- Operating burden that exists only once the system runs for real — `DEP-*`,
  `OBS-003`, and `QA-005`. These are Gate C and deliberately uncommitted.

## Agent dispatch contract

When dispatching an implementation agent, give it one task ID and require it to follow these rules:

1. Read this document, the assigned task, its dependencies, and the files named in `Likely areas`.
2. Do not silently expand scope into another backlog task.
3. Preserve existing user changes and coordinate before editing files another active agent is changing.
4. Do not add real credentials, tokens, customer data, or production endpoints to the repository.
5. Add or update automated tests for every behavioral change.
6. Run the task's verification plus relevant regression tests.
7. Update only the assigned task's `Status` and `Completion notes` after the acceptance criteria pass.
8. If blocked, set `Status: Blocked` and state the exact evidence and required decision. Do not mark partially implemented work `Done`.

Suggested dispatch prompt:

```text
Implement task <TASK-ID> from BACKLOG.md. Respect its dependencies and scope.
Preserve unrelated work. Add tests, run verification, and update that task's
status and completion notes only when every acceptance criterion passes.
```

## Architecture and security invariants

All future work must preserve these invariants:

- A protected tenant ID comes from authenticated server-side context, not an arbitrary request field.
- A visitor can access only their own conversation; an admin can access only authorized tenants.
- Conversation history is server-authoritative and append-only from the client's perspective.
- Durable application state is stored in the database, not process-global dictionaries or lists.
- Side-effecting actions are validated, transactional, auditable, and idempotent.
- Retrieved documents are untrusted data, never instructions.
- The model cannot grant itself tool permissions or bypass deterministic policy checks.
- PII and secrets do not appear in logs, metrics, traces, browser debug output, or error messages.
- Production manifests contain no literal credentials and do not install dependencies during pod startup.
- Degraded operation must fail safely: it may answer less, but it must not fabricate or duplicate business actions.
- Every answer is reconstructible. Given a turn ID, the router decision, retrieval candidate set, assembled prompt, model parameters, and validator verdicts can be retrieved and re-run. An answer whose provenance cannot be reconstructed is a bug, not a mystery.
- Prompt and evidence content lives in the inference trace plane only. The operational log, metric, and span plane carries identifiers, enums, counts, and versions.
- Agent-framework checkpoints hold resumable execution state; Postgres domain tables remain the system of record for conversations and business actions.
- Framework code may orchestrate typed domain tools, but authentication, authorization, validation, transactions, and idempotency stay in deterministic application services.

## Superseded agent-framework architecture decision

> **Do not implement from this section.** It is retained for history only.
> [`docs/adr/0001-agent-runtime.md`](docs/adr/0001-agent-runtime.md) supersedes
> it and narrows the scope to a single LangGraph runtime with no abstraction
> layer over agent frameworks. Point 2 below — the framework-neutral
> `AgentRuntime` protocol with two adapters — is the specific part that was
> rejected. Where the two disagree, the ADR wins.

- Decision ID: `ADR-001`
- Status: `Superseded by ADR-0001`
- Decision date: `2026-07-31`
- Superseded on: `2026-07-31`
- Applies to: `ARCH-001`, `AI-001`, `AGENT-001`, `RAG-006`, and `FEAT-004`

The target architecture is a hybrid rather than a framework-wide rewrite:

1. Keep business rules, tenant authorization, retrieval, booking, leads, handoff, and integrations as typed framework-independent services.
2. Extract the current short-lived model/tool loop behind an `AgentRuntime` protocol while the P0 API and data foundations are implemented.
3. Use LangGraph v1 for long-running, branching, resumable workflows that need checkpoints, human approval, handoff, or failure recovery.
4. Use LangChain v1 `create_agent` only where its high-level agent loop and middleware reduce code; do not add `langchain-classic` chains or spread LangChain types through domain services.
5. Keep LangGraph checkpoint state separate from authoritative domain records. Every side-effecting graph node must call an idempotent domain service and tolerate replay.
6. Use the Postgres outbox/background-worker layer for durable external delivery. Evaluate Temporal, DBOS, or Restate only if workflows must survive long waits or failures beyond the outbox design.
7. Use MCP selectively for reusable external tool interoperability. MCP does not replace service authorization, transactions, or domain validation.
8. Keep an OpenAI Agents SDK adapter as a documented alternative for an OpenAI-first deployment, but prefer the LangGraph path for the current local OpenAI-compatible model and multi-provider goal.

Decision drivers:

- The current loop is small and transparent; replacing it does not solve the existing security, persistence, or booking-integrity blockers.
- The target workflows require explicit state, pause/resume, staff approval, and safe restart recovery, which are strong LangGraph use cases.
- Provider and framework portability are portfolio goals.
- Production business effects must remain testable without an LLM or agent framework.

Primary references:

- [LangChain v1 agents](https://docs.langchain.com/oss/python/releases/langchain-v1)
- [LangGraph v1 runtime](https://docs.langchain.com/oss/python/releases/langgraph-v1)
- [LangGraph durable execution and idempotency](https://docs.langchain.com/oss/python/langgraph/functional-api)
- [OpenAI Agents SDK](https://openai.github.io/openai-agents-python/)
- [Anthropic: Building Effective Agents](https://www.anthropic.com/engineering/building-effective-agents)
- [Model Context Protocol](https://modelcontextprotocol.io/docs/getting-started/intro)

The architecture-as-code source and generated views for this decision live under `architecture/likec4/`.

## Global definition of done

A non-documentation task is `Done` only when:

- Its acceptance criteria are implemented.
- Unit and integration tests cover the changed behavior and important failure paths.
- Every route, repository, or migration it changes is exercised against a real
  Postgres through a documented `make` target, not only against fakes.
- Every tenant-scoped surface it adds has a cross-tenant case and an
  unauthorized-principal case asserting the error contract, not only the status
  code.
- Any exploit class it closes gains a permanent regression test under
  `tests/security/`, named after the exploit rather than the fix.
- A customer- or operator-visible workflow it completes ships its own
  end-to-end case. A task does not hand its workflow coverage to a later one.
- Any runtime dependency it introduces is represented in readiness and never in
  liveness, and its absence degrades the service rather than restarting it.
- Critical actions it adds emit the structured events and metrics the `OBS-001`
  and `OBS-002` contracts require, once those exist, with no PII in log fields
  or metric labels.
- Tests, linting, and static checks pass locally.
- Configuration and environment variables are documented with safe defaults.
- Errors are observable without exposing secrets or PII.
- No new high-severity dependency or container scan findings are knowingly introduced.
- The task's completion notes list changed files, verification commands, and any follow-up task IDs.

## Release gates

Gate A makes the project safe to show. **Gate B is the target** — it is the gate
that demonstrates the three claims above. Gate C is a costed description of what
production would additionally require, kept because knowing the gap is part of
the point, and not committed.

### Gate A — Safe public demo

Required: every `P0` task in this document. Until Gate A passes, expose the project only on a trusted development network.

### Gate B — Full RAG showcase — **the target**

Required: Gate A plus `ARCH-001`, `AI-001`, `AI-003`, `AGENT-001`, `REL-001`,
`REL-003`, `RAG-001` through `RAG-009`, `FEAT-001`, `FEAT-004` (Gate B slice),
`FEAT-008`, `FEAT-011`, `FEAT-015`, `OBS-001`, `OBS-002`, `OBS-004`, and
`PRIV-002`. The target
demonstration is a visible, tenant-safe document lifecycle: upload, parse,
chunk, embed, index, retrieve, rerank, answer with authorized citations, abstain
on weak evidence, and publish comparable evaluation results — with any single
answer in that lifecycle discoverable in the AI turn explorer and openable as
the actual executed call graph, showing why it said what it said and, where it
was wrong, which stage failed and how certain the causal diagnosis is.

Integration, tenant-isolation, and end-to-end coverage are not gate items; they
are conditions each required task satisfies to be `Done` at all. The gate is
verified by running the acceptance script below against a build in which every
required task has met that definition.

`FEAT-004` is in this gate because claim 2 names human handoff alongside booking
and lead capture. Without it the escalation path writes a queue row that no
operator can work, and the claim is a schema rather than a behavior. Its Gate B
slice is verified by its own end-to-end test, not by the trace script below,
which belongs to claim 3.

#### Gate B executable acceptance script

The showcase is complete only when the following seeded cases run end to end
across both demo tenants. Each case must define the expected customer behavior,
executed graph, automatic detector or explicit `inconclusive` result, diagnosis
status, safe replay result, and browser walkthrough in `FEAT-015`. Reuse these
fixtures in `OBS-004`, `RAG-009`, and `RAG-008` rather than maintaining three
independent corpora.

1. A correct grounded answer with an authorized citation.
2. A stale source that must not silently shape a current answer.
3. A published document whose index generation is missing or incomplete.
4. A relevant chunk retrieved but ranked below the selection cutoff.
5. Selected evidence dropped by the context budget.
6. A prompt regression isolated with the model and evidence held constant.
7. A model-behavior difference demonstrated through bounded repeated trials
   with prompt and evidence held constant.
8. A fabricated citation rejected mechanically.
9. An application, tool, or provider failure located at the executed node.
10. An indirect prompt-injection document quarantined without changing policy
    or invoking a tool.

### Gate C — Operational production candidate — documented, not committed

This gate exists to record what running the system for real would cost, not to
schedule it. Its tasks — high availability, autoscaling, disaster-recovery
drills, release provenance, load and soak testing — are the ones a demonstration
cannot honestly claim without an operating burden that teaches nothing further
about the architecture. Treat every `Gate C` task as `P3` in practice unless the
project's purpose changes; the priorities below record what production would
demand, not what to build next.

Required: Gate B plus `DEP-002` through `DEP-006`, `OBS-003`, `QA-005`, the
`FEAT-004` remainder, the real-provider integrations (`FEAT-002`, `FEAT-003`,
`FEAT-005`), the remaining production business workflows, and completed
runbooks.

## Backlog index

### Existing scope

- [x] `BASE-001` — Embeddable multi-tenant chat widget — `Done`
- [x] `BASE-002` — Tenant policy and LLM tool loop — `Done`
- [x] `BASE-003` — Lead capture prototype — `Done`
- [x] `BASE-004` — Availability and booking prototype — `Done`
- [x] `BASE-005` — Live admin transcript console — `Done`
- [x] `BASE-006` — Local and Postgres snapshot persistence — `Done`
- [x] `BASE-007` — Financing RAG agent — `Done`
- [x] `BASE-008` — Embedding and knowledge ingestion services — `Done`
- [x] `BASE-009` — Kubernetes deployment baseline — `Done`
- [x] `BASE-010` — Metrics, tracing, outcomes, and rule fallback baseline — `Done`

### P0 production exposure blockers

- [x] `QA-001` — Foundational automated test harness and CI — `P0`
- [x] `DATA-001` — Normalized schema and migration framework — `Done`
- [x] `API-001` — Production API runtime and typed contracts — `Done`
- [x] `DATA-002` — Server-authoritative repositories and concurrency control — `Done`
- [x] `DATA-003` — Transactional, idempotent booking — `Done`
- [x] `SEC-001` — Admin authentication and tenant-scoped RBAC — `P0`
- [x] `SEC-002` — Secure visitor sessions and tenant binding — `Done`
- [x] `SEC-003` — API abuse protection, CORS, and response hardening — `P0`
- [x] `SEC-004` — Service authentication and Kubernetes network boundaries — `Done`
- [x] `SEC-005` — Secret management and credential removal — `P0`
- [x] `PRIV-001` — PII classification, consent, retention, export, and deletion — `Done`
- [x] `DEP-001` — Immutable, reproducible application images — `Done`

### P1 demo-critical RAG path

- [x] `ARCH-001` — Agent runtime boundary and LangGraph adoption — `Done`
- [x] `AI-001` — Provider and model abstraction — `Done`
- [ ] `AI-003` — Versioned prompt assembly and template registry — `P1`
- [ ] `REL-001` — Resilient dependency clients — `P1`
- [x] `REL-003` — Durable background jobs and retry handling — `Done`
- [x] `RAG-001` — Versioned knowledge content model — `Done`
- [x] `RAG-002` — Secure asynchronous ingestion lifecycle — `Done`
- [ ] `RAG-003` — Production document parsing and chunking — `P1`
- [ ] `RAG-004` — Hybrid retrieval, reranking, and abstention — `P1`
- [ ] `RAG-005` — Evidence and citation contract — `P1`
- [ ] `RAG-006` — Conversation-aware retrieval — `P1`
- [ ] `RAG-007` — RAG prompt-injection and content safety defenses — `P1`
- [ ] `RAG-009` — Golden evaluation harness and scoreboard — `P1` — _prerequisite for `RAG-004` tuning_
- [ ] `RAG-008` — RAG evaluation and regression suite — `P1`
- [ ] `AGENT-001` — Persisted intent router and workflow state machine — `P1`
- [ ] `OBS-001` — Structured logging and request correlation — `P1`
- [ ] `OBS-002` — LLM, RAG, tool, and business metrics — `P1`
- [ ] `OBS-004` — Inference trace, answer provenance, and failure attribution — `P1`
- [ ] `PRIV-002` — Inference trace data plane, retention, and access control — `P1`

### P1 demo-critical business actions

- [ ] `FEAT-004` — Human handoff queue and agent takeover — `P1` — _Gate B slice; remainder is `P2`_

### P2 product maturity — after Gate B

- [ ] `AI-002` — Model safety, quotas, and cost controls — `P2`
- [ ] `FEAT-010` — Streaming, cancellation, and reliable message delivery — `P2`
- [ ] `FEAT-002` — Real availability and calendar integration — `P2`
- [ ] `FEAT-003` — CRM lead integration and delivery guarantees — `P2`
- [ ] `FEAT-005` — Notification and outbound webhook workflow — `P2`
- [ ] `FEAT-006` — Tenant onboarding, policy, and branding administration — `P2`
- [ ] `FEAT-007` — Conversation search, filters, and operator actions — `P2`
- [ ] `FEAT-009` — Business outcome and conversion analytics — `P2`
- [ ] `FEAT-012` — Booking cancellation and rescheduling — `P2`
- [ ] `FEAT-013` — Accessibility, responsive embed, and privacy UX — `P2` — _client slice complete_

### P3 operating burden — Gate C, not committed

- [ ] `DEP-002` — Kubernetes workload hardening — `P3`
- [ ] `DEP-003` — TLS ingress and production widget hosting — `P3`
- [ ] `DEP-004` — High availability and autoscaling — `P3`
- [ ] `DEP-005` — Backup, restore, and disaster-recovery drill — `P3`
- [ ] `DEP-006` — Release pipeline, scanning, and provenance — `P3`
- [ ] `OBS-003` — Dashboards, SLOs, and alerts as code — `P3`
- [ ] `QA-005` — Load, soak, and failure-injection tests — `P3`
- [ ] `FEAT-014` — Additional business-domain agents — `P3`

### Folded into the definition of done — `Cancelled`

Each of these existed only to test or operate what another task delivers. The
obligation is unchanged; it moved into the global definition of done, where it
is owned by the task that introduces the behavior.

- `QA-002` — API and database integration tests
- `QA-003` — Tenant-isolation and security regression tests
- `QA-004` — End-to-end business workflow tests
- `REL-002` — Dependency-aware health, graceful startup, and shutdown

## Feature and workflow task list

These tasks cover the missing customer-facing and operator-facing capabilities
identified in the production audit. Priority follows the three claims in *What
the demo is meant to prove*: a feature that supplies evidence for a claim is
`P1`; a feature that adds a vendor integration behind an interface already
proven by a domain task is `P2`, because the second CRM adapter teaches nothing
the first outbox did not.

- [ ] `FEAT-001` — Knowledge-base administration workflow — `P1`
- [ ] `FEAT-004` — Human handoff queue and agent takeover — `P1` — _Gate B slice; remainder is `P2`_
- [ ] `FEAT-008` — User feedback and reviewed-answer workflow — `P1`
- [ ] `FEAT-011` — Customer-facing citations and source viewer — `P1`
- [ ] `FEAT-015` — AI turn explorer and executed-graph console — `P1`
- [ ] `FEAT-010` — Streaming, cancellation, and reliable message delivery — `P2`
- [ ] `FEAT-002` — Real availability and calendar integration — `P2`
- [ ] `FEAT-003` — CRM lead integration and delivery guarantees — `P2`
- [ ] `FEAT-005` — Notification and outbound webhook workflow — `P2`
- [ ] `FEAT-006` — Tenant onboarding, policy, and branding administration — `P2`
- [ ] `FEAT-007` — Conversation search, filters, and operator actions — `P2`
- [ ] `FEAT-009` — Business outcome and conversion analytics — `P2`
- [ ] `FEAT-012` — Booking cancellation and rescheduling — `P2`
- [ ] `FEAT-013` — Accessibility, responsive embed, and privacy UX — `P2` — _client slice complete_
- [ ] `FEAT-014` — Additional business-domain agents — `P3`

## Completed baseline details

### BASE-001 — Embeddable multi-tenant chat widget

- Status: `Done`
- Priority: `Baseline`
- Evidence: `frontend/src/widget/` and `frontend/src/demo/`
- Existing scope:
  - Browser chat experience with quick actions and per-tenant session IDs.
  - Two demonstration companies with distinct policies and branding.
  - Configurable remote API base URL for embedding the widget.
- Completion notes: Existing prototype behavior; production session security is tracked in `SEC-002`.

### BASE-002 — Tenant policy and LLM tool loop

- Status: `Done`
- Priority: `Baseline`
- Evidence: `packages/core/` (dispatcher and policy rules) and `packages/orchestration/` (the `ARCH-001` graph). The prototype tool loop in `server.py` was deleted with the `API-001` cutover.
- Existing scope:
  - Server-owned tenant facts and pricing/booking policies.
  - OpenAI-compatible chat-completions loop with bounded tool rounds.
  - Deterministic policy checks around service area, availability, booking, lead capture, and handoff.
- Completion notes: Existing prototype behavior; authorization and provider abstraction remain outstanding.

### BASE-003 — Lead capture prototype

- Status: `Done`
- Priority: `Baseline`
- Evidence: `services/api/` (`POST /api/leads`), `frontend/src/widget/`, and `frontend/src/admin/`. The prototype `server.py` lead capture was deleted with the `API-001` cutover.
- Existing scope:
  - Required-field and contact validation.
  - Lead capture through model tools and rule fallback.
  - Lead display in the admin console.
- Completion notes: Leads are not yet delivered to a real CRM; see `FEAT-003`.

### BASE-004 — Availability and booking prototype

- Status: `Done`
- Priority: `Baseline`
- Evidence: `services/api/` (`POST /api/book`, availability) and `frontend/src/widget/components/BookingConfirmation.tsx`. The prototype `server.py`/`BookingForm.tsx` booking flow was deleted with the `API-001` cutover.
- Existing scope:
  - Static service-specific availability.
  - Structured booking form with contact and address validation.
  - Confirmation records shown in the admin console.
- Completion notes: This is a non-transactional mock; production integrity is tracked in `DATA-003` and `FEAT-002`.

### BASE-005 — Live admin transcript console

- Status: `Done`
- Priority: `Baseline`
- Evidence: `frontend/admin.html` and `frontend/src/admin/`
- Existing scope:
  - Session list, outcomes, transcripts, leads, bookings, and tool-event panels.
  - Polling for updates and staff messages into visitor conversations.
- Completion notes: Authentication and real handoff ownership are tracked in `SEC-001` and `FEAT-004`.

### BASE-006 — Local and Postgres snapshot persistence

- Status: `Done`
- Priority: `Baseline`
- Evidence: `packages/core/` (DML-only repositories) and `services/api/` persistence. The prototype JSONB snapshot writer in `server.py` was deleted with the `API-001` cutover.
- Existing scope:
  - Atomic local JSON archive writes.
  - Optional Postgres JSONB session snapshots.
  - Startup reload of prior conversations, leads, and bookings.
- Completion notes: Normalized durable state and multi-replica correctness are tracked in `DATA-001` and `DATA-002`.

### BASE-007 — Financing RAG agent

- Status: `Done`
- Priority: `Baseline`
- Evidence: `services/financing-agent/app.py`
- Existing scope:
  - Tenant-filtered vector search against financing documents.
  - Grounding-oriented financing prompt and conservative fallback response.
  - Retrieved chunk metadata returned to the main chatbot.
- Completion notes: Retrieval quality, citations, and evaluations are tracked in the `RAG-*` tasks.

### BASE-008 — Embedding and knowledge ingestion services

- Status: `Done`
- Priority: `Baseline`
- Evidence: `services/embedding/app.py` and `services/ingestion/app.py`
- Existing scope:
  - Dedicated sentence-transformer embedding service.
  - Markdown parsing, overlapping chunks, embedding, and Elasticsearch indexing.
  - Tenant and domain metadata on indexed chunks.
- Completion notes: Secure, versioned ingestion is tracked in `RAG-001` through `RAG-003`.

### BASE-009 — Kubernetes deployment baseline

- Status: `Done`
- Priority: `Baseline`
- Evidence: `k8s/app.yaml` and `k8s/deploy.sh`
- Existing scope:
  - Deployments/services for chat, embedding, ingestion, financing, Postgres, Elasticsearch, and Kibana.
  - Persistent volumes, readiness probes, resource requests, and seed jobs.
- Completion notes: This is a development deployment; hardening and release tasks are tracked in `DEP-*` and `SEC-004`.

### BASE-010 — Metrics, tracing, outcomes, and rule fallback baseline

- Status: `Done`
- Priority: `Baseline`
- Evidence: `services/*/app.py` metrics endpoints and ServiceMonitors, `k8s/otel-collector.yaml`, and the API's `/healthz` probe. The prototype `server.py` metrics scaffolding was deleted with the `API-001` cutover; the API itself gains Prometheus instrumentation in `OBS-002`.
- Existing scope:
  - Prometheus metrics endpoints and ServiceMonitors (side services; the API intentionally has no ServiceMonitor until it exposes `/metrics`).
  - OpenTelemetry auto-instrumentation scaffolding and Tempo export.
  - Conversation outcome inference and deterministic fallback when the LLM is unavailable.
- Completion notes: Production observability is tracked in `OBS-*`.

## P0 task details

### QA-001 — Foundational automated test harness and CI

- Status: `Done`
- Priority: `P0`
- Type: `Quality`
- Depends on: None
- Likely areas: `tests/`, `pyproject.toml`, frontend test configuration, `.github/workflows/` or equivalent CI directory
- Scope:
  - Add Python unit-test infrastructure and JavaScript test infrastructure.
  - Add characterization tests for tenant policies, tool validation, fallback routing, ingestion chunking, retrieval filters, and current API contracts.
  - Add linting, formatting checks, type checking, and dependency/container scan hooks.
  - Create one documented command that runs the complete local quality gate.
- Acceptance criteria:
  - Tests run without a live LLM, Elasticsearch, embedding model, or Kubernetes cluster.
  - External services are replaceable with fixtures/fakes.
  - CI runs on pull requests and blocks merge on failure.
  - Test output and coverage artifacts are retained by CI.
- Verification:
  - Run the documented full quality-gate command twice from a clean checkout.
- Completion notes: Added a locked Vitest/jsdom frontend harness with ESLint,
  Prettier, V8 coverage, and six offline widget characterizations; added offline
  characterizations for the ingestion and financing side-service contracts
  (`tests/test_side_service_contracts.py`) plus fixture-based domain and API
  contract tests. The remainder of `server.py`'s fallback characterization was
  deleted with the `API-001` cutover, where the behavior became the chat routes'
  `503 chat_unavailable` signal. Existing core/API tests cover
  tenant policy, tool validation, and current contracts with in-memory fakes.
  `make check` is the documented local/CI gate for Python and JavaScript and
  writes Python XML/HTML plus frontend Cobertura/HTML/summary coverage under
  `coverage/`. CI runs on pull requests, uploads JUnit test results, coverage,
  and vulnerability reports for 30 days, and blocks on actionable high/critical
  filesystem or container findings using a known-safe SHA-pinned Trivy action;
  no external service or configured secret is required. Changed: `package.json`,
  `package-lock.json`, `eslint.config.js`, `.prettierrc.json`,
  `vitest.config.js`, `frontend/tests/widget.test.js`,
  `tests/test_side_service_contracts.py`, `pyproject.toml`, `uv.lock`,
  `Makefile`, `.github/workflows/ci.yml`, `.gitignore`, `README.md`,
  `CLAUDE.md`, plus formatting-only updates to `app.js` and `admin.js`.
  Verified `make check` twice after removing dependency directories and cleaning
  generated artifacts: 271 Python tests (99% covered source) and six widget
  tests (86.21% statements) passed both times; `npm audit --audit-level=high`
  reported zero vulnerabilities. Follow-ups: integration, isolation, and
  end-to-end coverage became global definition-of-done obligations rather than
  the successor tasks recorded here when this shipped — `QA-002` through
  `QA-004` are `Cancelled` and name where each moved; `QA-005` still owns load
  and soak coverage at Gate C; `DEP-001` replaced the prototype image (deleted
  with the `API-001` cutover) and `DEP-006` owns release scanning/provenance.

### DATA-001 — Normalized schema and migration framework

- Status: `Done`
- Priority: `P0`
- Type: `Data`
- Depends on: `QA-001`
- Likely areas: new migration directory, data models, `requirements.txt`, deployment migration job
- Scope:
  - Introduce versioned migrations instead of creating schema during request-server startup.
  - Model tenants, chat sessions, messages, tool executions, leads, bookings, handoffs, idempotency keys, and audit events as tenant-scoped records.
  - Include foreign keys, uniqueness constraints, timestamps, state enums, and query indexes.
  - Provide an upgrade path from existing JSONB session snapshots or explicitly document a safe prototype-data reset.
- Acceptance criteria:
  - A new database can migrate from zero to current.
  - Re-running migrations is safe, and downgrade/restore guidance exists.
  - Tenant-scoped queries have supporting indexes and foreign keys.
  - The application role does not need schema-owner privileges during normal operation.
- Verification:
  - Migration tests run against a temporary Postgres instance.
- Completion notes: Alembic revision `0001_normalized` creates tenant-scoped
  tenants, sessions, messages, tool executions, leads, bookings, handoffs,
  idempotency keys, and audit events with composite tenant foreign keys, closed
  state types/checks, timestamps, uniqueness, and tenant-leading query indexes.
  Migrations use a schema-owner-only URL and a one-shot Kubernetes Job; the
  separately provisioned runtime role has DML without DDL or Alembic revision
  mutation, and audit events are append-only to it. Legacy JSONB snapshots fail
  closed and follow the documented backup/quarantine/reset decision rather than
  becoming trusted records. Changed: `alembic.ini`, `services/api/migrations/`,
  `services/api/pyproject.toml`, `tests/migrations/`, `.github/workflows/ci.yml`,
  `k8s/api-migration-job.yaml`, `docs/runbooks/database-migrations.md`,
  `Makefile`, `.env.example`, `README.md`, `pyproject.toml`, and `uv.lock`.
  Verified: `make test-migrations` (6 passed on disposable PostgreSQL 16) and
  `make check` (283 passed; lock, lint, format, and mypy strict clean).
  Follow-ups: `DATA-002` implements repositories; `DATA-003` owns transactional
  booking and slot/idempotency semantics; `DEP-001` supplies the immutable image
  digest used by the migration Job.

### API-001 — Production API runtime and typed contracts

- Status: `Done`
- Priority: `P0`
- Type: `Backend architecture`
- Depends on: `QA-001`
- Likely areas: `services/api/`, `k8s/app.yaml`
- Scope:
  - Replace the standard-library `ThreadingHTTPServer` with a maintained ASGI framework and production server.
  - Split routing, domain services, provider clients, configuration, and persistence into testable modules.
  - Define typed request/response schemas with bounds for every field and request body.
  - Add stable error codes, request IDs, OpenAPI documentation, and centralized exception handling.
  - Preserve existing public behavior through versioned endpoints or documented compatibility aliases.
- Acceptance criteria:
  - Invalid JSON, oversized bodies, unknown tenants, malformed contacts, and upstream errors return bounded, non-sensitive responses.
  - `0001234567` is rejected as a booking or lead contact by every route that accepts one. Ten digits is not a dialable NANP number, and a confirmed appointment against an unreachable contact is indistinguishable from a no-show.
  - API schemas are generated and contract-tested.
  - Client disconnects and server shutdown do not leave corrupt actions.
- Verification:
  - API contract tests pass under the production server command.

**Slice 1 — booking, lead, and tenant surface. Complete.**

`services/api` serves `/healthz`, `GET /api/tenants`, `GET
/api/tenants/{id}/availability`, `POST /api/book`, and `POST /api/leads` on
FastAPI, with every booking and lead rule in `tenantchat.core.commands`. Domain
errors map onto RFC 9457 Problem Details carrying a stable `code`, a request ID,
and typed extension members; `DomainError.detail` and Pydantic's rejected input
values are logged, never published. Run it with `make api`.

- Changed: `packages/core/{commands,errors,__init__}.py`, `services/api/**`, `pyproject.toml`, `uv.lock`, `Makefile`, `.env.example`.
- Verified: `make check` — 263 tests, ruff and mypy `strict` clean. `0001234567` returns `422 invalid_contact` under `make api`; a valid number returns `201`.

**Slice 2 — chat and admin surface. Complete.**

`services/api` serves the visitor conversation and the operator console over the
`ARCH-001` runtime. The prototype's tool loop was not transcribed: the graph is
the loop, and every effect still crosses an idempotent domain service.

- **Conversation identity is server-issued.** `POST /api/chat/session` mints the
  ID; `POST /api/chat` and `GET /api/chat/session/{id}` accept nothing else. A
  visitor-chosen label can be guessed or replayed, which is why `DATA-002`
  refuses to let one select a transcript. `SEC-002` replaces the unguessable ID
  with a signed credential that can also carry an expiry.
- **A proposed booking pauses instead of committing.** `POST /api/chat` returns
  `pending` and no reply; nothing is written until `POST /api/chat/confirmation`
  carries the customer's answer. Confirming a conversation that is waiting on
  nothing is a `409` rather than a second answer to one question.
- **The store is the record.** The visitor's message is appended before the
  runtime is asked anything, so a model outage loses the reply and never the
  question. Deleting every checkpoint costs resume points and no transcript.
- **Chat without `AI-001` is unavailable, not broken.** A deployment with no
  model adapter composes no runtime, and the chat routes answer `503
  chat_unavailable`. Reading a transcript does not depend on being able to
  answer one.
- **Admin routes fail closed.** They require the gateway-injected identity plus
  the shared `ADMIN_GATEWAY_TOKEN`, re-check the role in the service rather than
  trusting proxy routing, and take a double-submit CSRF token on staff replies.
  Both secrets are required at startup. A response to an admin path carries no
  CORS grant, so an allowlisted widget origin cannot become a way to read
  another tenant's transcripts. Tenant-scoped RBAC remains `SEC-001`.
- **The HTTP layer does not import the agent framework.** Handlers depend on
  `tenantchat.core.ports.ConversationRuntime`; `tenantchat.api.agent` is the one
  adapter, and `tests/test_architecture_invariants.py` still holds the line. See
  the boundary note added to [`ADR-0001`](docs/adr/0001-agent-runtime.md).

Deliberately not ported: the prototype's authenticated `GET /api/leads`. The
admin console never called it, and an unscoped listing hands every operator
every tenant's customer contact details — it belongs with the tenant membership
check in `SEC-001`, not ahead of it.

- Changed: `packages/core/{ports,__init__}.py`, `services/api/src/tenantchat/api/{app,agent,dependencies,faults,identity,schemas,settings,store}.py`, `services/api/src/tenantchat/api/routers/{chat,admin}.py`, `services/api/src/tenantchat/api/persistence/repositories.py`, `services/api/tests/**`, `tests/{repositories,agent_runtime}/**`, `README.md`, `.env.example`.
- Verified: `make check` — 606 hermetic tests, ruff and mypy `strict` clean. `make test-repositories` (27) and `make test-agent-runtime` (6) on disposable PostgreSQL 16, including a booking paused by one API instance and confirmed by a restarted one over the production composition and its PostgreSQL checkpointer.

**Slice 3 — cutover. Complete.**

The deployment now runs `services/api` and the prototype is gone: `server.py`,
the root `Dockerfile`, and the `prototype` image were deleted rather than
refactored. `k8s/app.yaml` runs the `api` image on the single port 8004; the
frontend, the nginx gateway, the Vite dev proxy, and the local compose/`make
api` flow target the new contracts, and the gateway's public listener forwards
exactly the API's visitor surface (including the visitor `POST /api/leads`
write and the path-parameter session and availability routes). The API holds no
`FINANCING_AGENT_URL` and no `CHAT_TO_FINANCING_TOKEN`; the network policies and
their live smoke were updated to match. A migration of a database still holding
prototype JSONB snapshots must follow the migration runbook and stop all
prototype writers first — which the deletion of the prototype image now
guarantees by construction.

- Completion notes: The `DEP-001` cutover shipped; `server.py` is deleted and
  the deployed image is `services/api`. `make check` is green (590 hermetic
  Python tests plus the frontend suite), and `make image-contracts`,
  `make deployment-security`, and `make images-check` reflect the five-image
  set.

### DATA-002 — Server-authoritative repositories and concurrency control

- Status: `Done`
- Priority: `P0`
- Type: `Data/backend`
- Depends on: `DATA-001`, `API-001` slice 1
- Likely areas: backend repositories/services, `server.py` replacement modules
- Scope:
  - Remove process-global sessions, leads, bookings, counters, and Postgres-as-snapshot behavior.
  - Make the database authoritative and append messages server-side.
  - Use a bounded connection pool and explicit transactions.
  - Add optimistic versioning or row locks for concurrent conversation updates.
- Acceptance criteria:
  - Two application replicas observe the same conversation state.
  - A client cannot replace prior assistant or staff messages.
  - Concurrent messages preserve ordering without lost updates.
  - Restarting a replica loses no committed state.
- Verification:
  - Multi-process integration test sends concurrent messages and asserts a consistent transcript.
- Completion notes: Production API composition now requires `DATABASE_URL` and
  builds tenant-qualified async SQLAlchemy repositories over a bounded psycopg
  pool; process-memory stores are available only through explicit test
  injection. Conversation and message UUIDs are server-issued, the repository
  exposes append rather than transcript replacement, and each append locks the
  tenant/session row, inserts at its monotonic version, and advances that version
  in one transaction. Current lead and static-slot booking writes are durable
  with tenant-qualified reads and explicit transactions; real provider slot
  reservation and request idempotency remain `DATA-003`. Revision
  `0002_repositories` adds write-only client correlation, faithful service/slot
  labels, and current lead urgency values without treating correlation as
  visitor identity. The normal application role cannot update/delete messages
  or delete sessions. Changed: `services/api/src/tenantchat/api/{app,settings,
  dependencies,store,persistence/,routers/}`, migration revision and role grants,
  `services/api/tests/`, `tests/{migrations,repositories}/`, `Makefile`, CI,
  configuration/readme/runbook docs, ADR-0005, LikeC4 source/generated diagrams,
  dependency manifests, and `uv.lock`. Verified: `make check` (289 Python and
  six frontend tests; lock, lint, format, mypy strict, coverage, and deployment
  security clean), `make test-database` (six migration plus six repository tests
  on disposable PostgreSQL 16), repeated repository suite (six passed, including
  24 appends from four spawned processes), and LikeC4 format/validation. Follow-
  ups: `SEC-002` replaces write-only client correlation with authenticated
  visitor credentials; `DATA-003` owns booking reservation/idempotency; `FEAT-006`
  moves code-owned tenant configuration into Postgres.

### DATA-003 — Transactional, idempotent booking

- Status: `Done`
- Priority: `P0`
- Type: `Business action`
- Depends on: `DATA-002`
- Likely areas: booking domain service, booking tables, `/api/book`, tool executor
- Scope:
  - Introduce an availability-provider interface and a database-backed fake provider for the demo.
  - Store timezone-aware slots with stable provider IDs.
  - Reserve and confirm a slot in a transaction with a uniqueness constraint.
  - Require idempotency keys for direct API and model-triggered booking actions.
  - Make retries return the original result without producing another booking.
- Acceptance criteria:
  - Two concurrent attempts for one slot produce exactly one confirmed booking.
  - Repeating the same idempotency key returns the original confirmation.
  - Expired or already-reserved slots fail with a stable error and refreshed alternatives.
  - The model cannot book an unoffered, past, or wrong-tenant slot.
- Verification:
  - Concurrency tests cover duplicate submission, retry after timeout, and competing users.
- Completion notes: Added an `AvailabilityProvider` port and a database-backed fake
  provider in `services/api/persistence/availability.py` (with an in-process demo
  provider in `registry.py` for the hermetic suite), and timezone-aware
  `OfferedSlot` values with stable provider IDs (`packages/core/slots.py`).
  `BookingCommand` now carries `slot_id`/`slot_start`/`slot_end`, so "not in the
  past" and "offered in this tenant's calendar" are decided by the domain while
  the DB enforces the rest. Migration `0005_booking_reservation` adds
  `availability_slots` and a partial unique index for one confirmed booking per
  slot, plus a composite FK that makes it impossible to attach another tenant's
  slot. Reserve+confirm+idempotency claim now commit in one transaction in
  `PostgresBookingStore.confirm` / `InMemoryBookingStore.confirm`, so a retry
  has no in-flight window. `POST /api/book` and the graph `commit_booking` node
  require a key and check `find_replay` before re-validating, so a repeat of a
  committed key returns the original confirmation even though that slot no
  longer reads as offered. Changed files:
  `packages/core/{__init__,commands,ports,slots}.py`,
  `packages/orchestration/nodes.py`, `services/api/persistence/availability.py`,
  `services/api/{actions,agent,app,dependencies,registry,routers/[{bookings,tenants}],
  schemas,store}.py`, `services/api/persistence/repositories.py`,
  `services/api/migrations/versions/0005_booking_reservation.py`, and the
  `test_commands`/`test_bookings`/`test_actions`/`test_problem_details`/
  `test_migrations`/`test_postgres_repositories`/`test_postgres_durability`
  suites. Verification: `make check` green (`make setup`; hermetic gate includes
  mypy strict, ruff, coverage); real Postgres 16 suites green via
  `uv run --frozen pytest tests/migrations tests/repositories tests/agent_runtime`
  (71 tests), including new cases for two concurrent attempts on one slot,
  retry/replay after the slot is taken, and cross-tenant slot refusal. No new
  environment variables. Follow-ups: `FEAT-005` replaces the fake provider with
  a real calendar and owns pruning; `SEC-002` replaces the correlation-labeled
  booking session with a server-issued visitor credential; `OBS-004` pins slot
  reservation outcomes to the inference trace.

### SEC-001 — Admin authentication and tenant-scoped RBAC

- Status: `Done`
- Priority: `P0`
- Type: `Security`
- Depends on: `API-001`, `DATA-001`
- Likely areas: auth middleware, admin routes/UI, tenant memberships, audit events
- Scope:
  - Add standards-based JWT/OIDC validation with configurable issuer, audience, and JWKS.
  - Add tenant-scoped roles such as viewer, support agent, tenant admin, and platform admin.
  - Protect chat lists/details, leads, bookings, staff replies, ingestion, metrics, and configuration endpoints as appropriate.
  - Provide an explicit, non-production local development auth mode.
- Acceptance criteria:
  - Unauthenticated protected requests return `401`; unauthorized tenant access returns `403` without confirming resource existence.
  - Staff replies and administrative mutations record principal, tenant, timestamp, and request ID.
  - No production deployment can start with development auth enabled.
- Verification:
  - RBAC matrix tests cover every protected route and cross-tenant access.
- Completion notes: Implemented the single-origin nginx gateway with
  oauth2-proxy authentication (ADR-0007). The gateway auth-gates `/admin/` and
  `/api/admin/` routes via `auth_request`; browser pages redirect to OIDC
  login, API requests get 401 JSON. Python re-enforces authorization with four
  roles (viewer, support_agent, tenant_admin, platform_admin). Spoofable
  identity headers are stripped at both nginx and Python. CSRF double-submit
  tokens protect state-changing admin operations. Widget CORS uses an explicit
  origin allowlist (never wildcard, never admin routes). Admin frontend and
  API are same-origin (no admin CORS). Tests cover auth/CSRF/spoofing/roles.
  The remaining tenant-scoped RBAC (per-tenant role assignment, cross-tenant
  403 without confirming resource existence) depends on `API-001`/`DATA-001`.

  Per-tenant RBAC landed with migration `0005_tenant_memberships` (PK
  `(tenant_id, principal_subject)`, role CHECK limited to viewer/support_agent/
  tenant_admin; `platform_admin` is directory-only and spans tenants). The
  effective role for a tenant is the tighter of the directory role and the
  membership row, so an assignment can narrow access but never widen it.
  Membership is resolved before any tenant record is touched, so a refused
  operator gets a byte-identical `tenant_access_denied` document whether the
  tenant exists or not, and refusals write no audit rows. Protected surface:
  tenant-scoped reads (chats, chat detail, leads, bookings) require any tenant
  membership; staff replies require `support_agent` inside the tenant plus
  CSRF; membership assignment/revocation require `platform_admin` plus CSRF.
  Staff replies and membership mutations append to the append-only
  `audit_events` table (UPDATE/DELETE revoked from the app role) with actor
  type, principal subject, tenant, request ID, and a server-stamped timestamp.
  Development auth is an explicit opt-in: `CHAT_API_DEV_AUTH=true` skips the
  gateway token and mints its own CSRF secret, but `create_app` refuses to
  start against a non-loopback `DATABASE_URL`, and the deployment security
  gate rejects a manifest that enables it. RBAC matrix tests cover every
  protected route, cross-tenant and phantom-tenant refusal, privilege-ceiling
  enforcement, immediate grant/revoke effect, and audit content; the Postgres
  migration and repository suites pin the table, indexes, and runtime-role
  privileges. Full `make check` passes.

### SEC-002 — Secure visitor sessions and tenant binding

- Status: `Done`
- Priority: `P0`
- Type: `Security`
- Depends on: `API-001`, `DATA-002`
- Likely areas: chat/session routes, widget session storage, tenant configuration
- Scope:
  - Generate cryptographically random sessions server-side.
  - Issue an opaque or signed visitor credential bound to exactly one tenant and session.
  - Stop accepting tenant changes for existing sessions.
  - Permit visitors to read only the minimal messages needed for their own session.
  - Define safe session expiry and rotation behavior.
- Acceptance criteria:
  - Guessing or reusing another session ID does not reveal or modify data.
  - Changing `tenantId` cannot move a session or invoke another tenant's tools.
  - Session tokens are not exposed in logs or query strings.
  - Expired credentials fail predictably and preserve a safe recovery path.
- Verification:
  - Automated tests reproduce and then prevent session hijacking and tenant reassignment.
- Completion notes: `services/api` now mints a signed `tc.v1.*` visitor
  credential at `POST /api/chat/session` and authenticates every other visitor
  route (`GET /api/chat/session`, `POST /api/chat`, `POST /api/chat/
  confirmation`) with the `X-Visitor-Credential` header. The request models no
  longer accept `tenant_id` or `session_id` at all (`extra="forbid"`), so a body
  cannot move a conversation and the old URL-shaped surface is gone. The
  credential is HMAC-SHA256-signed (core `HmacVisitorCredentialSigner`, no
  runtime dependencies) and names exactly one tenant plus one server-issued
  session, carries its own `exp`, and is reissued on every credentialed response
  so an active conversation never expires. Missing/forged/expired credentials
  all return a stable `401`; forged and missing are the indistinguishable code
  `invalid_visitor_credential`, expiry is the recoverable
  `visitor_credential_expired`, and the widget clears the stored token and
  opens a fresh session. The signed token is the visitor identity that
  `SEC-003` (rate limiting) and `PRIV-001` (customer export/delete) build on
  via the public `VisitorCredentialSigner` port, while the DATA-002 write-only
  client correlation field is left untouched for the action records that still
  use it. Credentials travel only in a header (never a query string) and are
  redacted in `str`/`repr`, so they cannot reach a log line; the signing key is
  a required startup secret (`CHAT_API_VISITOR_CREDENTIAL_SIGNING_KEY`) that
  the production composition, k8s manifest, deploy script, and README document,
  and deployments/tests use at least a 32-byte value. Changed:
  `packages/core/src/tenantchat/core/{visitor_session,errors,__init__}.py` and
  `packages/core/tests/test_visitor_session.py`; `services/api/src/tenantchat/
  api/{visitor,app,settings,problems,schemas}.py` and `routers/chat.py` plus
  their tests; new `tests/security/test_visitor_session_hijacking.py`
  (`TestSessionHijacking`, `TestTenantReassignment`) and root `conftest.py`
  plugin wiring; the agent-runtime durability and repository integration tests;
  frontend chat API, types, `useConversation` (credential refresh + rejection
  recovery), `visitorData` (credential in session storage, never a URL), nginx
  gateway, widget tests, and `.env.example`/`k8s/` secrets/docs. Verified:
  `make check` (647 hermetic Python plus 91 frontend tests; ruff, mypy strict,
  format, coverage, deployment-security, image-contracts all clean) and
  `make test-database` (migrations + 27 repository + 6 agent-runtime tests on
  disposable PostgreSQL 16), including the cross-tenant booking-record suite
  and a credential minted by one API instance and honored by a restarted one.
  Follow-ups: `SEC-003` consumes the credential as its per-session rate-limit
  key; `PRIV-001` consumes it as the authenticated customer identity for
  export/delete; `DATA-003` owns booking reservation/idempotency.

### SEC-003 — API abuse protection, CORS, and response hardening

- Status: `Done`
- Priority: `P0`
- Type: `Security/reliability`
- Depends on: `API-001`, `SEC-002`
- Likely areas: API middleware, widget configuration, ingress configuration
- Scope:
  - Add configurable tenant/origin allowlists instead of wildcard CORS.
  - Add per-IP, per-session, and per-tenant rate and concurrency limits.
  - Enforce request, message, history, upload, and response-size limits.
  - Add security headers and safe caching policy.
  - Hide raw tool arguments/results and internal upstream errors from public clients unless an explicit development debug flag is enabled.
- Acceptance criteria:
  - Limits return stable `429` or `413` responses with bounded retry guidance.
  - A malicious origin cannot read protected responses.
  - Public responses contain no internal URLs, stack traces, PII-rich tool payloads, or model configuration.
- Verification:
  - Abuse tests cover bursts, slow requests, large bodies, long messages, and disallowed origins.
- Completion notes: Replaced wildcard CORS with an explicit widget origin
  allowlist configured via `WIDGET_ALLOWED_ORIGINS`. The nginx gateway emits
  `Vary: Origin`, handles OPTIONS preflight, and never allows admin routes
  through CORS. Widget requests are credential-free by design.
- Completion notes (remaining slice): Added pure-ASGI request guards in
  `services/api/src/tenantchat/api/guards.py`: body-size limit (declared and
  chunked), fixed-window rate limits plus per-process concurrency budgets per
  IP/tenant/session, and a buffered response-size cap. Budgets are counted in
  `rate_limit_counters` (migration `0005_api_abuse_protection`) with an atomic
  sweep-and-upsert shared across replicas, falling back to
  `InMemoryRateLimitStore` in development/tests. Refusals are RFC 9457 problem
  documents (`code`, `requestId`, `limitScope`, `maxBytes`, `retryAfterSeconds`)
  with a `Retry-After` bounded by the window, and identity keys never reach a
  body or a log line. `default_visitor_identity` keys the session budget on the
  opaque server-issued `session_id` as the `SEC-002` seam. Hardened responses:
  security headers on every response (nosniff, no framing, no referrer,
  no-store), transcript reads truncated to `max_history_messages` (default 100),
  unexpected errors publish exception text only under `CHAT_API_DEBUG`,
  validation failures never echo rejected input, and raw tool args/results
  remain in the checkpoint/inference plane only (never the store or responses).
  New settings: `CHAT_API_MAX_RESPONSE_BYTES`, `CHAT_API_MAX_HISTORY_MESSAGES`,
  `CHAT_API_DEBUG`, `CHAT_API_{IP,TENANT,SESSION}_{RATE_LIMIT,CONCURRENCY}`,
  `CHAT_API_RATE_WINDOW_SECONDS`; all documented in `.env.example`.
  Verification: `services/api/tests/test_abuse.py` (19 tests: bursts,
  concurrency, chunked bodies, response caps, security headers, history cap,
  schema bounds, CORS surface, no key/log leakage) plus
  `tests/repositories/test_rate_limit_store.py` (per-key counting, sweep,
  cross-process no-lost-updates). `make check` passes. Follow-ups: `OBS-002`
  should export rate-refusal counters; `SEC-002` replaces the session-keyed
  extractor with signed visitor credentials.

### SEC-004 — Service authentication and Kubernetes network boundaries

- Status: `Done`
- Priority: `P0`
- Type: `Infrastructure security`
- Depends on: None
- Likely areas: `k8s/`, internal service middleware/configuration
- Scope:
  - Add default-deny NetworkPolicies and explicit allowed service flows.
  - Restrict ingestion, embedding, financing, databases, metrics, and observability ingestion to required callers.
  - Add workload identities or rotated service credentials for internal APIs.
  - Ensure only intended public routes reach the internet-facing ingress.
- Acceptance criteria:
  - A random namespace pod cannot query Elasticsearch, Postgres, ingestion, financing, or embedding APIs.
  - The chat service can reach only documented dependencies.
  - Metrics remain scrapeable by Prometheus without public exposure.
- Verification:
  - Network-policy smoke tests prove both allowed and denied paths.
- Completion notes: Added namespace-wide default-deny and explicit least-privilege
  NetworkPolicies for ingress, metrics, telemetry, DNS, service APIs, data stores,
  and model-provider egress. Split public visitor traffic from internal chat admin
  and metrics routes; removed non-chat LoadBalancer exposure. Added distinct,
  fail-closed bearer credentials for each internal caller channel, including the
  seed job, with production startup validation and external-key reuse rejection.
  Final integrated `make check` passes (314 Python and 6 frontend tests),
  deployment manifests pass server-side dry-run, and a disposable four-namespace
  MicroK8s smoke run proved 15 documented allows and 21 denials—including exact
  Prometheus port boundaries—before cleaning every created namespace.
  Standard NetworkPolicy cannot bind provider egress to a configured FQDN, so a
  production egress proxy or FQDN-aware policy remains recommended.

### SEC-005 — Secret management and credential removal

- Status: `Done`
- Priority: `P0`
- Type: `Infrastructure security`
- Depends on: None
- Likely areas: `k8s/app.yaml`, `k8s/kibana-setup-job.yaml`, deployment scripts, `.env.example`
- Scope:
  - Remove literal passwords, database URLs, API keys, and private model endpoints from tracked manifests.
  - Integrate a documented secret source appropriate for the target environment.
  - Add separate example configuration containing placeholders only.
  - Support authenticated LLM requests from both the main and financing services.
  - Rotate all credentials previously committed to version control before any public deployment.
- Acceptance criteria:
  - Repository secret scanning reports no live or default credentials.
  - Pods consume secrets without printing them or embedding them into ConfigMaps.
  - Missing required production secrets cause an immediate, clear startup failure.
- Verification:
  - Render deployment manifests and scan the rendered non-Secret output for sensitive values.
- Completion notes: Removed the tracked private LLM endpoint/model and replaced all
  sensitive runtime values with required Secret/ConfigMap refs in `k8s/app.yaml`;
  added shared fail-closed production configuration and bearer authentication for
  chat-backend and financing-agent; hardened the Kibana setup job against command-
  line credential exposure; added placeholder-only examples and the documented
  local MicroK8s/out-of-band production secret-source contract in `k8s/README.md`;
  and integrated `scripts/verify_deployment_security.py` into `make check` and CI.
  The gate has negative regression tests for literal credentials, private endpoints,
  camelCase secret keys, and literal sensitive environment values. Verification:
  `make check` (283 tests plus lock, lint, format, mypy, and deployment security
  scan) passed. The committed `REPLACE_WITH_*` and `.invalid` values are
  examples, not live credentials/endpoints. No live Secret was read or rotated;
  any external credential matching an earlier repository placeholder must still be
  rotated before public deployment. Production secret-controller automation remains
  an environment-specific deployment choice, while the workload reference contract
  remains fail-closed.

### PRIV-001 — PII classification, consent, retention, export, and deletion

- Status: `Done`
- Priority: `P0`
- Type: `Privacy/data governance`
- Depends on: `DATA-001`, `SEC-001`, `SEC-002`
- Likely areas: schema, privacy service/routes, widget notice, admin UI, telemetry filters
- Scope:
  - Document data classes and permitted uses for transcripts, contact details, addresses, bookings, and leads.
  - Record required consent for contact and follow-up actions.
  - Add configurable retention by record type and a deletion/anonymization worker.
  - Add authenticated customer export and deletion workflows.
  - Redact or tokenize PII before logging, tracing, analytics, and nonessential tool-event storage.
- Acceptance criteria:
  - An authorized export includes all records for one subject and no other subject.
  - A deletion request removes or irreversibly anonymizes data across primary tables and search indexes according to policy.
  - Expired records are purged automatically and auditable counts are emitted.
  - Contact actions cannot proceed without the configured consent state.
- Verification:
  - Privacy lifecycle integration tests cover consent, export, expiration, deletion, and audit records.
- Completion notes: PII classification, the consent gate, configurable
  retention, authenticated export/erasure, and operational-plane redaction are
  implemented and verified. The classification and permitted uses live in
  `tenantchat.core.privacy` (data classes, purposes, statement, retention rules,
  anonymization sentinels), mirrored for operators in `docs/privacy.md`.
  Consent is a server-owned grant (`POST /api/chat/consent`) keyed per session
  and purpose; the idempotent booking and lead services enforce it through the
  `ConsentSource` port, so a replayed node re-checks instead of re-recording.
  `0005_privacy.py` adds `consent_records` and the `privacy_requests` queue and
  widens `audit_events.resource_id` so privacy events can name a class or kind.
  Export assembles everything the platform holds about one contact (sessions,
  messages, leads, bookings, handoffs, consent) for one tenant only; the
  erasure worker (`privacy_worker.py`) fulfills queue requests and purges
  expired transcripts, emitting per-request and per-tenant audited counts.
  Erasure runs only under the `PRIVACY_DATABASE_URL` role, which is the sole
  role granted `DELETE` on sessions/transcripts/consent and is revoked from the
  app role (`provision_app_role.sql` / `provision_privacy_role.sql`); the app
  role cannot delete privacy records, and the audit table is append-only for
  every role. `redaction.py` scrubs free text and tool-event JSON trees, and a
  root-logger filter installed at composition root redacts an accidental
  f-string as a second line of defence against the ADR-0010 invariant. The
  widget grants `follow_up` at open and `booking` only at the gated
  confirmation, showing the statement the server records. Verified: full
  `make check` (quality gate) plus real-Postgres 16 suites — `test-privacy`
  (9 lifecycle tests: consent refusal and grant, one-subject export with
  same- and cross-tenant negatives, deletion fulfillment with row-level and
  audit assertions, expired-transcript purge with audited counts, and
  unauthorized-principal error contracts), `test-migrations` (11), 
  `test-repositories` (27), `test-agent-runtime` (6). Follow-ups: tenant-scoped
  RBAC for privacy surfaces and the erasure queue page live with `SEC-001`;
  inference-trace content export/erasure and retention are `PRIV-002`, which
  extends this task's export/erasure to turn records.

### DEP-001 — Immutable, reproducible application images

- Status: `Done`
- Priority: `P0`
- Type: `Delivery`
- Depends on: None
- Likely areas: all `Dockerfile` files, requirements/lock files, `k8s/app.yaml`, build scripts
- Scope:
  - Build each application as an immutable image; stop mounting application code from ConfigMaps and installing packages at startup.
  - Pin base images by digest and lock transitive dependencies with hashes.
  - Pin the embedding model revision and remove unrestricted remote-code execution where possible.
  - Run as a non-root user with a minimal runtime image.
- Acceptance criteria:
  - Images build without accessing mutable unpinned inputs after dependencies are locked.
  - Pods start without `pip install` or source-code ConfigMap mounts.
  - The main image includes its Postgres runtime dependency.
  - Image and dependency scans are integrated into verification.
- Verification:
  - Build all images, run smoke tests from the images, and record image digests.
- Completion notes: Five multi-stage images now cover the production API and
  migration runtime, embedding, ingestion, financing, and web gateway services.
  Every
  Dockerfile frontend, uv builder, Python base, and declared external Kubernetes
  image is digest-pinned; all Python graphs come from the hashed workspace
  `uv.lock` (including CPU-only PyTorch), and final images run as numeric
  `10001:10001`. The Qwen model uses reviewed commit
  `97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3` with
  `trust_remote_code=False`. Kubernetes release templates accept application
  digest substitution only, and the deploy gate requires all expected
  workload images to have 64-hex registry digests; runtime pip commands and
  executable ConfigMap mounts were removed. Final integrated `make check` passed
  with 314 Python and six frontend tests; all Kubernetes inputs passed server
  dry-run. All five arm64 images built from commit `451f403`, ran non-root
  import/writability/migration checks and live health smokes, and recorded local
  digests under `artifacts/images/`. The API smoke migrated a fresh pinned
  Postgres database through both revisions, then started the real Postgres
  repository composition and passed `/healthz`. Runtime-source contract tests
  follow local import closure so an image cannot omit `internal_auth.py` or
  `runtime_security.py`. A
  checksum-verified Trivy 0.69.3 scan found zero fixed HIGH/CRITICAL findings in
  the final lockfile and five images after upgrading the affected framework and
  embedding dependencies. CI now repeats build, smoke, metadata upload, and
  fixed HIGH/CRITICAL scan gates in a five-image matrix. The `API-001` slice-3
  cutover is part of this task's completion: the prototype image and the root
  `Dockerfile` were deleted, `k8s/app.yaml` runs the `api` image, and the smoke
  scripts cover the five remaining images. Publishing, SBOM,
  signing, and provenance remain `DEP-006`; network policy and service auth
  remain `SEC-004`.

## Prioritized platform task details

### REL-001 — Resilient dependency clients

- Status: `Todo`
- Priority: `P1`
- Type: `Reliability`
- Depends on: `API-001`, `AI-001`
- Likely areas: LLM, embedding, Elasticsearch, CRM, and calendar client modules
- Scope:
  - Centralize connect/read/total timeouts, bounded retries with jitter, connection pooling, and circuit breakers.
  - Retry only idempotent operations or operations protected by idempotency keys.
  - Propagate deadlines and cancellation through the request path.
  - Define safe degraded behavior for each dependency.
- Acceptance criteria:
  - Dependency outages fail within documented budgets and do not exhaust workers.
  - Circuit state and retry counts are observable.
  - A timeout after a committed action cannot create a duplicate on retry.
- Verification:
  - Failure-injection tests cover timeout, reset, `429`, `5xx`, malformed response, and recovery.
- Completion notes: _Pending._

### REL-002 — Dependency-aware health, graceful startup, and shutdown

- Status: `Cancelled`
- Priority: —
- Type: `Reliability`
- Cancelled on: `2026-08-04`
- Reason: Every service that gains a dependency must already express it
  correctly in its probes; that is a property of adding the dependency, not a
  project to run afterwards. Deferring it produces exactly the failure it aims
  to prevent — a readiness check written months after the client it guards, by
  someone who no longer remembers which failures are transient.
- Where the scope went:
  - Liveness/readiness/startup separation, and the rule that a new dependency
    enters readiness rather than liveness and degrades instead of restarting,
    are now global definition-of-done items.
  - Warming the embedding model before readiness succeeds belongs to the
    embedding service's own readiness contract under that same rule.
  - Draining traffic and finishing or safely cancelling in-flight work moved to
    `FEAT-010`, which owns message-delivery correctness and is the only place
    the guarantee is testable end to end.
  - `DEP-002` and `DEP-004` retain the Kubernetes rolling-restart verification
    at Gate C, where a live cluster exists to run it against.

### REL-003 — Durable background jobs and retry handling

- Status: `Done`
- Priority: `P1`
- Type: `Reliability/workflow`
- Depends on: `DATA-001`, `DATA-002`
- Likely areas: worker service, job/outbox tables, deployment manifests
- Scope:
  - Introduce a durable job/outbox mechanism for ingestion, CRM delivery, notifications, deletion, and webhook work.
  - Add leases, retry policy, exponential backoff, dead-letter state, and replay controls.
  - Make job handlers idempotent and tenant-scoped.
- Acceptance criteria:
  - Jobs survive process and cluster restarts.
  - Duplicate delivery does not duplicate external actions.
  - Operators can inspect, retry, or cancel failed jobs with an audit trail.
- Verification:
  - Restart workers mid-job and verify eventual exactly-once business effect.
- Completion notes: Added ADR 0011; migration `0009_durable_jobs`; the typed job
  state machine and PostgreSQL repository; `SKIP LOCKED` leases and heartbeats;
  capped exponential retry, dead-letter, replay, and cancellation; immutable
  job events; tenant-admin inspection/control APIs that never expose payloads;
  transactional privacy-deletion enqueueing and its idempotent handler; a
  graceful worker/readiness contract; Kubernetes deployment, credentials, and
  network policy; the background-job runbook; and unit, security, migration,
  repository, privacy-lifecycle, concurrent-lease, real-route, and restart-after-
  effect tests. `make check` passes (729 Python tests, 93 frontend tests, strict
  lint/format/type checks, coverage, deployment-security, and image contracts).
  After restoring the local container runtime, `make test-database` also passes:
  11 migration, 38 repository, 6 agent-runtime, and 9 privacy-lifecycle tests,
  including enqueue/dedupe, retry/dead-letter/operator-audit, concurrent leasing,
  and restart/exactly-once-effect coverage. The local MicroK8s release is live
  from immutable API/worker digest `sha256:35b1f954a55015b11edf27c0eb8b61b9c05cfed1145edfdb5f5a2793e51f1aff`:
  the combined Alembic/checkpoint migration Job completed, API and worker are
  `1/1` ready, in-cluster health and worker checks pass, and the public gateway
  returns HTTP 200. The 20 pre-Alembic prototype snapshots were backed up and
  quarantined rather than imported or deleted. Follow-ups: `RAG-002`,
  `FEAT-003`, and `FEAT-005` add ingestion, CRM, notification, and webhook
  handlers behind this contract.

### OBS-001 — Structured logging and request correlation

- Status: `Done`
- Priority: `P1`
- Type: `Observability`
- Depends on: `API-001`, `PRIV-001`
- Likely areas: logging configuration, API middleware, all service clients
- Scope:
  - Emit structured JSON logs with timestamp, level, service, environment, request ID, trace ID, tenant pseudonym, event, and safe error code.
  - Propagate correlation headers across internal services and background jobs.
  - Apply centralized PII/secret redaction.
- Acceptance criteria:
  - One chat turn can be traced across chat, financing, embedding, retrieval, and tool execution.
  - Logs contain no message content, contact details, credentials, or full document chunks by default.
  - Log volume and retention are configurable.
- Verification:
  - Automated redaction tests and a documented trace walkthrough pass.
- Completion notes: Structured JSON logging with server-minted request and
  trace IDs, tenant pseudonyms, and centralized redaction are implemented and
  verified. `tenantchat.api.correlation` is the correlation context: a
  pure-ASGI `CorrelationMiddleware` mints the request ID and trace ID per
  request (client-supplied IDs are never trusted), binds them in a
  contextvar so every in-request log line — router, agent runtime, tool —
  inherits them, echoes them on every response, and emits an opt-in access
  line; `bind_tenant` attaches the keyed HMAC tenant pseudonym once a
  verified visitor credential names the tenant (the claims dependency was
  made a coroutine so the binding lands in the request's task, not a
  threadpool copy). `tenantchat.api.logging_setup` is the structured plane:
  a JSON formatter with the fixed contract fields (timestamp, level, service,
  environment, logger, event, correlation, safe error codes), a strict extra
  allowlist so unknown `extra` keys cannot become log content, and
  `configure_logging` at both composition roots (API and job worker) that
  absorbs uvicorn's own loggers into one stream. `PiiLogFilter` now also
  scrubs formatted tracebacks, not just messages and args. Background jobs
  continue the enqueuing request's trace: enqueuers store `trace_id` in the
  job payload, `payload_fingerprint` treats it as attribution rather than
  work so retried enqueues still deduplicate, and the worker binds the
  payload's request ID, trace, and tenant pseudonym per job execution.
  Internal-service propagation is the `correlation_headers()` contract;
  financing-agent and ingestion forward `X-Request-Id`/`X-Trace-Id` to the
  embedding hop. Volume is configurable via `CHAT_API_LOG_LEVEL`,
  `CHAT_API_LOG_JSON`, and `CHAT_API_LOG_ACCESS`; retention is documented as
  the Loki knobs in `k8s/observability-drilldown-fixes.yaml`. The walkthrough
  is `docs/runbooks/trace-walkthrough.md`. `make check` passes (756 Python
  tests, 93 frontend tests, strict lint/format/type checks, coverage,
  deployment security, image contracts), and `make test-database` passes:
  11 migration, 38 repository, 6 agent-runtime, and 9 privacy-lifecycle
  tests. Follow-ups: set `APP_ENV` for the API and worker containers in
  `k8s/app.yaml` (the side services already set it), and route future
  RAG/financing calls through `correlation_headers()`.

### OBS-002 — LLM, RAG, tool, and business metrics

- Status: `Todo`
- Priority: `P1`
- Type: `Observability`
- Depends on: `OBS-001`, `AI-001`, `RAG-005`
- Likely areas: metrics modules and service instrumentation
- Scope:
  - Measure request/error/latency by operation using bounded-cardinality labels.
  - Measure model tokens, estimated cost, time to first token, provider errors, fallbacks, and cancellations.
  - Measure retrieval latency, candidate count, score distribution, reranking, abstention, and citation count.
  - Measure answer quality by class, not only by volume: the `OBS-004` diagnosis-cause distribution, router confidence and clarification rate, context-truncation rate, and citation-validation failure rate.
  - Correlate bounded quality and failure metrics with the component-manifest hash from `OBS-004` through trace exemplars and report queries, so a regression can be associated with the exact build and AI configuration without using the hash or individual versions as Prometheus labels.
  - Attach trace exemplars to quality and latency metrics so a Grafana spike links to one turn in `FEAT-015`. The exemplar carries a trace ID only; the content stays in the inference plane per `ADR-0010`.
  - Measure booking, lead, handoff, CRM-delivery, and conversion outcomes.
- Acceptance criteria:
  - Metrics remain correct across replicas and restarts.
  - No session IDs, user IDs, free text, or PII are metric labels.
  - Every label is a bounded enum or a tenant pseudonym, and a test asserts the per-metric cardinality ceiling so a new label value cannot quietly multiply series.
  - Each critical action has success, failure, and latency metrics.
- Verification:
  - Metric contract tests, a label-cardinality test, and an exemplar drill-through walkthrough are documented alongside sample Prometheus queries.
- Completion notes: _Pending._

### OBS-003 — Dashboards, SLOs, and alerts as code

- Status: `Todo`
- Priority: `P3`
- Type: `Operations`
- Gate: `C` — an SLO is a promise to someone who is paged, and this system has
  no on-call. The instrumentation the dashboards would read is not deferred
  with them: `OBS-002` owns the metrics and their cardinality ceiling, and
  `FEAT-015` is the surface the demo actually reads answers from.
- Depends on: `OBS-002`, `DEP-002`
- Likely areas: `k8s/observability/`, Grafana dashboards, Prometheus rules, runbooks
- Scope:
  - Define availability, latency, action-success, and RAG-quality SLOs.
  - Add dashboards for service health, model usage, RAG, business funnels, queues, and dependencies.
  - Add actionable burn-rate and failure alerts linked to runbooks.
- Acceptance criteria:
  - Dashboards and alert rules are version-controlled and provisioned automatically.
  - Every page-level alert has an owner, severity, threshold rationale, and runbook.
  - Synthetic traffic can intentionally trigger and resolve a test alert.
- Verification:
  - Validate rules and import dashboards in a clean observability environment.
- Completion notes: _Pending._

### PRIV-002 — Inference trace data plane, retention, and access control

- Status: `Done`
- Priority: `P1`
- Type: `Privacy/data governance`
- Depends on: `PRIV-001`, `SEC-001`
- Likely areas: turn-record schema, retention worker, admin RBAC and audit, `k8s/otel-collector.yaml`, privacy documentation
- Scope:
  - Implement the two-plane split from `ADR-0010`: enforce that message content, contact details, and document text reach the turn record only, never logs, metrics, or exported spans.
  - Add a collector redaction processor that runs ahead of every exporter, so adding a backend cannot widen what leaves the cluster.
  - Gate the `TRACE_CONTENT_EXPORT` setting: disabled by default, permitted only for a viewer inside the cluster trust boundary behind admin authentication, and refused at startup in production for any external backend.
  - Set turn-record retention independently of and shorter than transcript retention, with an automatic purge that emits auditable counts.
  - Add a distinct role for turn-record access, separate from ordinary transcript viewing, with every read audited to an actor, turn, and reason.
  - Extend `PRIV-001` export and erasure to cover turn records and any projection derived from them, including evaluation datasets promoted under `FEAT-008`.
  - Document the classification, lawful basis, retention period, and access rules for prompts, retrieved evidence, and model outputs.
- Acceptance criteria:
  - An erasure request removes or irreversibly anonymizes the subject's turn records and derived projections within the documented window, and the removal is verifiable.
  - No content field appears in any log line, metric label, or exported span under either `TRACE_CONTENT_EXPORT` setting.
  - Production startup fails when content export is enabled for a backend outside the trust boundary.
  - Turn-record reads by an actor without the dedicated role are refused and audited; permitted reads are audited too.
  - Expired turn records are purged automatically and the purge is observable without exposing what was purged.
- Verification:
  - Run privacy lifecycle tests for turn-record export, expiry, and erasure; a redaction test asserting the operational plane is content-free; a startup-refusal test for misconfigured export; and an RBAC/audit test for trace access.
- Completion notes: The `ADR-0010` inference plane's governance surface is
  implemented: retention, the dedicated read role, read and refusal audit,
  export/erasure coverage, collector redaction, and content-export gating.
  `0010_trace_privacy.py` adds the privacy-owned envelope — `turn_records`
  (opaque `content` jsonb `OBS-004` will populate, `recorded_at` as the
  retention timestamp, `trace_id` for plane correlation) — plus
  `turn_record_projections` (derived datasets such as `FEAT-008` evaluation
  rows, cascading off their turn record so one erasure statement removes every
  projection) and `trace_access_grants` (the dedicated `trace_viewer` role,
  tenant-scoped and orthogonal to transcript memberships). The new
  `DataClass.INFERENCE_TRACE` gets its own 30-day rule in
  `tenantchat.core.privacy`, independent of and shorter than the 90-day
  transcript rule, and `TurnRecordReadReason` closes the set of reasons a read
  may be audited with. `PostgresPrivacyStore` now matches subject discovery
  against turn-record content, exports the records and their projections,
  erases them (counted in `ErasureReport`), and purges them on their own
  schedule (counted in `PurgeReport`); the privacy worker's audit rows carry
  the new counts, so a purge is observable without exposing what was purged.
  The read surface is `GET /api/admin/traces/{turn_id}` behind
  `require_trace_read` (platform-admin or a `trace_access_grants` row only;
  refused reads are audited too) with a mandatory reason, and the grant/revoke
  routes are platform-admin mutations like membership assignment. `k8s/otel-collector.yaml`
  gains a `redaction` processor with an explicit operational allowlist ahead of
  every exporter in every pipeline; `TRACE_CONTENT_EXPORT` defaults off, and
  `create_app` refuses to start with it enabled for any endpoint outside the
  trust boundary (loopback or `*.svc.cluster.local`), with
  `verify_deployment_security.py` refusing a tracked manifest that enables it.
  Classification, lawful basis, retention, and access rules are documented in
  `docs/privacy.md` and the `inference-trace-plane` runbook. Verified: full
  `make check` (quality gate) plus `make test-database` — `test-migrations`
  (11, including app-role refusal on turn tables), `test-repositories` (45,
  including 7 new turn-record/grant-store specs), `test-agent-runtime` (6),
  `test-privacy` (13, including turn-record export with projections,
  turn-content subject discovery, erasure with cascade verification, and
  independent trace purge with transcript survival), plus hermetic trace-plane
  tests (startup refusal for external backends, collector redaction structure,
  RBAC/audit and refusal-audit contracts) and deployment-gate tests.
  Follow-ups: `OBS-004` populates the envelope (`TurnRecordStore.record`),
  builds the query/diagnosis/replay APIs on this governance, and selects the
  viewer whose collector pipeline the operator path documents.

### OBS-004 — Inference trace, answer provenance, and failure attribution

- Status: `Todo`
- Priority: `P1`
- Type: `AI observability`
- Depends on: `OBS-001`, `AI-001`, `AI-003`, `RAG-005`, `PRIV-002`
- Likely areas: turn-record schema and repository, orchestration instrumentation, replay service, trace query API, OpenTelemetry GenAI spans, `k8s/otel-collector.yaml`
- Scope:
  - Persist one append-only, tenant-qualified turn record per conversation turn, per `ADR-0010`, covering the router decision and the candidate intents it rejected, the standalone retrieval query and the history used to build it, every retrieval candidate with its lexical, vector, fused, and rerank scores, the evidence dropped by the context budget, the assembled prompt reference and content hash, model parameters and usage, raw output, parsed claims with citation IDs, validator verdicts, each tool call with its idempotency key and committed record ID, and the turn outcome.
  - Record the actual executed graph, not a reconstruction inferred from the final state: node and edge IDs, start and end time, status, attempt, retry or fallback decision, safe input/output references, rejected branch and reason, implementation/configuration version, safe exception code, tool-policy verdict, and committed domain-record reference.
  - Store one canonical component manifest and content-free hash for every turn: application build revision; graph version; prompt template and resolved-binding versions; router policy; parser and chunker versions; retriever configuration; immutable index generation or snapshot ID; embedding model; reranker; provider adapter; model ID and parameters including temperature and seed where supported; tool schema/contract versions; tenant-policy version; relevant feature flags; and the chunk and document versions of cited sources. Never include secrets in the manifest.
  - Represent attribution as zero or more diagnosis records rather than one flat label. Each record carries a bounded cause code, stage, `primary` or `contributing` role, `detected`, `suspected`, `confirmed`, or `inconclusive` status, low/medium/high confidence, evidence references, detector version, and any later reviewer decision. Use the Gate B cause set `stale_source`, `ingestion_or_index_error`, `routing_error`, `query_rewrite_error`, `filter_exclusion`, `retrieval_miss`, `retrieval_rank`, `context_truncation`, `prompt_regression`, `model_behavior`, `grounding_or_citation_error`, `tool_error`, `application_error`, and `provider_failure`; document finer subcodes as Gate C rather than building detectors for them now.
  - Record per-stage status on every turn, but create diagnosis records only for anomalous, automatically detected, or reviewed turns. Automatically detect every Gate B cause decidable from the record alone; ambiguous prompt/model causes remain `suspected` or `inconclusive` until controlled comparison or review adds evidence.
  - Emit OpenTelemetry spans following the GenAI semantic conventions for the operational plane, keeping content out of it per `ADR-0010`.
  - Expose tenant-scoped, RBAC-gated query, reconstruction, diagnosis, and replay APIs for `FEAT-015`; keep the first-party turn record, not any telemetry backend, authoritative.
  - Support safe replay from a turn ID: retrieval against a retained immutable index generation and against the current index; corrected-query and reviewer-labelled gold-evidence substitution; the model call against stored context; bounded repeated trials; and a whole turn under one changed component manifest with claim, citation, and output diffs.
  - Replace every effectful port with a recording fake or isolated sandbox during replay. A replay may write its experiment result but must never mutate production conversations, bookings, leads, handoffs, integrations, or other domain state.
  - Distinguish exact historical reconstruction from replay: reconstruction returns the stored prompt, evidence, output, and verdicts exactly; model replay is explicitly stochastic; retrieval replay is called reproducible only while the exact implementation, configuration, and immutable index generation remain available.
  - Finalize the durable turn record before marking an assistant answer delivered. A trace-finalization failure returns a safe retryable error, and retrying cannot redeliver an answer or repeat a committed business action.
- Acceptance criteria:
  - Any turn can be reconstructed from its ID alone: exact prompt, exact evidence, model parameters, and validator verdicts.
  - The turn record describes the actual executed nodes, attempts, branches, fallbacks, and failures, including partially completed turns.
  - Replaying retrieval for a Gate B fixture against its retained immutable index generation reproduces the recorded candidate set; when a historical generation is unavailable, the API refuses to claim reproducibility and still returns the exact stored historical result for inspection.
  - Seeded fixtures for an incomplete index, fabricated citation ID, evidence chunk dropped by the context budget, and chunk excluded by a tenant or version filter are detected without human input.
  - Diagnosis cause, role, status, and confidence are bounded enums safe as metric dimensions, and cause distribution is queryable by tenant, time, and component-manifest hash.
  - A replay test that traverses every effectful graph path produces zero production domain mutations.
  - Bounded repeated trials and gold-evidence substitution can add evidence to a diagnosis without overwriting the original record or representing a stochastic result as proof.
  - No assistant answer reaches `delivered` without a durable finalized turn record; injected trace-write failure is safe and retrying produces neither a duplicate answer nor a duplicate business action.
  - Turn-record reads are RBAC-gated and audited, and deleting every telemetry backend loses no turn record.
  - The operational plane contains no message content, contact details, or document text.
- Verification:
  - Run provenance reconstruction, executed-graph fidelity, immutable-index replay, stochastic replay labeling, gold-evidence substitution, zero-domain-write replay, trace-finalization failure, diagnosis fixture, RBAC/audit, and operational-plane redaction tests. `FEAT-015` owns the browser walkthrough.
- Completion notes: _Pending._

### ARCH-001 — LangGraph agent runtime over a framework-free domain

- Status: `Done`
- Priority: `P1`
- Type: `Architecture/agent platform`
- Governed by: [`ADR-0001`](docs/adr/0001-agent-runtime.md). The superseded inline `ADR-001` above described a two-adapter `AgentRuntime` protocol; that design is rejected and must not be built.
- Depends on: `QA-001`, `API-001` slice 1, `DATA-002`
- Likely areas: backend orchestration package, checkpoint adapter, composition root, `tests/test_architecture_invariants.py`, `architecture/likec4/`
- Scope:
  - Adopt LangGraph v1 with the Postgres checkpointer as the only agent runtime. Do not introduce an abstraction layer over agent frameworks, a second adapter, or a runtime-selection setting.
  - Replace the prototype's in-process tool dispatch (now removed with `server.py`) with calls to idempotent domain services.
  - Enforce the `ADR-0001` layer policy by dependency direction: `packages/core`, application-service public contracts, API schemas, and business repository adapters stay framework-free; graph orchestration, the checkpoint adapter, and the composition root import LangGraph freely.
  - Extend the architecture invariants scan from `packages/core` to `services/api` public contracts as those layers land, keeping orchestration and checkpoint adapters explicitly out of scope for the scan.
  - Record the graph version alongside the other component versions in the `OBS-004` turn record, so a behavior change is attributable to a graph revision.
  - Document why `langchain-classic` is excluded, when LangChain v1 `create_agent` is worth using inside a node, and what would have to change for the OpenAI Agents SDK alternative in `ADR-0001` to be reconsidered.
  - Keep the LikeC4 architecture model synchronized with the implementation boundary.
- Acceptance criteria:
  - A persisted workflow pauses at an interrupt, survives process restart, resumes, and completes without repeating a committed domain action.
  - Deleting every checkpoint record loses no conversation, booking, lead, or handoff, and the system remains able to start new conversations.
  - Graph nodes invoke side effects only through idempotent domain services with explicit idempotency keys, and a forced replay of a node commits nothing twice.
  - No `packages/core` module, application-service public contract, API schema, or business repository adapter imports LangChain or LangGraph.
  - A single-turn question that reaches no interrupt and commits no domain action stays within a documented checkpoint-write and latency budget, measured and recorded rather than assumed.
- Verification:
  - Run the architecture invariants test, a restart/resume integration test, an idempotent replay test, a checkpoint-deletion recovery test, the single-turn overhead benchmark, and `npm --prefix architecture/likec4 run validate`.
- Completion notes:
  - LangGraph v1.2.10 with the Postgres checkpointer is the only runtime. No
    abstraction over agent frameworks was built, and no runtime-selection
    setting exists. `packages/orchestration` holds the graph, its state, the
    nodes, and the checkpoint adapter.
  - The dispatcher graph is six nodes — model, tools, confirm booking, commit
    booking, escalate, finalize — versioned as `dispatch@1`, with the system
    prompt versioned separately as `dispatch-system@1`. `DispatchRuntime`
    returns both on every turn, so `OBS-004` can pin an answer to them without
    reaching into the graph.
  - Every effect crosses a `tenantchat.core.ports` Protocol taking an
    `IdempotencyKey`. Keys are derived from checkpointed values — tenant,
    session, tool, turn, and the provider's call ID — and hashed, so a replayed
    node derives the key it derived the first time and no key carries a
    customer's words into a log. `tenantchat.api.actions` implements the
    services over the existing `idempotency_keys` table with claim-then-complete
    semantics.
  - The booking confirmation is a real `interrupt`. A paused conversation is
    resumed by a second process with its own pools in
    `tests/agent_runtime/test_postgres_durability.py`, and truncating every
    checkpoint table there leaves the booking, and the ability to start new
    conversations, intact.
  - The architecture invariants scan now covers `services/api` as well as
    `packages/core`, banning LangChain, LangGraph, `tenantchat.orchestration`,
    and provider SDKs everywhere except `agent.py` and `app.py`. The exemption
    list is itself tested, so an entry for a file that no longer composes the
    runtime fails the build.
  - Measured, not assumed: a single-turn question that calls no tools costs 7
    checkpoint writes and about 1.2 ms of in-process overhead. Budgets of 8
    writes and a 25 ms median are enforced in
    `tests/agent_runtime/test_runtime_overhead.py`, which also records both
    figures into the JUnit XML.
  - `ADR-0001` gained the framework-surface section this task called for —
    why `langchain-classic` is excluded, when LangChain v1 `create_agent` earns
    a place inside a node, and what would have to change for the OpenAI Agents
    SDK to be reconsidered — plus the measured cost and the checkpoint-schema
    runbook.
  - Migration `0004_agent_runtime` adds `handoffs.summary`, nullable and
    non-blank, so the escalation path's context reaches the staff queue rather
    than only the transcript.
  - `make migrate-checkpoints` creates LangGraph's own tables under
    `DATABASE_MIGRATION_URL`. The library owns that schema, and the application
    role holds no `CREATE` on `public`.
  - **Not included, deliberately.** This task composed the runtime and stopped
    there. `API-001` slice 2 serves it over HTTP, and `AI-001` supplies the
    `ChatModel` adapter — this task added the port and a scripted double, not a
    provider client, so a deployment answers no turn until that lands.
    `AI-003` replaces `prompts.py` with the template
    registry, `RAG-006` replaces the fixed transcript window, and `DATA-003`
    collapses the idempotency claim and the booking write into one transaction,
    which removes the in-flight window where a crashed attempt makes a retry
    wait for expiry rather than proceeding.

### AI-001 — Provider and model abstraction

- Status: `Done`
- Priority: `P1`
- Type: `AI platform`
- Depends on: `API-001`, `ARCH-001`
- Likely areas: model provider interfaces, tenant configuration, financing agent
- Scope:
  - Define provider-neutral chat, tool-call, embedding, streaming, usage, and error contracts.
  - Support configurable OpenAI-compatible providers first and extension points for other providers.
  - Store model/provider selection by environment and approved tenant policy, never arbitrary visitor input.
  - Normalize tool calls and usage accounting.
- Acceptance criteria:
  - Unit tests run the same agent contract against at least two provider adapters or fakes.
  - Provider secrets are isolated and never returned to the client.
  - A provider/model change does not require changing domain workflow code.
- Verification:
  - Contract suite passes for every configured provider adapter.
- Completion notes: The provider-neutral port (`ChatModel` in
  `packages/orchestration/src/tenantchat/orchestration/model.py`, added by
  `ARCH-001`) now has the contract suite its acceptance criteria require:
  `packages/orchestration/tests/test_chat_model_contract.py` runs the same
  seven scenarios — plain chat, parsed tool calls, tool-result correlation,
  usage/model attribution, provider failure, empty response, multi-call
  scripts — against two adapters: the OpenAI-compatible adapter
  (`providers/openai_compatible.py`, spoken over a fake httpx transport via a
  new injectable `transport` seam) and the scripted double. A third provider
  joins by adding one driver, not by editing a scenario. Secrets are pinned
  never to reach the client: the adapter test asserts the key appears in the
  Authorization header and nowhere else (payload, `repr`, raised exceptions),
  and the API tests assert a configured `LLM_API_KEY` appears in no
  visitor-visible body — including when the provider's own exception text
  carries the key, in which case the graph publishes a handoff answer instead
  of the failure. Model selection stays server-side: `LLM_BASE_URL`/
  `LLM_MODEL`/`LLM_API_KEY` configure the adapter `create_app` composes, and a
  request body that tries to name a model is rejected `422 malformed_request`
  (`extra="forbid"`; `ChatRequest` carries only `message`). The deployed
  composition is exercised with the real adapter in
  `tests/agent_runtime/test_openai_compatible_runtime.py`: a full booking —
  wire-level tool call, confirmation pause, committed reservation over the
  production PostgreSQL stores and checkpointer — driven by
  `OpenAICompatibleChatModel`. No domain or graph workflow code names a
  provider: `nodes.py` calls the port, and the runtime suites pass against
  both the scripted double and the wire adapter unchanged. The financing
  agent's `LLM_*` environment names are preserved by `Settings`, so an
  existing deployment configures all three clients identically. Changed:
  `packages/orchestration/src/tenantchat/orchestration/providers/
  openai_compatible.py` (transport seam; stale `max_tool_rounds` docstring
  removed), `packages/orchestration/tests/test_chat_model_contract.py` (new
  contract suite), `packages/orchestration/tests/test_openai_compatible.py`
  (public transport injection; key-isolation test),
  `tests/agent_runtime/test_openai_compatible_runtime.py` (new integration
  case), `services/api/tests/test_ai001_wiring.py` (client-visible secret
  isolation, success and provider-failure paths),
  `services/api/tests/test_chat.py` (visitor cannot name a model), and the
  `AI-001` index entry/status above. Verified: `make check` (747 hermetic
  Python tests plus the frontend suite; lock, lint, format, mypy strict,
  coverage, and deployment security clean) and `make test-agent-runtime` (7
  integration tests on disposable PostgreSQL 16, including the new
  adapter-driven booking). Follow-ups: the embedding contract lives with
  `RAG-002`'s `EmbeddingProvider`/`EmbeddingResult` port in the ingestion
  surface; streaming delivery is `FEAT-010`; per-tenant approved-model
  selection becomes meaningful once `FEAT-006` moves tenant configuration
  into the database, and `AI-002` owns per-tenant budgets and fallback rules;
  `REL-001` owns retry/backoff inside the client, which this adapter
  deliberately does not.

### AI-002 — Model safety, quotas, and cost controls

- Status: `Todo`
- Priority: `P2`
- Type: `AI safety/operations`
- Priority note: Per-tenant spend budgets and cost attribution are a billing
  concern that arrives with paying tenants. The two parts that are safety
  rather than economics do not wait for this task: bounded tool rounds are
  already enforced in the `dispatch@1` graph, and the per-request context limit
  is enforced during assembly by `AI-003`, which reports what it excluded
  instead of truncating silently.
- Depends on: `AI-001`, `SEC-003`, `OBS-002`
- Likely areas: policy engine, model gateway, tenant plans/configuration
- Scope:
  - Add input/output policy checks appropriate to the business domain.
  - Enforce per-request context limits and per-tenant usage/concurrency budgets.
  - Add model fallback rules, maximum tool rounds, maximum action count, and spend alerts.
  - Cache safe, non-personalized responses where appropriate.
- Acceptance criteria:
  - Exceeded budgets degrade predictably without executing partial actions.
  - Usage and cost can be attributed to a tenant without exposing user content.
  - Policy blocks and model fallbacks are auditable and measurable.
- Verification:
  - Tests cover quota exhaustion, provider failure, unsafe input/output, and fallback selection.
- Completion notes: _Pending._

### AI-003 — Versioned prompt assembly and template registry

- Status: `Todo`
- Priority: `P1`
- Type: `AI platform`
- Depends on: `AI-001`, `DATA-002`
- Likely areas: prompt builder package, template registry, tenant policy, orchestration
- Scope:
  - Replace string concatenation with a typed prompt builder that takes tenant policy, workflow state, conversation history, and retrieved evidence and returns a closed assembled-prompt type carrying its template ID, template version, resolved bindings, and content hash.
  - Keep prompt templates as versioned artifacts in the repository under review, never runtime-editable and never tenant-authored.
  - Express tenant customization as schema-validated slots — tone, business facts, escalation rules, disclaimers — so template structure stays code and tenant input stays data.
  - Segment the assembled prompt explicitly into trusted and untrusted regions, with retrieved evidence and prior visitor turns always untrusted, so `RAG-007` has a single boundary to enforce rather than a convention.
  - Enforce the token and source budget during assembly and report what was excluded, rather than truncating silently.
  - Provide a canonical segment-level diff between any two retained template versions and their declared binding schemas, so `FEAT-015` can show exactly what changed without treating runtime values as template changes.
  - Make the assembled prompt the only input the model adapter accepts, so no code path can reach the provider with an unversioned prompt.
- Acceptance criteria:
  - Every model call is attributable to a template ID and version recorded in the turn record.
  - A tenant configuration value cannot introduce a new instruction section, only fill a declared slot.
  - Evidence and prior visitor text are marked untrusted in the assembled type, and the marking is visible to `RAG-007` checks and the `FEAT-015` viewer.
  - Assembly that exceeds the budget returns the excluded set explicitly; nothing is dropped without a record.
  - Changing a template produces a new version rather than mutating one already referenced by stored turn records.
  - Comparing two template versions produces a deterministic segment and binding-schema diff.
- Verification:
  - Run prompt-assembly unit tests covering slot validation, injection attempts through tenant configuration and evidence, budget exclusion reporting, version immutability, and canonical template diffing.
- Completion notes: _Pending._

### RAG-001 — Versioned knowledge content model

- Status: `Done`
- Priority: `P1`
- Type: `RAG/data`
- Depends on: `DATA-001`, `SEC-001`
- Likely areas: knowledge tables, search-index mapping, admin API
- Scope:
  - Model sources, documents, versions, approval state, checksum, effective/expiry dates, visibility, and indexing state.
  - Require tenant and domain ownership on every knowledge record.
  - Permit only approved, current document versions to be retrievable.
- Acceptance criteria:
  - Publishing a new version atomically supersedes the old version.
  - Rollback restores a prior approved version without stale mixed results.
  - Expired, deleted, draft, or wrong-tenant content cannot be retrieved.
- Verification:
  - Lifecycle integration tests cover draft, approve, publish, supersede, expire, delete, and rollback.
- Completion notes: `tenantchat.core.knowledge` models sources, documents, and
  versions with approval state, SHA-256 content checksum, effective/expiry
  window, visibility, and indexing state. Retrievability is derived rather than
  stored: one predicate combines approval state, indexing state, the half-open
  effective window, source ownership, and the asking audience, so no flag can go
  stale, and `RAG-004`'s index filter has a specification to match. Lifecycle
  transitions return plans instead of applying writes, which keeps the rules
  testable without a database while the adapter owns atomicity. Rollback is not a
  separate operation — publishing a superseded version supersedes the current one
  through the same path, so no window exists in which both answer. Revision
  `0003_knowledge` adds three tables whose composite foreign keys pin the
  denormalized domain to its source, a partial unique index that permits one
  published version per document, and a per-document checksum uniqueness
  constraint that makes re-ingestion idempotent under a racing worker.
  `PostgresKnowledgeStore` locks the document row, builds a plan from the loaded
  aggregate, and applies it in one transaction with guards that repeat the read
  state; the application role can no longer delete knowledge rows, because
  withdrawal is a tombstone the indexing worker must observe. Changed:
  `packages/core/src/tenantchat/core/{knowledge,lifecycle,errors,__init__}.py`,
  `packages/core/tests/{test_knowledge,test_errors}.py`,
  `services/api/src/tenantchat/api/{problems.py,persistence/{knowledge,tenancy,
  repositories,__init__}.py}`, `services/api/migrations/{versions/
  0003_knowledge_content_model.py,provision_app_role.sql}`,
  `services/api/tests/test_problem_details.py`, `tests/migrations/
  test_migrations.py`, `tests/repositories/test_knowledge_repository.py`, and
  the migration runbook. Verified: `make check` (407 Python and six frontend
  tests; lock, lint, format, mypy strict, coverage, deployment security, and
  image contracts clean) and `make test-database` (10 migration plus 23
  repository tests on disposable PostgreSQL 16, including the concurrent-publish
  and stored-versus-domain-filter agreement cases). Follow-ups: `RAG-002` owns
  upload, parsing, and the durable indexing job that sets indexing state;
  `RAG-004` implements the same retrieval predicate inside the search query;
  `FEAT-001` adds the admin workflow once `SEC-001` provides admin
  authentication, and owns restoring a deleted document, which re-uploading
  deliberately does not do.

### RAG-002 — Secure asynchronous ingestion lifecycle

- Status: `Done`
- Priority: `P1`
- Type: `RAG/workflow`
- Depends on: `RAG-001`, `REL-003`, `SEC-004`
- Likely areas: ingestion API/worker, storage adapter, search indexing
- Scope:
  - Remove caller-controlled filesystem paths.
  - Accept authorized source IDs or validated uploads into isolated object storage.
  - Run parsing, scanning, chunking, embedding, and indexing as observable background jobs.
  - Deactivate stale chunks and clean up partial failed indexes.
  - Persist the parser, chunker, embedding model, and immutable index-generation identifiers needed by `OBS-004` to distinguish ingestion or index failure from retrieval failure.
  - Detect published content missing from the active index generation, partial chunk indexing, stored-versus-indexed chunk-count mismatch, embedding-model mismatch, index lag beyond a documented threshold, and a superseded version that remains retrievable.
- Acceptance criteria:
  - A caller cannot read arbitrary container files or ingest another tenant's content.
  - Re-ingesting unchanged content is idempotent.
  - Failed jobs expose safe status and can retry without duplicate active chunks.
  - Each Gate B index-integrity fault has a bounded safe code, identifies the affected tenant-qualified source version and index generation, and is available to `FEAT-001` and `OBS-004` without document content appearing in operational telemetry.
- Verification:
  - Security and lifecycle tests cover path traversal, cross-tenant IDs, duplicate ingestion, mid-index failure, missing and partial index generations, count and embedding-version mismatch, index lag, and superseded content left retrievable.
- Completion notes: The prototype's caller-supplied path is gone: uploads land in
  tenant-isolated object storage (`tenantchat.api.storage`) under server-derived
  keys built from tenant, source, checksum, and a slugified external key —
  filename validation and key parsing both reject traversal by shape, and the
  disk adapter re-verifies containment. A validated upload stages a draft and
  stores bytes; approval (which `FEAT-001`'s workflow drives through
  `submit_ingestion`) enqueues the durable `INGESTION` job, whose handler
  (`tenantchat.api.ingestion`) scans, parses (Markdown-sections prototype,
  pinned to `markdown-sections.v1`/`token-window.v1`), chunks, embeds through
  the embedding-service client, and indexes chunks with their embedding model
  and a **deterministic per-(tenant, version) index generation** — retries reuse
  the same generation, delete its partial chunks before rewriting, and only a
  complete, verified generation makes a version retrievable, so at-least-once
  delivery never duplicates active chunks. Migration `0010` persists
  `knowledge_index_generations` (parser/chunker/model, chunk counts, status —
  the `OBS-004` component identifiers) and `knowledge_index_findings`
  (content-free by construction). The `IndexIntegrityDetector`
  (`tenantchat.api.index_integrity`) reports exactly the six bounded
  `IndexingFault` codes — missing generation, partial generation, chunk-count
  mismatch, embedding-model mismatch, lag (documented 24h threshold in
  `tenantchat.core.indexing.INDEX_LAG_THRESHOLD`), superseded-still-retrievable
  — each naming the tenant-qualified source version and generation, persisted by
  a per-tenant reconcile that keeps first-detection timestamps and exposed to
  `FEAT-001`/`OBS-004` through the admin upload, findings, and integrity-check
  endpoints. The worker composes the ingestion handler only when storage,
  Elasticsearch, and the embedding service are all configured (fail closed);
  the job-worker manifest gained those credentials, a shared knowledge-storage
  claim with the API, and SEC-004 egress to Elasticsearch and the embedding
  service. Changed: `packages/core/src/tenantchat/core/indexing.py`,
  `packages/core/tests/test_indexing.py`, `services/api/src/tenantchat/api/
  {storage,search,ingestion,index_integrity}.py`, `routers/knowledge.py`,
  `dependencies.py`, `schemas.py`, `settings.py`, `app.py`, `job_worker.py`,
  `jobs.py`, `faults.py`, `store.py`, `persistence/{knowledge,index_integrity}.py`,
  migration `0011_ingestion_generations`, `k8s/{app,network-policies}.yaml`,
  `services/api/pyproject.toml` (python-multipart), `BACKLOG.md`, and the
  migration/repository/security/unit test suites. Verified: `make check`
  (735 Python and 93 frontend tests; lock, lint, format, mypy strict, coverage,
  deployment-security, and image contracts clean) and `make test-database`
  (11 migration plus 45 repository tests on disposable PostgreSQL 16,
  including generation lifecycle, one-generation-per-version upsert, finding
  reconcile with first-detection preservation, and cross-tenant finding
  isolation). Follow-ups: `RAG-003` replaces the prototype parser and chunker
  behind the pinned version identifiers; `FEAT-001` builds the approve/publish/
  review workflow over `submit_ingestion` and the findings endpoints;
  `RAG-004` implements the retrieval predicate in the index query that these
  generation and chunk fields were designed for; the prototype
  `ingestion-service` deployment and its seed job remain for the demo corpus
  until the DEP cutover removes them.

### RAG-003 — Production document parsing and chunking

- Status: `Todo`
- Priority: `P1`
- Type: `RAG`
- Depends on: `RAG-002`
- Likely areas: parser adapters, chunker, ingestion fixtures
- Scope:
  - Parse Markdown, PDF, DOCX, HTML, and plain text through explicit adapters.
  - Preserve heading hierarchy, page/section anchors, tables where practical, and source metadata.
  - Use model-aware token counting, configurable chunk sizes, and bounded embedding batches.
  - Detect empty, corrupt, encrypted, oversized, and unsupported documents.
- Acceptance criteria:
  - Every chunk maps back to a stable human-readable source location.
  - Large documents are embedded in bounded batches below service limits.
  - Golden parser fixtures have deterministic output.
- Verification:
  - Parser/chunker snapshot tests cover all supported types and edge cases.
- Completion notes: _Pending._

### RAG-004 — Hybrid retrieval, reranking, and abstention

- Status: `Todo`
- Priority: `P1`
- Type: `RAG`
- Depends on: `RAG-001`, `RAG-003`
- Likely areas: retrieval service, Elasticsearch mapping/query, reranker adapter
- Scope:
  - Combine lexical and vector retrieval with tenant/domain/version filters.
  - Add deduplication/diversification and an optional reranking stage.
  - Calibrate minimum relevance/evidence thresholds and return an explicit insufficient-evidence state.
  - Bound retrieved context by token and source budgets.
- Acceptance criteria:
  - Irrelevant questions abstain instead of answering from weak nearest neighbors.
  - Retrieval never includes inactive or cross-tenant content.
  - Retrieval parameters are versioned and measurable.
- Verification:
  - Offline retrieval evaluation meets documented recall and precision thresholds.
- Completion notes: _Pending._

### RAG-005 — Evidence and citation contract

- Status: `Todo`
- Priority: `P1`
- Type: `RAG/API`
- Depends on: `RAG-004`, `AI-001`
- Likely areas: answer schema, prompt builder, response validator
- Scope:
  - Return structured answers with claims, citation IDs, source title, source location, document version, and retrieval metadata safe for users.
  - Require supported factual claims to reference retrieved evidence.
  - Validate citation IDs against the exact context sent to the model.
  - Produce a safe abstention or human-follow-up action when evidence is insufficient.
- Acceptance criteria:
  - The model cannot cite an unseen source or another tenant's document.
  - Citation links resolve to an authorized source view.
  - Public clients receive curated citation data, not raw tool/debug payloads.
- Verification:
  - Citation-integrity tests cover valid, missing, fabricated, stale, and unauthorized citations.
- Completion notes: _Pending._

### RAG-006 — Conversation-aware retrieval

- Status: `Todo`
- Priority: `P1`
- Type: `RAG`
- Depends on: `DATA-002`, `RAG-004`, `AGENT-001`
- Likely areas: query planner, conversation state, financing agent request contract
- Scope:
  - Resolve follow-up questions using authorized conversation state.
  - Generate a standalone retrieval query without allowing prior untrusted text to become system instructions.
  - Track referenced entities, service, tenant, and active workflow.
- Acceptance criteria:
  - Follow-ups such as “what about the other plan?” retrieve the correct context.
  - Topic changes reset or revise retrieval context predictably.
  - Only bounded, relevant history is used.
- Verification:
  - Multi-turn evaluation cases cover pronouns, corrections, topic shifts, and malicious prior turns.
- Completion notes: _Pending._

### RAG-007 — RAG prompt-injection and content safety defenses

- Status: `Todo`
- Priority: `P1`
- Type: `AI security`
- Depends on: `RAG-002`, `RAG-005`
- Likely areas: ingestion scanning, prompt builder, answer validator, tool policy
- Scope:
  - Treat document text as delimited untrusted evidence.
  - Detect/quarantine suspicious embedded instructions and unsupported active content.
  - Prevent retrieved text from changing tool permissions, tenant, identity, or system policy.
  - Add deterministic output validation for sensitive financial and business claims.
- Acceptance criteria:
  - Known indirect prompt-injection fixtures do not reveal secrets, alter policy, or trigger tools.
  - Quarantined documents cannot be retrieved until reviewed.
  - Security decisions are auditable without storing unsafe full content in logs.
- Verification:
  - Run a maintained adversarial RAG test corpus in CI.
- Completion notes: _Pending._

### RAG-009 — Golden evaluation harness and scoreboard

- Status: `Todo`
- Priority: `P1`
- Type: `AI quality`
- Depends on: `RAG-001`, `RAG-003`
- Blocks: `RAG-004` tuning, `RAG-005`, `RAG-006`, `RAG-007`, `RAG-008`
- Likely areas: `evals/`, fixtures, `make` target
- Scope:
  - Build the minimum scoreboard needed to tune anything: a runner, a fixture corpus, and three scores — retrieval recall@k against labelled gold chunks, citation precision, and abstention correctness on questions with no supporting evidence.
  - Start at 20–30 hand-labelled cases across both seed tenants, including at least one stale-document case, one cross-tenant isolation case, and one question the corpus cannot answer.
  - Reuse the Gate B executable acceptance fixtures where they apply, including reviewer-labelled gold chunks for retrieval and context-substitution experiments; do not create a divergent copy of the same scenarios.
  - Run offline against fixtures with no live model, database, search service, or embedding service, so it belongs to the hermetic gate rather than to an integration environment.
  - Pin and report the component versions under test — retriever configuration, embedding model, reranker, prompt template, model ID — so two runs are comparable.
  - Print a diffable summary and exit non-zero below configured thresholds, without yet gating CI.
- Acceptance criteria:
  - Two runs over unchanged inputs produce identical scores.
  - A deliberately weakened retriever configuration moves recall@k measurably and visibly.
  - The harness runs without network access and completes fast enough to sit in the ordinary development loop.
  - Fixtures contain no real customer PII.
- Verification:
  - Run the harness twice for identical output, then once against a seeded regression to confirm the score moves in the expected direction.
- Completion notes: This is the thin slice that makes `RAG-004` through `RAG-007` tunable. `RAG-008` grows it into the versioned, CI-gating suite. _Pending._

### RAG-008 — RAG evaluation and regression suite

- Status: `Todo`
- Priority: `P1`
- Type: `AI quality`
- Depends on: `RAG-009`, `RAG-004`, `RAG-005`, `RAG-007`, `OBS-004`, `FEAT-008`
- Likely areas: `evals/`, fixtures, CI workflow, evaluation reports
- Scope:
  - Grow the `RAG-009` harness rather than replacing it, so one runner and one scoring implementation serve both the development loop and the release gate.
  - Create versioned datasets for retrieval recall, grounded answer correctness, citation precision, refusal, tenant isolation, and multi-turn behavior.
  - Include financing policy edge cases and adversarial documents.
  - Promote reviewed cases from the production flywheel: an `OBS-004` turn record with a reviewed diagnosis and correction under `FEAT-008` becomes a dataset case, subject to the `PRIV-002` anonymization checks.
  - Compare baseline and candidate component manifests across application build, prompt, retriever, parser/chunker, index generation, embedding, reranker, model, tool contract, tenant policy, and relevant feature-flag versions.
  - Publish a baseline-versus-candidate report with the manifest diff, aggregate changes, improved and regressed cases, and a link from every regression to its evaluation trace or promoted turn record.
  - Score claim grounding with the same validator `RAG-005` runs online, so the property gated in CI is the property enforced at request time.
  - Define release thresholds and a reviewed exception process.
- Acceptance criteria:
  - Evaluation runs are deterministic where possible and publish comparable reports.
  - A reviewer can identify what changed between baseline and candidate, which cases regressed, and the trace supporting each regression without correlating separate reports by hand.
  - CI blocks statistically or materially significant regressions below thresholds.
  - Any LLM-as-judge scorer reports measured agreement against human labels on a held-out set; an unvalidated judge may inform review but may not gate a release.
  - Dataset examples contain no real customer PII.
- Verification:
  - Run the evaluation twice and confirm stable scoring within documented tolerance.
- Completion notes: _Pending._

### AGENT-001 — Persisted intent router and workflow state machine

- Status: `Todo`
- Priority: `P1`
- Type: `Agent platform`
- Depends on: `ARCH-001`, `DATA-002`, `AI-001`
- Likely areas: orchestration package, workflow tables, tool registry
- Scope:
  - Replace keyword-only financing routing with a structured intent router.
  - Implement stateful routing and interrupts as a versioned LangGraph graph, per `ADR-0001`, without an intervening runtime abstraction.
  - Record the whole routing decision, not the winner: every candidate intent with its score, the chosen intent, the confidence, the router policy version, and the clarification or handoff threshold applied. This is what distinguishes a `routing_error` from a retrieval failure in the `OBS-004` taxonomy.
  - Persist active workflow, collected fields, pending confirmation, tool results, and next allowed actions.
  - Register specialized agents with explicit input/output schemas and deterministic tool allowlists.
  - Support pause, resume, cancel, handoff, failure recovery, and topic switching.
- Acceptance criteria:
  - The same user message routes consistently under a versioned router policy.
  - A misrouted turn is diagnosable from its record alone: whether the correct intent was never a candidate, was scored and lost, or lost to a confidence threshold.
  - A specialized agent cannot call tools outside its allowlist.
  - Workflow recovery after restart does not repeat committed actions.
  - Low-confidence or conflicting intent asks a clarification or hands off safely.
- Verification:
  - State-machine tests cover happy paths, interruptions, retries, topic changes, and invalid transitions.
- Completion notes: _Pending._

The `DEP-*` tasks below are Gate C. The parts of each that are a normal
consequence of shipping an image or a route already landed with the task that
shipped it: non-root numeric users, digest pinning, and the removal of runtime
`pip` and executable ConfigMap mounts under `DEP-001`; default-deny
NetworkPolicies, per-caller credentials, and public-route separation under
`SEC-004`; the widget gateway, its CSP, and admin/visitor listener separation
under `DEP-003`'s recorded progress. What remains in each entry is the
operating burden — policy scanners, autoscaling, recovery drills, and a
promotion pipeline — which requires a cluster that runs for real.

### DEP-002 — Kubernetes workload hardening

- Status: `Todo`
- Priority: `P3`
- Type: `Infrastructure`
- Depends on: `DEP-001`, `SEC-004`
- Likely areas: `k8s/`
- Scope:
  - Add restricted pod/container security contexts, read-only root filesystems, dropped capabilities, seccomp, and dedicated service accounts.
  - Add requests/limits to every container and controlled writable volumes.
  - Add namespace quotas and policy checks.
- Acceptance criteria:
  - Workloads pass the chosen Kubernetes policy scanner at the enforced level.
  - Application containers run non-root and cannot write outside declared mounts.
  - Default service-account tokens are not mounted where unnecessary.
- Verification:
  - Render and policy-test manifests; deploy smoke tests pass.
- Completion notes: _Pending._

### DEP-003 — TLS ingress and production widget hosting

- Status: `Todo`
- Priority: `P3`
- Type: `Infrastructure/frontend`
- Depends on: `SEC-003`, `DEP-002`
- Likely areas: ingress/certificate manifests, frontend build/hosting, CSP configuration
- Scope:
  - Add domain-based HTTPS ingress with automated certificate management.
  - Host versioned widget assets with cache-busting and a restrictive Content Security Policy.
  - Route public chat traffic separately from admin and internal services.
- Acceptance criteria:
  - HTTP redirects to HTTPS and modern TLS settings are enforced.
  - Admin, metrics, ingestion, databases, and observability backends are not public through the chat ingress.
  - Widget asset versions can roll forward/back without breaking existing embeds.
- Verification:
  - External route and TLS scan tests pass in a staging environment.
- Progress: the widget hosting and route separation landed ahead of this task.
  A sixth image, `web` (`frontend/Dockerfile`), builds the browser bundles and
  serves them from nginx, and is now the public entrypoint in Kubernetes and docker compose; the
  chat backend is no longer reachable from the ingress controller. The public
  listener forwards only the four visitor API paths and answers every other
  `/api/` path `404`; the operator console is a second listener with its own
  document root that no ingress and no NetworkPolicy admits. A restrictive CSP
  and security headers ship with it. See ADR-0006. Still outstanding here:
  domain-based HTTPS ingress with automated certificates, HTTP-to-HTTPS
  redirection, and content-hashed asset URLs so widget versions can roll forward
  and back independently — the gateway currently revalidates assets on a short
  `max-age` instead.
- Completion notes: _Pending._

### DEP-004 — High availability and autoscaling

- Status: `Todo`
- Priority: `P3`
- Type: `Infrastructure/reliability`
- Depends on: `DATA-002`, `DEP-002`
- Likely areas: deployments, HPA, PDB, topology rules, database/search architecture docs
- Scope:
  - Run stateless services with multiple replicas, disruption budgets, rolling strategies, and topology spread.
  - Autoscale on appropriate CPU, concurrency, queue, or latency signals.
  - Document stateful-service availability choices for the demo environment versus production.
- Acceptance criteria:
  - Losing one stateless pod does not interrupt an active session beyond the retry budget.
  - Scaling replicas does not alter conversation, booking, or metric correctness.
  - Voluntary disruption respects availability budgets.
- Verification:
  - Scale and pod-termination tests pass under synthetic traffic.
- Completion notes: _Pending._

### DEP-005 — Backup, restore, and disaster-recovery drill

- Status: `Todo`
- Priority: `P3`
- Type: `Operations/data`
- Depends on: `DATA-001`, `RAG-001`, `DEP-002`
- Likely areas: backup jobs, encrypted storage configuration, runbooks
- Scope:
  - Define RPO/RTO and back up Postgres plus required search/document state.
  - Encrypt backups, control access, set retention, and monitor job success.
  - Prefer rebuilding derived vector indexes from authoritative document versions where practical.
- Acceptance criteria:
  - A clean environment can restore tenants, conversations, business actions, and knowledge state.
  - Restore procedures include secret rotation and integrity verification.
  - A timed recovery drill records actual RPO/RTO and follow-up issues.
- Verification:
  - Complete and document a restore drill using non-sensitive test data.
- Completion notes: _Pending._

### DEP-006 — Release pipeline, scanning, and provenance

- Status: `Todo`
- Priority: `P3`
- Type: `Delivery/security`
- Depends on: `QA-001`, `DEP-001`, `DEP-002`
- Likely areas: CI/CD workflows, registry configuration, deployment overlays
- Scope:
  - Build, test, scan, sign, and publish immutable images.
  - Generate SBOMs and record source-to-image provenance.
  - Promote the same digest through environments with approval gates and automatic rollback criteria.
  - Run migration, smoke, and post-deployment checks.
- Acceptance criteria:
  - Deployments reference digests produced by CI, not mutable tags.
  - High-severity policy failures block promotion with an auditable exception process.
  - Rollback restores the prior application version without corrupting schema or actions.
- Verification:
  - Execute a staging release and rollback using the documented pipeline.
- Completion notes: _Pending._

### QA-002 — API and database integration tests

- Status: `Cancelled`
- Priority: —
- Type: `Quality`
- Cancelled on: `2026-08-04`
- Reason: This was already happening without the task. `DATA-001`, `DATA-002`,
  and `RAG-001` each shipped their own migration and repository suites against
  disposable PostgreSQL 16 under `make test-database`, because a repository is
  not demonstrably correct against a fake. Keeping the entry implied a future
  sweep that would test routes whose authors had moved on, and made Gate B
  depend on it.
- Where the scope went: the global definition of done now requires every route,
  repository, and migration a task changes to be exercised against a real
  Postgres through a documented `make` target, asserting the error contract
  rather than the status code alone. `tests/integration/` was never created;
  coverage lives beside the code it covers, in `tests/migrations/`,
  `tests/repositories/`, and each service's own suite.

### QA-003 — Tenant-isolation and security regression tests

- Status: `Cancelled`
- Priority: —
- Type: `Security quality`
- Cancelled on: `2026-08-04`
- Reason: An isolation suite that depends on `SEC-001` through `SEC-005` and
  `PRIV-001` can only be written after every boundary it checks is already
  shipped and trusted. That inverts the order that catches anything: the
  cross-tenant case is worth most when it is written by the person building the
  boundary, as the argument that the boundary holds.
- Where the scope went:
  - Cross-tenant and unauthorized-principal negative cases are now a
    definition-of-done item for every tenant-scoped surface, owned by the task
    that adds the surface.
  - `tests/security/` survives as the required home for a permanent regression
    test per exploit class, added by the task that closes the exploit. It is a
    growing artifact with no single owner rather than a task.
  - The adversarial corpora keep their existing owners: indirect prompt
    injection is `RAG-007`, trace-plane PII leakage is `PRIV-002`, rate and
    origin abuse is `SEC-003`, session hijack and tenant reassignment is
    `SEC-002`.

### QA-004 — End-to-end business workflow tests

- Status: `Cancelled`
- Priority: —
- Type: `Quality`
- Cancelled on: `2026-08-04`
- Reason: It collected the end-to-end coverage of four features into a fifth
  task that could only start once all four had shipped untested end to end.
- Where the scope went: the global definition of done now requires a task
  completing a customer- or operator-visible workflow to ship its own
  end-to-end case, including durable and external side effects, sandbox
  provider failure, and duplicate callback. `FEAT-002` through `FEAT-005` and
  `FEAT-004` already name those cases in their own verification sections.

### QA-005 — Load, soak, and failure-injection tests

- Status: `Todo`
- Priority: `P3`
- Type: `Performance/reliability quality`
- Gate: `C` — an operating burden, not a demo claim. Sustained load and soak
  behavior can only be measured against a cluster that runs for real, and
  measuring it teaches nothing further about the architecture. Per-dependency
  failure injection is not deferred with it: `REL-001` owns timeout, reset,
  `429`, `5xx`, and malformed-response coverage for the clients it adds.
- Depends on: `REL-001`, `REL-003`, `DEP-004`
- Likely areas: `tests/performance/`, staging scripts, runbooks
- Scope:
  - Define expected concurrency, throughput, context size, and latency targets.
  - Test sustained chat load, embedding batches, ingestion bursts, and admin polling/streaming.
  - Inject slow/failing LLM, Elasticsearch, Postgres, CRM, calendar, and worker restarts.
- Acceptance criteria:
  - The system meets documented p50/p95/p99 targets without unbounded queues or memory growth.
  - Overload is rejected or degraded safely.
  - Recovery produces no duplicate business actions or lost committed messages.
- Verification:
  - Publish a versioned test report linked to the release candidate.
- Completion notes: _Pending._

## Feature and workflow task details

### FEAT-001 — Knowledge-base administration workflow

- Status: `Todo`
- Priority: `P1`
- Type: `Feature`
- Depends on: `SEC-001`, `RAG-001`, `RAG-002`
- Likely areas: admin API/UI, document storage, ingestion job views
- Scope:
  - Add source creation, file upload, preview, validation, approval, publish, reindex, rollback, expiry, and deletion.
  - Display indexing status, document version, chunk count, errors, and last successful publish.
  - Display the bounded index-integrity findings produced by `RAG-002` — missing or partial active generation, chunk-count or embedding-model mismatch, excessive index lag, and superseded content still retrievable — and link each finding to the affected source version and related turns when authorized.
- Acceptance criteria:
  - Tenant admins can manage only their tenant's sources.
  - Draft content never affects answers before approval.
  - Every mutation is audited and recoverable where appropriate.
  - A tenant admin can distinguish an ingestion/index-integrity failure from a retrieval-quality failure without inspecting infrastructure logs.
- Verification:
  - End-to-end test uploads, approves, publishes, queries, supersedes, and deletes a document, then seeds each bounded integrity fault and verifies its tenant-safe presentation.
- Completion notes: _Pending._

### FEAT-002 — Real availability and calendar integration

- Status: `Todo`
- Priority: `P2`
- Type: `Feature/integration`
- Priority note: The architectural claim — a booking that survives retry,
  timeout, and replay — is proven by `DATA-003` against its database-backed
  provider, behind the same availability interface a real provider implements.
  This task swaps the implementation and adds webhook reconciliation. That is
  vendor plumbing with real value for a customer and little additional evidence
  for the demo, which is why it sits after Gate B rather than inside it.
- Depends on: `DATA-003`, `REL-001`, `REL-003`, `SEC-005`
- Likely areas: calendar provider adapter, scheduling service, tenant integration settings
- Scope:
  - Integrate one real or sandbox calendar/field-service provider behind the availability interface.
  - Map tenant services, staff/resources, duration, buffers, business hours, blackout dates, and timezone.
  - Support webhook or polling reconciliation for external changes.
- Acceptance criteria:
  - Availability reflects provider state and never exposes another tenant's resources.
  - Booking is idempotent across timeout/retry and external webhook replay.
  - External cancellation or conflict updates local state and notifies the workflow.
- Verification:
  - Provider sandbox tests cover create, conflict, timeout, webhook replay, and reconciliation.
- Completion notes: _Pending._

### FEAT-003 — CRM lead integration and delivery guarantees

- Status: `Todo`
- Priority: `P2`
- Type: `Feature/integration`
- Priority note: `REL-003` owns the guarantee in the title — durable outbox,
  leases, backoff, dead-letter, and exactly-once external effect under
  duplicate delivery — and proves it against a fake receiver. A real CRM
  adapter demonstrates field mapping, not delivery semantics.
- Depends on: `REL-003`, `PRIV-001`, `SEC-005`
- Likely areas: CRM adapter, lead outbox, admin delivery status
- Scope:
  - Deliver consented leads to one real or sandbox CRM provider.
  - Map tenant fields and record external ID, attempt state, last error, and delivery timestamp.
  - Add retry, deduplication, and operator replay.
- Acceptance criteria:
  - A lead produces at most one CRM record per idempotency key.
  - Failed delivery is visible and retryable without losing the local lead.
  - Only approved fields are sent to the provider.
- Verification:
  - Sandbox tests cover success, validation rejection, timeout, duplicate callback, and replay.
- Completion notes: _Pending._

### FEAT-004 — Human handoff queue and agent takeover

- Status: `Todo`
- Priority: `P1` for the Gate B slice; `P2` for the remainder
- Type: `Feature/workflow`
- Depends on: `SEC-001`, `DATA-002`, `AGENT-001`
- Likely areas: handoff tables/service, admin UI, visitor message channel
- Rationale: claim 2 names human handoff alongside booking and lead capture.
  `ARCH-001` shipped the escalate node and `handoffs.summary`, so the
  conversation already leaves the agent — it just arrives nowhere. Until a staff
  member can take ownership of it, "escalates to a human" describes a table.
- Scope — Gate B slice:
  - Add queue, reason, summary, assignment, accept, release, and resolution
    states over the existing admin polling transport.
  - Enforce single ownership: exactly one staff owner at a time, with
    race-to-accept resolved in the database rather than in the console.
  - Pause automated replies while a staff member owns the conversation, and
    resume the graph safely on release without repeating a committed action.
  - Notify the visitor of queue, takeover, and resolution state without
    exposing staff identity or internal queue position.
- Scope — deferred remainder (`P2`, after Gate B):
  - Presence, priority ordering, SLA timers and breach events, reassignment
    between staff, and the operator notification channel, which belongs with
    `FEAT-005`.
  - Live push of takeover and staff replies, which arrives with `FEAT-010`;
    the Gate B slice is correct over polling and must stay correct after it.
- Acceptance criteria:
  - Only one staff owner can hold a conversation at a time, proven under
    concurrent accepts rather than by UI affordance.
  - Automated agents cannot reply during active takeover unless explicitly
    invited, and a queued turn resuming after release commits nothing twice.
  - Every assignment and message is tenant-scoped and audited to a principal,
    tenant, timestamp, and request ID.
  - A visitor cannot learn which staff member holds their conversation, or that
    another tenant's queue exists.
- Verification:
  - Multi-user end-to-end tests cover race-to-accept, staff disconnect,
    release-and-resume, and resolution, asserting durable state alongside UI
    behavior.
- Completion notes: _Pending._

### FEAT-005 — Notification and outbound webhook workflow

- Status: `Todo`
- Priority: `P2`
- Type: `Feature/integration`
- Priority note: Shares `REL-003`'s delivery guarantee with `FEAT-003`. Signed,
  replay-protected webhooks and consent-aware channels are genuine product
  work, but nothing in the three claims depends on an email leaving the
  cluster. Absorbs the operator notification channel deferred from `FEAT-004`.
- Depends on: `REL-003`, `PRIV-001`, `SEC-005`
- Likely areas: notification adapters, webhook subscriptions, delivery log
- Scope:
  - Add tenant-configurable events for new lead, booking, handoff, failed delivery, cancellation, and SLA breach.
  - Support signed webhooks and at least one email or SMS sandbox adapter.
  - Respect channel-specific consent and quiet-hour policies.
- Acceptance criteria:
  - Webhooks are signed, timestamped, retried, deduplicated, and replay-protected.
  - Notification content contains only policy-approved data.
  - Operators can inspect delivery status and safely replay failures.
- Verification:
  - Tests cover signature validation, replay, timeout, retry, opt-out, and quiet hours.
- Completion notes: _Pending._

### FEAT-006 — Tenant onboarding, policy, and branding administration

- Status: `Todo`
- Priority: `P2`
- Type: `Feature/platform`
- Priority note: Tenant isolation is enforced by tenant-qualified repositories,
  retrieval filters, and RBAC — not by where the two demo tenants' facts are
  stored. Moving them out of code removes a deployment from onboarding, which
  matters to a customer and not to any of the three claims. `RAG-001` and
  `FEAT-001` already establish the draft/publish/version pattern this task
  would reuse.
- Depends on: `SEC-001`, `DATA-001`, `AI-001`
- Likely areas: tenant APIs/tables, admin settings UI, widget configuration
- Scope:
  - Move tenant facts, services, branding, domains/origins, business hours, policies, model choice, quotas, and integrations out of source code.
  - Add validated draft/publish workflow and configuration version history.
- Acceptance criteria:
  - A tenant admin can configure a new tenant without a code deployment.
  - Invalid or unsafe policy combinations cannot be published.
  - Configuration changes are audited and can roll back.
- Verification:
  - End-to-end test creates and publishes a tenant, embeds the widget, and verifies isolation.
- Completion notes: _Pending._

### FEAT-007 — Conversation search, filters, and operator actions

- Status: `Todo`
- Priority: `P2`
- Type: `Feature/admin`
- Depends on: `SEC-001`, `DATA-002`, `PRIV-001`
- Likely areas: admin API/UI and database indexes
- Scope:
  - Add pagination, tenant-safe search, status/outcome/date/assignee filters, tags, internal notes, and bulk archive/export where allowed.
- Acceptance criteria:
  - Queries are paginated and perform within a documented budget.
  - Search results obey RBAC and privacy rules.
  - Bulk actions require confirmation and produce audit events.
- Verification:
  - API/UI tests cover pagination, filtering, RBAC, and large result sets.
- Completion notes: _Pending._

### FEAT-008 — User feedback and reviewed-answer workflow

- Status: `Todo`
- Priority: `P1`
- Type: `Feature/AI quality`
- Depends on: `DATA-002`, `RAG-005`, `SEC-001`, `OBS-004`, `FEAT-015`
- Likely areas: widget feedback UI, review queue, evaluation dataset tooling
- Scope:
  - Add thumbs up/down, optional reason, staff review state, corrected answer, and links to prompt/model/retrieval versions.
  - Automatically enqueue turns with a detected technical failure as well as user-reported turns; prioritize by bounded severity, recurrence, business outcome, and whether the component-manifest hash first appeared in the current candidate.
  - Open the reviewed turn in the `FEAT-015` console, and require the reviewer to confirm, reject, or amend the diagnosis records for any turn marked unsatisfactory.
  - Track the proposed fix and verify whether a later evaluation run closes the reviewed case without erasing the original diagnosis or answer.
  - Permit approved, anonymized examples to become evaluation cases.
- Acceptance criteria:
  - Feedback cannot expose another conversation or alter production prompts directly.
  - Review decisions are audited and preserve original answer/evidence.
  - Automatic technical failures enter the queue without requiring a thumbs-down.
  - Automatic and reviewer diagnoses remain distinct; disagreements are reported rather than silently overwritten.
  - A reviewed case links to the first evaluation run that passes it after the fix, or remains visibly open.
  - Dataset promotion applies privacy checks.
- Verification:
  - E2E test covers feedback, automatic failure enqueueing, diagnosis review and disagreement, correction, safe evaluation promotion, and fix-closure verification.
- Completion notes: _Pending._

### FEAT-009 — Business outcome and conversion analytics

- Status: `Todo`
- Priority: `P2`
- Type: `Feature/analytics`
- Depends on: `OBS-002`, `PRIV-001`, `FEAT-002`, `FEAT-003`, `FEAT-004`
- Likely areas: analytics events/model, admin dashboards
- Scope:
  - Define a versioned funnel from visit to conversation, qualified need, lead, booking, handoff, completion, cancellation, and downstream conversion.
  - Add tenant-safe dashboards and date/channel/service filters.
- Acceptance criteria:
  - Metrics reconcile with transactional records within a documented tolerance.
  - Analytics avoids raw PII and honors deletion/retention.
  - Outcome definitions are documented and versioned.
- Verification:
  - Seed deterministic journeys and verify dashboard totals.
- Completion notes: _Pending._

### FEAT-010 — Streaming, cancellation, and reliable message delivery

- Status: `Todo`
- Priority: `P2`
- Type: `Feature/frontend/backend`
- Depends on: `API-001`, `DATA-002`, `SEC-002`, `REL-001`
- Likely areas: chat transport, widget state, admin live updates, application lifecycle hooks
- Priority note: This is the highest-value `P2` and the first task to pick up
  after Gate B. A non-streaming widget reads as a prototype regardless of what
  is behind it. It is not `P1` because none of the three claims is about
  perceived latency, and `DATA-002` already made the underlying correctness
  property true: the visitor's message is committed before the runtime is
  asked anything, so an interrupted turn loses a reply, never a question.
- Scope:
  - Add authenticated SSE or WebSocket delivery for model tokens, message commits, handoff changes, and staff replies.
  - Add cancel, reconnect, resume cursor, delivery acknowledgements, and duplicate suppression.
  - Persist only complete or explicitly cancelled message states.
  - Drain traffic and finish or safely cancel in-flight work during shutdown,
    absorbed from the cancelled `REL-002`. A rolling update must not lose an
    accepted message or leave a partial one durable.
- Acceptance criteria:
  - Refresh/reconnect does not duplicate or lose committed messages.
  - Cancellation stops downstream generation where supported and never rolls back committed actions.
  - Staff replies arrive without two-second polling, and the `FEAT-004`
    ownership guarantees hold identically over the pushed transport.
  - A rolling restart mid-turn loses no accepted message and produces no
    duplicate business action.
- Verification:
  - Browser tests cover network interruption, reconnect, duplicate event, cancel, and takeover.
  - A restart-during-generation test asserts the drain guarantee.
- Completion notes: _Pending._

### FEAT-011 — Customer-facing citations and source viewer

- Status: `Todo`
- Priority: `P1`
- Type: `Feature/RAG UX`
- Depends on: `RAG-005`, `SEC-002`
- Likely areas: widget citation components, authorized source API/viewer
- Scope:
  - Render compact citations beside supported answers.
  - Provide an accessible source viewer showing authorized title, section/page, version, and excerpt.
  - Replace raw tool-call debug blocks with user-appropriate action and evidence states.
- Acceptance criteria:
  - Citation display matches the structured answer contract.
  - Source access rechecks tenant/session authorization.
  - Missing or revoked sources degrade without leaking metadata.
- Verification:
  - Browser tests cover multiple citations, abstention, revoked sources, and keyboard navigation.
- Completion notes: _Pending._

### FEAT-012 — Booking cancellation and rescheduling

- Status: `Todo`
- Priority: `P2`
- Type: `Feature/workflow`
- Depends on: `FEAT-002`, `SEC-002`, `AGENT-001`
- Likely areas: booking service/provider adapter, widget workflow, notifications
- Scope:
  - Add secure lookup, cancellation, and rescheduling with provider reconciliation and policy-aware cutoffs.
- Acceptance criteria:
  - Only an authorized visitor or staff principal can change a booking.
  - Rescheduling is transactional and cannot lose the original slot before the new slot is secured or safely compensated.
  - Changes are audited and notify configured parties.
- Verification:
  - Tests cover cutoffs, conflicts, provider timeout, retry, and compensation.
- Completion notes: _Pending._

### FEAT-013 — Accessibility, responsive embed, and privacy UX

- Status: `In progress` — _client slice complete; screen-reader pass and
  server-side consent record outstanding_
- Priority: `P2`
- Type: `Feature/frontend`
- Depends on: `SEC-002`, `PRIV-001`, `FEAT-010`
- Likely areas: widget HTML/CSS/JS, accessibility tests
- Scope:
  - Meet WCAG 2.2 AA for keyboard, focus, labels, contrast, motion, status announcements, and errors.
  - Add responsive embed sizing, host-page isolation, consent/privacy notice, data controls, and accessible failure states.
- Acceptance criteria:
  - Automated accessibility checks pass and a manual keyboard/screen-reader checklist is completed.
  - The widget does not break or inherit unsafe styles from the host page.
  - Consent and privacy controls are clear before contact data is submitted.
- Verification:
  - Run accessibility tests across supported viewport sizes and browsers.
- Completion notes: Moved the whole frontend into `frontend/` as a
  self-contained npm project (`frontend/public/` served as-is,
  `frontend/tests/` for Vitest, ESLint/Prettier/Vitest beside them); served URLs
  are unchanged. Rebuilt the widget to render into a shadow root with a scoped
  `all: initial` reset, split it into `widget/{api,privacy,styles,widget,embed}.js`,
  and added `embed.js` as the customer-facing entry now that `app.js` also drives
  the demo host page. Accessibility work: `role="dialog"` with an accessible
  name, `role="log"` transcript with `aria-live` and `aria-busy`, a `role="status"`
  region for pending replies, per-message speaker names, a real composer label,
  focus returned to launcher and composer on close and open, Escape to close,
  `aria-expanded` on the launcher and privacy toggle, `role="alert"` booking and
  backend-failure states with focus moved to the fix, full-viewport sizing under
  560 px wide or 520 px tall, `prefers-reduced-motion` and `forced-colors`
  handling, and a palette reworked so every painted pair meets AA. Privacy UX:
  a persistent notice, an expandable panel describing what is stored, a delete
  control that clears all browser state, a required consent checkbox that blocks
  submission and travels with the booking payload, prefill of details already
  given (§3.3.7), lazy session-id creation so nothing is stored until the visitor
  sends something, and an in-memory fallback when the browser refuses storage.
  Also raised control-border contrast on the demo and admin pages and labelled
  the admin reply input and transcript.
  Changed files: `frontend/**` (moved and rewritten), `docs/accessibility.md`
  (new), `server.py` (static root, public-route allowlist; deleted with the
  `API-001` cutover), `Makefile`,
  `Dockerfile`, `.github/workflows/ci.yml`, `.gitignore`, `.dockerignore`,
  `tests/test_network_boundaries.py`, `README.md`, `CLAUDE.md`.
  Verified: `make check` (407 Python and 50 frontend tests, 98% frontend
  coverage), plus a manual pass in Chromium at 1280×800, 740×400, and 320×568
  recorded in `docs/accessibility.md`.
  Follow-ups: screen-reader pass with VoiceOver and NVDA (recorded as
  outstanding in `docs/accessibility.md`); server-side consent record, retention,
  and erasure belong to `PRIV-001` — the widget sends a consent object that
  nothing yet persists; tenant-bound visitor sessions are `SEC-002`.
  Carried forward through the React rewrite ([ADR-0009](docs/adr/0009-react-frontend-build.md)):
  every guarantee above is asserted by the ported suites against the same element
  ids, and the contrast suite gained a scheme axis now that the widget honours
  `prefers-color-scheme`. New in that pass: a jump-to-latest control so a staff
  reply cannot scroll a reader away mid-sentence, an unread count on the launcher,
  a typing indicator that is hidden from assistive technology because the status
  region already announces it, and polling that no longer issues a conversation id
  for a visitor who has typed nothing.

### FEAT-014 — Additional business-domain agents

- Status: `Todo`
- Priority: `P3`
- Type: `Feature/agents`
- Priority note: `AGENT-001` proves the registry, the typed tool allowlist, and
  safe handoff; the dispatcher and financing agents already exercise two
  domains through it. A third and fourth agent add breadth, not evidence — the
  generality claim is carried by the registry contract and its tests, not by
  the count of agents registered against it.
- Depends on: `AGENT-001`, `RAG-008`, appropriate real integrations
- Likely areas: specialized agent registry, domain prompts/policies, tools, eval datasets
- Scope:
  - Add at least two bounded agents demonstrating recurring business workflows, such as support triage, order/status lookup, quote qualification, or document intake.
  - Each agent must have explicit permissions, typed tools, safe handoff, and its own evaluation set.
- Acceptance criteria:
  - Agents cannot access another tenant or tools outside their domain.
  - Router accuracy and each agent's task success meet documented evaluation thresholds.
  - Unsupported requests hand off or abstain safely.
- Verification:
  - Run end-to-end and evaluation suites for every added agent.
- Completion notes: _Pending._

### FEAT-015 — AI turn explorer and executed-graph console

- Status: `Todo`
- Priority: `P1`
- Type: `Feature/AI operations`
- Depends on: `OBS-004`, `SEC-001`
- Likely areas: admin trace API client, admin routing and filters, executed-graph visualization, retrieval/prompt/claim/diagnosis panels, browser tests
- Scope:
  - Add a tenant-scoped turn explorer with exactly the Gate B filters needed for diagnosis: time, tenant, outcome, diagnosis cause, diagnosis status, and component-manifest hash. Defer additional filter dimensions until usage proves they are needed.
  - Render the actual nodes, edges, attempts, branches, fallbacks, status, duration, and safe error codes from `OBS-004` as an accessible DAG or waterfall; never infer an idealized graph from the final answer.
  - Provide coordinated drill-down panels for the routing alternatives, standalone query and filters, lexical/vector/fused/reranked candidate funnel, context-budget exclusions, trusted/untrusted prompt segments, prompt-version diff, claim-to-evidence verdicts, tool-policy and execution results, and automatic versus reviewer diagnoses.
  - Overlay reviewer-labelled gold evidence when an evaluation or reviewed case provides it, and expose the safe replay controls supported by `OBS-004` with the original and changed component manifests visibly distinguished.
  - Link content-free operational errors and metric exemplars to the corresponding turn while keeping the content-bearing record inside the inference trace plane.
- Acceptance criteria:
  - Every displayed execution node and edge maps to a stored execution event, and failed or partially completed turns remain inspectable.
  - The six filters can locate every Gate B seeded failure without exposing another tenant's metadata or content.
  - Deterministic automatic claim verdicts are limited to `supported`, `unsupported`, and `fabricated_citation`; graded entailment judgements remain reviewer labels or explicitly non-gating suggestions until validated under `RAG-008`.
  - Trace reads and replay launches are RBAC-gated and audited, and the UI communicates `suspected` and `inconclusive` diagnoses without presenting them as confirmed causes.
  - All ten Gate B acceptance cases can be walked from explorer result to executed graph, evidence, diagnosis, and replay result where applicable.
- Verification:
  - Run component and browser tests for the six filters, actual-graph fidelity, partial traces, retrieval funnel, prompt diff, claim evidence, diagnosis uncertainty, gold-evidence overlay, replay safety messaging, tenant isolation, keyboard navigation, and the ten-case Gate B walkthrough.
- Completion notes: _Pending._

## Recommended dispatch sequence

This sequence reduces merge conflicts and prevents agents from building features on insecure foundations.

Complexity is implementation risk, not estimated duration: `S` is one bounded
component, `M` spans a few components behind stable contracts, `L` crosses a
security, persistence, or distributed boundary, and `XL` combines several such
boundaries or requires uncertain AI-quality judgement. Use an economical coding
model for `S` and most `M` tasks. Use the strongest available reasoning/coding
model for `L` and `XL`; a cheaper model may still implement mechanical fixtures
or UI subcomponents, but the task owner and final reviewer should remain the
strong model. P0 security changes always receive strong-model review regardless
of implementation complexity.

No wave contains a task whose output is coverage of an earlier wave. Integration
and isolation tests were previously scheduled as `QA-002` in Wave 1 and `QA-003`
in Wave 3; they are now part of what makes each row in every wave `Done`, which
is why the complexity ratings below already assume that work. A row that looks
cheaper than its rating suggests is probably not counting it.

### Wave 0 — Current unblockers

Run these two now; their likely areas do not need to overlap. `API-001` slice 2
landed the chat and admin surface, so everything Wave 1 was waiting on the API
for is now startable.

| Order | Task | Complexity | Model routing | Why now |
|---|---|---:|---|---|
| 0A | `DATA-003` | `L` | Strong | Independent P0 concurrency/idempotency boundary and required for safe business actions. |
| 0B | `REL-003` | `L` | Strong | Independent durable-job foundation required by ingestion. |

### Wave 1 — API, identity, and first stable contracts

Start each row when its listed dependencies are complete; tasks on the same row
may run in parallel only when their likely files do not overlap.

| Order | Task | Complexity | Model routing | Dependency note |
|---|---|---:|---|---|
| 1A | `AI-001` | `L` | Strong | Unblocked, and the last thing between the served runtime and an answered turn: chat routes report themselves unavailable until a provider adapter exists. |
| 1B | `SEC-001` remainder | `L` | Strong | After `API-001`; finish tenant membership and route-level RBAC rather than treating gateway auth as completion. |
| 1C | `SEC-002` | `L` | Strong | After `API-001`; establishes the visitor security boundary. |
| 1D | `RAG-002` | `L` | Strong | After `REL-003`; implements durable ingestion and index-integrity detection. |

### Wave 2 — Runtime, privacy foundation, and content preparation

| Order | Task | Complexity | Model routing | Dependency note |
|---|---|---:|---|---|
| 2A | `SEC-003` remainder | `M` | Economical implementation, strong security review | After `SEC-002`; rate, concurrency, and size limits are bounded middleware work. |
| 2B | `PRIV-001` | `XL` | Strong | After `SEC-001` and `SEC-002`; cross-cuts schema, APIs, UI, retention, export, and deletion. |
| 2C | `AI-003` | `M` | Economical implementation, strong boundary review | After `AI-001`; typed prompt assembly and deterministic template diff have explicit contracts. |
| 2D | `REL-001` | `M` | Economical | After `AI-001`; bounded resilient-client adapters and contract tests. |
| 2E | `AGENT-001` | `XL` | Strong | After `AI-001`; persisted routing, workflow recovery, and tool permissions are coupled. |
| 2F | `RAG-003` | `M` | Economical | After `RAG-002`; parser adapters and golden fixtures are well bounded. |
| 2G | `FEAT-001` | `M` | Economical | After `SEC-001` and `RAG-002`; admin workflow over established lifecycle APIs. |

### Wave 3 — Retrieval, grounding, and safety

| Order | Task | Complexity | Model routing | Dependency note |
|---|---|---:|---|---|
| 3A | `RAG-009` | `M` | Economical | After `RAG-003`; land the deterministic runner before tuning retrieval. |
| 3B | `RAG-004` | `XL` | Strong | After `RAG-003`; tune only after `RAG-009` can expose regressions. |
| 3C | `RAG-005` | `L` | Strong | After `RAG-004`; the evidence contract and mechanical citation validator become system boundaries. |
| 3D | `RAG-006` | `L` | Strong | After `RAG-004` and `AGENT-001`; conversation query planning must preserve untrusted-history boundaries. |
| 3E | `RAG-007` | `L` | Strong | After `RAG-005`; adversarial content and deterministic policy enforcement are security-sensitive. |
| 3F | `FEAT-011` | `S` | Economical | After `RAG-005` and `SEC-002`; bounded citation and authorized-source UI. |
| 3G | `OBS-001` | `M` | Economical | After `PRIV-001`; structured events, correlation, and redaction under a fixed privacy contract. |
| 3H | `PRIV-002` | `L` | Strong | After `PRIV-001`; protects the content-bearing inference plane. |
| 3I | `FEAT-004` Gate B slice | `M` | Economical implementation, strong review of the ownership transaction | After `AGENT-001` and the `SEC-001` remainder; completes claim 2's escalation path over the existing polling transport. |

### Wave 4 — Provenance product and quality flywheel

| Order | Task | Complexity | Model routing | Dependency note |
|---|---|---:|---|---|
| 4A | `OBS-002` | `M` | Economical | After `OBS-001` and `RAG-005`; bounded metrics and manifest correlation. |
| 4B | `OBS-004` | `XL` | Strong | After `AI-003`, `RAG-005`, `OBS-001`, and `PRIV-002`; authoritative trace, diagnosis, durability, and safe replay. |
| 4C | `FEAT-015` | `XL` | Strong | After `OBS-004`; integrates the actual graph and every diagnostic panel into the primary demo surface. |
| 4D | `FEAT-008` | `M` | Economical | After `FEAT-015`; bounded review queue, diagnosis reconciliation, and case promotion. |
| 4E | `RAG-008` | `L` | Strong | Last implementation task; integrates reviewed cases, adversarial evaluation, manifest comparison, and the release gate. |

### Wave 5 — Gate B verification, then stop

- Confirm that no required task shipped with a definition-of-done exemption. A
  waived integration, cross-tenant, or end-to-end obligation is a Gate B
  blocker recorded against the task that waived it, not a new backlog entry.
- Run the ten-case Gate B executable acceptance script across both tenants and
  require every case to be discoverable in `FEAT-015`.
- Run the `FEAT-004` handoff journey end to end, since claim 2's escalation
  path is verified there rather than by the trace script.
- Record measured results and any explicit exception; do not start Gate C work
  merely because a model or agent is idle.
- Keep `AI-002`, `FEAT-002`, `FEAT-003`, `FEAT-005` through `FEAT-007`,
  `FEAT-009`, `FEAT-010`, `FEAT-012`, `FEAT-014`, the `FEAT-004` remainder,
  `OBS-003`, `DEP-002` through `DEP-006`, and `QA-005` outside the Gate B
  dispatch queue unless the product goal changes.

Then stop. `FEAT-010` is the first task to pick up if the project continues:
live delivery is what a viewer notices before any of the three claims, and it
carries the shutdown-drain guarantee absorbed from the cancelled `REL-002`.

## Decision log required before implementation

Record these decisions as ADRs before or during their first dependent task:

- Authentication provider and development-auth strategy.
- ASGI framework and backend package structure.
- Migration tool and database tenancy strategy.
- Background job/outbox implementation.
- Object storage and supported document types.
- LLM and embedding provider contract.
- Agent runtime boundary and LangGraph adoption (`ADR-001` is accepted; record implementation consequences and future amendments).
- Calendar/field-service and CRM demonstration providers.
- Secret-management approach for local, staging, and production environments.
- Public hosting domain, ingress, certificate, and widget asset strategy.
- Privacy retention defaults and target compliance posture.
- Telemetry plane split and inference-trace ownership (`ADR-0010` is accepted; the first-party store and `FEAT-015` remain authoritative. If an optional third-party operational trace projection is adopted, record its licence terms, content-export setting, and retention here without making it the system of record).
