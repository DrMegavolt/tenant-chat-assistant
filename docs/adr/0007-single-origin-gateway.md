# 0007 — Single-origin gateway with OIDC

- **Status:** Accepted
- **Date:** 2026-08-02
- **Supersedes:** [ADR-0006](0006-frontend-delivery.md)

## Context

Separate public and admin listeners created two browser origins and treated an
unpublished port as an authentication boundary. Admin requests then needed CORS
or port forwarding, while a service configuration mistake could expose an
unauthenticated console.

## Decision

Use one nginx listener and one browser origin:

- Public assets and explicitly allowlisted visitor API paths remain anonymous.
- `/admin/` and `/api/admin/` require an oauth2-proxy authentication subrequest.
- `/oauth2/` is forwarded to oauth2-proxy for login, callback, and logout.
- Unknown `/api/` paths fail closed at nginx.
- Cross-origin widget requests use an explicit origin allowlist and the visitor
  credential header; admin routes never receive CORS headers.

nginx discards client-supplied identity headers, maps authenticated groups to the
application role vocabulary, and adds an internal gateway token. Python verifies
the token, authenticates the subject, and authorizes each operation again.

State-changing admin requests also require a subject-bound CSRF token. The
oauth2-proxy session cookie is Secure, HttpOnly, and SameSite=Lax.

Local Vite development reproduces route paths but does not replace identity.
Development auth may relax the gateway token only for an explicitly configured
loopback database; callers must still provide identity headers.

## Consequences

Public and admin applications share one origin without exposing admin CORS.
Authentication is structural, and authorization does not rely solely on trusted
proxy headers.

The deployment gains oauth2-proxy, OIDC configuration, a gateway secret, and a
CSRF secret. Login and role mapping must be exercised as part of deployment
verification.

## Alternatives considered

- **Keep two listeners and add authentication:** rejected because it preserves
  the cross-origin admin surface without adding useful isolation.
- **Implement browser OIDC sessions in Python:** rejected in favor of a focused,
  maintained proxy while Python retains authorization.
- **Use an API gateway with native OIDC:** a possible future simplification if
  it preserves the same defense-in-depth contract.
