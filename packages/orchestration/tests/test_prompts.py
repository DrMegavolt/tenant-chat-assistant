"""The system prompt carries tenant policy, and only tenant policy.

These read as tests of prose, which is unusual. They are here because the prompt
is the only place a tenant's private configuration and the model meet: a bug
that renders an approved price for a tenant who forbids pricing is a policy
breach that no amount of downstream validation catches.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tenantchat.core.catalog import ServiceCatalog, ServiceDefinition
from tenantchat.core.tenant import PricingPolicy, TenantPolicy
from tenantchat.orchestration.prompts import SYSTEM_PROMPT_VERSION, build_system_prompt

TenantBuilder = Callable[..., TenantPolicy]


@pytest.fixture
def build_tenant() -> TenantBuilder:
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
            "catalog": ServiceCatalog.from_definitions([ServiceDefinition("hvac", "HVAC")]),
            "pricing_policy": PricingPolicy.FIXED,
            "booking_enabled": True,
            "lead_capture_enabled": True,
            "proactive_lead_capture": True,
            "approved_prices": (("hvac", "$120 diagnostic visit"),),
            "served_zips": frozenset({"97205"}),
        }
        return TenantPolicy(**(defaults | overrides))  # type: ignore[arg-type]

    return factory


def test_an_approved_price_is_rendered_for_a_tenant_that_quotes(
    build_tenant: TenantBuilder,
) -> None:
    prompt = build_system_prompt(build_tenant())

    assert "- HVAC: $120 diagnostic visit" in prompt


def test_a_no_pricing_tenant_never_sees_its_own_price_list(
    build_tenant: TenantBuilder,
) -> None:
    """A configured price under ``NEVER`` is a misconfiguration, not permission.

    ``TenantPolicy.price_for`` already refuses it; this asserts the prompt asks
    that question rather than reading ``approved_prices`` directly.
    """
    prompt = build_system_prompt(build_tenant(pricing_policy=PricingPolicy.NEVER))

    assert "$120" not in prompt
    assert "None approved for chat." in prompt


def test_a_tenant_that_does_not_book_is_told_not_to_offer_slots(
    build_tenant: TenantBuilder,
) -> None:
    prompt = build_system_prompt(build_tenant(booking_enabled=False))

    assert "may not book" in prompt
    assert "Do not call get_availability" in prompt


def test_a_tenant_without_lead_capture_is_told_not_to_capture(
    build_tenant: TenantBuilder,
) -> None:
    prompt = build_system_prompt(build_tenant(lead_capture_enabled=False))

    assert "Do not call create_lead" in prompt


def test_the_served_zip_list_never_appears_in_the_prompt(
    build_tenant: TenantBuilder,
) -> None:
    """The coverage map is private; the assistant asks the tool one ZIP at a time."""
    prompt = build_system_prompt(build_tenant())

    assert "97205" not in prompt.split("Address:")[0]
    assert "check_service_area" in prompt


def test_the_prompt_carries_a_version(build_tenant: TenantBuilder) -> None:
    """`OBS-004` attributes a behavior change to a component version.

    An unversioned prompt makes "the answers changed" an unanswerable question.
    """
    assert SYSTEM_PROMPT_VERSION
