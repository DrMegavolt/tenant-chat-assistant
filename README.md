# Tenant Chat Assistant Prototype

This is a prototype for an embeddable website chat widget with a Python backend.

The production backend is being built alongside it in `services/api`, which
already serves tenant configuration, availability, booking, and lead capture with
the domain rules in `packages/core`. See `BACKLOG.md` (`API-001`) for what remains
before `server.py` is retired.

```bash
make api      # services/api on http://127.0.0.1:8080
make dev      # frontend with hot reload on http://127.0.0.1:5173
make setup    # install locked Python and frontend development dependencies
make check    # complete Python + JavaScript quality gate with coverage
make test-database # isolated PostgreSQL migrations and repository concurrency
```

`make check` runs without live services. Frontend tests use a DOM environment
and deterministic fake API responses; Python unit and API contract tests use
in-memory stores and fakes. Coverage reports are written below `coverage/`, and
JUnit test results below `artifacts/test-results/`.

Run the prototype backend locally:

```bash
python3 server.py
```

Then serve the frontend against it. For frontend work, use the dev server: it
hot-reloads `frontend/src/` and proxies `/api` to the backend on port 8000, so
the browser stays same-origin exactly as it is behind nginx.

```bash
make dev
```

```text
http://127.0.0.1:5173        the demo site
http://127.0.0.1:5173/admin/ the operator console
```

Point the proxy somewhere else — `services/api`, or a port-forwarded cluster —
with `CHAT_DEV_BACKEND_ORIGIN`:

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

`server.py` still serves the built frontend on port 8000 for convenience, so
run `make js-build` once before using it; the prototype image no longer contains
those files. In a deployment the `web` image serves them and proxies the API
back to the chat backend. See
[ADR-0007](docs/adr/0007-single-origin-gateway.md) and
[ADR-0009](docs/adr/0009-react-frontend-build.md).

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
LLM_BASE_URL=http://localhost:1234/v1 LLM_MODEL=local-model python3 server.py
```

Switch between the two configured companies:

- Company A: answers contact, address, hours, and service-area questions, but never shares pricing and does not book through chat.
- Company B: answers from fixed pricing, checks ZIP-code service area, separates availability by service category, and books a selected slot after confirmation.
- Both companies can capture follow-up leads after collecting name, contact, service, and request details.
- Both companies can politely offer callback capture after buying intent, but the assistant should not imply it can call unless the visitor provides contact info.
- Booking-enabled companies show a compact booking form after availability is checked: slot, name, address, and phone/email.

The backend owns the tenant policy and tool calls:

- Tenant configuration: allowed services, pricing policy, booking policy, phone, address, hours, escalation rules.
- Retrieval: approved company knowledge base for FAQs.
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
| `GET /healthz` | Liveness. Dependency-aware readiness is `REL-002`. |
| `GET /api/tenants` | Public tenant configuration, projected from `PublicTenantView`. |
| `GET /api/tenants/{id}/availability?service=` | Slots currently offered, and the list booking validates against. |
| `POST /api/book` | Books an offered slot. |
| `POST /api/leads` | Captures a callback request. |

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
See `docs/runbooks/database-migrations.md` before migrating a database used by
the JSONB-snapshot prototype or attempting a downgrade.

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
kubectl -n llm-chat port-forward svc/web 18080:80
kubectl -n llm-chat port-forward svc/web-admin 18081:8081   # operator console
```

To point a locally served frontend at a port-forwarded backend instead, forward
the API itself and configure the API base with one of these options:

```bash
kubectl -n llm-chat port-forward svc/chat-backend 18080:8000
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

Inspect captured prototype leads:

```text
http://127.0.0.1:8000/api/leads
```

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
