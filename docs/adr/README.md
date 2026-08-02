# Architecture Decision Records

Each record captures one decision, the context that forced it, and what it costs.
They are immutable once accepted: a decision that changes gets a new record that
supersedes the old one, so the reasoning trail survives.

Format is [MADR](https://adr.github.io/madr/)-shaped: Context, Decision,
Consequences, Alternatives considered.

| ID | Title | Status |
|----|-------|--------|
| [0001](0001-agent-runtime.md) | Single LangGraph agent runtime over a framework-free domain | Accepted |
| [0002](0002-backend-stack-and-layout.md) | Backend stack and repository layout | Accepted |
| [0003](0003-retrieval-store.md) | Elasticsearch as the retrieval store | Accepted |
| [0004](0004-model-provider.md) | Local OpenAI-compatible model provider by default | Accepted |
| [0005](0005-conversation-persistence.md) | Tenant-qualified append-only conversation persistence | Accepted |

## Relationship to BACKLOG.md

`BACKLOG.md` carries an earlier inline decision, `ADR-001`, covering the agent
framework. [0001](0001-agent-runtime.md) supersedes it and narrows the scope; the
backlog entry is kept for history. Where the two disagree, the record here wins.
