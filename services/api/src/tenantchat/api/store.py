"""Where accepted bookings and leads are recorded.

The ``Protocol`` pair below is the seam `DATA-002` replaces with SQLAlchemy
repositories and `DATA-003` makes transactional and idempotent. Routers depend on
the protocol, so that swap does not reach into request handling.

**The in-memory implementation is not production storage.** It loses everything
on restart and two replicas disagree, which is exactly the property `DATA-002`
exists to fix. It is here so the HTTP contract and its validation can be
finished, tested, and reviewed before the schema work lands.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from tenantchat.core.commands import BookingCommand, LeadCommand, LeadUrgency
from tenantchat.core.contact import Contact


def _reference(prefix: str) -> str:
    """A collision-free customer-facing reference.

    Random rather than a counter or a timestamp: sequential references leak
    business volume to anyone who books twice, and a per-process counter repeats
    itself the moment a second replica starts.
    """
    return f"{prefix}-{uuid.uuid4().hex[:12].upper()}"


@dataclass(frozen=True, slots=True)
class BookingRecord:
    booking_id: str
    tenant_id: str
    session_id: str
    customer_name: str
    contact: Contact
    address: str
    service_slug: str
    service_name: str
    slot: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class LeadRecord:
    lead_id: str
    tenant_id: str
    session_id: str
    customer_name: str
    contact: Contact
    service: str
    service_slug: str | None
    summary: str
    address_or_zip: str
    urgency: LeadUrgency
    created_at: datetime


class BookingStore(Protocol):
    def record(self, command: BookingCommand, *, session_id: str) -> BookingRecord: ...


class LeadStore(Protocol):
    def record(self, command: LeadCommand, *, session_id: str) -> LeadRecord: ...


class InMemoryBookingStore:
    """Process-local booking storage. See the module docstring."""

    def __init__(self) -> None:
        self._records: list[BookingRecord] = []

    def record(self, command: BookingCommand, *, session_id: str) -> BookingRecord:
        booking = BookingRecord(
            booking_id=_reference("BK"),
            tenant_id=command.tenant_id,
            session_id=session_id,
            customer_name=command.customer_name,
            contact=command.contact,
            address=command.address,
            service_slug=command.service.slug,
            service_name=command.service.display_name,
            slot=command.slot,
            created_at=datetime.now(UTC),
        )
        self._records.append(booking)
        return booking

    def for_tenant(self, tenant_id: str) -> tuple[BookingRecord, ...]:
        return tuple(record for record in self._records if record.tenant_id == tenant_id)


class InMemoryLeadStore:
    """Process-local lead storage. See the module docstring."""

    def __init__(self) -> None:
        self._records: list[LeadRecord] = []

    def record(self, command: LeadCommand, *, session_id: str) -> LeadRecord:
        lead = LeadRecord(
            lead_id=_reference("LD"),
            tenant_id=command.tenant_id,
            session_id=session_id,
            customer_name=command.customer_name,
            contact=command.contact,
            service=command.service,
            service_slug=command.service_slug,
            summary=command.summary,
            address_or_zip=command.address_or_zip,
            urgency=command.urgency,
            created_at=datetime.now(UTC),
        )
        self._records.append(lead)
        return lead

    def for_tenant(self, tenant_id: str) -> tuple[LeadRecord, ...]:
        return tuple(record for record in self._records if record.tenant_id == tenant_id)
