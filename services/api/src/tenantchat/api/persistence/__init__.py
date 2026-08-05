"""PostgreSQL system-of-record adapters."""

from tenantchat.api.persistence.database import Database, DatabasePoolSettings
from tenantchat.api.persistence.idempotency import PostgresIdempotencyStore
from tenantchat.api.persistence.jobs import PostgresJobStore
from tenantchat.api.persistence.knowledge import PostgresKnowledgeStore
from tenantchat.api.persistence.privacy import (
    PostgresConsentStore,
    PostgresPrivacyStore,
)
from tenantchat.api.persistence.repositories import (
    PostgresAuditStore,
    PostgresBookingStore,
    PostgresConversationStore,
    PostgresHandoffStore,
    PostgresLeadStore,
    PostgresMembershipStore,
)
from tenantchat.api.persistence.traces import (
    PostgresTraceAccessStore,
    PostgresTurnRecordStore,
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
    "PostgresJobStore",
    "PostgresKnowledgeStore",
    "PostgresLeadStore",
    "PostgresMembershipStore",
    "PostgresPrivacyStore",
    "PostgresTraceAccessStore",
    "PostgresTurnRecordStore",
]
