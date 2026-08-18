"""Tenant policy and the public/private projection boundary."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable

import pytest

from tenantchat.core.tenant import PricingPolicy, PublicTenantView, TenantPolicy

# The `build_tenant` fixture, named for use in a signature. Spelled out here
# rather than imported from conftest so no test module depends on another's
# import path.
TenantBuilder = Callable[..., TenantPolicy]


class TestPublicProjection:
    def test_public_view_carries_no_private_fields(self) -> None:
        """The structural guarantee: private data has nowhere to go.

        This asserts on field *names* of the type, not on one instance, so it
        keeps holding as fields are added.
        """
        public_fields = {field.name for field in dataclasses.fields(PublicTenantView)}

        assert "approved_prices" not in public_fields
        assert "served_zips" not in public_fields
        assert "pricing_policy" not in public_fields

    def test_public_view_omits_price_strings_entirely(self, build_tenant: TenantBuilder) -> None:
        tenant = build_tenant(approved_prices=(("hvac", "$120 diagnostic visit"),))

        rendered = repr(tenant.public_view())

        assert "$120" not in rendered

    def test_public_view_omits_served_zip_list(self, build_tenant: TenantBuilder) -> None:
        """Publishing coverage lets a competitor map the service area."""
        tenant = build_tenant(served_zips=frozenset({"97205", "97201"}))

        rendered = repr(tenant.public_view())

        assert "97201" not in rendered

    def test_public_view_publishes_the_tenant_consent_override(
        self, build_tenant: TenantBuilder
    ) -> None:
        """BUG-023: the widget must render this, not compose its own sentence.

        A tenant that overrides the statement is the case where a locally
        rebuilt default and the recorded copy silently disagree, so the value
        the visitor agreed to is not the value the grant stores.
        """
        override = "Clearview keeps what you enter here to schedule your visit."
        tenant = build_tenant(contact_consent_statement=override)

        assert tenant.public_view().contact_consent_statement == override
        assert tenant.consent_statement() == override

    def test_public_view_falls_back_to_the_server_composed_statement(
        self, build_tenant: TenantBuilder
    ) -> None:
        """A tenant that never overrides still publishes a statement to render."""
        tenant = build_tenant(contact_consent_statement=None)

        published = tenant.public_view().contact_consent_statement

        assert published == tenant.consent_statement()
        assert tenant.name in published

    def test_public_view_exposes_expected_branding(self, build_tenant: TenantBuilder) -> None:
        public = build_tenant().public_view()

        assert public.name == "Clearview Property Care"
        assert public.phone == "(555) 816-4420"
        assert public.services == ("HVAC", "Window Cleaning")


class TestPricingPolicy:
    def test_fixed_policy_returns_approved_price(self, build_tenant: TenantBuilder) -> None:
        tenant = build_tenant(pricing_policy=PricingPolicy.FIXED)

        assert tenant.price_for("hvac") == "$120 diagnostic visit"

    def test_never_policy_returns_none_even_when_a_price_is_configured(
        self, build_tenant: TenantBuilder
    ) -> None:
        """Defense in depth against tenant misconfiguration.

        A NEVER-pricing tenant with a stray price row must still never quote. The
        policy check does not trust that the price table is empty.
        """
        tenant = build_tenant(
            pricing_policy=PricingPolicy.NEVER,
            approved_prices=(("hvac", "$120 diagnostic visit"),),
        )

        assert tenant.price_for("hvac") is None

    def test_unknown_service_has_no_price_under_fixed_policy(
        self, build_tenant: TenantBuilder
    ) -> None:
        assert build_tenant().price_for("plumbing") is None


class TestServiceArea:
    @pytest.mark.parametrize("zip_code", ["97205", "  97205  "])
    def test_served_zip_matches_after_trimming(
        self, zip_code: str, build_tenant: TenantBuilder
    ) -> None:
        assert build_tenant().serves_zip(zip_code) is True

    @pytest.mark.parametrize("zip_code", ["98101", "", "9720"])
    def test_unserved_zip_is_rejected(self, zip_code: str, build_tenant: TenantBuilder) -> None:
        assert build_tenant().serves_zip(zip_code) is False


class TestImmutability:
    def test_policy_is_frozen(self, build_tenant: TenantBuilder) -> None:
        """Tenant policy is server-authoritative; nothing mutates it mid-turn."""
        tenant = build_tenant()

        with pytest.raises(dataclasses.FrozenInstanceError):
            tenant.booking_enabled = False  # type: ignore[misc]
