# Seed Knowledge

Drives the governed ingestion pipeline for both demo tenants: upload, approve,
publish, and poll the durable ingestion job to completion. Replaces the legacy
`k8s/seed-ingestion-job.yaml` for the governed path; the legacy seed still
populates the financing side-agent's separate Elasticsearch index.

## Usage

```bash
# Local (needs API running on default port, dev auth enabled):
make seed-knowledge
```

```bash
# Direct invocation with custom API endpoint:
uv run --frozen python scripts/seed_knowledge.py
export API_BASE_URL=http://localhost:8004
export ADMIN_GATEWAY_TOKEN=...
export ADMIN_CSRF_SECRET=...
uv run --frozen python scripts/seed_knowledge.py
```

## k8s Job

Apply after `api-migration-job.yaml` and before the chat-backend deployment is
live. The job uses the same API image and talks to `chat-admin:8004`.

```bash
kubectl apply -f k8s/seed-knowledge-job.yaml
kubectl wait --for=condition=complete job/seed-knowledge -n llm-chat --timeout=120s
```

## Idempotency

- Source registration is idempotent on `(tenant, domain, display_name)`.
- Document upload is idempotent on content checksum.
- Approval is a no-op on an already-approved version.
- Publishing is a no-op on an already-published version.
- Ingestion job submission is idempotent per `(tenant, version)`.

Re-running the seed against an already-seeded cluster is safe and changes nothing.

## Environment

| Variable                | Default                      | Purpose                       |
|-------------------------|------------------------------|-------------------------------|
| `API_BASE_URL`          | `http://chat-admin:8004`     | Admin API root                |
| `ADMIN_GATEWAY_TOKEN`   | (required)                   | Gateway-to-API auth token     |
| `ADMIN_CSRF_SECRET`     | (required)                   | CSRF signing secret           |
| `SEED_API_TIMEOUT`      | `30`                         | Per-request HTTP timeout (s)  |
| `SEED_POLL_INTERVAL`    | `2`                          | Job-status poll interval (s)  |
| `SEED_POLL_ATTEMPTS`    | `60`                         | Max poll cycles               |
