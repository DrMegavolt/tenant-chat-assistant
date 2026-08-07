"""The metrics plane: a closed, content-free vocabulary and the port that records it.

`ADR-0010` gives the operational plane identifiers and no content; this module is
the metric half of that contract. Everything that can reach a metric — the name,
every label name, every label value — is a closed enum or a tenant pseudonym, so
a label value containing query text, a contact detail, or a session ID is not a
review failure but a type error waiting for a call site that broke the contract.

The enforcement is layered. Call sites pass values from the enums below (or the
pseudonym), the adapter rejects any value outside the bounded charset and any
label name outside the per-metric contract, and
`services/api/tests/test_metrics.py` asserts the whole reachable vocabulary stays
under :data:`METRIC_CARDINALITY_CEILING`, so a new enum member cannot quietly
multiply series.

The port lives in core because orchestration nodes and service adapters both
record, and `ADR-0001` keeps their shared vocabulary on the domain side. The
Prometheus adapter implements it in the service that owns the I/O.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from enum import StrEnum
from typing import Final, Protocol

# Prometheus label values. The charset is deliberately narrower than Prometheus
# permits: a real label value (an intent, a tool, a verdict) is lowercase
# ``[a-z0-9_.-]``, and anything with a space, a slash, or an uppercase letter
# cannot be a value an enum produced. ``@`` is admitted for one closed value
# family: registry artifact refs such as ``dispatch-system@3``, which the
# versioned template registry enumerates. Free text is structurally impossible
# either way — a visitor message or a contact detail always carries a space or
# a character outside the pattern — and the end-to-end suite asserts no PII
# value ever reaches a label.
LABEL_VALUE_PATTERN: Final = re.compile(r"\A[a-z0-9_.@-]{1,64}\Z")

# The ceiling the cardinality test asserts every metric stays under. The
# reachable vocabulary is the union of the closed enums below plus the routing,
# tool, intent, resilience, and executed-graph enums and the one ``none``
# sentinel; a new label dimension is a deliberate widening that must raise this
# number with a test.
METRIC_CARDINALITY_CEILING: Final = 80

# The value the routing metric records for the intent label when the router
# chose no intent (a clarification): the clarify outcome has no chosen intent,
# and the label still needs a bounded value.
ROUTING_NONE_INTENT: Final = "none"


class MetricName(StrEnum):
    """The closed inventory of every metric the platform records.

    Names carry the Prometheus suffix convention (``_total`` for counters,
    ``_seconds`` for durations) so the scrape contract is visible at a glance
    and a new metric is a reviewable addition to this enum plus its adapter
    definition.
    """

    TURN_LATENCY = "tenantchat_turn_latency_seconds"
    TURN_OUTCOMES = "tenantchat_turn_outcomes_total"
    LLM_CALLS = "tenantchat_llm_calls_total"
    LLM_LATENCY = "tenantchat_llm_latency_seconds"
    LLM_TOKENS = "tenantchat_llm_tokens_total"
    RETRIEVAL_RUNS = "tenantchat_retrieval_runs_total"
    RETRIEVAL_LATENCY = "tenantchat_retrieval_latency_seconds"
    RETRIEVAL_CANDIDATES = "tenantchat_retrieval_candidates_total"
    TOOL_CALLS = "tenantchat_tool_calls_total"
    TOOL_LATENCY = "tenantchat_tool_latency_seconds"
    NODE_LATENCY = "tenantchat_node_latency_seconds"
    ROUTING_DECISIONS = "tenantchat_routing_decisions_total"
    BUSINESS_ACTIONS = "tenantchat_business_actions_total"
    BUSINESS_LATENCY = "tenantchat_business_latency_seconds"
    CITATION_VALIDATION = "tenantchat_citation_validation_total"
    FEEDBACK_SUBMITTED = "tenantchat_feedback_submitted_total"
    DEPENDENCY_RETRIES = "tenantchat_dependency_retries_total"
    CIRCUIT_STATE = "tenantchat_circuit_state"


class MetricKind(StrEnum):
    """What a metric aggregates; the adapter builds the matching collector."""

    COUNTER = "counter"
    HISTOGRAM = "histogram"
    GAUGE = "gauge"


class MetricLabelName(StrEnum):
    """The closed set of label names any metric may carry.

    Bounded by construction: an adapter rejects a label name that is not the
    per-metric contract, so a call site cannot invent a dimension (and with it
    a place to leak something) by spelling a new key.
    """

    OPERATION = "operation"
    OUTCOME = "outcome"
    STATUS = "status"
    INTENT = "intent"
    RULE = "rule"
    TOOL = "tool"
    NODE = "node"
    KIND = "kind"
    TEMPLATE = "template"
    VERDICT = "verdict"
    RATING = "rating"
    DEPENDENCY = "dependency"
    REASON = "reason"
    STATE = "state"


class Operation(StrEnum):
    """The named critical actions a shared latency metric discriminates."""

    TURN = "turn"
    BOOKING = "booking"
    LEAD = "lead"
    HANDOFF = "handoff"


class Status(StrEnum):
    """Call outcome, coarse enough to be a bounded enum and nothing more."""

    OK = "ok"
    ERROR = "error"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"


class TurnOutcome(StrEnum):
    """How a turn ended, by class

    ``CLARIFIED`` and ``ABSTAINED`` are the quality classes of the routing and
    retrieval contracts (AGENT-001, RAG-005): a clarification is the router
    declining to guess, an abstention is the retrieval verdict declining to
    answer. ``PAUSED`` is a turn that stopped at a confirmation.
    ``ANSWER_REFUSED`` is the `RAG-007` claim validator rejecting an answer
    whole, which is a quality class of its own: the model produced prose and the
    server would not publish it. Its value is qualified rather than a bare
    ``refused`` because ``outcome`` is a shared label name and ``ToolOutcome``
    already spends that value — a query for one must never match the other.
    ``FAILED`` is a turn whose graph crashed mid-run (`OBS-006`), qualified for
    the same reason: ``ToolOutcome`` already spends ``failed``, and an
    ``outcome="failed"`` query must not match a failed tool call.

    The classes partition every terminal path, so the sum across this label
    equals the number of turns the API completed. A path that ends a turn
    without recording one of these is a bug, not an unclassified turn.
    """

    ANSWERED = "answered"
    CLARIFIED = "clarified"
    ABSTAINED = "abstained"
    ANSWER_REFUSED = "answer_refused"
    HANDED_OFF = "handed_off"
    PAUSED = "paused"
    FAILED = "turn_failed"


class ToolOutcome(StrEnum):
    """What running one tool call produced."""

    SUCCEEDED = "succeeded"
    REFUSED = "refused"
    FAILED = "failed"


class RetrievalVerdict(StrEnum):
    """The abstention verdict (`RAG-005`), mirrored onto the metric plane."""

    SUFFICIENT = "sufficient"
    INSUFFICIENT = "insufficient"


class TokenKind(StrEnum):
    """The token accounting a provider returns, as the three bounded kinds."""

    PROMPT = "prompt"
    COMPLETION = "completion"
    TOTAL = "total"


class CitationVerdict(StrEnum):
    """Whether a citation the model wrote was inside its evidence context."""

    VALID = "valid"
    INVALID = "invalid"


class ActionStatus(StrEnum):
    """What an idempotent business action did with the attempt.

    ``COMMITTED`` is a first-time effect, ``REPLAYED`` is the idempotency key
    answering a duplicate attempt, and ``REFUSED`` is a policy, consent, or
    validation refusal. ``REPLAYED`` is counted separately so the committed
    series stays an exactly-once business count even though the action service
    is re-invoked. ``DECLINED`` is the customer refusing a proposed booking
    confirmation: no commit was attempted, and the funnel must still see it.
    """

    COMMITTED = "committed"
    REPLAYED = "replayed"
    REFUSED = "refused"
    DECLINED = "declined"


class FeedbackRatingValue(StrEnum):
    """How the visitor rated one turn, as the bounded label vocabulary.

    Distinct from ``tenantchat.core.reviews.FeedbackRating`` on purpose: this
    is the closed set a metric label may carry, and the two are asserted equal
    by the cardinality test so a new rating cannot reach a label silently.
    """

    UP = "up"
    DOWN = "down"


# The label names each metric may carry, as the adapter's hard contract. A label
# name that is not listed here is rejected at record time, so a metric can never
# grow a dimension by omission.
METRIC_LABELS: Final[Mapping[MetricName, tuple[MetricLabelName, ...]]] = {
    MetricName.TURN_LATENCY: (MetricLabelName.OPERATION,),
    MetricName.TURN_OUTCOMES: (MetricLabelName.OUTCOME,),
    MetricName.LLM_CALLS: (MetricLabelName.STATUS, MetricLabelName.TEMPLATE),
    MetricName.LLM_LATENCY: (MetricLabelName.STATUS, MetricLabelName.TEMPLATE),
    MetricName.LLM_TOKENS: (MetricLabelName.KIND, MetricLabelName.TEMPLATE),
    MetricName.RETRIEVAL_RUNS: (MetricLabelName.STATUS, MetricLabelName.VERDICT),
    MetricName.RETRIEVAL_LATENCY: (MetricLabelName.STATUS,),
    MetricName.RETRIEVAL_CANDIDATES: (),
    MetricName.TOOL_CALLS: (MetricLabelName.TOOL, MetricLabelName.OUTCOME),
    MetricName.TOOL_LATENCY: (MetricLabelName.TOOL, MetricLabelName.OUTCOME),
    MetricName.NODE_LATENCY: (MetricLabelName.NODE, MetricLabelName.STATUS),
    MetricName.ROUTING_DECISIONS: (
        MetricLabelName.INTENT,
        MetricLabelName.OUTCOME,
        MetricLabelName.RULE,
    ),
    MetricName.BUSINESS_ACTIONS: (MetricLabelName.OPERATION, MetricLabelName.STATUS),
    MetricName.BUSINESS_LATENCY: (MetricLabelName.OPERATION,),
    MetricName.CITATION_VALIDATION: (MetricLabelName.VERDICT,),
    MetricName.FEEDBACK_SUBMITTED: (MetricLabelName.RATING,),
    MetricName.DEPENDENCY_RETRIES: (MetricLabelName.DEPENDENCY, MetricLabelName.REASON),
    MetricName.CIRCUIT_STATE: (MetricLabelName.DEPENDENCY, MetricLabelName.STATE),
}

# The value enums this package owns. The routing, tool, and intent vocabularies
# live beside their definitions (``tenantchat.core.routing``,
# ``tenantchat.orchestration.tools``) and are folded into the cardinality test.
BOUNDED_LABEL_VALUE_ENUMS: Final[tuple[type[StrEnum], ...]] = (
    Operation,
    Status,
    TurnOutcome,
    ToolOutcome,
    RetrievalVerdict,
    TokenKind,
    CitationVerdict,
    ActionStatus,
    FeedbackRatingValue,
)


def label_value_is_safe(value: str) -> bool:
    """Whether *value* is admissible as a metric label value.

    The adapter refuses anything that is not: a visitor message, a contact
    detail, or any free text contains a space, an ``@``, or a character outside
    the pattern, and a value that passes the charset but is not in the bounded
    vocabulary fails the cardinality test instead.
    """
    return LABEL_VALUE_PATTERN.fullmatch(value) is not None


class MetricLabelError(ValueError):
    """A metric observation violated the label contract.

    Raised by the adapter before anything reaches the registry, so a call site
    that tries to record free text fails loudly in the turn that would have
    leaked it.
    """


class MetricsReporter(Protocol):
    """Records observations against the closed metric inventory.

    Implemented by the Prometheus adapter in ``services/api``; orchestration
    nodes and model adapters receive it through the composition root and never
    see a registry. ``value`` is a count or a duration: the adapter's
    per-metric definition decides whether it increments a counter or observes a
    histogram.

    Observation is an execution count, not a durable effect: a graph node that
    re-runs after a crash re-observes the work it re-executed. Exactly-once
    business counts are recorded by the idempotent domain services instead,
    where the replay verdict is known (`ActionStatus.REPLAYED`).
    """

    def observe(
        self,
        name: MetricName,
        value: float,
        *,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        """Record one observation.

        Raises:
            MetricLabelError: the name is not in the inventory, a label name is
                not part of the metric's contract, or a label value is not
                bounded-safe.
        """
        ...
