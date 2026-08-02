# Tenant Chat Target Architecture

This directory is the architecture-as-code source for the proposed production end state. It models the accepted agent approach from ADR-0001:

- Framework-independent typed domain services own authorization, policy, transactions, idempotency, and business records.
- LangGraph v1 is the single branching, checkpointed, human-in-the-loop runtime; its checkpoint data is not the business system of record.
- Postgres and a durable outbox/worker layer own committed application state and external delivery.
- MCP is an optional interoperability boundary, not a replacement for domain validation.

## Views

- `index`: system context and external dependencies.
- `platform_overview`: logical platform containers and data stores.
- `agent_runtime`: agent runtime, router, specialized agents, and deterministic tool boundary.
- `knowledge_pipeline`: governed ingestion, retrieval, evidence, and citation flow.
- `business_actions`: transactional tools, outbox workers, and external integrations.
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
