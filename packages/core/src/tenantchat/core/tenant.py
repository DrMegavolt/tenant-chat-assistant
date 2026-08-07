"""Tenant identity and the server-owned policy that constrains the assistant.

The public projection is a **separate type**, not a list of allowed key names.
Filtering a dict against a string allowlist works until someone adds a field and
forgets to think about it: the new field is then private by accident rather than
by decision, and the failure is silent. Adding a field to ``TenantPolicy`` cannot
leak through ``PublicTenantView``, because the public type does not have it —
exposing it takes a deliberate edit to a class whose docstring says everything on
it is world-readable.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from tenantchat.core.budgets import TenantBudget
from tenantchat.core.catalog import ServiceCatalog
from tenantchat.core.privacy import ConsentPurpose, consent_statement


class PricingPolicy(StrEnum):
    """Whether the assistant may discuss prices at all."""

    NEVER = "never"
    """Never quote, estimate, or range a price. Route pricing questions to phone."""

    FIXED = "fixed"
    """Quote only from the tenant's approved price list. Never extrapolate."""


@dataclass(frozen=True, slots=True)
class PublicTenantView:
    """Tenant configuration served to the unauthenticated chat widget.

    Every field on this type is world-readable by design. Before adding one, ask
    whether you would publish it: the widget is embedded on customer sites, so
    anything here is visible to anyone who opens dev tools.
    """

    tenant_id: str
    name: str
    assistant_name: str
    tagline: str
    phone: str
    address: str
    hours: str
    services: tuple[str, ...]
    quick_actions: tuple[str, ...]
    booking_enabled: bool
    lead_capture_enabled: bool
    # Published so the widget can show the sentence the visitor will be held to.
    # World-readable by design: it is the text a consent dialog displays.
    contact_consent_statement: str
    # The host page's own marketing copy. Published because the demo site
    # renders the tenant's branding rather than shipping it in the bundle, and
    # because a headline is the least private thing a business owns.
    site_headline: str
    site_description: str
    # Whether the assistant opens with a lead-capture offer. Already observable
    # from one conversation, so publishing it reveals nothing the widget's own
    # behaviour does not. `pricing_policy` stays private on purpose — see the
    # projection test that pins it.
    proactive_lead_capture: bool


@dataclass(frozen=True, slots=True)
class TenantPolicy:
    """Server-authoritative configuration for one tenant.

    This type holds private policy — approved prices and the served ZIP list —
    alongside public branding. It must never be serialized directly to a visitor;
    call :meth:`public_view` instead.

    FEAT-006 moves these records out of source code and into the database with a
    draft/publish workflow. The shape stays the same.
    """

    tenant_id: str
    name: str
    assistant_name: str
    tagline: str
    phone: str
    address: str
    hours: str
    catalog: ServiceCatalog
    pricing_policy: PricingPolicy
    booking_enabled: bool
    lead_capture_enabled: bool
    proactive_lead_capture: bool
    quick_actions: tuple[str, ...] = ()
    # Private: exposing the approved price list to a NEVER-pricing tenant's widget
    # would defeat the entire policy.
    approved_prices: tuple[tuple[str, str], ...] = ()
    # Private: publishing the served ZIP list lets a competitor map coverage.
    served_zips: frozenset[str] = frozenset()
    # The purposes an action must have consent for, per action. Declared here
    # rather than hard-coded in the commands so a tenant can relax or tighten
    # the bar in the policy database (`FEAT-006`) without a code change.
    booking_required_consent: frozenset[ConsentPurpose] = frozenset(
        {ConsentPurpose.BOOKING, ConsentPurpose.FOLLOW_UP}
    )
    lead_required_consent: frozenset[ConsentPurpose] = frozenset({ConsentPurpose.FOLLOW_UP})
    # Overrides the sentence the server builds from the required purposes. The
    # default matters as much as the override: the visitor must never be shown
    # a statement the server does not also record.
    contact_consent_statement: str | None = None
    # Prompt customization (`AI-003`). These fill declared template slots —
    # tone, escalation rules, disclaimers — and are validated against the
    # slot schema at assembly, so tenant input can never introduce a new
    # instruction section. They default empty; the template supplies defaults.
    assistant_tone: str | None = None
    escalation_rules: tuple[str, ...] = ()
    disclaimers: tuple[str, ...] = ()
    # Host-page copy for the demo site. Empty falls back to the tagline rather
    # than rendering a blank hero, so a tenant added without copy still shows
    # something a reviewer can read.
    site_headline: str = ""
    site_description: str = ""
    # AI-002: the tenant's model-safety, quota, and cost limits. ``None`` is
    # "the platform default", never unlimited. Private by design: publishing a
    # tenant's budget would hand an attacker their cost ceiling for free.
    budgets: TenantBudget | None = None

    def public_view(self) -> PublicTenantView:
        """Project to the world-readable subset."""
        return PublicTenantView(
            tenant_id=self.tenant_id,
            name=self.name,
            assistant_name=self.assistant_name,
            tagline=self.tagline,
            phone=self.phone,
            address=self.address,
            hours=self.hours,
            services=self.catalog.offered_names(),
            quick_actions=self.quick_actions,
            booking_enabled=self.booking_enabled,
            lead_capture_enabled=self.lead_capture_enabled,
            contact_consent_statement=self.consent_statement(),
            site_headline=self.site_headline or self.tagline,
            site_description=self.site_description or self.tagline,
            proactive_lead_capture=self.proactive_lead_capture,
        )

    def consent_statement(self) -> str:
        """The statement a visitor must have agreed to for this tenant.

        The server builds the same sentence from the required purposes, so a
        tenant that never overrides it still has a statement, and the API and
        the widget are guaranteed to agree on the text.
        """
        return self.contact_consent_statement or consent_statement(
            self.name,
            self.booking_required_consent | self.lead_required_consent,
        )

    def serves_zip(self, zip_code: str) -> bool:
        """Whether this tenant covers a five-digit US ZIP code."""
        return zip_code.strip() in self.served_zips

    def price_for(self, service_slug: str) -> str | None:
        """The approved price for a service, if pricing is permitted at all.

        Returns ``None`` under :attr:`PricingPolicy.NEVER` regardless of whether a
        price happens to be configured, so a misconfigured tenant cannot leak one.
        """
        if self.pricing_policy is PricingPolicy.NEVER:
            return None
        return dict(self.approved_prices).get(service_slug)
