"""Request and response models.

Every field is bounded. The bounds here are a cheap outer gate that rejects
absurd input before it reaches domain parsing — they are not the business rule,
which lives in ``tenantchat.core.commands`` and applies to callers that never
touch HTTP. Where the two overlap, the domain's limit is the tighter one and the
one that decides.

``extra="forbid"`` on every request model: a typo in a field name fails loudly
rather than being silently dropped and treated as absent.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from tenantchat.api.store import BookingRecord, LeadRecord
from tenantchat.core.tenant import PublicTenantView

# Generous outer bounds. The domain applies the meaningful limits.
_TENANT_ID = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$")
# A correlation label, never an identity. It is client-supplied, so it must not
# be used to authorize a read or to bind a record to a visitor — `SEC-002`
# replaces it with a server-issued session token that can carry that weight.
# Nothing here reads by session, which is what keeps the weak value harmless.
_SESSION_ID = Field(default="", max_length=128)
_SHORT_TEXT = Field(default="", max_length=512)
_LONG_TEXT = Field(default="", max_length=4096)


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BookingRequest(_Request):
    tenant_id: str = _TENANT_ID
    session_id: str = _SESSION_ID
    service: str = _SHORT_TEXT
    slot: str = _SHORT_TEXT
    customer_name: str = _SHORT_TEXT
    address: str = _SHORT_TEXT
    # Named for what the customer may supply, since either kind is accepted and
    # the domain decides which it is.
    contact: str = _SHORT_TEXT


class LeadRequest(_Request):
    tenant_id: str = _TENANT_ID
    session_id: str = _SESSION_ID
    customer_name: str = _SHORT_TEXT
    contact: str = _SHORT_TEXT
    service: str = _SHORT_TEXT
    summary: str = _LONG_TEXT
    address_or_zip: str = _SHORT_TEXT
    urgency: str = _SHORT_TEXT


class BookingResponse(BaseModel):
    """Confirmation of an accepted booking.

    The contact is echoed in its canonical form rather than as typed, so the
    customer sees the number the business will actually dial.
    """

    booking_id: str
    service: str
    slot: str
    customer_name: str
    contact: str
    address: str
    created_at: datetime

    @classmethod
    def of(cls, record: BookingRecord) -> BookingResponse:
        return cls(
            booking_id=record.booking_id,
            service=record.service_name,
            slot=record.slot,
            customer_name=record.customer_name,
            contact=record.contact.display,
            address=record.address,
            created_at=record.created_at,
        )


class LeadResponse(BaseModel):
    lead_id: str
    service: str
    summary: str
    urgency: str
    created_at: datetime

    @classmethod
    def of(cls, record: LeadRecord) -> LeadResponse:
        return cls(
            lead_id=record.lead_id,
            service=record.service,
            summary=record.summary,
            urgency=record.urgency.value,
            created_at=record.created_at,
        )


class TenantSummary(BaseModel):
    """One tenant as the embeddable widget sees it.

    Built only from :class:`~tenantchat.core.tenant.PublicTenantView`. Adding a
    private field to ``TenantPolicy`` cannot leak through here, because the type
    this reads from does not have it.
    """

    tenant_id: str
    name: str
    assistant_name: str
    tagline: str
    phone: str
    address: str
    hours: str
    services: list[str]
    quick_actions: list[str]
    booking_enabled: bool
    lead_capture_enabled: bool

    @classmethod
    def of(cls, view: PublicTenantView) -> TenantSummary:
        return cls(
            tenant_id=view.tenant_id,
            name=view.name,
            assistant_name=view.assistant_name,
            tagline=view.tagline,
            phone=view.phone,
            address=view.address,
            hours=view.hours,
            services=list(view.services),
            quick_actions=list(view.quick_actions),
            booking_enabled=view.booking_enabled,
            lead_capture_enabled=view.lead_capture_enabled,
        )


class TenantsResponse(BaseModel):
    tenants: dict[str, TenantSummary]


class AvailabilityResponse(BaseModel):
    service: str
    slots: list[str]


class HealthResponse(BaseModel):
    status: str
