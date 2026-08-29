# Documentation

Start with the root [README](../README.md) to install and run the project. The
documents here explain design decisions and operations in more depth.

## Architecture and design

- [Architecture diagrams](../architecture/likec4/README.md) describe the
  components and reference Kubernetes deployment currently present in the
  repository.
- [Architecture decision records](adr/README.md) preserve why the major
  technical choices were made. Their context sections may refer to code that
  has since been removed; the status and amendment notes explain whether a
  decision is still active.
- [Privacy model](privacy.md) covers consent, retention, export, deletion, and
  inference-record access.
- [Accessibility](accessibility.md) separates automated checks from the manual
  keyboard and screen-reader pass.

## Operations

| Topic | Document |
| --- | --- |
| Kubernetes deployment and required secrets | [`k8s/README.md`](../k8s/README.md) |
| Container builds and release manifests | [Container images](runbooks/container-images.md) |
| Database roles, migrations, and rollback | [Database migrations](runbooks/database-migrations.md) |
| Durable job worker | [Background jobs](runbooks/background-jobs.md) |
| Seeded knowledge lifecycle | [Seed knowledge](runbooks/seed-knowledge.md) |
| Local operator credentials | [Demo access](runbooks/demo-access.md) |
| Live presentation flow | [Demo walkthrough](runbooks/demo-walkthrough.md) |
| Structured logs and correlation | [Trace walkthrough](runbooks/trace-walkthrough.md) |
| Metrics | [Metrics walkthrough](runbooks/metrics-walkthrough.md) |
| Grafana and other observability UIs | [Observability dashboards](runbooks/observability-dashboards.md) |
| Content-bearing inference records | [Inference trace plane](runbooks/inference-trace-plane.md) |
| Offline evals and MLflow run comparison | [Evals](runbooks/evals.md) |

The demo walkthrough is a record of a specific local deployment and includes
dated limitations. Run its readiness checks against the revision being shown
before using it as evidence.

## Project records

- [`BACKLOG.md`](../BACKLOG.md) tracks implemented, planned, cancelled, and
  reopened work. It is detailed because tests and task tooling consume its
  stable identifiers and status fields.

The tenant financing documents under `docs/apex/` and `docs/clearview/` are
demo knowledge sources loaded through the real ingestion lifecycle. They are
sample business content, not operating documentation.
