# 0010 — Separate operational and inference telemetry

- **Status:** Accepted
- **Date:** 2026-08-02

## Context

Operational telemetry must exclude message content, contact details, and full
document text. Diagnosing answer quality requires the exact prompt, evidence,
model output, and component versions. Sending all of that through one telemetry
pipeline would either make diagnosis impossible or weaken the privacy boundary.

## Decision

Maintain two planes with different data and access rules.

**Operational plane:** OpenTelemetry spans, metrics, and structured logs contain
identifiers, counts, durations, versions, safe enums, and error codes. They do
not contain conversation or document content. Prometheus, Loki, Tempo, and
Grafana provide the backends.

**Inference plane:** an append-only, tenant-qualified PostgreSQL turn record
contains routing, retrieval candidates, assembled prompts, model parameters,
outputs, validation results, and component versions. It is the authoritative
answer-provenance record and is governed like conversation content: restricted
access, audited reads, short retention, export, and erasure.

Application code emits standard telemetry and writes the turn record through a
domain repository. The collector is the only fan-out point. Optional trace
viewers are disposable projections, and exporting content to them is disabled by
default.

## Consequences

The operational redaction rule remains literal while detailed answer diagnosis
and replay remain possible. Changing an observability product does not move the
authoritative evidence record.

Two planes cost more to operate and require correlation through trace IDs. Turn
records are substantially larger than ordinary telemetry, so retention and
access controls are part of the data-governance design.

## Alternatives considered

- **Put content in traces behind a flag:** rejected because telemetry retention
  and access are unsuitable for private conversation data.
- **Use a hosted tracing product as the system of record:** rejected because
  provenance and erasure would depend on an external store.
- **Reconstruct answers from logs:** rejected because logs cannot reliably pin
  the exact context and versions used for a historical turn.
