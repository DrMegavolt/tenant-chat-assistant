# Demo access and credential recovery

This runbook is intentionally deployment-neutral. Visitors do not log in.
Human administration uses Keycloak, while infrastructure UIs use the
Kubernetes Secrets named below. Never commit decoded values or paste them into
task logs.

## Configure deployment-specific URLs

Copy the checked-in example to the gitignored local configuration:

```bash
mkdir -p .local/k8s
cp k8s/examples/demo-access.env.example .local/k8s/demo-access.env
```

Edit `.local/k8s/demo-access.env` with the endpoints for the current cluster.
The same file can override namespaces, Secret names, the Keycloak realm, and
the operator group when an installation differs from the repository defaults.
It is a trusted shell environment file, so only use a file controlled by the
local operator.

For LoadBalancer installations, this command is a useful discovery starting
point:

```bash
kubectl get svc -A -o wide
```

Keep the example file in Git; keep `.local/k8s/demo-access.env`, rendered Helm
values, decoded credentials, bearer tokens, and kubeconfigs out of Git. The
repository's `.gitignore` already excludes the entire `.local/` tree.

## One-command credential preparation

From the repository root:

```bash
./scripts/prepare_demo_credentials.sh
```

This reads the live cluster and writes `.local/k8s/demo-credentials.env` with
mode `0600`. It prints only the destination, never a username or password. Load
the generated values into the current shell with:

```bash
set -a
source .local/k8s/demo-credentials.env
set +a
```

The required TenantChat, Keycloak, and Grafana Secrets must exist. Phoenix and
Elasticsearch variables are included when their Secrets exist. Configured UI
URLs are copied from `.local/k8s/demo-access.env`.

To keep local configuration somewhere else, set `DEMO_ACCESS_CONFIG` to that
file's absolute path before running either helper.

## Demo surfaces

| Surface | Username | Password source | URL variable |
|---|---|---|---|
| Apex visitor | None | Signed visitor session; no login | `VISITOR_URL` |
| Clearview visitor | None | Signed visitor session; no login | `VISITOR_URL` |
| TenantChat admin | `identity/keycloak-bootstrap-user:username` | `identity/keycloak-bootstrap-user:password` | `TENANTCHAT_ADMIN_URL` |
| Keycloak admin | `identity/keycloak-admin-credentials:username` | `identity/keycloak-admin-credentials:password` | `KEYCLOAK_URL` |
| Grafana | Normally `admin` | `observability/kube-prom-stack-grafana:admin-password` | `GRAFANA_URL` |
| Phoenix | `admin@localhost` | Initial value in `observability/phoenix-secret:PHOENIX_DEFAULT_ADMIN_INITIAL_PASSWORD` | `PHOENIX_URL` |
| MLflow | None | Authentication is not configured | `MLFLOW_URL` |
| Prometheus | None | No UI login | `PROMETHEUS_URL` |
| Tempo | None | API only by default; no standalone UI | `TEMPO_URL` |
| Kafka UI | None | Authentication is disabled in the demo stack | `KAFKA_UI_URL` |
| Kubernetes Dashboard | None | Short-lived `dashboard-admin` bearer token | `KUBERNETES_DASHBOARD_URL` |
| Elasticsearch | `llm-chat/elastic-credentials:username` | `llm-chat/elastic-credentials:password` | Internal service credential, not a normal application login |
| Kibana | No dedicated human user | Do not use `kibana_system`; it is a service account | `KIBANA_URL` |

The single seeded TenantChat operator is a `platform_admin` and spans both
`apex` and `clearview`; the default deployment does not seed separate
tenant-admin, support-agent, or viewer accounts.

`keycloak-admin-credentials` is only the Keycloak master administrator. It is
not a TenantChat application login. The seeded application operator comes from
`keycloak-bootstrap-user`.

A manually created Keycloak user has no TenantChat access until it joins one of
the recognized groups: `viewer`, `support_agent`, `tenant_admin`, or
`platform_admin`. `platform_admin` spans every tenant. The other groups are a
privilege ceiling and also require a corresponding tenant membership in the
application. A signed-in user with no recognized group receives a 403 and the
console displays a role-assignment message.

The namespace and Secret names in this table are the repository defaults. Use
the override keys shown in `k8s/examples/demo-access.env.example` if a deployed
release uses different names.

## Direct Secret reads

These commands intentionally print credentials to the caller's terminal. Use
them only in a private local terminal; prefer the preparation helper for
automation.

TenantChat operator:

```bash
kubectl -n identity get secret keycloak-bootstrap-user \
  -o go-template='username={{index .data "username" | base64decode}}{{"\n"}}password={{index .data "password" | base64decode}}{{"\n"}}email={{index .data "email" | base64decode}}{{"\n"}}'
```

Keycloak master administrator:

```bash
kubectl -n identity get secret keycloak-admin-credentials \
  -o go-template='username={{index .data "username" | base64decode}}{{"\n"}}password={{index .data "password" | base64decode}}{{"\n"}}'
```

Grafana:

```bash
kubectl -n observability get secret kube-prom-stack-grafana \
  -o go-template='username={{index .data "admin-user" | base64decode}}{{"\n"}}password={{index .data "admin-password" | base64decode}}{{"\n"}}'
```

Phoenix's initial administrator password:

```bash
kubectl -n observability get secret phoenix-secret \
  -o go-template='username=admin@localhost{{"\n"}}password={{index .data "PHOENIX_DEFAULT_ADMIN_INITIAL_PASSWORD" | base64decode}}{{"\n"}}'
```

Kubernetes Dashboard token:

```bash
kubectl -n kubernetes-dashboard create token dashboard-admin --duration=1h
```

## TenantChat operator repair

If the Keycloak password was changed through the browser, Kubernetes cannot
retrieve the new password and the bootstrap Secret becomes stale. After
confirming the current `kubectl` context, a demo operator can intentionally
restore the Secret as the authoritative credential:

```bash
kubectl config current-context
DEMO_ALLOW_OPERATOR_PASSWORD_RESET=true \
  ./scripts/repair_demo_operator_access.sh
```

The helper uses the configured Keycloak administrator Secret, resolves the
exact seeded user, verifies its configured group, resets only that user's
password to the bootstrap Secret value as non-temporary, and proves it
authenticates. It does not print any credential.

The public Helm example keeps `bootstrapUser.temporaryPassword: true`, which is
the safer first-login policy. A private, resettable demo may override it to
`false` in `.local/k8s/keycloak-values.yaml` so later Helm upgrades preserve a
recoverable local credential. Do not use that override merely for convenience
in a production or shared environment.

## Verification and troubleshooting

Safe checks that expose no Secret value:

```bash
kubectl -n identity describe secret \
  keycloak-bootstrap-user keycloak-admin-credentials
kubectl -n observability describe secret \
  kube-prom-stack-grafana phoenix-secret
kubectl -n identity get pod,job
kubectl -n identity logs job/keycloak-bootstrap
make grafana-smoke
```

If TenantChat login succeeds but the admin API returns 403, inspect the user's
Keycloak group. It must include one of `viewer`, `support_agent`,
`tenant_admin`, or `platform_admin`; the seeded user should be
`platform_admin`. If Grafana rejects the Secret password, the Grafana database
password was changed after installation; use Grafana's documented admin reset
flow, then update the Kubernetes Secret to the same value.

Phoenix's `PHOENIX_DEFAULT_ADMIN_INITIAL_PASSWORD` is consumed only when the
original `admin@localhost` account is created. Later Secret changes do not
change an existing Phoenix account, so treat it as an initial credential rather
than a guaranteed password-recovery mechanism.
