"""Tool-permission guard: the policy gate no injected prompt can widen (`RAG-007`).

The verdicts are written as code-first refusals — a booking-disabled tenant
refuses a booking tool even when the agent's allowlist names it — because the
tenant's policy is server-owned while the allowlist is only routing metadata.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tenantchat.core.catalog import ServiceCatalog, ServiceDefinition
from tenantchat.core.guards import ToolRefusal, tool_permission
from tenantchat.core.tenant import PricingPolicy, TenantPolicy


@pytest.fixture
def policy() -> Callable[..., TenantPolicy]:
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
            "proactive_lead_capture": False,
            "quick_actions": (),
            "approved_prices": (("hvac", "$120 diagnostic visit"),),
            "served_zips": frozenset({"97205"}),
        }
        return TenantPolicy(**(defaults | overrides))  # type: ignore[arg-type]

    return factory


def test_agent_allowed_tool_runs(
    policy: Callable[..., TenantPolicy],
) -> None:
    verdict = tool_permission(
        "get_availability",
        allowed_tools=("get_availability", "book_appointment"),
        policy=policy(),
    )
    assert verdict.permitted is True
    assert verdict.refusal is None


def test_unknown_tool_never_runs(
    policy: Callable[..., TenantPolicy],
) -> None:
    verdict = tool_permission(
        "sys_exec",
        allowed_tools=("get_availability",),
        policy=policy(),
    )
    assert verdict.permitted is False
    assert verdict.refusal is ToolRefusal.NOT_ALLOWED


def test_tool_outside_agent_allowlist_is_refused(
    policy: Callable[..., TenantPolicy],
) -> None:
    verdict = tool_permission(
        "get_availability",
        allowed_tools=("book_appointment",),
        policy=policy(),
    )
    assert verdict.permitted is False
    assert verdict.refusal is ToolRefusal.NOT_ALLOWED


def test_booking_disabled_tenant_refuses_booking_tools(
    policy: Callable[..., TenantPolicy],
) -> None:
    tenant = policy(booking_enabled=False)
    for tool in ("get_availability", "book_appointment"):
        verdict = tool_permission(
            tool, allowed_tools=("get_availability", "book_appointment"), policy=tenant
        )
        assert verdict.permitted is False
        assert verdict.refusal is ToolRefusal.BOOKING_DISABLED


def test_booking_disabled_tenant_refuses_even_when_allowlist_agrees(
    policy: Callable[..., TenantPolicy],
) -> None:
    tenant = policy(booking_enabled=False)
    verdict = tool_permission(
        "book_appointment", allowed_tools=("book_appointment",), policy=tenant
    )
    assert verdict.permitted is False
    assert verdict.refusal is ToolRefusal.BOOKING_DISABLED


def test_lead_capture_disabled_tenant_refuses_lead_tool(
    policy: Callable[..., TenantPolicy],
) -> None:
    tenant = policy(lead_capture_enabled=False)
    verdict = tool_permission("create_lead", allowed_tools=("create_lead",), policy=tenant)
    assert verdict.permitted is False
    assert verdict.refusal is ToolRefusal.LEAD_CAPTURE_DISABLED


def test_non_policy_tool_ignores_disabled_booking(
    policy: Callable[..., TenantPolicy],
) -> None:
    tenant = policy(booking_enabled=False)
    verdict = tool_permission("get_quote", allowed_tools=("get_quote",), policy=tenant)
    assert verdict.permitted is True
