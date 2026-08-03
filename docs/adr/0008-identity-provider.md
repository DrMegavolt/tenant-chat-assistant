# 0008 — Keycloak as the identity provider, with split browser and backchannel URLs

- **Status:** Accepted
- **Date:** 2026-08-02
- **Extends:** `ADR-0007`
- **Affects:** `SEC-001`, `DEP-003`

## Context

ADR-0007 put an oauth2-proxy sidecar in front of the admin routes and left the
OIDC provider itself out of scope: `oidc-credentials` was a Secret someone
filled in with an issuer URL, a client ID, and a client secret. There was no
provider to fill it in from, so the auth path could not be exercised end to end
outside unit tests. A local deployment could start, and every admin login would
fail at the first redirect.

Choosing a provider forces a second decision. OIDC has two classes of endpoint:

- **Browser-facing** — the authorization endpoint the user is redirected to.
  It must be an address the user's machine can reach and whose certificate the
  user's browser trusts.
- **Backchannel** — code redemption, JWKS, and userinfo, called by
  oauth2-proxy. It must be an address the *cluster* can reach and whose
  certificate the *pod* trusts.

On a local cluster those are not the same address. The public host resolves to
the ingress load balancer and is served with a certificate the browser was told
to trust; inside the cluster, that name may not resolve at all, and the pod's
trust store does not contain the local CA.

## Decision

**Run Keycloak in a dedicated `identity` namespace, deployed by a first-party
Helm chart. Fix Keycloak's issued URLs to the public hostname, and configure
oauth2-proxy with discovery disabled so its browser-facing and backchannel
endpoints can differ.**

### Issuer stability

Keycloak derives every URL it issues, including the `iss` claim, from the
request host unless told otherwise. A token redeemed over the in-cluster
Service would then carry `iss: http://keycloak.identity.svc:8080/realms/...`,
which oauth2-proxy compares against its configured issuer and rejects.

`KC_HOSTNAME` set to the public URL, with `KC_HOSTNAME_BACKCHANNEL_DYNAMIC=false`,
makes the issuer constant regardless of which address was dialed.

### Endpoint split

oauth2-proxy runs with `--skip-oidc-discovery` and explicit endpoints:

| Setting | Address | Reached by |
|---------|---------|-----------|
| `oidc-issuer-url` | public | compared against `iss` |
| `login-url` | public | browser redirect |
| `redirect-url` | public (gateway) | browser return |
| `redeem-url` | in-cluster Service | oauth2-proxy |
| `oidc-jwks-url` | in-cluster Service | oauth2-proxy |
| `profile-url` | in-cluster Service | oauth2-proxy |

The three backchannel URLs are plain HTTP inside the cluster, constrained by a
NetworkPolicy that admits only oauth2-proxy pods. The public URLs stay HTTPS.

### Realm shape

The `tenantchat` realm defines the four groups ADR-0007 named — `viewer`,
`support_agent`, `tenant_admin`, `platform_admin` — and a `groups` client scope
whose mapper writes group names into the ID token, access token, and userinfo
response. This is the claim oauth2-proxy forwards as `X-Auth-Request-Groups`
and nginx maps to a single role.

The confidential client requires PKCE with S256. With discovery disabled the
method cannot be negotiated, so oauth2-proxy states it explicitly.

### Credential handling

Realm import runs from a ConfigMap, which anything with namespace read access
can read, so the realm definition carries no credential. A post-install Job
applies the two credential-bearing parts with `kcadm`: the client secret, and
the first operator account. Both come from Secrets provisioned out of band,
matching the contract the tracked manifests already use.

## Consequences

**Gained.** The whole auth path — login, callback, session, group claim, role
mapping, Python authorization — runs on a laptop cluster with no external
identity provider and no account at a hosted one.

**Gained.** The backchannel never leaves the cluster, so token redemption does
not depend on split-horizon DNS, on the ingress being up, or on the pod
trusting a local CA.

**Gained.** Keycloak is a standards-conformant provider. The gateway is not
coupled to it: swapping in a hosted provider means changing the five values in
`oidc-endpoints` and the `oidc-credentials` Secret, with no manifest change.

**Cost.** Discovery is disabled, so five endpoint URLs are configuration rather
than something fetched. A provider that moves an endpoint breaks login until
the ConfigMap is updated. This is the direct price of the split.

**Cost.** Another stateful service and its database. Keycloak wants ~1 GiB of
memory, which is real on a laptop cluster.

**Cost.** Realm import is first-install only. Later realm changes are applied
through the console or `kcadm`, so the realm JSON is a starting point rather
than a continuously reconciled desired state.

**Constraint.** The realm's group names are a contract with the nginx
group-to-role map in `frontend/nginx/entrypoint.sh`. Renaming one there without
the other authenticates a user and then denies them every route.

## Alternatives considered

**One URL for both browser and backchannel.** Rejected for local use. It needs
the cluster to resolve the public host to the ingress and to trust its
certificate — split-horizon DNS plus a CA bundle mounted into oauth2-proxy — to
erase a distinction OIDC already accommodates. It remains the simpler choice
where the provider is genuinely external and publicly resolvable, which is why
the external-HTTPS egress rule for oauth2-proxy is kept.

**A hosted provider (Auth0, Okta, Google) for local development.** Rejected.
It makes a laptop deployment depend on an internet account, an externally
reachable callback URL, and someone else's rate limits, and it cannot be
exercised offline or in CI.

**The upstream Keycloak or Bitnami chart.** Rejected. Both are configured
through a values schema wider than what is needed here, and expressing the
digest pinning, default-deny policies, and out-of-band Secret contract through
someone else's templates costs more than the first-party chart. Revisit if
Keycloak becomes a production dependency rather than a local one.

**Dex or a smaller OIDC provider.** Reasonable, and lighter. Rejected because
Keycloak has the group and role modeling that `SEC-001`'s tenant-scoped RBAC
will need, and switching later costs more than the memory saved now.

**Traefik's OIDC middleware instead of oauth2-proxy.** Still deferred, as in
ADR-0007. It would remove oauth2-proxy, not Keycloak, so it is orthogonal to
this decision.
