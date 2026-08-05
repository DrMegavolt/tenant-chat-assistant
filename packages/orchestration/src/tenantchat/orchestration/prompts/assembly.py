"""Assembly: turn a template, tenant policy, workflow state, history, and
retrieved evidence into the one prompt the model adapter accepts (`AI-003`).

The template and everything the server wrote are **trusted** and are never
excluded: if they do not fit the budget, assembly fails loudly. Visitor turns
and retrieved evidence are **untrusted** — they are marked as such on the
assembled type, which is the single boundary `RAG-007` enforces — and they are
admitted until the budgets are spent. Everything left out is returned in
:attr:`AssemblyOutcome.excluded` with its reason; nothing is dropped without a
record.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from tenantchat.core.tenant import TenantPolicy
from tenantchat.orchestration.model import (
    AssembledMessage,
    AssembledPrompt,
    MessageRole,
    PromptRegion,
    PromptSegment,
    ToolCall,
)
from tenantchat.orchestration.prompts.registry import TemplateVersion
from tenantchat.orchestration.prompts.schema import PromptBindingError

# A deterministic token estimate: one token per four characters. Providers
# return real counts (`OBS-002`), but assembly needs a number before the call
# and this one is stable and testable; `AI-002` tunes it against provider data.
CHARS_PER_TOKEN = 4

_PLACEHOLDER_RE = re.compile(r"\{([a-z][a-z0-9_]*)\}")


def estimate_tokens(text: str) -> int:
    """Estimate how many tokens ``text`` costs, deterministically."""
    return math.ceil(len(text) / CHARS_PER_TOKEN)


class PromptBudgetError(ValueError):
    """The fixed part of the prompt cannot fit the budget.

    Nothing trusted is ever excluded — the template, the visitor's latest
    message, and pending tool calls are mandatory — so when those alone exceed
    the total budget, assembly fails loudly instead of silently sending a
    prompt over budget.
    """


class ExcludedKind(StrEnum):
    """Which input an exclusion came from."""

    HISTORY = "history"
    EVIDENCE = "evidence"


class ExcludedReason(StrEnum):
    """Why one input item was left out of the assembled prompt."""

    HISTORY_BUDGET = "history_token_budget"
    EVIDENCE_BUDGET = "evidence_token_budget"
    SOURCE_BUDGET = "source_budget"
    TOTAL_BUDGET = "total_budget"


@dataclass(frozen=True, slots=True)
class ExcludedItem:
    """One input item assembly dropped, with the record of why.

    ``position`` is the item's index in the input it came from, ``reference``
    names it for the operator (a source ID for evidence), and ``tokens`` is the
    estimated size that did not fit. `OBS-004` persists these in the inference
    trace plane, which is the one place budget cuts are allowed to live.
    """

    kind: ExcludedKind
    position: int
    reference: str
    reason: ExcludedReason
    tokens: int


@dataclass(frozen=True, slots=True)
class AssemblyOutcome:
    """The assembled prompt plus everything that was not included."""

    prompt: AssembledPrompt
    excluded: tuple[ExcludedItem, ...]


@dataclass(frozen=True, slots=True)
class PromptBudget:
    """The caps assembly enforces. Units are estimated tokens (4 chars each).

    ``max_history_tokens`` and ``max_evidence_tokens`` cap discretionary
    untrusted content; ``max_sources`` caps how many evidence passages fit at
    all; ``max_total_tokens`` caps the whole prompt and is the only budget that
    also constrains the fixed, trusted part.
    """

    max_total_tokens: int = 4000
    max_history_tokens: int = 2000
    max_evidence_tokens: int = 1500
    max_sources: int = 8


@dataclass(frozen=True, slots=True)
class HistoryTurn:
    """One conversation entry as assembly sees it: role, text, tool shape."""

    role: MessageRole
    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None


@dataclass(frozen=True, slots=True)
class PromptEvidence:
    """One retrieved passage, always assembled as an untrusted segment.

    `RAG-005` owns the citation contract; this is the input seam assembly
    requires — a reference, a title for the viewer, and the content. The
    reference is what an exclusion record names, so a budget cut stays
    attributable.
    """

    source_id: str
    title: str
    content: str


def assemble_prompt(
    template: TemplateVersion,
    *,
    policy: TenantPolicy,
    workflow: Mapping[str, object],
    history: Sequence[HistoryTurn],
    evidence: Sequence[PromptEvidence],
    budget: PromptBudget,
) -> AssemblyOutcome:
    """Assemble the one prompt a model call may receive.

    The system message is the rendered template (trusted segments) followed by
    the admitted evidence (untrusted segments). The transcript follows in
    chronological order, with visitor turns marked untrusted. Discretionary
    history is admitted newest-first and evidence in the order given, each until
    its budget is spent; every item left out is returned in
    :attr:`AssemblyOutcome.excluded` with its reason.

    Raises:
        PromptBindingError: the tenant's values do not fill the declared slots.
        PromptBudgetError: the template plus mandatory transcript content
            exceeds ``max_total_tokens``.
    """
    values = dict(template.bindings(policy, workflow))
    template.schema.validate(values)
    system_segments = _render(template, values)
    system_tokens = estimate_tokens("".join(segment.text for segment in system_segments))

    mandatory_indexes, discretionary = _history_split(history)
    mandatory_tokens = sum(estimate_tokens(history[index].content) for index in mandatory_indexes)
    fixed_tokens = system_tokens + mandatory_tokens
    if fixed_tokens > budget.max_total_tokens:
        raise PromptBudgetError(
            f"fixed prompt content is {fixed_tokens} tokens, above max_total_tokens "
            f"({budget.max_total_tokens}); nothing trusted can be excluded"
        )

    remaining = budget.max_total_tokens - fixed_tokens
    selected_history, excluded, remaining = _fit_history(
        discretionary,
        sub_budget=budget.max_history_tokens,
        total_remaining=remaining,
    )
    selected_evidence, evidence_excluded, remaining = _fit_evidence(
        evidence,
        sub_budget=budget.max_evidence_tokens,
        max_sources=budget.max_sources,
        total_remaining=remaining,
    )
    excluded.extend(evidence_excluded)

    positions = sorted(mandatory_indexes | {index for index, _ in selected_history})
    history_messages = [_history_message(history[index], index) for index in positions]
    evidence_segments = tuple(
        PromptSegment(
            segment_id=f"evidence:{item.source_id}",
            region=PromptRegion.UNTRUSTED,
            text=f"{item.title}\n{item.content}",
        )
        for item in selected_evidence
    )
    prompt = AssembledPrompt(
        template_id=template.template_id,
        template_version=template.version,
        bindings=values,
        messages=(
            AssembledMessage(role=MessageRole.SYSTEM, segments=system_segments + evidence_segments),
            *history_messages,
        ),
    )
    return AssemblyOutcome(prompt=prompt, excluded=tuple(excluded))


def _render(template: TemplateVersion, values: Mapping[str, str]) -> tuple[PromptSegment, ...]:
    """Render the template's segments, dropping any whose slots are all empty.

    Dropping is by placeholder value, not by resolved text: a segment like
    ``Note: {disclaimers}`` must disappear whole when a tenant provides no
    disclaimer, rather than leave its static label behind.
    """
    segments: list[PromptSegment] = []
    for template_segment in template.segments:
        placeholders = _PLACEHOLDER_RE.findall(template_segment.text)
        if placeholders and all(not values[name] for name in placeholders):
            continue
        try:
            text = template_segment.text.format(**values)
        except (KeyError, ValueError) as error:
            raise PromptBindingError(
                f"template {template.ref} cannot render segment "
                f"{template_segment.segment_id!r}: {error}"
            ) from error
        segments.append(
            PromptSegment(
                segment_id=template_segment.segment_id,
                region=template_segment.region,
                text=text,
            )
        )
    return tuple(segments)


def _history_split(
    history: Sequence[HistoryTurn],
) -> tuple[set[int], list[tuple[int, HistoryTurn]]]:
    """Separate the mandatory tail from what the budget may admit.

    Two things must reach the model or the conversation breaks: the visitor's
    latest message, and the most recent assistant turn with tool calls together
    with everything after it (tool results reference those calls). Everything
    else is discretionary, admitted newest-first.
    """
    mandatory_indexes: set[int] = set()
    newest_user = next(
        (
            len(history) - 1 - offset
            for offset, turn in enumerate(reversed(history))
            if turn.role is MessageRole.USER
        ),
        None,
    )
    if newest_user is not None:
        mandatory_indexes.add(newest_user)
    newest_calls = next(
        (
            len(history) - 1 - offset
            for offset, turn in enumerate(reversed(history))
            if turn.role is MessageRole.ASSISTANT and turn.tool_calls
        ),
        None,
    )
    if newest_calls is not None:
        mandatory_indexes.update(range(newest_calls, len(history)))
    discretionary = [
        (index, turn) for index, turn in enumerate(history) if index not in mandatory_indexes
    ]
    discretionary.sort(key=lambda pair: pair[0], reverse=True)
    return mandatory_indexes, discretionary


def _fit_history(
    entries: list[tuple[int, HistoryTurn]],
    *,
    sub_budget: int,
    total_remaining: int,
) -> tuple[list[tuple[int, HistoryTurn]], list[ExcludedItem], int]:
    selected: list[tuple[int, HistoryTurn]] = []
    excluded: list[ExcludedItem] = []
    sub_left = sub_budget
    remaining = total_remaining
    for position, turn in entries:
        tokens = estimate_tokens(turn.content)
        if tokens > sub_left or tokens > remaining:
            reason = (
                ExcludedReason.TOTAL_BUDGET if tokens > remaining else ExcludedReason.HISTORY_BUDGET
            )
            excluded.append(
                ExcludedItem(
                    kind=ExcludedKind.HISTORY,
                    position=position,
                    reference=turn.role.value,
                    reason=reason,
                    tokens=tokens,
                )
            )
            continue
        selected.append((position, turn))
        sub_left -= tokens
        remaining -= tokens
    return selected, excluded, remaining


def _fit_evidence(
    evidence: Sequence[PromptEvidence],
    *,
    sub_budget: int,
    max_sources: int,
    total_remaining: int,
) -> tuple[list[PromptEvidence], list[ExcludedItem], int]:
    selected: list[PromptEvidence] = []
    excluded: list[ExcludedItem] = []
    sub_left = sub_budget
    remaining = total_remaining
    for position, item in enumerate(evidence):
        tokens = estimate_tokens(item.content)
        if len(selected) >= max_sources:
            reason = ExcludedReason.SOURCE_BUDGET
        elif tokens > sub_left:
            reason = ExcludedReason.EVIDENCE_BUDGET
        elif tokens > remaining:
            reason = ExcludedReason.TOTAL_BUDGET
        else:
            selected.append(item)
            sub_left -= tokens
            remaining -= tokens
            continue
        excluded.append(
            ExcludedItem(
                kind=ExcludedKind.EVIDENCE,
                position=position,
                reference=item.source_id,
                reason=reason,
                tokens=tokens,
            )
        )
    return selected, excluded, remaining


def _history_message(turn: HistoryTurn, position: int) -> AssembledMessage:
    if turn.role is MessageRole.USER:
        return AssembledMessage(
            role=turn.role,
            segments=(PromptSegment(f"user:{position}", PromptRegion.UNTRUSTED, turn.content),),
        )
    region = (
        PromptRegion.TRUSTED
        if turn.role in (MessageRole.ASSISTANT, MessageRole.TOOL)
        else PromptRegion.UNTRUSTED
    )
    return AssembledMessage(
        role=turn.role,
        segments=(PromptSegment(f"{turn.role.value}:{position}", region, turn.content),),
        tool_calls=turn.tool_calls,
        tool_call_id=turn.tool_call_id,
    )
