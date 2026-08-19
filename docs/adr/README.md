# Architecture Decision Records

Architecture Decision Records (ADRs) explain choices that are difficult to infer
from the code alone. They record the problem, the chosen approach, and its main
trade-offs. They are not implementation plans or status reports.

| ID | Decision | Status |
| --- | --- | --- |
| [0001](0001-agent-runtime.md) | LangGraph runtime over a framework-free domain | Accepted |
| [0002](0002-backend-stack-and-layout.md) | Backend stack and repository layout | Accepted |
| [0003](0003-retrieval-store.md) | Elasticsearch as the retrieval store | Accepted |
| [0004](0004-model-provider.md) | Local OpenAI-compatible model provider by default | Accepted |
| [0005](0005-conversation-persistence.md) | Tenant-qualified append-only conversations | Accepted |
| [0006](0006-frontend-delivery.md) | nginx serves the frontend | Superseded by 0007 |
| [0007](0007-single-origin-gateway.md) | Single-origin gateway with OIDC | Accepted |
| [0008](0008-identity-provider.md) | Keycloak with separate browser and backchannel URLs | Accepted |
| [0009](0009-react-frontend-build.md) | React build with a self-contained embed | Accepted |
| [0010](0010-telemetry-planes.md) | Separate operational and inference telemetry | Accepted |
| [0011](0011-postgres-durable-jobs.md) | PostgreSQL outbox with at-least-once delivery | Accepted |

A changed decision gets a new ADR that supersedes the old one. A superseded
record remains here because it explains the transition.
