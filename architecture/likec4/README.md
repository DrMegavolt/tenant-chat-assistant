# Architecture diagrams

This directory models the implementation and reference Kubernetes deployment
currently present in the repository. It does not include proposed calendar,
CRM, notification, streaming, high-availability, or managed-data services.
Those remain in [`BACKLOG.md`](../../BACKLOG.md).

The source is [`architecture.c4`](architecture.c4), written in LikeC4. Generated
PNG files are committed under [`diagrams/`](diagrams/) so they render on
GitHub without a build step.

## Views

| View | Scope |
| --- | --- |
| [`index`](diagrams/index.png) | Visitors, operators, the platform, and its external dependencies |
| [`platform_overview`](diagrams/platform_overview.png) | Current process and storage boundaries |
| [`agent_runtime`](diagrams/agent_runtime.png) | The implemented `dispatch@3` LangGraph path and deterministic tool boundary |
| [`knowledge_pipeline`](diagrams/knowledge_pipeline.png) | Upload, ingestion, indexing, retrieval, and evidence validation |
| [`business_actions`](diagrams/business_actions.png) | Locally committed bookings, leads, and handoffs |
| [`answer_provenance`](diagrams/answer_provenance.png) | Turn records, operator review, and content-free operational telemetry |
| [`production_deployment`](diagrams/production_deployment.png) | The single-replica MicroK8s topology tracked under `k8s/` |

The final view keeps its historical filename so existing links do not break,
but it shows the repository's reference deployment rather than claiming a
highly available production topology.

## Update the diagrams

From the repository root:

```bash
npm --prefix architecture/likec4 ci
make arch-validate
make arch-build
```

To check formatting or open the interactive viewer:

```bash
npm --prefix architecture/likec4 run format:check
npm --prefix architecture/likec4 exec likec4 serve
```

`make arch-build` regenerates the tracked PNGs. LikeC4 also writes a static
site to `dist/`; that directory is ignored because it is fully rebuildable.
