"""Production composition must not silently fall back to process memory."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

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
    "drop_credential",
    [
        pytest.param(lambda given: replace(given, admin_gateway_token=None), id="gateway-token"),
        pytest.param(lambda given: replace(given, admin_csrf_secret=None), id="csrf-secret"),
    ],
)
def test_production_composition_requires_the_admin_credentials(
    settings: Settings, drop_credential: Callable[[Settings], Settings]
) -> None:
    """A deployment missing one fails at startup rather than at the first login.

    Both are shared with the gateway, so an absent value means the console
    rejects every operator. That reads as a broken login, and an operator
    debugging a broken login does not look for an unset variable.
    """
    deployed = replace(settings, database_url="postgresql+psycopg://user@host/db")

    with pytest.raises(ValueError, match="admin routes require"):
        create_app(drop_credential(deployed))


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
