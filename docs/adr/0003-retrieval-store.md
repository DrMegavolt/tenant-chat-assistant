# 0003 — Elasticsearch as the retrieval store

- **Status:** Accepted
- **Date:** 2026-07-31

## Context

Retrieval combines lexical and vector signals while filtering by tenant and
approved document version. PostgreSQL is already the system of record, so using
it for retrieval would simplify operations, but would require custom fusion and
score calibration.

## Decision

Use Elasticsearch 8.15 for hybrid retrieval. PostgreSQL remains authoritative
for documents and version state; Elasticsearch contains rebuildable chunks
only. Retrieval stays behind a domain port.

Publishing a version and indexing it are separate operations. Indexing therefore
runs as a durable job with explicit status and retry behavior. Elasticsearch
authentication is enabled locally as well as in Kubernetes.

## Consequences

Elasticsearch provides mature BM25 search, filtered vector search, and native
reciprocal-rank fusion. Exact names and numeric terms benefit from the lexical
signal without giving up semantic matches.

The system gains another datastore, additional memory use, and a cross-store
consistency boundary. The index is not backed up as authoritative data because
it can be rebuilt from PostgreSQL.

## Alternatives considered

- **PostgreSQL full-text search with pgvector:** operationally simpler and still
  viable if that becomes more important than retrieval quality.
- **Vector-only retrieval:** rejected because it handles exact terms poorly.
- **A dedicated vector database:** rejected because it would not remove the
  need for strong lexical retrieval.
