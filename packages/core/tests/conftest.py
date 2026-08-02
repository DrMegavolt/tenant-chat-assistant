"""Shared builders for domain tests.

Exposed as a fixture rather than an importable helper: pytest injects it without
a cross-module import, which keeps the test tree free of the sys.path games that
importing from a sibling test file otherwise needs.

Tests build policies through this factory so that adding a field to
``TenantPolicy`` breaks one place instead of every test module, and so each test
states only the attribute it is actually about.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tenantchat.core.catalog import ServiceCatalog, ServiceDefinition
from tenantchat.core.tenant import PricingPolicy, TenantPolicy


@pytest.fixture
def build_tenant() -> Callable[..., TenantPolicy]:
    """A booking- and lead-enabled tenant, overridable one field at a time."""

    def factory(**overrides: object) -> TenantPolicy:
        defaults: dict[str, object] = {
            "tenant_id": "clearview",
            "name": "Clearview Property Care",
            "assistant_name": "Clearview assistant",
            "tagline": "Pricing and booking enabled",
            "phone": "(555) 816-4420",
            "address": "480 Lakeview Avenue, Portland, OR 97205",
            "hours": "Daily 7:00 AM-7:00 PM",
            "catalog": ServiceCatalog.from_definitions(
                [
                    ServiceDefinition("hvac", "HVAC"),
                    ServiceDefinition("window-cleaning", "Window Cleaning"),
                ]
            ),
            "pricing_policy": PricingPolicy.FIXED,
            "booking_enabled": True,
            "lead_capture_enabled": True,
            "proactive_lead_capture": True,
            "quick_actions": ("Book HVAC",),
            "approved_prices": (("hvac", "$120 diagnostic visit"),),
            "served_zips": frozenset({"97205", "97201"}),
        }
        return TenantPolicy(**(defaults | overrides))  # type: ignore[arg-type]

    return factory
