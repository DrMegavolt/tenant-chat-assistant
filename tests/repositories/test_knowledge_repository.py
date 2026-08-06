"""Knowledge lifecycle against a real PostgreSQL: draft to rollback to deletion.

These are the `RAG-001` acceptance criteria as executable specifications. They run
against Postgres rather than a fake because the guarantees being claimed are
half-owned by the schema: the partial unique index is what makes a publish
atomic, and the composite foreign keys are what make a cross-tenant reference
impossible rather than merely unlikely.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import pytest

from tenantchat.api.persistence import (
    Database,
    DatabasePoolSettings,
    PostgresKnowledgeStore,
)
from tenantchat.api.registry import TenantRegistry
from tenantchat.core.errors import ConflictError, InvalidVersionTransitionError, NotFoundError
from tenantchat.core.knowledge import (
    ContentChecksum,
    DocumentVersion,
    KnowledgeDocument,
    KnowledgeDomain,
    RetrievalAudience,
    RetrievalContext,
    SourceKind,
    Visibility,
)
from tenantchat.core.lifecycle import IndexingState, VersionState
from tenantchat.core.safety import SafetyState

TEST_POOL = DatabasePoolSettings(size=2, max_overflow=1, timeout_seconds=5)
FINANCING = KnowledgeDomain.parse("financing")
NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
TENANT = "clearview"
OTHER_TENANT = "apex"


async def _database(database_url: str) -> Database:
    database = Database.connect(database_url, TEST_POOL)
    registry = TenantRegistry.seeded()
    await database.synchronize_tenants(
        (record.policy.tenant_id, record.policy.name) for record in registry.all().values()
    )
    return database


def run(database_url: str, scenario: Callable[[PostgresKnowledgeStore], Awaitable[None]]) -> None:
    """Run one scenario against a disposable store, disposing the pool after."""

    async def main() -> None:
        database = await _database(database_url)
        try:
            await scenario(PostgresKnowledgeStore(database.engine))
        finally:
            await database.dispose()

    asyncio.run(main())


async def stage(
    store: PostgresKnowledgeStore,
    *,
    tenant_id: str = TENANT,
    source_name: str = "Financing partner brochures",
    external_key: str = "plan-terms.pdf",
    title: str = "Financing plan terms",
    content: bytes = b"original terms",
    visibility: Visibility = Visibility.PUBLIC,
) -> tuple[KnowledgeDocument, DocumentVersion]:
    """Register a source if needed and stage one draft revision of a document."""
    source = await store.register_source(
        tenant_id,
        domain=FINANCING,
        kind=SourceKind.UPLOAD,
        display_name=source_name,
    )
    checksum = ContentChecksum.of(content)
    document = await store.stage_version(
        tenant_id,
        source_id=source.source_id,
        external_key=external_key,
        title=title,
        checksum=checksum,
        byte_size=len(content),
        media_type="text/markdown",
        storage_key=f"tenants/{tenant_id}/financing/{external_key}/{checksum.value[:12]}",
        visibility=visibility,
    )
    staged = document.version_with_checksum(checksum)
    assert staged is not None
    return document, staged


async def make_current(
    store: PostgresKnowledgeStore,
    version: DocumentVersion,
    *,
    tenant_id: str = TENANT,
    at: datetime = NOW,
    expires_at: datetime | None = None,
) -> KnowledgeDocument:
    """Take one staged version all the way to retrievable."""
    await store.approve(tenant_id, version.version_id, approved_by="ops@example", at=at)
    await store.publish(tenant_id, version.version_id, at=at, expires_at=expires_at)
    return await store.record_indexed(tenant_id, version.version_id, at=at)


def visitor(moment: datetime = NOW, *, tenant_id: str = TENANT) -> RetrievalContext:
    return RetrievalContext(
        tenant_id=tenant_id, domain=FINANCING, audience=RetrievalAudience.VISITOR, moment=moment
    )


@pytest.mark.integration
def test_publishing_a_new_version_atomically_supersedes_the_old_one(
    repository_database_url: str,
) -> None:
    """The swap is one transaction: there is never a moment with two answers."""

    async def scenario(store: PostgresKnowledgeStore) -> None:
        _, first = await stage(store, content=b"2026 terms")
        await make_current(store, first)

        _, second = await stage(store, content=b"2027 terms")
        assert second.revision == 2
        published = await make_current(store, second, at=NOW + timedelta(days=1))

        assert published.version(first.version_id).state is VersionState.SUPERSEDED
        assert published.version(second.version_id).state is VersionState.PUBLISHED

        retrievable = await store.retrievable_versions(visitor(NOW + timedelta(days=2)))
        assert [version.version_id for version in retrievable] == [second.version_id]

    run(repository_database_url, scenario)


@pytest.mark.integration
def test_rollback_restores_a_prior_version_without_stale_mixed_results(
    repository_database_url: str,
) -> None:
    """Republishing the superseded version is the rollback, so nothing overlaps."""

    async def scenario(store: PostgresKnowledgeStore) -> None:
        _, good = await stage(store, content=b"correct rates")
        await make_current(store, good)
        _, bad = await stage(store, content=b"typo in rates")
        await make_current(store, bad, at=NOW + timedelta(hours=1))

        rolled_back = await store.publish(TENANT, good.version_id, at=NOW + timedelta(hours=2))

        assert rolled_back.version(good.version_id).state is VersionState.PUBLISHED
        assert rolled_back.version(bad.version_id).state is VersionState.SUPERSEDED

        retrievable = await store.retrievable_versions(visitor(NOW + timedelta(hours=3)))
        assert [version.version_id for version in retrievable] == [good.version_id]

    run(repository_database_url, scenario)


@pytest.mark.integration
def test_expiry_ends_retrievability_at_the_boundary_without_demoting_the_version(
    repository_database_url: str,
) -> None:
    async def scenario(store: PostgresKnowledgeStore) -> None:
        _, version = await stage(store, content=b"seasonal promotion")
        await make_current(store, version)
        ends = NOW + timedelta(days=30)

        expired = await store.expire(TENANT, version.version_id, at=ends)

        assert expired.version(version.version_id).state is VersionState.PUBLISHED
        assert await store.retrievable_versions(visitor(ends - timedelta(seconds=1)))
        assert not await store.retrievable_versions(visitor(ends))

    run(repository_database_url, scenario)


@pytest.mark.integration
def test_scheduled_content_is_not_retrievable_before_it_takes_effect(
    repository_database_url: str,
) -> None:
    async def scenario(store: PostgresKnowledgeStore) -> None:
        _, version = await stage(store, content=b"next year's rates")
        starts = NOW + timedelta(days=7)
        await store.approve(TENANT, version.version_id, approved_by="ops@example", at=NOW)
        await store.publish(TENANT, version.version_id, at=NOW, effective_at=starts)
        await store.record_indexed(TENANT, version.version_id, at=NOW)

        assert not await store.retrievable_versions(visitor(starts - timedelta(seconds=1)))
        assert await store.retrievable_versions(visitor(starts))

    run(repository_database_url, scenario)


@pytest.mark.integration
def test_unreviewed_and_unindexed_content_is_never_retrievable(
    repository_database_url: str,
) -> None:
    """Approval and indexing are separate gates, and both must be passed."""

    async def scenario(store: PostgresKnowledgeStore) -> None:
        _, version = await stage(store, content=b"awaiting review")
        assert not await store.retrievable_versions(visitor())

        await store.approve(TENANT, version.version_id, approved_by="ops@example", at=NOW)
        assert not await store.retrievable_versions(visitor())

        await store.publish(TENANT, version.version_id, at=NOW)
        assert not await store.retrievable_versions(visitor()), "published but not yet indexed"

        await store.record_indexed(TENANT, version.version_id, at=NOW)
        assert await store.retrievable_versions(visitor())

    run(repository_database_url, scenario)


@pytest.mark.integration
def test_a_failed_index_withdraws_content_until_it_succeeds(
    repository_database_url: str,
) -> None:
    async def scenario(store: PostgresKnowledgeStore) -> None:
        _, version = await stage(store, content=b"indexed then broken")
        await make_current(store, version)

        failed = await store.record_index_failure(
            TENANT, version.version_id, error_code="embedding_timeout"
        )

        assert failed.version(version.version_id).indexing_state is IndexingState.FAILED
        assert not await store.retrievable_versions(visitor())

        await store.record_indexed(TENANT, version.version_id, at=NOW + timedelta(minutes=5))
        assert await store.retrievable_versions(visitor())

    run(repository_database_url, scenario)


@pytest.mark.integration
def test_a_draft_can_never_be_published_or_indexed(repository_database_url: str) -> None:
    """Approval is the review gate; neither path may route around it."""

    async def scenario(store: PostgresKnowledgeStore) -> None:
        _, version = await stage(store, content=b"unreviewed")

        with pytest.raises(InvalidVersionTransitionError):
            await store.publish(TENANT, version.version_id, at=NOW)
        with pytest.raises(InvalidVersionTransitionError):
            await store.record_indexed(TENANT, version.version_id, at=NOW)

        document = await store.load_document(TENANT, version.document_id)
        assert document.version(version.version_id).state is VersionState.DRAFT

    run(repository_database_url, scenario)


@pytest.mark.integration
def test_deleting_a_document_withdraws_every_version_and_is_idempotent(
    repository_database_url: str,
) -> None:
    async def scenario(store: PostgresKnowledgeStore) -> None:
        _, first = await stage(store, content=b"v1")
        await make_current(store, first)
        _, second = await stage(store, content=b"v2")
        await make_current(store, second, at=NOW + timedelta(hours=1))

        deleted = await store.delete_document(
            TENANT, second.document_id, at=NOW + timedelta(days=1)
        )

        assert deleted.deleted
        assert all(version.state is VersionState.DELETED for version in deleted.versions)
        assert not await store.retrievable_versions(visitor(NOW + timedelta(days=2)))

        again = await store.delete_document(TENANT, second.document_id, at=NOW + timedelta(days=2))
        assert again.deleted

    run(repository_database_url, scenario)


@pytest.mark.integration
def test_deleted_content_cannot_be_revived_by_re_uploading_it(
    repository_database_url: str,
) -> None:
    """A tombstone is a decision; an automated re-crawl must not reverse it."""

    async def scenario(store: PostgresKnowledgeStore) -> None:
        _, version = await stage(store, content=b"withdrawn")
        await make_current(store, version)
        await store.delete_document(TENANT, version.document_id, at=NOW + timedelta(days=1))

        with pytest.raises(NotFoundError):
            await stage(store, content=b"withdrawn")

    run(repository_database_url, scenario)


@pytest.mark.integration
def test_reingesting_identical_content_creates_no_new_revision(
    repository_database_url: str,
) -> None:
    async def scenario(store: PostgresKnowledgeStore) -> None:
        _, first = await stage(store, content=b"unchanged bytes")
        document, again = await stage(store, content=b"unchanged bytes")

        assert again.version_id == first.version_id
        assert len(document.versions) == 1
        assert document.next_revision() == 2

    run(repository_database_url, scenario)


@pytest.mark.integration
def test_a_renamed_document_keeps_one_identity(repository_database_url: str) -> None:
    """The external key is the identity; a new title must not fork the history."""

    async def scenario(store: PostgresKnowledgeStore) -> None:
        first, _ = await stage(store, title="Plan terms", content=b"v1")
        second, _ = await stage(store, title="2027 plan terms", content=b"v2")

        assert second.document_id == first.document_id
        assert second.title == "2027 plan terms"
        assert len(second.versions) == 2

    run(repository_database_url, scenario)


@pytest.mark.integration
def test_another_tenant_cannot_read_publish_or_delete_a_known_document(
    repository_database_url: str,
) -> None:
    """A leaked UUID is not authorization, and absence is indistinguishable."""

    async def scenario(store: PostgresKnowledgeStore) -> None:
        _, version = await stage(store, content=b"clearview only")
        await make_current(store, version)

        with pytest.raises(NotFoundError):
            await store.load_document(OTHER_TENANT, version.document_id)
        with pytest.raises(NotFoundError):
            await store.publish(OTHER_TENANT, version.version_id, at=NOW)
        with pytest.raises(NotFoundError):
            await store.delete_document(OTHER_TENANT, version.document_id, at=NOW)
        with pytest.raises(NotFoundError):
            await store.record_indexed(OTHER_TENANT, version.version_id, at=NOW)

        assert not await store.retrievable_versions(visitor(tenant_id=OTHER_TENANT))

    run(repository_database_url, scenario)


@pytest.mark.integration
def test_content_is_scoped_to_its_tenant_and_domain(repository_database_url: str) -> None:
    async def scenario(store: PostgresKnowledgeStore) -> None:
        _, mine = await stage(store, content=b"clearview terms")
        await make_current(store, mine)
        _, theirs = await stage(store, tenant_id=OTHER_TENANT, content=b"apex terms")
        await make_current(store, theirs, tenant_id=OTHER_TENANT)

        assert [version.version_id for version in await store.retrievable_versions(visitor())] == [
            mine.version_id
        ]

        other_domain = RetrievalContext(
            tenant_id=TENANT,
            domain=KnowledgeDomain.parse("services"),
            audience=RetrievalAudience.VISITOR,
            moment=NOW,
        )
        assert not await store.retrievable_versions(other_domain)

    run(repository_database_url, scenario)


@pytest.mark.integration
def test_disabling_a_source_withdraws_its_documents_without_rewriting_history(
    repository_database_url: str,
) -> None:
    async def scenario(store: PostgresKnowledgeStore) -> None:
        _, version = await stage(store, content=b"partner brochure")
        document = await make_current(store, version)

        await store.set_source_enabled(TENANT, document.source.source_id, enabled=False)
        assert not await store.retrievable_versions(visitor())

        restored = await store.set_source_enabled(TENANT, document.source.source_id, enabled=True)
        assert restored.enabled
        assert await store.retrievable_versions(visitor())
        reloaded = await store.load_document(TENANT, document.document_id)
        assert reloaded.version(version.version_id).state is VersionState.PUBLISHED

    run(repository_database_url, scenario)


@pytest.mark.integration
def test_internal_content_answers_staff_and_not_visitors(repository_database_url: str) -> None:
    async def scenario(store: PostgresKnowledgeStore) -> None:
        _, internal = await stage(
            store,
            external_key="dealer-rate-sheet.md",
            title="Dealer rate sheet",
            content=b"internal margins",
            visibility=Visibility.INTERNAL,
        )
        await make_current(store, internal)

        assert not await store.retrievable_versions(visitor())
        staff = RetrievalContext(
            tenant_id=TENANT, domain=FINANCING, audience=RetrievalAudience.STAFF, moment=NOW
        )
        assert [version.version_id for version in await store.retrievable_versions(staff)] == [
            internal.version_id
        ]

    run(repository_database_url, scenario)


@pytest.mark.integration
def test_the_stored_filter_agrees_with_the_domain_predicate(
    repository_database_url: str,
) -> None:
    """The SQL filter and the domain rule are two statements of one predicate.

    They are asserted against each other over a corpus holding every state, so a
    filter dropped from either side fails here rather than in an answer.
    """

    async def scenario(store: PostgresKnowledgeStore) -> None:
        document_ids: list[uuid.UUID] = []

        _, retrievable = await stage(store, external_key="live.md", content=b"live")
        await make_current(store, retrievable)
        document_ids.append(retrievable.document_id)

        _, draft = await stage(store, external_key="draft.md", content=b"draft")
        document_ids.append(draft.document_id)

        _, unindexed = await stage(store, external_key="unindexed.md", content=b"unindexed")
        await store.approve(TENANT, unindexed.version_id, approved_by="ops@example", at=NOW)
        await store.publish(TENANT, unindexed.version_id, at=NOW)
        document_ids.append(unindexed.document_id)

        _, expired = await stage(store, external_key="expired.md", content=b"expired")
        await make_current(store, expired, expires_at=NOW + timedelta(hours=1))
        document_ids.append(expired.document_id)

        _, withdrawn = await stage(store, external_key="withdrawn.md", content=b"withdrawn")
        await make_current(store, withdrawn)
        await store.delete_document(TENANT, withdrawn.document_id, at=NOW)
        document_ids.append(withdrawn.document_id)

        _, hidden = await stage(
            store,
            external_key="internal.md",
            content=b"internal",
            visibility=Visibility.INTERNAL,
        )
        await make_current(store, hidden)
        document_ids.append(hidden.document_id)

        _, quarantined = await stage(store, external_key="flagged.md", content=b"flagged")
        await make_current(store, quarantined)
        await store.quarantine(TENANT, quarantined.version_id, at=NOW)
        document_ids.append(quarantined.document_id)

        context = visitor(NOW + timedelta(days=1))
        documents = [await store.load_document(TENANT, item) for item in document_ids]
        expected = {
            version.version_id
            for document in documents
            if (version := document.retrievable_version(context)) is not None
        }
        stored = {version.version_id for version in await store.retrievable_versions(context)}

        assert stored == expected
        assert expected == {retrievable.version_id}

    run(repository_database_url, scenario)


@pytest.mark.integration
def test_quarantine_withdraws_a_version_for_every_audience_without_touching_lifecycle(
    repository_database_url: str,
) -> None:
    """Quarantine is a safety flag, not a state transition (`RAG-007`).

    The version stays published — clearing it is a review decision, and the
    row must survive re-publishing — but the retrieval filter drops it for
    every audience, and the indexing state is reset so the flagged bytes have
    to be re-scanned and re-embedded before they can answer again.
    """

    async def scenario(store: PostgresKnowledgeStore) -> None:
        _, version = await stage(store)
        await make_current(store, version)

        document = await store.quarantine(TENANT, version.version_id, at=NOW)
        held = document.version(version.version_id)
        assert held.safety_state is SafetyState.QUARANTINED
        assert held.state is VersionState.PUBLISHED
        assert held.indexing_state is IndexingState.PENDING

        staff = RetrievalContext(
            tenant_id=TENANT, domain=FINANCING, audience=RetrievalAudience.STAFF, moment=NOW
        )
        assert not await store.retrievable_versions(visitor())
        assert not await store.retrievable_versions(staff)

        # Idempotent: re-flagging an already-quarantined version changes nothing.
        await store.quarantine(TENANT, version.version_id, at=NOW)

    run(repository_database_url, scenario)


@pytest.mark.integration
def test_approved_review_clears_the_flag_but_only_reindexing_restores_retrieval(
    repository_database_url: str,
) -> None:
    """Approval is permission to re-embed, not a pass for the old chunks.

    If clearing the flag made the version retrievable from the index it was
    written to, the reviewed-byte contract in the domain plan would be a
    comment instead of a rule: the flagged content must not answer again
    until a successful re-ingestion replaces it.
    """

    async def scenario(store: PostgresKnowledgeStore) -> None:
        _, version = await stage(store)
        await make_current(store, version)
        await store.quarantine(TENANT, version.version_id, at=NOW)

        document = await store.quarantine_review(
            TENANT, version.version_id, approved=True, reviewed_by="reviewer@example", at=NOW
        )
        held = document.version(version.version_id)
        assert held.safety_state is SafetyState.CLEAR
        assert held.indexing_state is IndexingState.PENDING
        assert not await store.retrievable_versions(visitor())

        # A successful re-ingestion is what makes the version answerable again.
        await store.record_indexed(TENANT, version.version_id, at=NOW)
        assert [item.version_id for item in await store.retrievable_versions(visitor())] == [
            version.version_id
        ]

    run(repository_database_url, scenario)


@pytest.mark.integration
def test_rejected_review_keeps_the_version_quarantined_forever(
    repository_database_url: str,
) -> None:
    async def scenario(store: PostgresKnowledgeStore) -> None:
        _, version = await stage(store)
        await make_current(store, version)
        await store.quarantine(TENANT, version.version_id, at=NOW)

        document = await store.quarantine_review(
            TENANT, version.version_id, approved=False, reviewed_by="reviewer@example", at=NOW
        )
        held = document.version(version.version_id)
        assert held.safety_state is SafetyState.QUARANTINED
        assert not await store.retrievable_versions(visitor())

        # Even a re-index cannot make it retrievable: rejection is terminal.
        await store.record_indexed(TENANT, version.version_id, at=NOW)
        assert not await store.retrievable_versions(visitor())

    run(repository_database_url, scenario)


@pytest.mark.integration
def test_reviewing_a_clear_version_is_refused(repository_database_url: str) -> None:
    async def scenario(store: PostgresKnowledgeStore) -> None:
        _, version = await stage(store)
        await make_current(store, version)
        with pytest.raises(InvalidVersionTransitionError):
            await store.quarantine_review(
                TENANT, version.version_id, approved=True, reviewed_by="reviewer@example", at=NOW
            )

    run(repository_database_url, scenario)


@pytest.mark.integration
def test_a_draft_cannot_be_quarantined(repository_database_url: str) -> None:
    """Quarantine before review is meaningless: only reviewed content is scanned."""

    async def scenario(store: PostgresKnowledgeStore) -> None:
        _, version = await stage(store)
        with pytest.raises(ConflictError):
            await store.quarantine(TENANT, version.version_id, at=NOW)

    run(repository_database_url, scenario)


@pytest.mark.integration
def test_versions_in_safety_state_is_tenant_qualified(
    repository_database_url: str,
) -> None:
    async def scenario(store: PostgresKnowledgeStore) -> None:
        _, version = await stage(store)
        await make_current(store, version)
        await store.quarantine(TENANT, version.version_id, at=NOW)

        _, other = await stage(
            store, tenant_id=OTHER_TENANT, external_key="apex-terms.md", content=b"apex"
        )
        await make_current(store, other, tenant_id=OTHER_TENANT)

        quarantined = await store.versions_in_safety_state(TENANT, SafetyState.QUARANTINED)
        assert [item.version_id for item in quarantined] == [version.version_id]
        assert await store.versions_in_safety_state(OTHER_TENANT, SafetyState.QUARANTINED) == ()

    run(repository_database_url, scenario)


@pytest.mark.integration
def test_concurrent_publishes_serialize_on_the_document(
    repository_database_url: str,
) -> None:
    """The row lock, not luck, is what leaves exactly one current version."""

    async def scenario(store: PostgresKnowledgeStore) -> None:
        _, first = await stage(store, content=b"v1")
        await make_current(store, first)
        _, second = await stage(store, content=b"v2")
        await store.approve(TENANT, second.version_id, approved_by="ops@example", at=NOW)

        await asyncio.gather(
            store.publish(TENANT, first.version_id, at=NOW + timedelta(minutes=1)),
            store.publish(TENANT, second.version_id, at=NOW + timedelta(minutes=1)),
        )

        document = await store.load_document(TENANT, first.document_id)
        published = [
            version for version in document.versions if version.state is VersionState.PUBLISHED
        ]
        assert len(published) == 1

    run(repository_database_url, scenario)
