# 0010 — Two telemetry planes, with the turn record as the system of record

- **Status:** Accepted
- **Date:** 2026-08-02
- **Affects:** `OBS-001`, `OBS-002`, `OBS-004`, `PRIV-001`, `PRIV-002`, `RAG-005`, `RAG-008`, `AI-003`

## Context

Two accepted rules contradict each other. The invariant list forbids PII in logs,
metrics, and traces. `OBS-001` additionally forbids message content and full
document chunks from logs by default. Meanwhile, deciding whether a wrong answer
came from a hallucinating model, a stale source document, a retrieval miss, or a
context budget that silently dropped the evidence requires the exact prompt, the
exact retrieved chunks, and the exact output — precisely the forbidden fields.

Read as one pipeline, the rules leave only two outcomes: answer quality is
undebuggable, or the redaction rule is a fiction that a debugging session
quietly suspends.

The prototype has no answer-level record at all. `k8s/otel-collector.yaml`
already forwards OTLP traces to Tempo and metrics to Prometheus, which is the
right plumbing for latency and error rates and the wrong store for payload-heavy
evidence. Separately, every hosted LLM-observability product wants to be the
place answers live. That is a lock-in problem and a governance problem: a
`PRIV-001` erasure request must reach every copy of a transcript, and a store
outside the trust boundary cannot be reasoned about.

## Decision

**Split telemetry into two planes with different content rules, retention,
access control, and backends. The authoritative record of what produced an
answer is a first-party Postgres table, not a telemetry backend.**

### Plane 1 — Operational

OpenTelemetry spans and metrics plus structured JSON logs, carrying identifiers,
enums, counts, durations, versions, and safe error codes. No message content, no
contact details, no document text. Long retention, ordinary engineering access.
Backends are the existing stack: Prometheus, Loki, Tempo, Grafana. `OBS-001`'s
redaction rule applies here unchanged and unweakened.

### Plane 2 — Inference

The **turn record**: one append-only, tenant-qualified Postgres row per
conversation turn, holding the router decision, retrieval candidate set, assembled
prompt, model parameters, raw output, validator verdicts, and the version of every
component that contributed. It carries content, and is therefore short-retention,
narrowly authorized, audited on read, and in scope for `PRIV-001` export and
erasure. `OBS-004` defines its schema; `PRIV-002` defines its governance.

The turn record is the system of record for answer provenance. This is the same
principle 0001 applies to LangGraph checkpoints and 0005 applies to conversation
history: the framework is not the record, and neither is the vendor.

### Instrumentation and portability

Application code emits OpenTelemetry spans following the **GenAI semantic
conventions** and writes the turn record through a domain repository. It knows
nothing about any observability product.

The collector is the only fan-out point. A redaction processor runs before every
exporter, so a new backend cannot receive content by being added. Adopting,
replacing, or dropping an LLM-trace viewer is a collector configuration change
and a dashboard, never an instrumentation change or a schema migration.

Any viewer is therefore a disposable projection. `OBS-004` selects the first one
from self-hostable OSS candidates — Arize Phoenix, Langfuse, OpenLIT — on
operational fit, verifying license terms and which features sit behind an
open-core boundary at adoption time. The choice is deliberately cheap to revisit
and must not be load-bearing.

### Content export is off by default

Whether the projection receives prompt and evidence text is one setting,
`TRACE_CONTENT_EXPORT`, defaulting to disabled. It may be enabled only for a
viewer deployed inside the cluster trust boundary, behind the same authentication
as the admin console, and covered by the same retention job. It is required
disabled for any backend outside that boundary, and production startup fails if
it is enabled for one.

With it disabled the viewer still shows the full skeleton — routing decisions and
their rejected alternatives, candidate chunk IDs with lexical, vector, fused, and
rerank scores, token counts, latencies, validator verdicts, failure classes. The
text bodies are served by the first-party admin viewer reading Postgres under
`SEC-001` RBAC.

## Consequences

**Gained.** `OBS-001`'s redaction rule becomes literally true rather than
aspirational, because content never enters the log or metric pipeline at all.
Answer provenance survives changing observability tools. Subject erasure has a
finite, enumerable set of places to reach. Every component version that shaped an
answer is queryable with SQL, so a quality regression can be bisected against a
prompt, retriever, embedding, reranker, or model version.

**Cost.** Two pipelines to build and operate rather than one. A turn record is
substantially larger than a log line, and retrieval candidate sets dominate its
size, so retention is a storage decision that needs measuring rather than
guessing. Correlating a Grafana latency spike with a specific bad answer requires
following `trace_id` across planes instead of reading one screen.

**Cost.** With content export disabled — the default — the third-party viewer is
markedly less useful than its marketing demonstrates, and the first-party viewer
in `OBS-004` has to be genuinely good. That viewer is worth building regardless:
it is the operator-facing feature, not a debugging convenience.

**Boundary.** This record fixes where inference telemetry lives and how it moves.
It does not define the turn record's columns, the failure taxonomy, or the
citation validator, which are `OBS-004`. It does not define retention periods,
consent, or access roles, which are `PRIV-002` under `PRIV-001`'s policy.

## Alternatives considered

**One plane, with content in logs and traces behind a debug flag.** Rejected.
Loki and Tempo retention, replication, and access model are wrong for PII, and a
flag that widens what is logged is discovered in the incident where it was left
on. It also puts transcripts somewhere `PRIV-001` erasure cannot practically
reach.

**A hosted LLM-observability platform as the system of record.** Rejected. It
recreates, one layer up, the coupling 0001 rejected for agent frameworks: the
data that explains the product's behavior would live in a store the project does
not control, cannot migrate without loss, and cannot fully purge on request.

**Tempo as the inference store.** Rejected. Span attribute size limits make
whole prompts and candidate sets a poor fit, evidence needs relational queries
against document versions, and Tempo has no dataset or evaluation primitives.
Tempo remains correct for the operational plane.

**No turn record; reconstruct provenance from logs when needed.** Rejected.
Reconstruction is not replay. Re-running one turn against a new prompt version
requires the exact context that was sent, and scattered log lines cannot pin
versions that have since moved. The reconstruction would also be attempted for
the first time during an incident.
