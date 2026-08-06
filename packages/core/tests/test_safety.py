"""Quarantine: safety state on versions and the review rule (`RAG-007`).

The retrieval predicate is the contract that matters: a quarantined version
must be invisible to every audience, and only a stored review may clear it.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

from tenantchat.core.errors import InvalidVersionTransitionError, NotFoundError, ValidationError
from tenantchat.core.knowledge import (
    ContentChecksum,
    DocumentVersion,
    KnowledgeDocument,
    KnowledgeDomain,
    KnowledgeSource,
    RetrievalAudience,
    RetrievalContext,
    SourceKind,
    Visibility,
)
from tenantchat.core.lifecycle import IndexingState, VersionState
from tenantchat.core.safety import SafetyState

TENANT = "clearview"
FINANCING = KnowledgeDomain.parse("financing")
NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


def checksum(seed: str) -> ContentChecksum:
    return ContentChecksum.of(seed.encode())


def review_rejection(raised: pytest.ExceptionInfo[InvalidVersionTransitionError]) -> str:
    return raised.value.detail or ""


@pytest.fixture
def source() -> KnowledgeSource:
    return KnowledgeSource(
        source_id=uuid.uuid4(),
        tenant_id=TENANT,
        domain=FINANCING,
        kind=SourceKind.UPLOAD,
        display_name="Financing partner brochures",
    )


@pytest.fixture
def build_version() -> Callable[..., DocumentVersion]:
    document_id = uuid.uuid4()

    def factory(**overrides: object) -> DocumentVersion:
        defaults: dict[str, object] = {
            "version_id": uuid.uuid4(),
            "tenant_id": TENANT,
            "document_id": document_id,
            "revision": 1,
            "state": VersionState.PUBLISHED,
            "indexing_state": IndexingState.INDEXED,
            "visibility": Visibility.PUBLIC,
            "checksum": checksum("rev-1"),
            "byte_size": 4096,
            "media_type": "application/pdf",
            "storage_key": "tenants/clearview/financing/plan-terms/v1.pdf",
            "effective_at": NOW - timedelta(days=1),
            "expires_at": None,
        }
        return DocumentVersion(**(defaults | overrides))  # type: ignore[arg-type]

    return factory


@pytest.fixture
def quarantined_document(
    source: KnowledgeSource, build_version: Callable[..., DocumentVersion]
) -> KnowledgeDocument:
    version = build_version(safety_state=SafetyState.QUARANTINED)
    return KnowledgeDocument(
        document_id=version.document_id,
        tenant_id=TENANT,
        source=source,
        external_key="plan-terms.pdf",
        title="Financing plan terms",
        versions=(version,),
    )


@pytest.fixture
def visitor_now() -> RetrievalContext:
    return RetrievalContext(
        tenant_id=TENANT, domain=FINANCING, audience=RetrievalAudience.VISITOR, moment=NOW
    )


def test_quarantined_version_is_not_retrievable_by_any_audience(
    quarantined_document: KnowledgeDocument, visitor_now: RetrievalContext
) -> None:
    staff_now = RetrievalContext(
        tenant_id=TENANT,
        domain=FINANCING,
        audience=RetrievalAudience.STAFF,
        moment=NOW,
    )
    assert quarantined_document.retrievable_version(visitor_now) is None
    assert quarantined_document.retrievable_version(staff_now) is None


def test_clear_version_is_retrievable(
    source: KnowledgeSource,
    build_version: Callable[..., DocumentVersion],
    visitor_now: RetrievalContext,
) -> None:
    version = build_version()
    document = KnowledgeDocument(
        document_id=version.document_id,
        tenant_id=TENANT,
        source=source,
        external_key="plan-terms.pdf",
        title="Financing plan terms",
        versions=(version,),
    )
    assert document.retrievable_version(visitor_now) is version


def test_quarantine_is_the_only_difference(
    quarantined_document: KnowledgeDocument, visitor_now: RetrievalContext
) -> None:
    published = quarantined_document.published()
    assert published is not None
    assert published.indexing_state is IndexingState.INDEXED
    assert quarantined_document.retrievable_version(visitor_now) is None


def test_approval_review_clears_quarantine(
    quarantined_document: KnowledgeDocument,
) -> None:
    version = quarantined_document.published()
    assert version is not None
    plan = quarantined_document.plan_quarantine_review(
        version.version_id, approved=True, reviewed_by="sergio@clearview.example", at=NOW
    )
    assert plan.approved is True
    assert plan.reviewed_by == "sergio@clearview.example"
    assert plan.version_id == version.version_id


def test_rejection_review_is_recorded_not_applied(
    quarantined_document: KnowledgeDocument,
) -> None:
    version = quarantined_document.published()
    assert version is not None
    plan = quarantined_document.plan_quarantine_review(
        version.version_id, approved=False, reviewed_by="sergio@clearview.example", at=NOW
    )
    assert plan.approved is False


def test_reviewing_a_clear_version_is_refused(
    source: KnowledgeSource, build_version: Callable[..., DocumentVersion]
) -> None:
    version = build_version()
    document = KnowledgeDocument(
        document_id=version.document_id,
        tenant_id=TENANT,
        source=source,
        external_key="plan-terms.pdf",
        title="Financing plan terms",
        versions=(version,),
    )
    with pytest.raises(InvalidVersionTransitionError) as raised:
        document.plan_quarantine_review(
            version.version_id, approved=True, reviewed_by="sergio@clearview.example", at=NOW
        )
    assert "not quarantined" in review_rejection(raised)


def test_reviewing_an_unknown_version_is_a_not_found(
    quarantined_document: KnowledgeDocument,
) -> None:
    with pytest.raises(NotFoundError):
        quarantined_document.plan_quarantine_review(
            uuid.uuid4(), approved=True, reviewed_by="sergio@clearview.example", at=NOW
        )


def test_review_requires_an_owner(
    quarantined_document: KnowledgeDocument,
) -> None:
    version = quarantined_document.published()
    assert version is not None
    with pytest.raises(ValidationError):
        quarantined_document.plan_quarantine_review(
            version.version_id, approved=True, reviewed_by="", at=NOW
        )
