"""Safe replay of one turn record through the current model.

`OBS-004`'s reconstruction contract made executable under `FEAT-015`: the
stored prompt section is rebuilt with :func:`reconstruct_prompt`, re-hashed
against the stored content hash, and sent through the deployment's *current*
model with no tools — so no domain effect (booking, lead, handoff) can be
touched. The stored and current component manifests are then compared on their
content-free versions, which is what lets the console show exactly which
components changed between the turn and the replay.

A single replayed trial is stochastic by definition: the response labels it as
an observation, never a proof. Repeated trials, immutable-index retrieval
replay, and gold-evidence substitution are `OBS-004` follow-ups; this service
deliberately does not pretend to offer them.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping

from tenantchat.api.schemas import (
    ComponentVersionSnapshot,
    ReplayOutput,
    TraceReplayResponse,
)
from tenantchat.api.store import TurnRecord
from tenantchat.core.errors import TraceReplayError
from tenantchat.core.routing import ROUTING_POLICY_VERSION
from tenantchat.orchestration.agents import AGENTS_VERSION
from tenantchat.orchestration.graph import GRAPH_VERSION
from tenantchat.orchestration.model import AssembledPrompt, ChatModel, ModelResponse
from tenantchat.orchestration.prompts import DISPATCH_SYSTEM_REF
from tenantchat.orchestration.tools import TOOLS_VERSION
from tenantchat.orchestration.trace import manifest_hash, reconstruct_prompt

# Components whose current version this deployment can state, and the accessor
# that reads each side. Only versions are compared — content never enters the
# comparison or the response's manifest fields.
_COMPARED_COMPONENTS = (
    "graph",
    "prompt_template",
    "routing_policy",
    "agents",
    "tools",
    "model",
    "retriever",
)


def _scalar(manifest: Mapping[str, object], name: str) -> object:
    return manifest.get(name)


def _template_ref(manifest: Mapping[str, object], name: str) -> object:
    value = manifest.get(name)
    if isinstance(value, Mapping) and value.get("ref"):
        return {"ref": str(value["ref"])}
    return None


def _retriever_static(manifest: Mapping[str, object], name: str) -> object:
    """The retriever's static envelope, as recorded or as currently served.

    Runtime values — the index generation a run answered against and the
    embedding model it used — are per-query evidence, not component versions,
    so they are excluded from both sides of the comparison; including them
    would make every replay look "changed" for a reason that has nothing to do
    with the components.
    """
    value = manifest.get(name)
    if not isinstance(value, Mapping):
        return None
    return {
        key: value.get(key)
        for key in ("version", "reranker", "min_evidence_score", "parameters", "budget")
    }


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


_ACCESSORS: dict[str, Callable[[Mapping[str, object], str], object]] = {
    "graph": _scalar,
    "prompt_template": _template_ref,
    "routing_policy": _scalar,
    "agents": _scalar,
    "tools": _scalar,
    "model": _scalar,
    "retriever": _retriever_static,
}


def compare_manifests(
    stored: Mapping[str, object],
    current: Mapping[str, object],
) -> tuple[tuple[ComponentVersionSnapshot, ...], bool]:
    """The content-free component diff between a turn and this deployment.

    Each compared component is canonicalized the same way on both sides, so
    equality means the component really has the same version — never that both
    sides are equally vague.
    """
    snapshots: list[ComponentVersionSnapshot] = []
    changed = False
    for name in _COMPARED_COMPONENTS:
        accessor = _ACCESSORS[name]
        stored_value = _canonical(accessor(stored, name))
        current_value = _canonical(accessor(current, name))
        differs = stored_value != current_value
        changed = changed or differs
        snapshots.append(
            ComponentVersionSnapshot(
                name=name,
                stored=stored_value,
                current=current_value,
                changed=differs,
            )
        )
    return tuple(snapshots), changed


def current_manifest(model_name: str, retriever: Mapping[str, object] | None) -> dict[str, object]:
    """The manifest this deployment serves now, in the stored trace's shape.

    ``model_name`` is what the model call just reported, because the port owns
    the identifier; ``retriever`` is the evidence source's static envelope, or
    ``None`` when the deployment composed none (an absent component reads as
    "not served", which the comparison then shows as changed).
    """
    return {
        "graph": GRAPH_VERSION,
        "prompt_template": {"ref": DISPATCH_SYSTEM_REF},
        "routing_policy": ROUTING_POLICY_VERSION,
        "agents": AGENTS_VERSION,
        "tools": TOOLS_VERSION,
        "retriever": retriever,
        "model": {"id": model_name or None, "parameters": {}},
    }


async def replay_turn(
    *,
    record: TurnRecord,
    model: ChatModel,
    retriever: Mapping[str, object] | None,
) -> TraceReplayResponse:
    """Rebuild the stored prompt and run one trial of it through *model*.

    Raises:
        TraceReplayError: the stored prompt section is absent or does not name
            a reconstructible template; the record stays untouched either way.
    """
    content = record.content
    prompt_section = content.get("prompt")
    if not isinstance(prompt_section, Mapping):
        raise TraceReplayError(detail="trace prompt section absent")
    try:
        rebuilt = reconstruct_prompt(prompt_section)
    except ValueError as error:
        raise TraceReplayError(detail="stored prompt not reconstructible") from error

    stored_hash = str(prompt_section.get("content_hash", ""))
    response = await _complete(model, rebuilt)
    current = current_manifest(response.model_name, retriever)
    stored_manifest = content.get("component_manifest", {})
    if not isinstance(stored_manifest, Mapping):
        stored_manifest = {}
    components, changed = compare_manifests(stored_manifest, current)

    output = content.get("output", {})
    raw_output = str(output.get("raw", "")) if isinstance(output, Mapping) else ""
    return TraceReplayResponse(
        turn_id=record.turn_id,
        recorded_at=record.recorded_at,
        manifest_hash=str(content.get("manifest_hash", "")),
        current_manifest_hash=manifest_hash(current),
        manifest_changed=changed,
        stochastic=True,
        components=list(components),
        original=ReplayOutput(
            content_hash=stored_hash,
            model_name=_stored_model_name(content),
            output_raw=raw_output,
        ),
        replayed=ReplayOutput(
            content_hash=rebuilt.content_hash,
            model_name=response.model_name,
            output_raw=response.content,
        ),
    )


def _stored_model_name(content: Mapping[str, object]) -> str:
    manifest = content.get("component_manifest")
    if isinstance(manifest, Mapping):
        model = manifest.get("model")
        if isinstance(model, Mapping):
            return str(model.get("id") or "")
    return ""


async def _complete(model: ChatModel, prompt: AssembledPrompt) -> ModelResponse:
    # The graph's tool-spec list is deliberately not imported here: replay
    # offers no tools, so a replayed turn cannot propose a booking, capture a
    # lead, or hand off to a human.
    return await model.complete(prompt, tools=())
