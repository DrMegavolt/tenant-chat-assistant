"""PostgreSQL system-of-record adapters."""

from tenantchat.api.persistence.database import Database, DatabasePoolSettings
from tenantchat.api.persistence.idempotency import PostgresIdempotencyStore
from tenantchat.api.persistence.knowledge import PostgresKnowledgeStore
from tenantchat.api.persistence.privacy import (
    PostgresAuditStore,
    PostgresConsentStore,
    PostgresPrivacyStore,
)
from tenantchat.api.persistence.repositories import (
    PostgresBookingStore,
    PostgresConversationStore,
    PostgresHandoffStore,
    PostgresLeadStore,
)

__all__ = [
    "Database",
    "DatabasePoolSettings",
    "PostgresAuditStore",
    "PostgresBookingStore",
    "PostgresConsentStore",
    "PostgresConversationStore",
    "PostgresHandoffStore",
    "PostgresIdempotencyStore",
    "PostgresKnowledgeStore",
    "PostgresLeadStore",
    "PostgresPrivacyStore",
]
