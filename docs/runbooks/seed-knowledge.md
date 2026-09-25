# Seed Knowledge

Drives the governed ingestion pipeline for both demo tenants: upload, approve,
publish, and poll the durable ingestion job to completion. It is the only seed path.

## Usage

```bash
# Local: with `make api` running on its default port (127.0.0.1:8080) and the
# admin credentials exported (set -a; source .env; set +a):
make seed-knowledge
```

```bash
# Direct invocation with a custom API endpoint (the default targets the local
# API on 127.0.0.1:8080):
export API_BASE_URL=https://chat.example.internal
export ADMIN_GATEWAY_TOKEN=...
export ADMIN_CSRF_SECRET=...
uv run --frozen python scripts/seed_knowledge.py
```

## k8s Job

Apply after the API migration and chat-backend rollout. The job uses the same
immutable API image and sets `API_BASE_URL=http://chat-admin:8004` explicitly,
talking to the API through its dedicated default-deny NetworkPolicy path.

```bash
kubectl apply -f /secure/release/seed-knowledge-job.yaml
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
| `API_BASE_URL`          | `http://127.0.0.1:8080`      | Admin API root                |
| `ADMIN_GATEWAY_TOKEN`   | (required)                   | Gateway-to-API auth token     |
| `ADMIN_CSRF_SECRET`     | (required)                   | CSRF signing secret           |
| `SEED_API_TIMEOUT`      | `30`                         | Per-request HTTP timeout (s)  |
| `SEED_POLL_INTERVAL`    | `2`                          | Job-status poll interval (s)  |
| `SEED_POLL_ATTEMPTS`    | `60`                         | Max poll cycles               |
