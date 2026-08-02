# Container image build and release evidence

The repository builds six application images. Five come from one hashed
`uv.lock` — `prototype`, `api`, `embedding`, `ingestion`, and `financing` — and
the API image is also the migration image, so a migration and its serving
release cannot acquire different Python dependencies. Build stages use
digest-pinned Python and uv images; final stages contain the virtual environment
and application only, run as numeric user/group `10001:10001`, and do not
contain uv or pip build steps.

The sixth, `web`, is the nginx gateway built from `frontend/Dockerfile`. It
carries no Python: its content is `frontend/public/` plus the configuration in
`frontend/nginx/`. Its smoke asserts the two document roots stay separate and
that a malformed upstream origin stops the container instead of rendering a
configuration that rewrites every proxied request. See
[ADR-0006](../adr/0006-frontend-delivery.md).

The embedding runtime pins Qwen3-Embedding-0.6B to commit
`97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3`, uses the CPU-only PyTorch index,
and sets `trust_remote_code=False`. The first embedding request still downloads
the files for that exact revision into the declared model-cache volume. A
release can pre-populate that cache, but must not change the revision.

## Local verification

From a clean checkout with Docker Buildx available:

```bash
make image-contracts
make images-build
make images-smoke
```

`scripts/build_images.sh` passes the source commit and its commit timestamp as
stable OCI inputs. It deliberately disables BuildKit SBOM/provenance output:
signing, SBOMs, provenance, and publishing belong to `DEP-006`. For each image it
writes BuildKit metadata, `docker image inspect` output, and the exact local tag
under ignored `artifacts/images/`. The smoke script confirms the configured
non-root user, imports the packaged runtime (including psycopg in both backend
images), verifies the writable prototype data directory, checks Alembic in the
API image, and calls the service health endpoint from a running container.

To verify one image while iterating:

```bash
./scripts/build_images.sh api
./scripts/smoke_images.sh api
```

CI performs the same build and smoke in a six-image matrix. It uploads the
metadata, inspect output, health response, and Trivy JSON for each image and
fails on fixed HIGH or CRITICAL findings. A separate filesystem scan covers the
locked dependencies and repository inputs.

## Release digest contract

Publishing is not performed by these scripts. After release automation pushes
an image, record the registry-reported `sha256:<64 hex>` digest alongside the
build evidence. Render copies of `k8s/app.yaml` and
`k8s/api-migration-job.yaml` by replacing only their corresponding
`REPLACE_WITH_*_DIGEST` token. Keep the repository templates unresolved so a tag
cannot accidentally become the deployment contract.

Validate the rendered YAML locally, run the migration Job from the same API
digest, and pass the rendered application manifest explicitly:

```bash
kubectl apply --dry-run=client -f /secure/release/api-migration-job.yaml
kubectl apply --dry-run=client -f /secure/release/app.yaml
k8s/deploy.sh /secure/release/app.yaml
```

Do not commit rendered manifests if their environment configuration belongs
outside the repository. The deployment script refuses unresolved digest tokens,
and application pods never receive source-code ConfigMaps or startup package
installation commands.
