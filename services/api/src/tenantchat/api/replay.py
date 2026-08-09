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

② **Generation-pinned retrieval replay** (:func:`replay_with_retrieval`):
reranks the exact retained index generation, rebuilds the evidence segments,
then re-executes the resulting prompt. Gold evidence can replace that result.

③ **Template-version-pinned replay** (:func:`replay_with_template`): renders the
selected retained template using the stored bindings while holding evidence and
conversation history constant, then re-executes that counterfactual prompt.
"""

from __future__ import annotations

import asyncio
import json
import time
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
from tenantchat.core.errors import (
    GenerationUnavailableError,
    ReplayModelUnavailableError,
    ReplayTimeoutError,
    TraceReplayError,
)
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
from tenantchat.orchestration.prompts.assembly import render_template_segments
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


async def _complete(
    model: ChatModel, prompt: AssembledPrompt, *, timeout_seconds: float
) -> ModelResponse:
    """Send one assembled prompt through the model inside an end-to-end deadline.

    A long or unreachable model must not hang the replay console. The
    ``timeout_seconds`` deadline is the outer budget: if the model client's
    own resilience envelope (retries, the fallback chain, and the provider
    read timeout) has not produced a result by then this function cancels and
    maps the failure to a typed error the API and UI can distinguish.
    """
    started = time.monotonic()
    try:
        return await asyncio.wait_for(
            model.complete(prompt, tools=()),
            timeout=timeout_seconds,
        )
    except TimeoutError as exc:
        elapsed = time.monotonic() - started
        raise ReplayTimeoutError(
            detail=f"replay timed out after {elapsed:.1f}s (limit={timeout_seconds:.0f}s)"
        ) from exc
    except BaseException as exc:
        if isinstance(exc, TraceReplayError | KeyboardInterrupt | SystemExit):
            raise
        elapsed = time.monotonic() - started
        raise ReplayModelUnavailableError(
            detail=f"model call failed after {elapsed:.1f}s: {type(exc).__name__}: {exc}"
        ) from exc


async def replay_turn(
    *,
    record: TurnRecord,
    model: ChatModel,
    retriever: Mapping[str, object] | None,
    replay_timeout_seconds: float = 120,
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
    started = time.monotonic()
    response = await _complete(model, rebuilt, timeout_seconds=replay_timeout_seconds)
    elapsed = time.monotonic() - started
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
        elapsed_seconds=round(elapsed, 1),
    )


async def replay_trials(
    *,
    record: TurnRecord,
    model: ChatModel,
    retriever: Mapping[str, object] | None,
    trials: int,
    replay_timeout_seconds: float = 120,
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
    started = time.monotonic()
    for index in range(trials):
        response = await _complete(model, rebuilt, timeout_seconds=replay_timeout_seconds)
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
    elapsed = time.monotonic() - started

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
        elapsed_seconds=round(elapsed, 1),
    )


async def replay_with_retrieval(
    *,
    record: TurnRecord,
    model: ChatModel,
    retriever: Mapping[str, object] | None,
    generation_exists: bool,
    retrieved_evidence: list[dict[str, str]] | None = None,
    gold_evidence: list[dict[str, str]] | None = None,
    replay_timeout_seconds: float = 120,
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

    replay_evidence = gold_evidence if gold_evidence is not None else retrieved_evidence
    if replay_evidence is None:
        raise TraceReplayError(detail="generation retrieval result absent")
    rebuilt = _substitute_evidence(rebuilt, replay_evidence)

    stored_hash = str(prompt_section.get("content_hash", ""))
    started = time.monotonic()
    response = await _complete(model, rebuilt, timeout_seconds=replay_timeout_seconds)
    elapsed = time.monotonic() - started
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
        elapsed_seconds=round(elapsed, 1),
    )


async def replay_with_template(
    *,
    record: TurnRecord,
    model: ChatModel,
    retriever: Mapping[str, object] | None,
    template_version: int | None = None,
    replay_timeout_seconds: float = 120,
) -> TraceReplayTemplateResponse:
    content = record.content
    prompt_section = content.get("prompt")
    if not isinstance(prompt_section, Mapping):
        raise TraceReplayError(detail="trace prompt section absent")
    try:
        rebuilt = reconstruct_prompt(prompt_section)
    except ValueError as error:
        raise TraceReplayError(detail="stored prompt not reconstructible") from error

    rebuilt, template_ref = _rerender_template(rebuilt, template_version)
    template_matches_current = template_ref == DISPATCH_SYSTEM_REF

    stored_hash = str(prompt_section.get("content_hash", ""))
    started = time.monotonic()
    response = await _complete(model, rebuilt, timeout_seconds=replay_timeout_seconds)
    elapsed = time.monotonic() - started
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
        elapsed_seconds=round(elapsed, 1),
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


def _rerender_template(prompt: AssembledPrompt, version: int | None) -> tuple[AssembledPrompt, str]:
    selected_version = prompt.template_version if version is None else version
    try:
        template = DEFAULT_REGISTRY.resolve(prompt.template_id, selected_version)
        values = {
            slot.name: str(prompt.bindings.get(slot.name, "")) for slot in template.schema.slots
        }
        base, trailing = render_template_segments(template, values)
    except (KeyError, TypeError, ValueError) as error:
        raise TraceReplayError(detail="selected template cannot render stored bindings") from error

    messages: list[AssembledMessage] = []
    replaced_system = False
    for message in prompt.messages:
        if message.role.value == "system" and not replaced_system:
            evidence = tuple(
                segment
                for segment in message.segments
                if segment.segment_id.startswith("evidence:")
            )
            messages.append(
                AssembledMessage(
                    role=message.role,
                    segments=(*base, *evidence, *trailing),
                    tool_calls=message.tool_calls,
                    tool_call_id=message.tool_call_id,
                )
            )
            replaced_system = True
        else:
            messages.append(message)
    if not replaced_system:
        raise TraceReplayError(detail="stored prompt has no system message")
    return (
        AssembledPrompt(
            template_id=template.template_id,
            template_version=template.version,
            bindings=values,
            messages=tuple(messages),
        ),
        template.ref,
    )


def _substitute_evidence(
    prompt: AssembledPrompt, evidence: list[dict[str, str]]
) -> AssembledPrompt:
    new_messages: list[AssembledMessage] = []
    inserted = False
    for message in prompt.messages:
        new_segments: list[PromptSegment] = []
        insertion_index: int | None = None
        for segment in message.segments:
            if segment.segment_id.startswith("evidence:"):
                insertion_index = len(new_segments) if insertion_index is None else insertion_index
                continue
            new_segments.append(segment)
        if message.role.value == "system" and not inserted:
            at = len(new_segments) if insertion_index is None else insertion_index
            replacements = [
                PromptSegment(
                    segment_id=f"evidence:{chunk['source_id']}",
                    region=PromptRegion.UNTRUSTED,
                    text=(
                        f'<evidence source_id="{chunk["source_id"]}">\n'
                        f'{chunk["text"]}\n</evidence>'
                    ),
                )
                for chunk in evidence
            ]
            new_segments[at:at] = replacements
            inserted = True
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
