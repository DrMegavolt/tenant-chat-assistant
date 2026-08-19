# 0008 — Keycloak with separate browser and backchannel URLs

- **Status:** Accepted
- **Date:** 2026-08-02
- **Extends:** [ADR-0007](0007-single-origin-gateway.md)

## Context

The local Kubernetes deployment needs a complete OIDC login path without a
hosted identity account. In a local cluster, the browser-facing issuer hostname
and the address reachable from oauth2-proxy are different. Routing every request
through the public hostname would require split-horizon DNS and distributing the
local certificate authority into the pod.

## Decision

Run Keycloak in a dedicated namespace. Fix its issuer to the public hostname,
disable oauth2-proxy discovery, and configure endpoints explicitly:

- Browser login and callback use the public HTTPS hostname.
- Token redemption, JWKS, and user information use the in-cluster service.
- Network policy limits the internal Keycloak endpoint to oauth2-proxy.

The realm publishes the four application groups used by the gateway:
`viewer`, `support_agent`, `tenant_admin`, and `platform_admin`. The confidential
client requires PKCE with S256.

Tracked realm configuration contains no credentials. A deployment job reads
Secrets to set the client secret and initial operator credentials.

## Consequences

The full login, group mapping, session, and authorization flow works locally
without an external identity account. Backchannel calls remain inside the
cluster while tokens keep a stable public issuer.

Discovery is intentionally disabled, so endpoint changes require configuration
updates. Keycloak also adds a stateful service and meaningful memory use. Realm
group names and nginx role mapping form a shared contract.

## Alternatives considered

- **Use one public URL everywhere:** simpler in hosted environments, but awkward
  for a local cluster with private DNS and certificates.
- **Use a hosted provider for development:** rejected because it prevents
  offline and account-free deployments.
- **Use a smaller OIDC provider:** possible, but Keycloak already supplies the
  group and role model required by the application.
