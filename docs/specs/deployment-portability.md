# Docker Compose and Kubernetes runtime parity

## Trigger

An operator needs to run the Tenant Chat application from this repository either
on one Docker host or on the supported Kubernetes cluster, without running Python
or frontend processes directly on the host.

## Expected result

- `make compose-up` builds and starts the deployable images plus PostgreSQL,
  Elasticsearch, the embedding service, the API, the durable worker, and the
  nginx web gateway.
- The Compose release runs the same Alembic and LangGraph checkpoint migrations
  as Kubernetes before the API or worker starts.
- `make compose-smoke` proves that every long-running Compose service is healthy,
  that API dependency readiness succeeds, and that the visitor API is reachable
  through nginx.
- `make compose-seed` loads the governed demo knowledge through the real admin
  lifecycle after the stack is ready.
- `make deploy-local` remains the repeat-release path for the provisioned local
  MicroK8s cluster; its workloads use the same three application images and the
  same migration ordering.

## Tenant boundary

Deployment packaging does not introduce a second data path. The gateway calls
the existing tenant-qualified visitor API, and seeding calls the existing admin
API with an explicit operator identity. PostgreSQL, Elasticsearch, upload
storage, visitor credentials, and the domain authorization rules retain their
existing tenant boundaries in both runtimes.

## Failure behavior

- Missing required Compose configuration fails during interpolation or service
  startup and names the missing variable; secrets never gain checked-in defaults.
- A failed migration prevents the API and worker from starting.
- The API is not healthy until `/readyz` has verified PostgreSQL, Elasticsearch,
  and the embedding service when required RAG mode is enabled.
- A failed dependency or gateway route makes `make compose-smoke` fail with a
  non-zero status.
- Kubernetes continues to reject unresolved image digests or missing Secret and
  ConfigMap keys before changing application workloads.

## Exclusions

- Compose is the local visitor-demo runtime. Browser OIDC login and the operator
  console require the Keycloak and oauth2-proxy Kubernetes deployment; Compose
  seeding uses the existing internal gateway-token contract instead.
- Compose does not reproduce cluster ingress, NetworkPolicy, OpenTelemetry,
  Grafana, Kibana, or high-availability behavior.
- A compatible chat model remains external configuration. The Compose default
  reaches a model server on the Docker host; it does not download or start one.
