"""OfferedSlot construction: a slot cannot exist with naive or inverted bounds.

Parsing over validating: construction is the one place to reject a bad slot,
so every consumer downstream compares and persists bounds that are complete.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tenantchat.core.errors import ValidationError
from tenantchat.core.slots import OfferedSlot

START = datetime.now(UTC) + timedelta(days=1)


def test_an_aware_ordered_window_constructs() -> None:
    slot = OfferedSlot(
        id="slot-1", service_slug="hvac", start=START, end=START + timedelta(hours=1)
    )

    assert slot.end > slot.start


def test_a_naive_start_is_rejected_at_construction() -> None:
    """A naive bound once surfaced as a bare TypeError at comparison time."""
    with pytest.raises(ValidationError) as caught:
        OfferedSlot(
            id="slot-1",
            service_slug="hvac",
            start=START.replace(tzinfo=None),
            end=START + timedelta(hours=1),
        )

    assert caught.value.detail == "start must be timezone-aware"


def test_a_naive_end_is_rejected_at_construction() -> None:
    with pytest.raises(ValidationError) as caught:
        OfferedSlot(
            id="slot-1",
            service_slug="hvac",
            start=START,
            end=(START + timedelta(hours=1)).replace(tzinfo=None),
        )

    assert caught.value.detail == "end must be timezone-aware"


def test_an_end_not_after_start_is_rejected_at_construction() -> None:
    with pytest.raises(ValidationError) as caught:
        OfferedSlot(id="slot-1", service_slug="hvac", start=START, end=START - timedelta(minutes=1))

    assert caught.value.detail == "slot end is not after start"


def test_a_zero_length_window_is_rejected_at_construction() -> None:
    """A zero-length window books nothing; it is invalid, not merely empty."""
    with pytest.raises(ValidationError) as caught:
        OfferedSlot(id="slot-1", service_slug="hvac", start=START, end=START)

    assert caught.value.detail == "slot end is not after start"
