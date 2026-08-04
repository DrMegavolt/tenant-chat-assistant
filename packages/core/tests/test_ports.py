"""Specifications for the idempotency key the agent runtime commits through."""

from __future__ import annotations

import pytest

from tenantchat.core.errors import ValidationError
from tenantchat.core.ports import IdempotencyKey


def test_the_same_inputs_derive_the_same_key() -> None:
    """The property the whole replay guarantee rests on."""
    first = IdempotencyKey.derive("clearview", "session-9", "book_appointment", "1", "call-1")
    second = IdempotencyKey.derive("clearview", "session-9", "book_appointment", "1", "call-1")

    assert first == second


def test_a_different_turn_derives_a_different_key() -> None:
    """Two genuine attempts must not collapse into one committed action."""
    first = IdempotencyKey.derive("clearview", "session-9", "book_appointment", "1", "call-1")
    second = IdempotencyKey.derive("clearview", "session-9", "book_appointment", "2", "call-1")

    assert first != second


def test_a_derived_key_reveals_none_of_its_parts() -> None:
    """Keys are logged and stored, and the parts include a customer's own words.

    ADR-0010 keeps content out of the operational plane; a readable key would
    walk it straight in.
    """
    key = IdempotencyKey.derive("clearview", "session-9", "create_lead", "1", "Dana Ruiz")

    assert "Dana" not in key.value
    assert "clearview" not in key.value
    assert len(key.value) == 64


def test_part_boundaries_cannot_be_forged_by_concatenation() -> None:
    """``("ab", "c")`` and ``("a", "bc")`` are different attempts and must differ."""
    assert IdempotencyKey.derive("ab", "c") != IdempotencyKey.derive("a", "bc")


def test_deriving_without_parts_is_refused() -> None:
    """One key for every action in the system is the worst possible default."""
    with pytest.raises(ValueError, match="at least one"):
        IdempotencyKey.derive()


@pytest.mark.parametrize(
    "supplied",
    ["short", "", "  ", "has spaces in it", "unsafe/slash/value", "x" * 201],
    ids=["too-short", "empty", "blank", "spaces", "slashes", "too-long"],
)
def test_an_unusable_supplied_key_is_refused(supplied: str) -> None:
    """A caller-supplied key ends up in storage and logs, so its shape is bounded."""
    with pytest.raises(ValidationError):
        IdempotencyKey.parse(supplied)


def test_a_supplied_key_is_accepted_with_surrounding_whitespace_removed() -> None:
    assert IdempotencyKey.parse("  booking-2026-08-03-a1  ").value == "booking-2026-08-03-a1"
