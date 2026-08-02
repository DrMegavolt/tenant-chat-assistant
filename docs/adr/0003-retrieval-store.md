# 0003 — Elasticsearch as the retrieval store

- **Status:** Accepted
- **Date:** 2026-07-31
- **Affects:** `RAG-001`, `RAG-002`, `RAG-004`, `DEP-005`

## Context

Retrieval quality is the core of this product. `RAG-004` requires lexical and
vector retrieval combined, filtered by tenant, domain, and document version, with
a calibrated abstention threshold. `RAG-001` requires versioned documents where
only approved, current versions are retrievable.

The system already runs PostgreSQL for domain records and LangGraph checkpoints,
so using `pgvector` and Postgres full-text search would avoid a second datastore.
That was the initial recommendation and it was reversed.

## Decision

**Use Elasticsearch 8.15 as the retrieval store, with Postgres remaining the
system of record for documents and their version state.**

Elasticsearch holds derived data only. Every chunk is rebuildable from the
authoritative document versions in Postgres, so the search index can be dropped
and reconstructed without data loss — which is also what `DEP-005` prefers over
backing up a derived index.

Retrieval sits behind a port in `packages/core`, so the store is replaceable
without touching domain rules.

## Consequences

**Gained.** Elasticsearch 8.x ships reciprocal rank fusion as a native retriever,
so hybrid lexical-plus-vector search is configuration rather than hand-written
fusion and score normalization — the part most likely to be subtly wrong when
hand-rolled. BM25 is mature and well understood. Filtered kNN applies tenant and
version filters inside the vector search rather than post-filtering, which
preserves recall.

**Cost.** A second datastore to run, back up, secure, and reason about. Roughly
1 GB of JVM heap locally, which is the single largest contributor to laptop
startup time. Cross-store consistency is now a real concern: a document approved
in Postgres is not retrievable until indexing completes, so indexing state must be
explicit and observable rather than assumed.

**Cost.** Retrieval cannot participate in a Postgres transaction. Publishing a
document version and making it retrievable are two steps that can fail
independently, which is why `RAG-002` runs indexing as a durable background job
with an observable status rather than inline with the publish request.

**Security.** The local Elasticsearch runs with authentication enabled (TLS off
for plain HTTP), so the retrieval client exercises the same credential path
locally that it uses in Kubernetes. Running locally with security disabled would
leave that path untested until deployment.

## Alternatives considered

**`pgvector` + Postgres full-text search.** Genuinely close, and the initial
recommendation. One datastore, hybrid retrieval and document-version state
queryable in a single transaction, dramatically simpler local setup. Rejected
because reciprocal rank fusion would have to be hand-written and calibrated, and
because Postgres full-text search is weaker than BM25 for the phrase and
stemming behavior that financing and policy questions produce. Remains the
recommended path if operational simplicity later outweighs retrieval quality —
the port makes that switch a contained change.

**Vector-only retrieval (the current prototype).** Rejected. Single-vector kNN
without a lexical signal misses exact-term matches, which is precisely the failure
mode for questions containing specific plan names, dollar figures, or model
numbers.

**A dedicated vector database (Qdrant, Weaviate, Pinecone).** Rejected. Strong at
vector search but adds a third datastore while still leaving lexical retrieval
unsolved. Elasticsearch covers both.
