"""Everything the graph is allowed to reach.

One frozen bundle rather than six constructor arguments threaded through every
node, and — more usefully — an exhaustive list of the graph's capabilities. If a
node wants to do something that is not reachable from this object, the reviewer
sees a new field rather than a new import.

Every field is a ``Protocol`` from :mod:`tenantchat.core.ports` or
:mod:`tenantchat.orchestration.model`. The graph therefore has no idea whether it
is running against PostgreSQL and a hosted model or against six test doubles,
which is what makes the whole runtime testable without either.
"""

from __future__ import annotations

from dataclasses import dataclass

from tenantchat.core.ports import (
    AvailabilityProvider,
    BookingService,
    HandoffService,
    LeadService,
    TenantPolicySource,
)
from tenantchat.orchestration.model import ChatModel


@dataclass(frozen=True, slots=True)
class DispatchDependencies:
    """The ports one dispatcher graph runs against."""

    model: ChatModel
    policies: TenantPolicySource
    availability: AvailabilityProvider
    bookings: BookingService
    leads: LeadService
    handoffs: HandoffService
