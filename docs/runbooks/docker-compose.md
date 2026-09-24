# Docker Compose runtime

Docker Compose is the supported single-host visitor-demo runtime. It uses the
same API, embedding, and web Dockerfiles as Kubernetes and preserves the release
order: PostgreSQL becomes healthy, both schema migrations finish, required RAG
dependencies become healthy, then the API, worker, and gateway start.

## Configure

Install the locked development dependencies and create the local environment
file:

```bash
make setup
```

Replace every `REPLACE_WITH_*` value in `.env`. Use distinct generated values
where the example asks for distinct credentials. Configure the external
OpenAI-compatible model that containers can reach:

```dotenv
LLM_MODEL=your-loaded-model
COMPOSE_LLM_BASE_URL=http://host.docker.internal:1234/v1
```

The model server is deliberately outside this stack. If it is not on the Docker
host, set `COMPOSE_LLM_BASE_URL` to an address reachable from containers.

## Start and verify

```bash
make compose-up
make compose-smoke
make compose-seed
```

`compose-up` builds and starts PostgreSQL, Elasticsearch, the embedding model,
the one-shot migration container, API, durable worker, and nginx. The first
start can take several minutes while the embedding container downloads its
pinned model revision; the cache persists in a named volume.

`compose-smoke` verifies all long-running containers, the migration exit code,
dependency-aware API readiness, nginx health, and the visitor API through the
gateway. `compose-seed` then exercises the governed knowledge lifecycle for both
demo tenants and waits for their ingestion jobs to succeed.

Open <http://127.0.0.1:8080>. The API is also published at
<http://127.0.0.1:8004>; with the example configuration its OpenAPI UI is at
<http://127.0.0.1:8004/docs>.

Compose intentionally does not run Keycloak or oauth2-proxy. Public visitor
routes work through nginx, and the seed task authenticates directly with the
internal gateway contract. Use the [Kubernetes runtime](../../k8s/README.md) for
browser OIDC and the operator console.

## Operate and stop

```bash
make ps
make logs
make compose-down
```

`compose-down` preserves PostgreSQL, Elasticsearch, uploaded knowledge, and the
embedding-model cache. `make down-clean` deletes those Docker volumes and all
local data; use it only when a clean rebuild is intended.

If startup fails, inspect the first failed dependency instead of starting later
services manually:

```bash
docker compose --profile app ps -a
docker compose --profile app logs migrate api worker embedding
```

A failed migration blocks API and worker startup. With `CHAT_RAG_REQUIRED=true`,
an unavailable Elasticsearch or embedding service keeps the API unready and
therefore prevents nginx from starting.
