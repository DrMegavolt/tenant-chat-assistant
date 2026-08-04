# Tenant Chat Target Architecture

This directory is the architecture-as-code source for the proposed production end
state. It is a *target* model, not a picture of `main` — most of it is the plan in
[`BACKLOG.md`](../../BACKLOG.md), not shipped code.

It models the decisions in [`docs/adr/`](../../docs/adr/):

- Framework-independent typed domain services own authorization, policy,
  transactions, idempotency, and business records.
- LangGraph v1 is the **only** agent runtime, with no abstraction layer over
  agent frameworks ([ADR-0001](../../docs/adr/0001-agent-runtime.md)). Its
  checkpoints are not the business system of record.
- Postgres and a durable outbox/worker layer own committed application state and
  external delivery.
- Telemetry is split into two planes
  ([ADR-0010](../../docs/adr/0010-telemetry-planes.md)): a content-free
  operational plane, and an inference plane whose turn record — a first-party
  Postgres table, not a vendor — is the system of record for answer provenance.
- MCP is an optional interoperability boundary, not a replacement for domain
  validation.

## Views

- `index`: system context and external dependencies.
- `platform_overview`: logical platform containers and data stores.
- `agent_runtime`: the LangGraph runtime, router, prompt assembly, specialized agents, and the deterministic tool boundary. The dispatcher graph, the checkpoint adapter, and the action idempotency store are built (`ARCH-001`); the rest of this view is target state.
- `knowledge_pipeline`: governed ingestion, retrieval, evidence, and citation flow.
- `business_actions`: transactional tools, outbox workers, and external integrations.
- `answer_provenance`: the turn record, failure attribution, replay, evaluation, and the two telemetry planes.
- `production_deployment`: proposed Kubernetes and managed-data deployment.

## Commands

Run from this directory:

```bash
npm install
npm run format:check
npm run validate
npm run build
npm run export:png
```

Preview interactively:

```bash
npx likec4 serve
```

Generated PNG files are written to `diagrams/`. The generated static site is written to `dist/` and intentionally ignored because it can be rebuilt from the model.
