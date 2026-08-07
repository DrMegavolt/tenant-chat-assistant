# tenantchat-keycloak

Keycloak as the OIDC provider for the single-origin gateway (ADR-0007), so the
whole auth path runs locally: browser → nginx → oauth2-proxy → Keycloak.

The chart installs Keycloak, an embedded Postgres, the `tenantchat` realm with
the four role groups the gateway understands, NetworkPolicies matching the
default-deny shape of `llm-chat`, and a post-install Job that applies the two
credential-bearing parts of the realm from Secrets plus the `groups` client
scope.

## Why the Job creates the `groups` scope

A realm representation that carries a `clientScopes` array is authoritative:
Keycloak assigns exactly that list and skips the built-in scopes it would
otherwise create. Declaring `groups` there therefore deleted `profile`, `email`,
`roles`, `web-origins`, and `basic` from the realm, and the gateway's
`openid profile email groups` request came back `invalid_scope` — the console
was unreachable for every user. Realm import has no additive mode for that key,
so the realm JSON omits it and the Job, which already holds admin credentials
for the client secret, creates the scope and attaches it to the client.

It is deliberately a first-party chart rather than a wrapper around an upstream
one: the digest pinning, default-deny policies, and out-of-band Secret contract
are the same rules the tracked manifests follow, and expressing them through
someone else's values schema costs more than the ~400 lines here.

## Why the URLs are split

Keycloak has to mint tokens whose `iss` a browser could also have reached, and
oauth2-proxy has to redeem codes and fetch JWKS without leaving the cluster.
Those pull in opposite directions.

`KC_HOSTNAME` fixes every URL Keycloak issues to the public one, and
`KC_HOSTNAME_BACKCHANNEL_DYNAMIC=false` keeps that true for requests arriving
on the in-cluster Service. `iss` is therefore constant no matter which address
was dialed. oauth2-proxy then runs with discovery off: its issuer and login URL
are public, its redeem/JWKS/userinfo URLs are the in-cluster Service.

The alternative — one URL everywhere — needs the cluster to resolve the public
host to the ingress and to trust its certificate. On a local MicroK8s that
means split-horizon DNS and a CA bundle in the oauth2-proxy pod, to remove a
distinction the OIDC spec already accounts for. See ADR-0008.

## Prerequisites

- An ingress controller terminating TLS for the Keycloak host. The gateway
  origin must be HTTPS too: oauth2-proxy sets `Secure` on its session cookie,
  and a browser will not return that cookie over plain HTTP.
- The `identity` namespace labelled with `kubernetes.io/metadata.name: identity`
  (Kubernetes applies this automatically).
- The four Secrets below.

## Secrets

Create them from the placeholders in `k8s/examples/`, the same way the `llm-chat`
Secrets are created. Nothing in the chart, the realm ConfigMap, or the values
file carries a credential.

```bash
mkdir -p .local/k8s && chmod 700 .local .local/k8s
cp k8s/examples/keycloak-*.env.example .local/k8s/
chmod 600 .local/k8s/*
# replace every REPLACE_WITH_ value, then:
kubectl create namespace identity --dry-run=client -o yaml | kubectl apply -f -
for name in keycloak-admin-credentials keycloak-db-credentials keycloak-client-credentials keycloak-bootstrap-user; do
  kubectl -n identity create secret generic "$name" \
    --from-env-file=".local/k8s/$name.env.example" \
    --dry-run=client -o yaml | kubectl apply -f -
done
```

`keycloak-client-credentials.clientSecret` and
`oidc-credentials.clientSecret` in `llm-chat` are two copies of one credential.
They must match, and they rotate together.

## Install

```bash
helm upgrade --install keycloak k8s/helm/keycloak \
  --namespace identity --create-namespace \
  -f .local/k8s/keycloak-values.yaml
```

Start from `values.local.example.yaml`. Three values have no default and the
chart refuses to render without them: `keycloak.publicUrl`, `realm.gatewayUrl`,
and `keycloak.image.digest`.

Record the digest before installing:

```bash
crane digest quay.io/keycloak/keycloak:26.4.0
```

Rendering without installing, which is also how to review what changes:

```bash
helm template keycloak k8s/helm/keycloak -f .local/k8s/keycloak-values.yaml
```

## Wiring the gateway

`helm install` prints the exact values. They go into the `llm-chat` namespace as
the `oidc-endpoints` ConfigMap and the `oidc-credentials` Secret; see
`k8s/README.md` for the commands. `k8s/deploy.sh` refuses to deploy without
them.

## What the bootstrap Job does

Realm import runs from a ConfigMap, which is readable by anything with
namespace read access, so it carries no credential. The post-install Job fills
the two gaps with `kcadm`:

1. sets the confidential client's secret from `keycloak-client-credentials`,
2. creates the first operator account from `keycloak-bootstrap-user`, puts it in
   `bootstrapUser.group`, and sets a password Keycloak forces the user to change.

It reads before it writes, so `helm upgrade` re-runs it safely. Its log is the
first place to look when login fails:

```bash
kubectl -n identity logs job/keycloak-bootstrap
```

Realm *import* is first-install only — Keycloak does not overwrite an existing
realm. Changes to groups, scopes, or the client's redirect URIs after that must
be applied through the admin console or `kcadm`, or by deleting the realm and
re-running the import.

## Groups and roles

The realm defines `viewer`, `support_agent`, `tenant_admin`, and
`platform_admin` as groups, exposed through a `groups` client scope mapped into
the ID token, access token, and userinfo response.

oauth2-proxy passes them on as `X-Auth-Request-Groups`; nginx maps them to a
single role in `frontend/nginx/entrypoint.sh` (highest privilege wins); Python
re-checks the role independently. **These names are a contract with that map** —
renaming a group here without changing the map authenticates the user and then
denies them everything.

A user in no recognized group is authenticated but has no role, and every admin
route fails closed. That is the intended behavior, not a misconfiguration.

## What is checked, and where

```bash
make keycloak-check
```

Lints the chart and runs `tests/test_keycloak_realm_chart.py`, which renders
against `values.local.example.yaml` and asserts what the section above is about:
the realm declares no `clientScopes`, its client names no scope that import
cannot resolve, the bootstrap Job creates `groups` with its group-membership
mapper and attaches it, and every scope in the gateway's `OAUTH2_PROXY_SCOPE`
is reachable one of those two ways.

Rendering needs helm, which `make check` deliberately does without, so those
tests carry pytest's `chart` marker and run in CI's `Helm charts` job.

## Not covered by the static gates

`make deployment-security` and `make image-contracts` scan `k8s/*.yaml`, which
is a flat glob and does not reach this chart — those checks parse plain YAML and
cannot read Go templates. Scan the rendered output instead:

```bash
helm template keycloak k8s/helm/keycloak -f .local/k8s/keycloak-values.yaml | grep -n 'image:'
```

Every image must end in `@sha256:` and a 64-hex digest.
