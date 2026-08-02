"""Test fixtures for the HTTP edge.

Settings are constructed explicitly rather than read from the environment, so a
variable left in a developer's shell cannot change what these tests assert.

Payload builders are fixtures rather than importable helpers: pytest injects them
without a cross-module import, and no test module ends up depending on another's
import path.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient

from tenantchat.api.app import create_app
from tenantchat.api.settings import Settings

# Small enough that a body-limit test can exceed it without building a megabyte.
TEST_MAX_REQUEST_BYTES = 2048


@pytest.fixture
def settings() -> Settings:
    return Settings(
        allowed_origins=("https://widget.example",),
        max_request_bytes=TEST_MAX_REQUEST_BYTES,
        docs_enabled=True,
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    """A client over a freshly built app, so stored records never leak between tests."""
    # `raise_server_exceptions=False` returns the 500 an operator would see
    # instead of re-raising, which would hide whether the handler ran at all.
    with TestClient(create_app(settings), raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture
def booking_payload() -> Callable[..., dict[str, object]]:
    """A complete, valid booking against the seeded booking-enabled tenant."""

    def build(**overrides: object) -> dict[str, object]:
        return {
            "tenant_id": "clearview",
            "session_id": "session-test",
            "service": "HVAC",
            "slot": "Mon Jul 1, 2:00 PM",
            "customer_name": "Dana Ruiz",
            "address": "12 Alder Court, Portland, OR 97205",
            "contact": "555-222-1919",
        } | overrides

    return build


@pytest.fixture
def lead_payload() -> Callable[..., dict[str, object]]:
    """A complete, valid lead against the seeded lead-enabled tenant."""

    def build(**overrides: object) -> dict[str, object]:
        return {
            "tenant_id": "apex",
            "session_id": "session-test",
            "customer_name": "Dana Ruiz",
            "contact": "dana@example.com",
            "service": "HVAC",
            "summary": "Furnace is making a grinding noise.",
        } | overrides

    return build
