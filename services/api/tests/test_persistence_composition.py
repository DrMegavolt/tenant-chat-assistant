"""Production composition must not silently fall back to process memory."""

from __future__ import annotations

import pytest

from tenantchat.api.app import create_app
from tenantchat.api.persistence import DatabasePoolSettings
from tenantchat.api.settings import Settings
from tenantchat.api.store import InMemoryBookingStore


def test_missing_database_url_fails_instead_of_building_process_local_stores(
    settings: Settings,
) -> None:
    with pytest.raises(ValueError, match="DATABASE_URL is required"):
        create_app(settings)


def test_partial_test_store_injection_is_rejected(settings: Settings) -> None:
    with pytest.raises(ValueError, match="inject all stores together"):
        create_app(settings, booking_store=InMemoryBookingStore())


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"size": 0}, "size"),
        ({"max_overflow": -1}, "overflow"),
        ({"timeout_seconds": 0}, "timeout"),
        ({"recycle_seconds": 0}, "recycle"),
    ],
)
def test_pool_bounds_reject_invalid_configuration(overrides: dict[str, int], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        DatabasePoolSettings(**overrides)
