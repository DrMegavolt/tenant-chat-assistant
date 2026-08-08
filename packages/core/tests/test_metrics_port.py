"""The core metrics port: a closed, content-free vocabulary.

`OBS-002`'s acceptance criteria are mechanical: every label value is a bounded
enum or a tenant pseudonym, and the reachable vocabulary has a cardinality
ceiling so a new value cannot quietly multiply series. These tests pin the
vocabulary this package owns; the cross-package ceiling (routing, tools, and
the ``none`` sentinel included) is asserted in ``services/api/tests`` where the
adapter lives.
"""

from __future__ import annotations

from enum import StrEnum

from tenantchat.core.metrics import (
    BOUNDED_LABEL_VALUE_ENUMS,
    METRIC_CARDINALITY_CEILING,
    METRIC_LABELS,
    MetricLabelName,
    MetricName,
    label_value_is_safe,
)


def test_metric_names_are_unique_and_prefixed() -> None:
    """Two metrics cannot share a series name, and every name is namespaced.

    A duplicate name would make the Prometheus adapter raise at first use
    instead of at review; a missing ``tenantchat_`` prefix would let a series
    collide with something outside this product's namespace.
    """
    names = [name.value for name in MetricName]
    assert len(names) == len(set(names))
    assert all(name.startswith("tenantchat_") for name in names)


def test_every_metric_carries_its_label_contract() -> None:
    """Every metric names its labels, and every label name is in the closed set.

    A metric without an entry would be recordable with no labels (fine) but
    would silently bypass review of what dimensions it may carry.
    """
    assert set(METRIC_LABELS) == set(MetricName)
    label_names = {label.value for labels in METRIC_LABELS.values() for label in labels}
    assert label_names <= {label.value for label in MetricLabelName}


def test_every_owned_label_value_is_bounded_and_disjoint_per_label_name() -> None:
    """Every value this package owns is recordable, and labels never mix.

    ``label_value_is_safe`` is the adapter's runtime gate, so every enum member
    must pass it. Two enums that can appear on the *same label name* must not
    share a value — a shared value would collapse two label meanings into one
    series. Enums on different labels (``ToolOutcome`` versus
    ``ActionStatus``) may coincide harmlessly: they can never appear on the
    same label.
    """
    from tenantchat.core.metrics import (
        ActionStatus,
        CitationVerdict,
        Operation,
        RetrievalVerdict,
        Status,
        TokenKind,
        ToolOutcome,
        TruncationKind,
        TurnOutcome,
    )
    from tenantchat.core.resilience import CircuitState, Dependency, FailureKind

    enums_by_label: dict[str, tuple[type[StrEnum], ...]] = {
        # The label names the metrics contract distributes, from METRIC_LABELS:
        # every core family that feeds a label, and only those.
        "outcome": (TurnOutcome, ToolOutcome),
        "status": (Status, ActionStatus),
        "verdict": (RetrievalVerdict, CitationVerdict),
        "operation": (Operation,),
        "kind": (TokenKind, TruncationKind),
        "dependency": (Dependency,),
        "reason": (FailureKind,),
        "state": (CircuitState,),
    }
    for label, enums in enums_by_label.items():
        members = {member.value for enum in enums for member in enum}
        assert all(label_value_is_safe(value) for value in members), label
        assert len(members) == sum(
            len(enum) for enum in enums
        ), f"overlapping values on the {label!r} label: {members}"


def test_the_owned_vocabulary_stays_under_the_cardinality_ceiling() -> None:
    """The values this package contributes to any label stay bounded.

    The ceiling is asserted against the *union* of every label-value family in
    the repository's metric tests; this test guards the half that lives here
    so a new enum cannot grow the whole vocabulary silently.
    """
    owned = sum(len(enum) for enum in BOUNDED_LABEL_VALUE_ENUMS)
    assert owned < METRIC_CARDINALITY_CEILING


def test_free_text_is_never_a_safe_label_value() -> None:
    """Anything a turn could produce as prose fails the charset.

    This is the property the adapter relies on: a visitor message, a name, or
    an address always carries a space or a character outside the pattern, so
    it is refused before it can become a series label. Contact details whose
    characters would pass (a bare phone number, an email) are caught by the
    vocabulary-membership assertions in ``services/api/tests`` instead: the
    charset narrows the surface, the vocabulary closes it.
    """
    for value in (
        "Book an appointment Tuesday",
        "12 Alder Court, Portland, OR",
        "Dana Ruiz",
        "call me back please!",
        "What are your hours of operation?",
        "session-1:turn-2",
        "We open at 9:00",
    ):
        assert not label_value_is_safe(value), value
    for value in ("answered", "dispatch-system@3", "check_service_area", "t-abc1234"):
        assert label_value_is_safe(value), value
