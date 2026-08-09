"""Safe replay of one turn record through the current model.

`OBS-004`'s reconstruction contract made executable under `FEAT-015`: the
stored prompt section is rebuilt with :func:`reconstruct_prompt`, re-hashed
against the stored content hash, and sent through the deployment's *current*
model with no tools — so no domain effect (booking, lead, handoff) can be
touched. The stored and current component manifests are then compared on their
content-free versions, which is what lets the console show exactly which
components changed between the turn and the replay.

Three facilities serve the Gate B case walkthrough:

① **Bounded repeated trials** (:func:`replay_trials`): N trials with prompt and
evidence held constant, reported as an aggregate with an explicit stochastic
label. This is what makes case 7 (model-behavior difference) demonstrable.

② **Generation-availability check + prompt re-execution**
(:func:`replay_with_retrieval`): checks that the stored index generation still
exists, then re-executes the stored prompt. It does not rerun retrieval through
the pinned retriever/index generation; genuine counterfactual retrieval replay
needs a retained index snapshot (Gate C).

③ **Template-reference comparison + prompt re-execution**
(:func:`replay_with_template`): compares the stored template reference against a
pinned version, then re-executes the stored prompt. This isolates a template
reference mismatch — not a prompt regression, which would require re-rendering
the template with the held-constant data to produce a different prompt.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping

from tenantchat.api.schemas import (
    ComponentVersionSnapshot,
    ReplayOutput,
    ReplayTrialResult,
    TraceReplayResponse,
    TraceReplayRetrievalResponse,
    TraceReplayTemplateResponse,
    TraceReplayTrialsResponse,
)
from tenantchat.api.store import TurnRecord
from tenantchat.core.errors import GenerationUnavailableError, TraceReplayError
from tenantchat.core.routing import ROUTING_POLICY_VERSION
from tenantchat.orchestration.agents import AGENTS_VERSION
from tenantchat.orchestration.graph import GRAPH_VERSION
from tenantchat.orchestration.model import (
    AssembledMessage,
    AssembledPrompt,
    ChatModel,
    ModelResponse,
    PromptRegion,
    PromptSegment,
)
from tenantchat.orchestration.prompts import DEFAULT_REGISTRY, DISPATCH_SYSTEM_REF
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
    return {
        "graph": GRAPH_VERSION,
        "prompt_template": {"ref": DISPATCH_SYSTEM_REF},
        "routing_policy": ROUTING_POLICY_VERSION,
        "agents": AGENTS_VERSION,
        "tools": TOOLS_VERSION,
        "retriever": retriever,
        "model": {"id": model_name or None, "parameters": {}},
    }


async def _complete(model: ChatModel, prompt: AssembledPrompt) -> ModelResponse:
    return await model.complete(prompt, tools=())


async def replay_turn(
    *,
    record: TurnRecord,
    model: ChatModel,
    retriever: Mapping[str, object] | None,
) -> TraceReplayResponse:
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


async def replay_trials(
    *,
    record: TurnRecord,
    model: ChatModel,
    retriever: Mapping[str, object] | None,
    trials: int,
) -> TraceReplayTrialsResponse:
    content = record.content
    prompt_section = content.get("prompt")
    if not isinstance(prompt_section, Mapping):
        raise TraceReplayError(detail="trace prompt section absent")
    try:
        rebuilt = reconstruct_prompt(prompt_section)
    except ValueError as error:
        raise TraceReplayError(detail="stored prompt not reconstructible") from error

    stored_hash = str(prompt_section.get("content_hash", ""))
    output = content.get("output", {})
    raw_output = str(output.get("raw", "")) if isinstance(output, Mapping) else ""
    stored_manifest = content.get("component_manifest", {})
    if not isinstance(stored_manifest, Mapping):
        stored_manifest = {}

    trial_results: list[ReplayTrialResult] = []
    representative_model_name = "unknown"
    for index in range(trials):
        response = await _complete(model, rebuilt)
        if index == 0:
            representative_model_name = response.model_name
        trial_results.append(
            ReplayTrialResult(
                trial_index=index,
                content_hash=rebuilt.content_hash,
                model_name=response.model_name,
                output_raw=response.content,
            )
        )

    current = current_manifest(representative_model_name, retriever)
    components, changed = compare_manifests(stored_manifest, current)

    return TraceReplayTrialsResponse(
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
        trials=trial_results,
        trial_count=trials,
    )


async def replay_with_retrieval(
    *,
    record: TurnRecord,
    model: ChatModel,
    retriever: Mapping[str, object] | None,
    generation_exists: bool,
    gold_evidence: list[dict[str, str]] | None = None,
) -> TraceReplayRetrievalResponse:
    content = record.content
    prompt_section = content.get("prompt")
    if not isinstance(prompt_section, Mapping):
        raise TraceReplayError(detail="trace prompt section absent")

    if not generation_exists:
        generation_id = _stored_generation_id(content)
        raise GenerationUnavailableError(
            detail=f"generation {generation_id} is no longer in the index"
        )

    try:
        rebuilt = reconstruct_prompt(prompt_section)
    except ValueError as error:
        raise TraceReplayError(detail="stored prompt not reconstructible") from error

    if gold_evidence:
        rebuilt = _substitute_gold_evidence(rebuilt, gold_evidence)

    stored_hash = str(prompt_section.get("content_hash", ""))
    response = await _complete(model, rebuilt)
    current = current_manifest(response.model_name, retriever)
    stored_manifest = content.get("component_manifest", {})
    if not isinstance(stored_manifest, Mapping):
        stored_manifest = {}
    components, changed = compare_manifests(stored_manifest, current)

    output = content.get("output", {})
    raw_output = str(output.get("raw", "")) if isinstance(output, Mapping) else ""
    return TraceReplayRetrievalResponse(
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
        generation_available=True,
        generation_id=_stored_generation_id(content),
        gold_evidence_count=len(gold_evidence) if gold_evidence else 0,
    )


async def replay_with_template(
    *,
    record: TurnRecord,
    model: ChatModel,
    retriever: Mapping[str, object] | None,
    template_version: int | None = None,
) -> TraceReplayTemplateResponse:
    content = record.content
    prompt_section = content.get("prompt")
    if not isinstance(prompt_section, Mapping):
        raise TraceReplayError(detail="trace prompt section absent")
    try:
        rebuilt = reconstruct_prompt(prompt_section)
    except ValueError as error:
        raise TraceReplayError(detail="stored prompt not reconstructible") from error

    template_ref = _resolve_template_ref(rebuilt, template_version)
    template_matches_current = template_ref == DISPATCH_SYSTEM_REF

    stored_hash = str(prompt_section.get("content_hash", ""))
    response = await _complete(model, rebuilt)
    current = current_manifest(response.model_name, retriever)
    stored_manifest = content.get("component_manifest", {})
    if not isinstance(stored_manifest, Mapping):
        stored_manifest = {}
    components, changed = compare_manifests(stored_manifest, current)

    output = content.get("output", {})
    raw_output = str(output.get("raw", "")) if isinstance(output, Mapping) else ""
    return TraceReplayTemplateResponse(
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
        template_ref=template_ref,
        template_matches_current=template_matches_current,
    )


def _stored_model_name(content: Mapping[str, object]) -> str:
    manifest = content.get("component_manifest")
    if isinstance(manifest, Mapping):
        model = manifest.get("model")
        if isinstance(model, Mapping):
            return str(model.get("id") or "")
    return ""


def _stored_generation_id(content: Mapping[str, object]) -> str | None:
    retrieval = content.get("retrieval")
    if isinstance(retrieval, Mapping):
        gid = retrieval.get("generation_id")
        if gid is not None:
            return str(gid)
    return None


def _resolve_template_ref(prompt: AssembledPrompt, version: int | None) -> str:
    import logging

    _logger = logging.getLogger(__name__)

    if version is not None:
        try:
            template = DEFAULT_REGISTRY.resolve(prompt.template_id, version)
            return template.ref
        except Exception:
            _logger.debug("template version %s not resolvable for %s", version, prompt.template_id)
    return prompt.template_ref


def _substitute_gold_evidence(
    prompt: AssembledPrompt, gold_evidence: list[dict[str, str]]
) -> AssembledPrompt:
    gold_count = len(gold_evidence)
    gold_index = 0
    evidence_index = 0

    new_messages: list[AssembledMessage] = []
    for message in prompt.messages:
        new_segments: list[PromptSegment] = []
        for segment in message.segments:
            if segment.region == PromptRegion.UNTRUSTED and segment.segment_id.startswith(
                "evidence-"
            ):
                if gold_index < gold_count:
                    chunk = gold_evidence[gold_index]
                    new_segments.append(
                        PromptSegment(
                            segment_id=f"gold-evidence-{gold_index}",
                            region=PromptRegion.UNTRUSTED,
                            text=chunk["text"],
                        )
                    )
                    gold_index += 1
                evidence_index += 1
            else:
                new_segments.append(segment)
        while gold_index < gold_count:
            chunk = gold_evidence[gold_index]
            new_segments.append(
                PromptSegment(
                    segment_id=f"gold-evidence-{gold_index}",
                    region=PromptRegion.UNTRUSTED,
                    text=chunk["text"],
                )
            )
            gold_index += 1
        new_messages.append(
            AssembledMessage(
                role=message.role,
                segments=tuple(new_segments),
                tool_calls=message.tool_calls,
                tool_call_id=message.tool_call_id,
            )
        )

    return AssembledPrompt(
        template_id=prompt.template_id,
        template_version=prompt.template_version,
        bindings=dict(prompt.bindings),
        messages=tuple(new_messages),
    )
