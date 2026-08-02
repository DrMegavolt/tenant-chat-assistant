# 0006 — nginx serves the frontend; the backend serves only the API

- **Status:** Superseded by ADR-0007 (single-origin gateway with OIDC auth)
- **Date:** 2026-08-02
- **Superseded by:** `ADR-0007`
- **Affects:** `DEP-003`, `SEC-003`, `API-001`, `DEP-001`

## Context

`server.py` served `frontend/public/` from its own request handler with a
hand-rolled path check, and that handler was the only thing in front of the
widget in every environment: local development, docker compose, and Kubernetes.
Three problems follow from it.

The static handler is prototype code on the deletion path in `API-001`, so
`services/api` would have to grow a second static handler purely to keep the
demo page alive. Serving assets is not a thing the API should learn to do.

Cache policy, compression, and response headers had nowhere to live. The
prototype sends `Cache-Control: no-store` on every asset and no security header
at all, so the widget cannot be cached and a `Content-Security-Policy` has no
owner.

Local frontend work required restarting a Python process to see a changed line
of JavaScript, because the file was read through that handler on every request.

## Decision

Add a sixth image, `web`: nginx with `frontend/public/` baked in, one document
root per audience, and a config rendered at container start from two upstream
origins. It is the public entrypoint in Kubernetes and in docker compose; the
chat backend keeps only the API and is no longer reachable from the ingress
controller.

The public listener (8080) serves the embed assets and forwards exactly four
upstream paths — `/api/tenants`, `/api/chat`, `/api/chat/session`, `/api/book` —
with every other `/api/` path answered `404` at the edge. The operator console
listener (8081) has its own document root and forwards the admin routes to the
backend's separate admin port. `scripts/verify_image_contracts.py` fails the
build if the public root gains an admin asset or the public listener gains an
upstream path, and `tests/test_web_gateway.py` asserts the edge allowlist equals
the backend's own `_PUBLIC_*_PATHS`.

Frontend development runs on Vite (`make dev`), which serves the same unbundled
modules the image serves, hot-reloads them, and proxies `/api` to whichever
backend is running locally.

## Consequences

The widget is same-origin with the API in every environment, so the demo path
exercises no CORS behavior that production does not have.

Assets and API now version independently: rolling the `web` image forward or
back does not touch the backend, which is what `DEP-003` needs for embeds that
must keep working across a widget release.

Two roots and two listeners mean the console cannot be published by a single
mistake — a wrong proxy rule, a wrong document root, or a wrong ingress path
each fails to reach it on its own. Authentication in front of the console is
still `SEC-001`; nothing here makes the admin routes safe to expose.

`style-src` carries `'unsafe-inline'`, because the widget writes its shadow-root
stylesheet as a `<style>` element. Removing it means moving the widget to
constructed stylesheets, which is a frontend change rather than a gateway one.

Asset filenames are not content-hashed yet, so cacheable responses use a short
`max-age` with revalidation. Long-lived immutable asset URLs are `DEP-003`.

## Alternatives considered

**Keep serving assets from the application.** Free today, but it puts a static
file server inside `services/api`, and every cache and security header becomes
Python code with its own tests. The API would also have to grow a second listener
to keep console assets off the public port.

**A CDN or object store in front of the assets.** The right end state for a real
deployment, and compatible with this decision — the `web` image can be replaced
by a bucket sync without changing the widget. It is not useful for a local demo
that must run without cloud accounts, and it does not solve the API proxying
that keeps the widget same-origin.

**Templating the whole config into a ConfigMap.** Rejected under `DEP-001`:
configuration that decides which upstream a public listener forwards to belongs
with the reviewed image, not with cluster state that can be edited in place.
