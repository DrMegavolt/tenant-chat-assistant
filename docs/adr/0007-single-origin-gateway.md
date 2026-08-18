# 0007 — Single-origin nginx gateway with OIDC auth

- **Status:** Accepted
- **Date:** 2026-08-02
- **Supersedes:** the two-listener model in `ADR-0006`
- **Affects:** `SEC-001`, `SEC-003`, `DEP-003`, `API-001`

## Context

ADR-0006 introduced an nginx `web` image with two listeners: a public port
(8080) serving the visitor frontend and visitor API, and a separate admin port
(8081) serving the operator console and admin API. The admin port was
deliberately not published by any ingress or LoadBalancer; reaching it required
`kubectl port-forward`. Authentication was deferred to `SEC-001`.

That model had three problems for production:

1. **Two origins.** The admin console and the public site had different base
   URLs, so the admin frontend could not share cookies or make same-origin API
   calls. Every admin API request was cross-origin relative to the admin page,
   requiring CORS on the admin API — exactly the surface SEC-003 wants to
   avoid for admin routes.

2. **Port-based isolation is fragile.** The admin listener was "internal"
   only because no ingress published it. A misconfigured Service, a new
   LoadBalancer, or a future port-forward convention would expose it with
   no authentication.

3. **No auth path.** The admin routes had no authentication at all, so the
   "internal" listener was the entire security boundary — and port-based
   isolation is not authentication.

## Decision

**Replace the two-listener model with a single public nginx listener and one
browser origin. Authentication is provided by an oauth2-proxy sidecar; Python
re-enforces authorization independently.**

### Architecture

```
Internet → nginx (8080) ──┬── /               → /srv/public (public frontend)
                          ├── /admin/          → /srv/admin (auth-gated)
                          ├── /api/tenants     → chat-admin:8004 (public API)
                          ├── /api/chat        → chat-admin:8004
                          ├── /api/chat/session→ chat-admin:8004
                          ├── /api/book        → chat-admin:8004
                          ├── /api/leads       → chat-admin:8004 (public write)
                          ├── /api/admin/...   → chat-admin:8004 (auth-gated)
                          ├── /api/            → 404 (fail closed)
                          └── /oauth2/*        → oauth2-proxy:4180
                                                     ↑
                          auth_request ────────→ oauth2-proxy:4180/oauth2/auth
```

The wiring reflects the `API-001` cutover: the API image serves visitor and
admin routes on one port (8004), the `chat-backend` port-8000 alias Service is
gone, and `POST /api/leads` is a visitor write. The auth gate applies only to
`/admin/` and `/api/admin/` routes.

### Authentication flow

1. Browser requests `/admin/` → nginx `auth_request` calls oauth2-proxy's
   `/oauth2/auth` internal endpoint.
2. If the OIDC session cookie is valid, oauth2-proxy returns 202 with
   `X-Auth-Request-*` identity headers. nginx maps provider groups to the
   application role vocabulary, overwrites client-supplied identity headers,
   and attaches a shared internal gateway token before proxying to Python.
3. If the session is invalid, oauth2-proxy returns 401. nginx redirects browser
   requests to the proxy login flow; API requests receive a plain 401 JSON
   response (no redirect).
4. Python authenticates the nginx-to-backend hop with the independent gateway
   token, then validates the role and authorizes each operation.

### Authorization model

Four roles, ordered by privilege:

| Role | Can read | Can reply | Can manage |
|------|----------|-----------|------------|
| `viewer` | ✓ | ✗ | ✗ |
| `support_agent` | ✓ | ✓ | ✗ |
| `tenant_admin` | ✓ | ✓ | ✓ |
| `platform_admin` | ✓ | ✓ | ✓ |

Python enforces these independently via `_require_auth(handler, min_role)`.

### CSRF protection

State-changing admin operations (currently `POST /api/admin/chats/.../messages`)
require a synchronizer-style CSRF token:

1. The admin frontend fetches a token from `GET /api/admin/csrf-token` (which
   is itself auth-gated).
2. The token is an HMAC of the identity subject using `ADMIN_CSRF_SECRET`.
3. The client sends it as `X-CSRF-Token` on POST requests.
4. Python validates it with `hmac.compare_digest` (constant-time comparison).

This is defense in depth behind the `SameSite=Lax` auth cookie.

### Cookie security

The oauth2-proxy session cookie is configured with:
- `Secure` — HTTPS only.
- `HttpOnly` — not accessible to JavaScript.
- `SameSite=Lax` — allows the top-level callback from the OIDC provider while
  withholding the cookie from cross-site subrequests.

These properties prevent cookie theft via XSS and CSRF via cross-origin
submissions.

### Widget CORS

Third-party widget embeds call the visitor API cross-origin. The gateway
handles this with an explicit, tightly allowlisted CORS policy:

- Origins are listed in the `WIDGET_ALLOWED_ORIGINS` environment variable.
- The nginx `map` directive returns the origin only if it matches the
  allowlist; otherwise empty (deny).
- `Vary: Origin` is always emitted for cache correctness.
- `Access-Control-Allow-Credentials` is never set — the widget uses an explicit
  `X-Visitor-Credential` bearer header, not ambient browser cookies.
- Admin routes are never exposed through CORS.
- `OPTIONS` preflight returns 204 with explicit headers.

### What was removed

- The `web-admin` Service (port 8081) is gone.
- The admin listener (8081) is gone from the nginx config and the web
  Deployment.
- The `WEB_ADMIN_PORT` environment variable is gone.
- The separate `web-admin` Service in Kubernetes is replaced by the
  `oauth2-proxy` Service.

### Vite development

The Vite dev server reproduces production paths:
- `/` serves the public frontend.
- `/admin/` is accessible (Vite serves from the same root).
- All `/api` routes proxy to the single API listener (`127.0.0.1:8080` by
  default); there is no separate admin listener to target.
- Admin routes fail closed without the gateway's identity headers; there is no
  dev auth bypass.

## Consequences

**Gained.** One origin means the admin frontend and public frontend share the
same browser security context. Admin API calls are same-origin — no admin
CORS surface. The auth gate is structural (nginx `auth_request`), not
port-based.

**Gained.** Python re-enforces authorization and authenticates the internal
gateway hop, so spoofed identity headers alone cannot grant admin access.

**Gained.** Widget CORS is explicit and tested. A disallowed origin gets no
CORS headers; admin routes are never CORS-accessible.

**Cost.** An additional service (oauth2-proxy) and its OIDC provider
configuration. This is the standard, well-understood cost of adding real
authentication to nginx OSS.

**Cost.** The admin frontend must fetch and include a CSRF token, adding one
round trip per session.

**Constraint.** Production requires `ADMIN_CSRF_SECRET`, an independent
`ADMIN_GATEWAY_TOKEN`, provider groups matching the documented roles, and OIDC
provider credentials to be provisioned before deploy.

## Alternatives considered

**Keep the two-listener model and add auth to the admin port.** Rejected.
Two origins still force admin CORS, and port-based isolation adds no security
value once auth is in place. The single-origin model is simpler and has a
smaller attack surface.

**Use nginx Plus auth_request with JWT validation directly.** Rejected for
cost (nginx Plus is commercial) and for coupling identity validation to the
gateway rather than to a dedicated, well-tested proxy.

**Implement OIDC validation in Python instead of using a proxy.** Rejected.
Python should enforce authorization, but the gateway should handle the OIDC
redirect flow, session management, and token refresh. oauth2-proxy is a
mature, purpose-built tool for this; reimplementing it in the prototype
server would be prototype code on the deletion path.

**Use an API gateway with built-in OIDC (Kong, Traefik with OIDC plugin).**
Deferred. The current ingress is Traefik; a Traefik OIDC middleware could
replace oauth2-proxy. Kept as a future simplification once the auth flow is
proven with oauth2-proxy.
