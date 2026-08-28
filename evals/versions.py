"""The component manifest a run pins, so two runs are comparable.

Every versioned input to the RAG path is recorded in the report's
``components`` block, mirroring the content-free manifest an `OBS-004` turn
record carries: application build, prompt template, retriever, parser and
chunker, index generation, embedding, reranker, model, tool contract, tenant
policy, and feature flags. Only versions and counts appear — never content —
so the manifest can be diffed and hashed without touching the inference
plane.

Component reads that need an importable package (the prompt registry, the
orchestration version constants) degrade to ``None`` rather than failing a
run: a version annotation must never break the hermetic harness.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from evals.corpus import FixtureCorpus
from evals.retriever import RetrieverConfig


def prompt_template_manifest() -> dict[str, object] | None:
    """The registered dispatch prompt version, if the registry is importable.

    Reads the append-only template registry (`AI-003`) so the report pins the
    exact prompt artifact a candidate run would have used. ``None`` when the
    orchestration package is unavailable — the harness must never fail a run
    over a version annotation.
    """
    try:
        from tenantchat.orchestration.prompts import DEFAULT_REGISTRY
    except ImportError:
        return None
    try:
        template = DEFAULT_REGISTRY.current("dispatch-system")
    except Exception:
        return None
    return {"template_id": template.template_id, "version": template.version}


def tool_contract_manifest() -> dict[str, object] | None:
    """The graph, agent, tool, and routing-policy versions served by default."""
    try:
        from tenantchat.core.routing import ROUTING_POLICY_VERSION
        from tenantchat.orchestration.agents import AGENTS_VERSION
        from tenantchat.orchestration.graph import GRAPH_VERSION
        from tenantchat.orchestration.tools import TOOLS_VERSION
    except ImportError:
        return None
    return {
        "graph": GRAPH_VERSION,
        "agents": AGENTS_VERSION,
        "tools": TOOLS_VERSION,
        "routing_policy": ROUTING_POLICY_VERSION,
    }


def query_planner_manifest() -> dict[str, object]:
    """The conversation-aware query planner version, if the domain is importable."""
    try:
        from tenantchat.core.planning import PLANNER_VERSION
    except ImportError:
        return {"version": None}
    return {"version": PLANNER_VERSION}


def component_manifest(
    *,
    retriever: RetrieverConfig,
    embedding_model: str,
    reranker: str | None,
    abstain_threshold: float,
    min_recall: float,
    min_citation_precision: float,
    min_abstention: float,
    min_grounding: float,
    corpus_chunks: int,
    corpus_digest: str,
    parser_chunker: str | None = None,
    tenant_policy: str | None = None,
) -> dict[str, Any]:
    """One deterministic manifest entry for a run's report.

    ``corpus_digest`` is the fingerprint of the indexed corpus (a SHA-256
    over each chunk's id, text, and active flag, plus the embedding model),
    so any corpus edit — rewording or a soft delete — shows up in the
    manifest diff without any text leaving the dataset.
    """
    return {
        "build": {
            "kind": "hermetic-fixtures",
            "corpus_digest": corpus_digest,
            "chunks": corpus_chunks,
        },
        "prompt_template": prompt_template_manifest(),
        "retriever": {
            "name": retriever.name,
            "version": retriever.version,
            "k": retriever.k,
            "parameters": dict(retriever.parameters),
        },
        "query_planner": query_planner_manifest(),
        "parser_chunker": {"method": "fixture-authoring", "version": parser_chunker},
        "index_generation": {"method": "fixture-index", "chunks": corpus_chunks},
        "embedding": embedding_model,
        "reranker": reranker,
        "model": {"id": None, "parameters": {}},
        "tool_contract": tool_contract_manifest(),
        "tenant_policy": tenant_policy,
        "feature_flags": {},
        "abstain_threshold": abstain_threshold,
        "min_recall": min_recall,
        "min_citation_precision": min_citation_precision,
        "min_abstention": min_abstention,
        "min_grounding": min_grounding,
    }


def manifest_hash(components: Mapping[str, object]) -> str:
    """SHA-256 over the canonical JSON of a manifest block.

    Deterministic and content-free by construction: the block carries only
    versions, parameters, and counts. Two runs over the same components hash
    the same, which is what binds an exception or a closure to a specific
    report.
    """
    canonical = json.dumps(components, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def corpus_digest(corpus: FixtureCorpus) -> str:
    """The content fingerprint of a fixture corpus, stable across runs.

    Covers every input the retrieval results depend on — each chunk's id,
    text, and active flag, plus the embedding model — because waivers and
    run ids bind to the manifest hash this feeds: a digest blind to text or
    activity let a reviewed waiver silently apply to edited or deactivated
    chunks no reviewer had seen (R-15). Chunks serialize sorted by id under
    canonical compact JSON, so neither corpus order nor the interpreter's
    hash seed can move the value.
    """
    entries = [
        {"active": chunk.active, "chunk_id": chunk.chunk_id, "text": chunk.text}
        for chunk in sorted(corpus.chunks, key=lambda chunk: chunk.chunk_id)
    ]
    canonical = json.dumps(
        {"chunks": entries, "embedding_model": corpus.embedding_model},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
