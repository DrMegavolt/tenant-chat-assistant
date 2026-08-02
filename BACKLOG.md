---
project: tenant-chat-assistant
document_type: implementation-backlog
schema_version: 1
last_updated: 2026-08-01
source_of_truth: BACKLOG.md
---

# Production RAG Chatbot Backlog

## Product goal

Turn the current tenant chatbot prototype into a production-oriented demonstration of a secure, multi-tenant RAG and agent platform. The finished demo should show safe knowledge retrieval, reliable business actions, human escalation, measurable outcomes, and an operational deployment story.

This backlog is written for both humans and implementation agents. Every task has a stable ID, status, priority, dependencies, bounded scope, acceptance criteria, and expected verification.

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
- Agent-framework checkpoints hold resumable execution state; Postgres domain tables remain the system of record for conversations and business actions.
- Framework code may orchestrate typed domain tools, but authentication, authorization, validation, transactions, and idempotency stay in deterministic application services.

## Accepted agent-framework architecture decision

- Decision ID: `ADR-001`
- Status: `Accepted`
- Decision date: `2026-07-31`
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
- Tests, linting, and static checks pass locally.
- Configuration and environment variables are documented with safe defaults.
- Errors are observable without exposing secrets or PII.
- Tenant isolation and authorization are covered where relevant.
- No new high-severity dependency or container scan findings are knowingly introduced.
- The task's completion notes list changed files, verification commands, and any follow-up task IDs.

## Release gates

### Gate A — Safe public demo

Required: every `P0` task in this document. Until Gate A passes, expose the project only on a trusted development network.

### Gate B — Production RAG and business workflow demo

Required: Gate A plus `ARCH-001`, `RAG-001` through `RAG-008`, `AGENT-001`, `FEAT-002` through `FEAT-005`, `OBS-001` through `OBS-003`, and `REL-001` through `REL-003`.

### Gate C — Operational production candidate

Required: Gate B plus `DEP-002` through `DEP-006`, `QA-002` through `QA-005`, and completed runbooks.

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
- [ ] `DATA-001` — Normalized schema and migration framework — `P0`
- [ ] `API-001` — Production API runtime and typed contracts — `P0` — _slice 1 of 2 complete_
- [ ] `DATA-002` — Server-authoritative repositories and concurrency control — `P0`
- [ ] `DATA-003` — Transactional, idempotent booking — `P0`
- [ ] `SEC-001` — Admin authentication and tenant-scoped RBAC — `P0`
- [ ] `SEC-002` — Secure visitor sessions and tenant binding — `P0`
- [ ] `SEC-003` — API abuse protection, CORS, and response hardening — `P0`
- [ ] `SEC-004` — Service authentication and Kubernetes network boundaries — `P0`
- [ ] `SEC-005` — Secret management and credential removal — `P0`
- [ ] `PRIV-001` — PII classification, consent, retention, export, and deletion — `P0`
- [ ] `DEP-001` — Immutable, reproducible application images — `P0`

### P1 production core

- [ ] `REL-001` — Resilient dependency clients — `P1`
- [ ] `REL-002` — Dependency-aware health, graceful startup, and shutdown — `P1`
- [ ] `REL-003` — Durable background jobs and retry handling — `P1`
- [ ] `OBS-001` — Structured logging and request correlation — `P1`
- [ ] `OBS-002` — LLM, RAG, tool, and business metrics — `P1`
- [ ] `OBS-003` — Dashboards, SLOs, and alerts as code — `P1`
- [ ] `ARCH-001` — Agent runtime boundary and LangGraph adoption — `P1`
- [ ] `AI-001` — Provider and model abstraction — `P1`
- [ ] `AI-002` — Model safety, quotas, and cost controls — `P1`
- [ ] `RAG-001` — Versioned knowledge content model — `P1`
- [ ] `RAG-002` — Secure asynchronous ingestion lifecycle — `P1`
- [ ] `RAG-003` — Production document parsing and chunking — `P1`
- [ ] `RAG-004` — Hybrid retrieval, reranking, and abstention — `P1`
- [ ] `RAG-005` — Evidence and citation contract — `P1`
- [ ] `RAG-006` — Conversation-aware retrieval — `P1`
- [ ] `RAG-007` — RAG prompt-injection and content safety defenses — `P1`
- [ ] `RAG-008` — RAG evaluation and regression suite — `P1`
- [ ] `AGENT-001` — Persisted intent router and workflow state machine — `P1`
- [ ] `DEP-002` — Kubernetes workload hardening — `P1`
- [ ] `DEP-003` — TLS ingress and production widget hosting — `P1`
- [ ] `DEP-004` — High availability and autoscaling — `P1`
- [ ] `DEP-005` — Backup, restore, and disaster-recovery drill — `P1`
- [ ] `DEP-006` — Release pipeline, scanning, and provenance — `P1`
- [ ] `QA-002` — API and database integration tests — `P1`
- [ ] `QA-003` — Tenant-isolation and security regression tests — `P1`
- [ ] `QA-004` — End-to-end business workflow tests — `P1`
- [ ] `QA-005` — Load, soak, and failure-injection tests — `P1`

## Feature and workflow task list

These tasks cover the missing customer-facing and operator-facing capabilities identified in the production audit.

- [ ] `FEAT-001` — Knowledge-base administration workflow — `P1`
- [ ] `FEAT-002` — Real availability and calendar integration — `P1`
- [ ] `FEAT-003` — CRM lead integration and delivery guarantees — `P1`
- [ ] `FEAT-004` — Human handoff queue and agent takeover — `P1`
- [ ] `FEAT-005` — Notification and outbound webhook workflow — `P1`
- [ ] `FEAT-006` — Tenant onboarding, policy, and branding administration — `P1`
- [ ] `FEAT-007` — Conversation search, filters, and operator actions — `P2`
- [ ] `FEAT-008` — User feedback and reviewed-answer workflow — `P2`
- [ ] `FEAT-009` — Business outcome and conversion analytics — `P2`
- [ ] `FEAT-010` — Streaming, cancellation, and reliable message delivery — `P1`
- [ ] `FEAT-011` — Customer-facing citations and source viewer — `P1`
- [ ] `FEAT-012` — Booking cancellation and rescheduling — `P2`
- [ ] `FEAT-013` — Accessibility, responsive embed, and privacy UX — `P2`
- [ ] `FEAT-014` — Additional business-domain agents — `P2`

## Completed baseline details

### BASE-001 — Embeddable multi-tenant chat widget

- Status: `Done`
- Priority: `Baseline`
- Evidence: `index.html`, `app.js`, and `styles.css`
- Existing scope:
  - Browser chat experience with quick actions and per-tenant session IDs.
  - Two demonstration companies with distinct policies and branding.
  - Configurable remote API base URL for embedding the widget.
- Completion notes: Existing prototype behavior; production session security is tracked in `SEC-002`.

### BASE-002 — Tenant policy and LLM tool loop

- Status: `Done`
- Priority: `Baseline`
- Evidence: `server.py`
- Existing scope:
  - Server-owned tenant facts and pricing/booking policies.
  - OpenAI-compatible chat-completions loop with bounded tool rounds.
  - Deterministic policy checks around service area, availability, booking, lead capture, and handoff.
- Completion notes: Existing prototype behavior; authorization and provider abstraction remain outstanding.

### BASE-003 — Lead capture prototype

- Status: `Done`
- Priority: `Baseline`
- Evidence: `server.py`, `app.js`, and `admin.js`
- Existing scope:
  - Required-field and contact validation.
  - Lead capture through model tools and rule fallback.
  - Lead display in the admin console.
- Completion notes: Leads are not yet delivered to a real CRM; see `FEAT-003`.

### BASE-004 — Availability and booking prototype

- Status: `Done`
- Priority: `Baseline`
- Evidence: `server.py` and `app.js`
- Existing scope:
  - Static service-specific availability.
  - Structured booking form with contact and address validation.
  - Confirmation records shown in the admin console.
- Completion notes: This is a non-transactional mock; production integrity is tracked in `DATA-003` and `FEAT-002`.

### BASE-005 — Live admin transcript console

- Status: `Done`
- Priority: `Baseline`
- Evidence: `admin.html` and `admin.js`
- Existing scope:
  - Session list, outcomes, transcripts, leads, bookings, and tool-event panels.
  - Polling for updates and staff messages into visitor conversations.
- Completion notes: Authentication and real handoff ownership are tracked in `SEC-001` and `FEAT-004`.

### BASE-006 — Local and Postgres snapshot persistence

- Status: `Done`
- Priority: `Baseline`
- Evidence: `server.py` and `requirements.txt`
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
- Evidence: `server.py`, service applications, and `k8s/otel-collector.yaml`
- Existing scope:
  - Prometheus metrics endpoints and ServiceMonitors.
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
  Prettier, V8 coverage, and six offline widget characterizations; added seven
  offline prototype characterizations for fallback routing, ingestion chunking,
  and tenant/domain/active retrieval filters. Existing core/API tests cover
  tenant policy, tool validation, and current contracts with in-memory fakes.
  `make check` is the documented local/CI gate for Python and JavaScript and
  writes Python XML/HTML plus frontend Cobertura/HTML/summary coverage under
  `coverage/`. CI runs on pull requests, uploads JUnit test results, coverage,
  and vulnerability reports for 30 days, and blocks on actionable high/critical
  filesystem or container findings using a known-safe SHA-pinned Trivy action;
  no external service or configured secret is required. Changed: `package.json`,
  `package-lock.json`, `eslint.config.js`, `.prettierrc.json`,
  `vitest.config.js`, `frontend/tests/widget.test.js`,
  `tests/test_prototype_characterization.py`, `pyproject.toml`, `uv.lock`,
  `Makefile`, `.github/workflows/ci.yml`, `.gitignore`, `README.md`,
  `CLAUDE.md`, plus formatting-only updates to `app.js` and `admin.js`.
  Verified `make check` twice after removing dependency directories and cleaning
  generated artifacts: 271 Python tests (99% covered source) and six widget
  tests (86.21% statements) passed both times; `npm audit --audit-level=high`
  reported zero vulnerabilities. Follow-ups: `QA-002` through `QA-005` deepen
  integration, isolation, E2E, and failure coverage; `DEP-001` replaces the
  scanned prototype image and `DEP-006` owns release scanning/provenance.

### DATA-001 — Normalized schema and migration framework

- Status: `Todo`
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
- Completion notes: _Pending._

### API-001 — Production API runtime and typed contracts

- Status: `In Progress`
- Priority: `P0`
- Type: `Backend architecture`
- Depends on: `QA-001`
- Likely areas: `server.py`, `services/api/`, `Dockerfile`, `k8s/app.yaml`
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

**Slice 2 — remaining before cutover.**

- Port `/api/chat`, `/api/chat/session`, and the admin routes. The tool loop
  moves behind the `AgentRuntime` boundary in `ARCH-001` rather than being
  transcribed, so this slice is gated on that task.
- Repoint `Dockerfile` and `k8s/app.yaml` at `services/api` under `DEP-001`, then
  delete `server.py`. Until that lands, the deployed image still runs the
  prototype and still accepts `0001234567`.

- Completion notes: _Slice 1 complete; task stays `In Progress` until slice 2 ships and `server.py` is deleted._

### DATA-002 — Server-authoritative repositories and concurrency control

- Status: `Todo`
- Priority: `P0`
- Type: `Data/backend`
- Depends on: `DATA-001`, `API-001`
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
- Completion notes: _Pending._

### DATA-003 — Transactional, idempotent booking

- Status: `Todo`
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
- Completion notes: _Pending._

### SEC-001 — Admin authentication and tenant-scoped RBAC

- Status: `Todo`
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
- Completion notes: _Pending._

### SEC-002 — Secure visitor sessions and tenant binding

- Status: `Todo`
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
- Completion notes: _Pending._

### SEC-003 — API abuse protection, CORS, and response hardening

- Status: `Todo`
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
- Completion notes: _Pending._

### SEC-004 — Service authentication and Kubernetes network boundaries

- Status: `Todo`
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
- Completion notes: _Pending._

### SEC-005 — Secret management and credential removal

- Status: `Todo`
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
- Completion notes: _Pending._

### PRIV-001 — PII classification, consent, retention, export, and deletion

- Status: `Todo`
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
- Completion notes: _Pending._

### DEP-001 — Immutable, reproducible application images

- Status: `Todo`
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
- Completion notes: _Pending._

## P1 production core task details

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

- Status: `Todo`
- Priority: `P1`
- Type: `Reliability`
- Depends on: `API-001`, `DATA-002`, `DEP-001`
- Likely areas: service health routes, Kubernetes probes, application lifecycle hooks
- Scope:
  - Separate liveness, readiness, and startup checks.
  - Check required dependencies without turning transient optional failures into restart loops.
  - Warm the embedding model before readiness succeeds.
  - Drain traffic and finish or safely cancel in-flight work during shutdown.
- Acceptance criteria:
  - No service receives traffic before it can fulfill its required contract.
  - Liveness does not depend on remote systems.
  - Rolling updates do not lose accepted messages or actions.
- Verification:
  - Probe and rolling-restart tests pass in Kubernetes.
- Completion notes: _Pending._

### REL-003 — Durable background jobs and retry handling

- Status: `Todo`
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
- Completion notes: _Pending._

### OBS-001 — Structured logging and request correlation

- Status: `Todo`
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
- Completion notes: _Pending._

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
  - Measure booking, lead, handoff, CRM-delivery, and conversion outcomes.
- Acceptance criteria:
  - Metrics remain correct across replicas and restarts.
  - No session IDs, user IDs, free text, or PII are metric labels.
  - Each critical action has success, failure, and latency metrics.
- Verification:
  - Metric contract tests and sample Prometheus queries are documented.
- Completion notes: _Pending._

### OBS-003 — Dashboards, SLOs, and alerts as code

- Status: `Todo`
- Priority: `P1`
- Type: `Operations`
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

### ARCH-001 — Agent runtime boundary and LangGraph adoption

- Status: `Todo`
- Priority: `P1`
- Type: `Architecture/agent platform`
- Depends on: `QA-001`, `API-001`, `DATA-002`
- Likely areas: backend orchestration package, agent runtime adapters, architecture decision records, `architecture/likec4/`
- Scope:
  - Define a framework-neutral `AgentRuntime` protocol with typed turn input, structured output, tool events, usage, cancellation, and error semantics.
  - Move the current custom tool loop behind a `SimpleAgentRuntime` adapter without changing behavior.
  - Add a LangGraph v1 runtime adapter with a durable Postgres checkpointer for workflows that require pause/resume, branching, human approval, or recovery.
  - Keep domain tool interfaces and implementations free of LangChain/LangGraph types.
  - Document why LangChain v1 `create_agent` is optional, why `langchain-classic` is excluded, and when an OpenAI Agents SDK adapter would be appropriate.
  - Keep the LikeC4 architecture model synchronized with the implementation boundary.
- Acceptance criteria:
  - The simple and LangGraph adapters pass the same runtime contract suite for shared capabilities.
  - Ordinary one-turn chat can use the simple adapter without graph/checkpoint overhead.
  - A persisted LangGraph test workflow pauses, survives process restart, resumes, and completes without repeating a committed domain action.
  - Framework checkpoint records can be deleted and rebuilt without deleting authoritative conversations, bookings, leads, or handoffs.
  - Graph nodes invoke side effects only through idempotent domain services with explicit idempotency keys.
  - No API, repository, or domain-service public contract imports LangChain or LangGraph classes.
- Verification:
  - Run runtime contract tests, a restart/resume integration test, an idempotent replay test, and `npm --prefix architecture/likec4 run validate`.
- Completion notes: _Pending._

### AI-001 — Provider and model abstraction

- Status: `Todo`
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
- Completion notes: _Pending._

### AI-002 — Model safety, quotas, and cost controls

- Status: `Todo`
- Priority: `P1`
- Type: `AI safety/operations`
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

### RAG-001 — Versioned knowledge content model

- Status: `Todo`
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
- Completion notes: _Pending._

### RAG-002 — Secure asynchronous ingestion lifecycle

- Status: `Todo`
- Priority: `P1`
- Type: `RAG/workflow`
- Depends on: `RAG-001`, `REL-003`, `SEC-004`
- Likely areas: ingestion API/worker, storage adapter, search indexing
- Scope:
  - Remove caller-controlled filesystem paths.
  - Accept authorized source IDs or validated uploads into isolated object storage.
  - Run parsing, scanning, chunking, embedding, and indexing as observable background jobs.
  - Deactivate stale chunks and clean up partial failed indexes.
- Acceptance criteria:
  - A caller cannot read arbitrary container files or ingest another tenant's content.
  - Re-ingesting unchanged content is idempotent.
  - Failed jobs expose safe status and can retry without duplicate active chunks.
- Verification:
  - Security and lifecycle tests cover path traversal, cross-tenant IDs, duplicate ingestion, and mid-index failure.
- Completion notes: _Pending._

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

### RAG-008 — RAG evaluation and regression suite

- Status: `Todo`
- Priority: `P1`
- Type: `AI quality`
- Depends on: `RAG-004`, `RAG-005`, `RAG-007`
- Likely areas: `evals/`, fixtures, CI workflow, evaluation reports
- Scope:
  - Create versioned datasets for retrieval recall, grounded answer correctness, citation precision, refusal, tenant isolation, and multi-turn behavior.
  - Include financing policy edge cases and adversarial documents.
  - Compare prompt, retriever, embedding, reranker, and model versions.
  - Define release thresholds and a reviewed exception process.
- Acceptance criteria:
  - Evaluation runs are deterministic where possible and publish comparable reports.
  - CI blocks statistically or materially significant regressions below thresholds.
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
  - Implement stateful routing and interrupts as a versioned LangGraph graph behind the `AgentRuntime` boundary.
  - Persist active workflow, collected fields, pending confirmation, tool results, and next allowed actions.
  - Register specialized agents with explicit input/output schemas and deterministic tool allowlists.
  - Support pause, resume, cancel, handoff, failure recovery, and topic switching.
- Acceptance criteria:
  - The same user message routes consistently under a versioned router policy.
  - A specialized agent cannot call tools outside its allowlist.
  - Workflow recovery after restart does not repeat committed actions.
  - Low-confidence or conflicting intent asks a clarification or hands off safely.
- Verification:
  - State-machine tests cover happy paths, interruptions, retries, topic changes, and invalid transitions.
- Completion notes: _Pending._

### DEP-002 — Kubernetes workload hardening

- Status: `Todo`
- Priority: `P1`
- Type: `Infrastructure`
- Depends on: `DEP-001`, `SEC-004`, `REL-002`
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
- Priority: `P1`
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
- Completion notes: _Pending._

### DEP-004 — High availability and autoscaling

- Status: `Todo`
- Priority: `P1`
- Type: `Infrastructure/reliability`
- Depends on: `DATA-002`, `REL-002`, `DEP-002`
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
- Priority: `P1`
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
- Priority: `P1`
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

- Status: `Todo`
- Priority: `P1`
- Type: `Quality`
- Depends on: `API-001`, `DATA-002`
- Likely areas: `tests/integration/`
- Scope:
  - Cover every API route against real Postgres and replaceable fake providers.
  - Test transactions, migrations, error contracts, pagination, ordering, and concurrency.
- Acceptance criteria:
  - Tests run hermetically in CI and clean up their data.
  - Success and failure contracts are asserted, not only HTTP status codes.
- Verification:
  - Run the integration suite repeatedly and in randomized order.
- Completion notes: _Pending._

### QA-003 — Tenant-isolation and security regression tests

- Status: `Todo`
- Priority: `P1`
- Type: `Security quality`
- Depends on: `SEC-001` through `SEC-005`, `PRIV-001`
- Likely areas: `tests/security/`
- Scope:
  - Maintain automated cross-tenant, session-hijack, RBAC, CORS, rate-limit, path-traversal, prompt-injection, and PII-leak tests.
- Acceptance criteria:
  - Every formerly identified P0 exploit has a regression test.
  - Security tests run in CI and against staging before release.
- Verification:
  - Run the suite with two or more tenants and principals of every role.
- Completion notes: _Pending._

### QA-004 — End-to-end business workflow tests

- Status: `Todo`
- Priority: `P1`
- Type: `Quality`
- Depends on: `FEAT-002` through `FEAT-005`, `AGENT-001`
- Likely areas: browser/API E2E tests and provider sandboxes
- Scope:
  - Test visitor-to-booking, visitor-to-lead, financing-to-follow-up, and visitor-to-human-handoff journeys.
  - Cover staff takeover, notifications, external delivery, retry, and final outcome state.
- Acceptance criteria:
  - Tests assert both UI behavior and durable/external side effects.
  - Sandbox provider failures and duplicate callbacks are covered.
- Verification:
  - Run in CI or a disposable staging environment.
- Completion notes: _Pending._

### QA-005 — Load, soak, and failure-injection tests

- Status: `Todo`
- Priority: `P1`
- Type: `Performance/reliability quality`
- Depends on: `REL-001` through `REL-003`, `DEP-004`
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
- Acceptance criteria:
  - Tenant admins can manage only their tenant's sources.
  - Draft content never affects answers before approval.
  - Every mutation is audited and recoverable where appropriate.
- Verification:
  - End-to-end test uploads, approves, publishes, queries, supersedes, and deletes a document.
- Completion notes: _Pending._

### FEAT-002 — Real availability and calendar integration

- Status: `Todo`
- Priority: `P1`
- Type: `Feature/integration`
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
- Priority: `P1`
- Type: `Feature/integration`
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
- Priority: `P1`
- Type: `Feature/workflow`
- Depends on: `SEC-001`, `DATA-002`, `AGENT-001`, `FEAT-010`
- Likely areas: handoff tables/service, admin UI, visitor message channel
- Scope:
  - Add queue, priority, reason, summary, assignment, presence, SLA, accept, takeover, release, and resolution states.
  - Pause automated replies while a staff member owns the conversation.
  - Notify the visitor of queue/takeover/resolution state without exposing staff internals.
- Acceptance criteria:
  - Only one staff owner can hold a conversation at a time.
  - Automated agents cannot reply during active takeover unless explicitly invited.
  - Every assignment and message is tenant-scoped and audited.
- Verification:
  - Multi-user E2E tests cover race-to-accept, disconnect, reassignment, and resolution.
- Completion notes: _Pending._

### FEAT-005 — Notification and outbound webhook workflow

- Status: `Todo`
- Priority: `P1`
- Type: `Feature/integration`
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
- Priority: `P1`
- Type: `Feature/platform`
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
- Priority: `P2`
- Type: `Feature/AI quality`
- Depends on: `DATA-002`, `RAG-005`, `SEC-001`
- Likely areas: widget feedback UI, review queue, evaluation dataset tooling
- Scope:
  - Add thumbs up/down, optional reason, staff review state, corrected answer, and links to prompt/model/retrieval versions.
  - Permit approved, anonymized examples to become evaluation cases.
- Acceptance criteria:
  - Feedback cannot expose another conversation or alter production prompts directly.
  - Review decisions are audited and preserve original answer/evidence.
  - Dataset promotion applies privacy checks.
- Verification:
  - E2E test covers feedback, review, correction, and safe evaluation promotion.
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
- Priority: `P1`
- Type: `Feature/frontend/backend`
- Depends on: `API-001`, `DATA-002`, `SEC-002`, `REL-001`
- Likely areas: chat transport, widget state, admin live updates
- Scope:
  - Add authenticated SSE or WebSocket delivery for model tokens, message commits, handoff changes, and staff replies.
  - Add cancel, reconnect, resume cursor, delivery acknowledgements, and duplicate suppression.
  - Persist only complete or explicitly cancelled message states.
- Acceptance criteria:
  - Refresh/reconnect does not duplicate or lose committed messages.
  - Cancellation stops downstream generation where supported and never rolls back committed actions.
  - Staff replies arrive without two-second polling.
- Verification:
  - Browser tests cover network interruption, reconnect, duplicate event, cancel, and takeover.
- Completion notes: _Pending._

### FEAT-011 — Customer-facing citations and source viewer

- Status: `Todo`
- Priority: `P1`
- Type: `Feature/RAG UX`
- Depends on: `RAG-005`, `FEAT-010`
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

- Status: `Todo`
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
- Completion notes: _Pending._

### FEAT-014 — Additional business-domain agents

- Status: `Todo`
- Priority: `P2`
- Type: `Feature/agents`
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

## Recommended dispatch sequence

This sequence reduces merge conflicts and prevents agents from building features on insecure foundations.

### Wave 1 — Parallel foundation work

- Agent A: `QA-001`
- Agent B: `DATA-001` after the initial `QA-001` harness is available
- Agent C: `SEC-005`
- Agent D: `DEP-001`
- `SEC-004` can run in parallel if no active agent is editing the same Kubernetes sections.

### Wave 2 — Core backend conversion

Run these mostly sequentially because they substantially overlap the current `server.py`:

1. `API-001`
2. `DATA-002`
3. `SEC-001` and `SEC-002`
4. `SEC-003`
5. `DATA-003`
6. `PRIV-001`

### Wave 3 — Parallel platform tracks

- RAG track: `RAG-001` → `RAG-002` → `RAG-003` → `RAG-004` → `RAG-005` → `RAG-007` → `RAG-008`
- Agent track: `ARCH-001` → `AI-001` → `AGENT-001` → `RAG-006`
- Reliability track: `REL-001` → `REL-002` and `REL-003`
- Infrastructure track: `DEP-002` → `DEP-003` and `DEP-004` → `DEP-005` and `DEP-006`
- Observability track: `OBS-001` → `OBS-002` → `OBS-003`

### Wave 4 — Business workflows

- `FEAT-001`, `FEAT-002`, `FEAT-003`, `FEAT-005`, and `FEAT-006` can be developed in parallel after their dependencies.
- Implement `FEAT-010` before `FEAT-004` so handoff uses the durable real-time transport.
- Implement `FEAT-011` after the citation contract is stable.

### Wave 5 — Validation and product maturity

- `QA-002` and `QA-003` should grow continuously, then become release gates.
- Complete `QA-004` after core business workflows.
- Complete `QA-005` after HA and resilience work.
- Finish the `P2` feature tasks based on demo narrative and user feedback.

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
