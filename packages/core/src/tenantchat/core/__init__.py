"""Framework-free domain model for the tenant chat platform.

Nothing in this package may import a web framework, ORM, agent framework, or
model SDK. See ``packages/core/pyproject.toml`` for why, and
``tests/test_architecture_invariants.py`` for the check that enforces it.
"""

from tenantchat.core.catalog import ServiceCatalog, ServiceDefinition, normalize_term
from tenantchat.core.commands import BookingCommand, LeadCommand, LeadUrgency
from tenantchat.core.contact import Contact, ContactKind
from tenantchat.core.errors import (
    BookingNotPermittedError,
    ConflictError,
    DomainError,
    InvalidContactError,
    LeadCaptureNotPermittedError,
    MissingRequiredFieldsError,
    NotFoundError,
    PolicyViolationError,
    PricingNotPermittedError,
    SlotUnavailableError,
    UnknownServiceError,
    ValidationError,
)
from tenantchat.core.fields import RequiredField
from tenantchat.core.tenant import PricingPolicy, PublicTenantView, TenantPolicy

__all__ = [
    "BookingCommand",
    "BookingNotPermittedError",
    "ConflictError",
    "Contact",
    "ContactKind",
    "DomainError",
    "InvalidContactError",
    "LeadCaptureNotPermittedError",
    "LeadCommand",
    "LeadUrgency",
    "MissingRequiredFieldsError",
    "NotFoundError",
    "PolicyViolationError",
    "PricingNotPermittedError",
    "PricingPolicy",
    "PublicTenantView",
    "RequiredField",
    "ServiceCatalog",
    "ServiceDefinition",
    "SlotUnavailableError",
    "TenantPolicy",
    "UnknownServiceError",
    "ValidationError",
    "normalize_term",
]
