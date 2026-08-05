# Tenant Chat Assistant

A multi-tenant RAG and agent platform for home-services dispatch: an embeddable
chat widget that answers from tenant-approved knowledge, books appointments,
captures leads, and escalates to a human.

It is built as a demonstration, which means the interesting part is not that it
answers questions — it is what happens around the answer.

**What it sets out to prove:**

1. **Grounded answers, with the receipts.** Documents are parsed, chunked,
   embedded, and retrieved under tenant and version filters. Answers cite the
   exact authorized source version or abstain and offer a human. A citation the
   model invented is rejected mechanically rather than discouraged by a prompt.
2. **Business actions that survive reality.** Booking, lead capture, and handoff
   are transactional, idempotent, and authorized by deterministic domain
   services — so a retry, a restart, or a replayed workflow node cannot
   double-book anyone.
3. **Answers you can debug.** Every turn is recorded with the routing decision
   and its rejected alternatives, the retrieval candidate set with per-stage
   scores, the exact assembled prompt, and each claim linked to its supporting
   chunk. When an answer is wrong, the record says *which stage* was wrong —
   stale source, retrieval miss, truncated context, or a genuinely ungrounded
   claim — rather than leaving "the model hallucinated" as the diagnosis.

The third is the one that is hard to fake, and it drives most of the
architecture.

**State of play.** `services/api` is the production backend and serves tenant
configuration, availability, booking, lead capture, the visitor chat surface,
and the operator console over the domain rules in `packages/core`.
`packages/orchestration` holds the LangGraph agent runtime (`ARCH-001`): a
versioned dispatcher graph that pauses for a customer to confirm a booking,
survives a process restart, and commits only through idempotent domain services.
Chat is served over that runtime, and a deployment answers turns once `AI-001`
supplies the model provider adapter — until then the chat routes report
themselves unavailable rather than guessing. The `DEP-001` cutover is shipped:
the deployed `api` image is `services/api`, the prototype `server.py` and its
image are gone, and the gateway forwards exactly the API's visitor routes. Claim
3 above is designed and specified, not yet built; the planning artifacts below
are where that work is defined.

## Planning and architecture artifacts

| Artifact | What it is |
| --- | --- |
| [`BACKLOG.md`](BACKLOG.md) | Every task with acceptance criteria and verification. Gate B is the target; Gate C is documented, not committed. |
| [`docs/adr/`](docs/adr/README.md) | Decision records — what was chosen, what it cost, what was rejected and why. |
| [`architecture/likec4/`](architecture/likec4/README.md) | Architecture-as-code for the target end state, with generated diagrams. |
| [`CLAUDE.md`](CLAUDE.md) | Working agreements and the invariants enforced by tests. |
| [`docs/runbooks/`](docs/runbooks/) | Operational procedures for migrations and container images. |

Decisions worth reading first: [ADR-0001](docs/adr/0001-agent-runtime.md)
(LangGraph as the single agent runtime over a framework-free domain) and
[ADR-0010](docs/adr/0010-telemetry-planes.md) (two telemetry planes, with the
turn record — not a vendor — as the system of record for answer provenance).

## Running it

```bash
make api      # services/api on http://127.0.0.1:8080
make dev      # frontend with hot reload on http://127.0.0.1:5173
make setup    # install locked Python and frontend development dependencies
make check    # complete Python + JavaScript quality gate with coverage
make test-database # isolated PostgreSQL migrations, repositories, durable workflows
```

`make check` runs without live services. Frontend tests use a DOM environment
and deterministic fake API responses; Python unit and API contract tests use
in-memory stores and fakes. Coverage reports are written below `coverage/`, and
JUnit test results below `artifacts/test-results/`.

Run the API locally:

```bash
make api
```

Then serve the frontend against it. For frontend work, use the dev server: it
hot-reloads `frontend/src/` and proxies `/api` to the API on port 8080, so the
browser stays same-origin exactly as it is behind nginx.

```bash
make dev
```

```text
http://127.0.0.1:5173        the demo site
http://127.0.0.1:5173/admin/ the operator console
```

Point the proxy somewhere else — a port-forwarded cluster, or a second API — with
`CHAT_DEV_BACKEND_ORIGIN`:

```bash
CHAT_DEV_BACKEND_ORIGIN=http://127.0.0.1:8080 make dev
```

To exercise the deployed shape instead — the nginx image, its cache and security
headers, and its public route allowlist — run the gateway from docker compose
against the same backend:

```bash
make web
```

```text
http://127.0.0.1:8080        the demo site
http://127.0.0.1:8080/admin/ the operator console, behind the gateway's auth
```

`make web` serves the built frontend from the `web` image and proxies the API
back to the chat backend. See
[ADR-0007](docs/adr/0007-single-origin-gateway.md) and
[ADR-0009](docs/adr/0009-react-frontend-build.md). In a deployment the same image
serves both document roots and the operator console sits behind the gateway's
auth.

Chat archives are saved as JSON files locally in:

```text
chats/
```

When `DATABASE_URL` is set, sessions are persisted in Postgres `chat_sessions.payload`
as JSONB instead.

By default, the backend calls an OpenAI-compatible local API at:

```text
http://localhost:1234/v1/chat/completions
```

You can override this with environment variables:

```bash
LLM_BASE_URL=http://localhost:1234/v1 LLM_MODEL=local-model make api
```

Two seed tenants exercise opposite policies, which is what makes tenant isolation
visible rather than asserted:

- **Apex Home Services** (`apex`) — a phone-first desk. Answers contact, address,
  hours, and service-area questions; never shares pricing; does not book through
  chat.
- **Clearview Property Care** (`clearview`) — answers from fixed pricing, checks
  ZIP-code service area, separates availability by service category, and books a
  selected slot after confirmation.

Both capture follow-up leads once name, contact, service, and request details are
collected, and both may offer callback capture after buying intent — without
implying a call is possible before the visitor provides contact details. A
booking-enabled tenant shows a compact form after availability is checked: slot,
name, address, and phone or email.

The backend owns the tenant policy and tool calls:

- Tenant configuration: allowed services, pricing policy, booking policy, phone, address, hours, escalation rules.
- Retrieval: the prototype's main chat path answers from tenant policy and tools, not from retrieval. Only the financing side-agent (`services/financing-agent`) queries the knowledge index today; putting governed retrieval on the main path is `RAG-004` through `RAG-006`.
- Tools: `check_service_area`, `get_availability`, `book_appointment`, `create_lead`, `handoff_to_human`.
- Guardrails: no pricing unless policy allows it, no booking unless policy allows it, human handoff for uncertainty or risky requests.
- Admin: live chat list, transcript view, lead/tool panels, and manual staff replies into a visitor chat.
- Booking form endpoint: `POST /api/book` validates the structured form and records the booking in the chat archive.
- Persistence: each local session is saved to `chats/<session-id>.json`; deployed sessions use Postgres JSONB when `DATABASE_URL` is configured.
- Outcomes: admin marks chats as active, abandoned, booked, lead, handoff, completed, or empty.

## The production API (`services/api`)

FastAPI, with every booking and lead rule in `packages/core` so the same checks
apply whether a request arrives from the booking form, a model tool call, or an
operator action.

| Route | Purpose |
| --- | --- |
| `GET /healthz` | Liveness. Dependency-aware readiness ships with each dependency, per the backlog's definition of done. |
| `GET /api/tenants` | Public tenant configuration, projected from `PublicTenantView`. |
| `GET /api/tenants/{id}/availability?service=` | Slots currently offered, and the list booking validates against. |
| `POST /api/book` | Books an offered slot. |
| `POST /api/leads` | Captures a callback request. |
| `POST /api/chat/session` | Opens a conversation and returns the server-issued ID. |
| `GET /api/chat/session/{id}?tenant_id=` | The transcript, and anything the conversation is waiting on. |
| `POST /api/chat` | Answers one visitor turn through the agent runtime. |
| `POST /api/chat/confirmation` | Approves or declines a booking the assistant proposed. |
| `GET /api/admin/chats?tenant_id=` | Operator console: conversations with a transcript, newest first. |
| `GET /api/admin/chats/{id}?tenant_id=` | One conversation in full. |
| `POST /api/admin/chats/{id}/messages` | A staff reply, stored as a person speaking. |
| `GET /api/admin/csrf-token` | The double-submit token a staff reply must echo. |

A booking proposed by the assistant is not committed when it is proposed. The
turn pauses, `POST /api/chat` returns `pending` instead of a reply, and nothing
is written until `POST /api/chat/confirmation` carries the customer's answer.

Admin routes require the identity headers the gateway injects plus the shared
`ADMIN_GATEWAY_TOKEN`, and staff replies additionally require the CSRF token.
Both values are required at startup, so a deployment missing one fails to boot
rather than rejecting every operator. They are never reachable cross-origin: the
`CHAT_API_ALLOWED_ORIGINS` allowlist covers the embedded widget only.

Failures return RFC 9457 Problem Details with a stable `code` a client can branch
on, plus typed members (`missingFields`, `offeredServices`, `offeredSlots`) so
recovery never requires parsing prose:

```json
{
  "type": "/problems/invalid_contact",
  "status": 422,
  "code": "invalid_contact",
  "detail": "Provide a valid email address or a complete 10-digit US phone number including the area code.",
  "requestId": "a9f019936ae44127ae33938efc917317"
}
```

Set `CHAT_API_DOCS_ENABLED=true` for the OpenAPI schema at `/docs`. It is off by
default: it names every field and error code the API accepts.

## Database schema

The normalized production schema is versioned under `services/api/migrations`.
It is upgraded as a release step with `DATABASE_MIGRATION_URL`; API startup never
creates or alters schema. The separate `DATABASE_URL` role has runtime DML only.
See `docs/runbooks/database-migrations.md` before migrating a database that held
the pre-cutover JSONB snapshots or attempting a downgrade.

Normal API composition requires `DATABASE_URL` and constructs bounded async
PostgreSQL repositories; process-memory stores exist only as explicitly injected
test doubles. Messages are server-appended under tenant-qualified session locks,
so all replicas observe one immutable, gap-free transcript order. Pool size,
overflow, checkout timeout, and recycle interval use the `CHAT_API_DATABASE_*`
settings documented in `.env.example`. See ADR-0005 for the concurrency decision.

## Running the frontend against a remote backend

The frontend only needs the chat backend API. For Kubernetes local testing:

`k8s/app.yaml` deliberately contains no credential values or private model
endpoint. Before running `k8s/deploy.sh`, provision the documented runtime
resources in `llm-chat` from an out-of-band source:

- `elastic-credentials`: `username`, `password`
- `postgres-credentials`: `username`, `password`, `database`, `databaseUrl`
- `postgres-migration-credentials`: schema-owner `databaseUrl` for the release Job
- `kibana-credentials`: `username`, `password`
- `llm-provider-credentials`: `apiKey`
- `llm-runtime` ConfigMap: `baseUrl`, `model`, `timeoutSeconds`

The placeholder-only examples, safe local provisioning workflow, production
secret-manager path, and mandatory rotation warning are in
[`k8s/README.md`](k8s/README.md). The deploy script fails before changing
workloads when a required resource or key is missing and never displays values.
It also requires a rendered application manifest whose six release image
contracts have been replaced with registry digests. See
[`docs/runbooks/container-images.md`](docs/runbooks/container-images.md) for the
locked build, non-root smoke, metadata, and scanning workflow.

For the full local MicroK8s sequence, install the Keycloak Helm chart first and
then run `./k8s/deploy.sh` as documented in [`k8s/README.md`](k8s/README.md).
That deploy applies the public `web-lb` and `keycloak-lb` MetalLB Services at
`192.168.1.180` and `192.168.1.181`; the HTTPS browser endpoints continue to use
the Traefik ingress hostname.

The deployed site is the `web` Service, which serves the assets and proxies the
visitor API to the backend:

```bash
kubectl -n llm-chat port-forward svc/web 18080:80     # demo site + visitor API
open http://127.0.0.1:18080/admin/                    # operator console
```

To point a locally served frontend at a port-forwarded backend instead, forward
the API itself and configure the API base with one of these options:

```bash
kubectl -n llm-chat port-forward svc/chat-admin 18080:8004
```

```html
<script>
  window.CHAT_API_BASE_URL = "http://127.0.0.1:18080";
</script>
<script type="module" src="embed.js"></script>
```

```html
<div
  id="tenant-chat"
  data-company-id="clearview"
  data-api-base-url="http://127.0.0.1:18080"
></div>
```

```html
<script type="module" src="embed.js" data-api-base-url="http://127.0.0.1:18080"></script>
```

The admin page supports the same global or script setting, plus `data-api-base-url`
on `<body>`.

Captured leads are written but not listable: reading them waits on the
tenant-scoped RBAC in `SEC-001`, and the API deliberately has no unauthenticated
read side. The operator console lists conversations today.

Useful lead-capture test message:

```text
Please have someone call me. My name is Sam Lee, my phone is 555-222-1919, I need HVAC help in 97205 this week.
```

Example embed shape:

```html
<div id="tenant-chat" data-company-id="clearview"></div>
<script type="module" src="https://your-domain.com/embed.js"></script>
```

The mount element carries everything the widget needs. `data-open="true"` starts
the panel expanded (the demo page does this; a real embed should not), and
`data-color-scheme="light"` or `"dark"` pins the scheme on a host page that is
not scheme-aware.

Serving the embed from a different origin than the customer's site requires that
origin in `WIDGET_ALLOWED_ORIGINS`, which the gateway turns into the CORS
allowlist for both the visitor API and `/embed.js` itself.

## The frontend

Everything the browser loads lives under `frontend/`, which is a self-contained
npm project: React 19 and TypeScript in `strict` mode, built by Vite. The
`make dev` and `make js-*` targets drive it; nothing at the repository root is
an npm package.

```text
frontend/index.html, admin.html   the two page shells
frontend/src/widget/              the embeddable widget; no host-page coupling
frontend/src/demo/                the stand-in customer site that embeds it
frontend/src/admin/               the operator console
frontend/src/embed/main.ts        the entry a customer site includes
frontend/tests/                   the Vitest suite
frontend/nginx/                   the gateway that serves all of it
frontend/Dockerfile               the `web` image, Node build stage included
frontend/vite.config.ts           three build passes and the dev server
```

`npm run build` (`make js-build`) runs three passes, because the deployment has
three audiences: the public page and the operator console are separate builds so
the two nginx document roots share no chunk, and `embed.js` is a third,
self-contained file whose name never changes — customer sites hard-code that
URL, and a module script's imports are fetched under CORS. See
[ADR-0009](docs/adr/0009-react-frontend-build.md).

The widget renders into a shadow root, so an embedding page can neither style
its internals nor collide with its element ids, and its own styles never escape.
It follows the visitor's `prefers-color-scheme`, or an explicit
`data-color-scheme` on the mount element. Accessibility is covered in
[`docs/accessibility.md`](docs/accessibility.md): automated axe, contrast, focus,
and consent checks run in `make check`, and the manual keyboard and
screen-reader list lives there too.
