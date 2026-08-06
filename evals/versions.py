"""The component manifest a run pins, so two runs are comparable.

Every versioned input to the RAG path — retriever configuration, embedding
model, reranker, prompt template, model ID — is recorded in the report.
``RAG-008`` extends this into the baseline-versus-candidate manifest diff;
the fields here are the ones the golden harness needs today.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


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


def component_manifest(
    *,
    retriever_name: str,
    retriever_version: str,
    k: int,
    embedding_model: str,
    reranker: str | None = None,
    retriever_parameters: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    """One deterministic manifest entry for a run's report."""
    return {
        "retriever": {
            "name": retriever_name,
            "version": retriever_version,
            "k": k,
            "parameters": dict(retriever_parameters or {}),
        },
        "embedding_model": embedding_model,
        "reranker": reranker,
        "prompt_template": prompt_template_manifest(),
        "model_id": None,
    }
