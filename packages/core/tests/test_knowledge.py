"""Versioned knowledge: what is retrievable, and which transitions are allowed.

The retrievability cases are written one reason at a time. Retrieval returns
``None`` for every reason, so a single "unhappy path" test would pass while three
of the five filters were broken.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

from tenantchat.core.errors import (
    DomainError,
    InvalidVersionTransitionError,
    NotFoundError,
    ValidationError,
)
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

TENANT = "clearview"
FINANCING = KnowledgeDomain.parse("financing")
NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


def checksum(seed: str) -> ContentChecksum:
    return ContentChecksum.of(seed.encode())


def rejection(raised: pytest.ExceptionInfo[DomainError]) -> str:
    """Which rule rejected the call.

    ``pytest.raises(match=...)`` compares against ``str(error)``, which is
    deliberately the one publishable sentence every instance of a type shares, so
    it cannot tell two validation failures apart. Operator context lives on
    ``detail``.
    """
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
def document_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def build_version(document_id: uuid.UUID) -> Callable[..., DocumentVersion]:
    """A published, indexed, public version, overridable one field at a time."""

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
def build_document(
    document_id: uuid.UUID, source: KnowledgeSource
) -> Callable[..., KnowledgeDocument]:
    def factory(**overrides: object) -> KnowledgeDocument:
        defaults: dict[str, object] = {
            "document_id": document_id,
            "tenant_id": TENANT,
            "source": source,
            "external_key": "plan-terms.pdf",
            "title": "Financing plan terms",
            "versions": (),
            "deleted": False,
        }
        return KnowledgeDocument(**(defaults | overrides))  # type: ignore[arg-type]

    return factory


@pytest.fixture
def visitor_now() -> RetrievalContext:
    return RetrievalContext(
        tenant_id=TENANT, domain=FINANCING, audience=RetrievalAudience.VISITOR, moment=NOW
    )


class TestValueObjects:
    def test_identical_bytes_produce_an_identical_checksum(self) -> None:
        """Re-ingestion idempotency rests on this, so it is asserted directly."""
        assert ContentChecksum.of(b"rate sheet") == ContentChecksum.of(b"rate sheet")

    def test_checksum_parses_a_stored_digest_case_insensitively(self) -> None:
        digest = ContentChecksum.of(b"rate sheet")

        assert ContentChecksum.parse(digest.value.upper()) == digest

    @pytest.mark.parametrize("raw", ["", "not-hex", "abc123", "z" * 64])
    def test_checksum_rejects_anything_that_is_not_a_sha256_digest(self, raw: str) -> None:
        with pytest.raises(ValidationError):
            ContentChecksum.parse(raw)

    @pytest.mark.parametrize("raw", ["Financing", " financing "])
    def test_domain_folds_case_and_surrounding_whitespace(self, raw: str) -> None:
        """A domain that differs only by case would filter to an empty result set."""
        assert KnowledgeDomain.parse(raw) == FINANCING

    @pytest.mark.parametrize("raw", ["", "x", "9lives", "has space", "under_score", "a" * 64])
    def test_domain_rejects_values_that_are_not_slugs(self, raw: str) -> None:
        with pytest.raises(ValidationError):
            KnowledgeDomain.parse(raw)

    def test_value_objects_render_as_their_stored_form(self) -> None:
        """These land in index filters and storage keys, not just in comparisons."""
        assert str(FINANCING) == "financing"
        assert str(ContentChecksum.of(b"x")) == ContentChecksum.of(b"x").value

    def test_audience_visibility_matrix(self) -> None:
        assert RetrievalAudience.VISITOR.may_read(Visibility.PUBLIC)
        assert not RetrievalAudience.VISITOR.may_read(Visibility.INTERNAL)
        assert RetrievalAudience.STAFF.may_read(Visibility.PUBLIC)
        assert RetrievalAudience.STAFF.may_read(Visibility.INTERNAL)


class TestVersionInvariants:
    def test_naive_dates_are_rejected(self, build_version: Callable[..., DocumentVersion]) -> None:
        """A window off by the deployment's UTC offset publishes content early."""
        with pytest.raises(ValidationError):
            build_version(effective_at=datetime(2026, 8, 1, 12, 0))  # noqa: DTZ001 - the point

    def test_a_published_version_must_have_an_effective_time(
        self, build_version: Callable[..., DocumentVersion]
    ) -> None:
        with pytest.raises(ValidationError) as raised:
            build_version(effective_at=None)

        assert "effective_at" in rejection(raised)

    def test_an_inverted_effective_window_is_rejected(
        self, build_version: Callable[..., DocumentVersion]
    ) -> None:
        with pytest.raises(ValidationError) as raised:
            build_version(effective_at=NOW, expires_at=NOW - timedelta(hours=1))

        assert "expires_at" in rejection(raised)

    @pytest.mark.parametrize(("field", "value"), [("revision", 0), ("byte_size", 0)])
    def test_counters_must_be_positive(
        self, build_version: Callable[..., DocumentVersion], field: str, value: int
    ) -> None:
        with pytest.raises(ValidationError) as raised:
            build_version(**{field: value})

        assert field in rejection(raised)

    def test_an_expiry_without_a_start_is_rejected(
        self, build_version: Callable[..., DocumentVersion]
    ) -> None:
        """An open-ended start would make "expired" mean "never live"."""
        with pytest.raises(ValidationError) as raised:
            build_version(
                state=VersionState.DRAFT, effective_at=None, expires_at=NOW + timedelta(days=1)
            )

        assert "expires_at" in rejection(raised)

    @pytest.mark.parametrize("field", ["media_type", "storage_key"])
    def test_text_fields_are_bounded(
        self, build_version: Callable[..., DocumentVersion], field: str
    ) -> None:
        """A runaway value from a parsed document must not reach a log line."""
        with pytest.raises(ValidationError) as raised:
            build_version(**{field: "x" * 4096})

        assert field in rejection(raised)

    def test_the_effective_window_is_half_open(
        self, build_version: Callable[..., DocumentVersion]
    ) -> None:
        """A version expiring at 09:00 must not answer a question asked at 09:00."""
        version = build_version(effective_at=NOW, expires_at=NOW + timedelta(hours=1))

        assert version.is_effective_at(NOW)
        assert not version.is_effective_at(NOW - timedelta(seconds=1))
        assert not version.is_effective_at(NOW + timedelta(hours=1))

    def test_a_version_that_was_never_published_is_effective_at_no_moment(
        self, build_version: Callable[..., DocumentVersion]
    ) -> None:
        draft = build_version(state=VersionState.DRAFT, effective_at=None)

        assert not draft.is_effective_at(NOW)


class TestDocumentInvariants:
    def test_two_published_versions_cannot_coexist(
        self,
        build_document: Callable[..., KnowledgeDocument],
        build_version: Callable[..., DocumentVersion],
    ) -> None:
        """The whole publish/rollback contract rests on there being exactly one."""
        with pytest.raises(ValidationError) as raised:
            build_document(
                versions=(
                    build_version(revision=1, checksum=checksum("a")),
                    build_version(revision=2, checksum=checksum("b")),
                )
            )

        assert "published versions" in rejection(raised)

    def test_a_version_from_another_document_is_rejected(
        self,
        build_document: Callable[..., KnowledgeDocument],
        build_version: Callable[..., DocumentVersion],
    ) -> None:
        with pytest.raises(ValidationError) as raised:
            build_document(versions=(build_version(document_id=uuid.uuid4()),))

        assert "different document or tenant" in rejection(raised)

    def test_a_source_from_another_tenant_is_rejected(
        self, build_document: Callable[..., KnowledgeDocument]
    ) -> None:
        foreign = KnowledgeSource(
            source_id=uuid.uuid4(),
            tenant_id="apex",
            domain=FINANCING,
            kind=SourceKind.UPLOAD,
            display_name="Apex brochures",
        )

        with pytest.raises(ValidationError) as raised:
            build_document(source=foreign)

        assert "different tenants" in rejection(raised)

    def test_two_versions_cannot_share_a_revision_number(
        self,
        build_document: Callable[..., KnowledgeDocument],
        build_version: Callable[..., DocumentVersion],
    ) -> None:
        """Revision is how an operator names a version in a rollback request."""
        with pytest.raises(ValidationError) as raised:
            build_document(
                versions=(
                    build_version(
                        revision=1, state=VersionState.SUPERSEDED, checksum=checksum("a")
                    ),
                    build_version(revision=1, checksum=checksum("b")),
                )
            )

        assert "duplicate revision" in rejection(raised)

    def test_next_revision_continues_the_sequence(
        self,
        build_document: Callable[..., KnowledgeDocument],
        build_version: Callable[..., DocumentVersion],
    ) -> None:
        empty = build_document()
        assert empty.next_revision() == 1

        document = build_document(
            versions=(
                build_version(revision=1, state=VersionState.SUPERSEDED, checksum=checksum("a")),
                build_version(revision=2, checksum=checksum("b")),
            )
        )
        assert document.next_revision() == 3

    def test_unchanged_content_resolves_to_the_existing_version(
        self,
        build_document: Callable[..., KnowledgeDocument],
        build_version: Callable[..., DocumentVersion],
    ) -> None:
        """Re-ingesting an unchanged document must not create a revision."""
        existing = build_version(checksum=checksum("rev-1"))
        document = build_document(versions=(existing,))

        assert document.version_with_checksum(checksum("rev-1")) is existing
        assert document.version_with_checksum(checksum("other")) is None

    def test_deleted_content_does_not_satisfy_a_checksum_match(
        self,
        build_document: Callable[..., KnowledgeDocument],
        build_version: Callable[..., DocumentVersion],
    ) -> None:
        """Re-uploading withdrawn content must produce a fresh, reviewable draft."""
        document = build_document(
            versions=(
                build_version(
                    state=VersionState.DELETED,
                    effective_at=None,
                    indexing_state=IndexingState.PENDING,
                ),
            )
        )

        assert document.version_with_checksum(checksum("rev-1")) is None

    def test_an_unknown_version_id_is_not_found(
        self, build_document: Callable[..., KnowledgeDocument]
    ) -> None:
        with pytest.raises(NotFoundError):
            build_document().version(uuid.uuid4())


class TestRetrievability:
    def test_the_published_indexed_current_version_is_retrievable(
        self,
        build_document: Callable[..., KnowledgeDocument],
        build_version: Callable[..., DocumentVersion],
        visitor_now: RetrievalContext,
    ) -> None:
        current = build_version(revision=2, checksum=checksum("rev-2"))
        document = build_document(
            versions=(
                build_version(
                    revision=1,
                    state=VersionState.SUPERSEDED,
                    checksum=checksum("old"),
                    effective_at=NOW - timedelta(days=30),
                ),
                current,
            )
        )

        assert document.retrievable_version(visitor_now) is current

    @pytest.mark.parametrize(
        ("reason", "version_overrides", "document_overrides"),
        [
            ("draft", {"state": VersionState.DRAFT, "effective_at": None}, {}),
            ("approved but unpublished", {"state": VersionState.APPROVED}, {}),
            ("superseded", {"state": VersionState.SUPERSEDED}, {}),
            ("deleted version", {"state": VersionState.DELETED}, {}),
            ("indexing not finished", {"indexing_state": IndexingState.PENDING}, {}),
            ("indexing failed", {"indexing_state": IndexingState.FAILED}, {}),
            ("not yet effective", {"effective_at": NOW + timedelta(hours=1)}, {}),
            (
                "expired",
                {"effective_at": NOW - timedelta(days=2), "expires_at": NOW - timedelta(hours=1)},
                {},
            ),
            ("internal visibility", {"visibility": Visibility.INTERNAL}, {}),
            ("deleted document", {}, {"deleted": True}),
        ],
    )
    def test_unavailable_content_is_indistinguishable_from_absent(
        self,
        build_document: Callable[..., KnowledgeDocument],
        build_version: Callable[..., DocumentVersion],
        visitor_now: RetrievalContext,
        reason: str,
        version_overrides: dict[str, object],
        document_overrides: dict[str, object],
    ) -> None:
        document = build_document(
            versions=(build_version(**version_overrides),), **document_overrides
        )

        assert document.retrievable_version(visitor_now) is None, reason

    def test_a_disabled_source_withdraws_its_documents(
        self,
        build_document: Callable[..., KnowledgeDocument],
        build_version: Callable[..., DocumentVersion],
        source: KnowledgeSource,
        visitor_now: RetrievalContext,
    ) -> None:
        document = build_document(
            source=KnowledgeSource(
                source_id=source.source_id,
                tenant_id=source.tenant_id,
                domain=source.domain,
                kind=source.kind,
                display_name=source.display_name,
                enabled=False,
            ),
            versions=(build_version(),),
        )

        assert document.retrievable_version(visitor_now) is None

    def test_another_tenants_document_is_never_retrievable(
        self,
        build_document: Callable[..., KnowledgeDocument],
        build_version: Callable[..., DocumentVersion],
    ) -> None:
        document = build_document(versions=(build_version(),))
        other_tenant = RetrievalContext(
            tenant_id="apex", domain=FINANCING, audience=RetrievalAudience.STAFF, moment=NOW
        )

        assert document.retrievable_version(other_tenant) is None

    def test_another_domain_does_not_match(
        self,
        build_document: Callable[..., KnowledgeDocument],
        build_version: Callable[..., DocumentVersion],
    ) -> None:
        document = build_document(versions=(build_version(),))
        other_domain = RetrievalContext(
            tenant_id=TENANT,
            domain=KnowledgeDomain.parse("services"),
            audience=RetrievalAudience.VISITOR,
            moment=NOW,
        )

        assert document.retrievable_version(other_domain) is None

    def test_staff_may_read_internal_content_visitors_may_not(
        self,
        build_document: Callable[..., KnowledgeDocument],
        build_version: Callable[..., DocumentVersion],
    ) -> None:
        document = build_document(versions=(build_version(visibility=Visibility.INTERNAL),))
        staff = RetrievalContext(
            tenant_id=TENANT, domain=FINANCING, audience=RetrievalAudience.STAFF, moment=NOW
        )

        assert document.retrievable_version(staff) is not None

    def test_retrieval_context_rejects_a_naive_moment(self) -> None:
        with pytest.raises(ValidationError):
            RetrievalContext(
                tenant_id=TENANT,
                domain=FINANCING,
                audience=RetrievalAudience.VISITOR,
                moment=datetime(2026, 8, 1, 12, 0),  # noqa: DTZ001 - the point of the test
            )


class TestApproval:
    def test_a_draft_can_be_approved(
        self,
        build_document: Callable[..., KnowledgeDocument],
        build_version: Callable[..., DocumentVersion],
    ) -> None:
        draft = build_version(state=VersionState.DRAFT, effective_at=None)
        document = build_document(versions=(draft,))

        plan = document.plan_approval(draft.version_id, approved_by="ops@clearview", at=NOW)

        assert plan.version_id == draft.version_id
        assert plan.approved_by == "ops@clearview"
        assert plan.approved_at == NOW

    @pytest.mark.parametrize(
        "state", [VersionState.APPROVED, VersionState.PUBLISHED, VersionState.SUPERSEDED]
    )
    def test_only_a_draft_can_be_approved(
        self,
        build_document: Callable[..., KnowledgeDocument],
        build_version: Callable[..., DocumentVersion],
        state: VersionState,
    ) -> None:
        version = build_version(state=state)
        document = build_document(versions=(version,))

        with pytest.raises(InvalidVersionTransitionError) as raised:
            document.plan_approval(version.version_id, approved_by="ops@clearview", at=NOW)

        assert raised.value.current is state
        assert raised.value.permitted == (VersionState.DRAFT,)

    def test_an_anonymous_approval_is_rejected(
        self,
        build_document: Callable[..., KnowledgeDocument],
        build_version: Callable[..., DocumentVersion],
    ) -> None:
        """Approval is the audit record of who accepted responsibility."""
        draft = build_version(state=VersionState.DRAFT, effective_at=None)
        document = build_document(versions=(draft,))

        with pytest.raises(ValidationError) as raised:
            document.plan_approval(draft.version_id, approved_by="  ", at=NOW)

        assert "approved_by" in rejection(raised)


class TestPublication:
    def test_publishing_supersedes_the_current_version_in_one_plan(
        self,
        build_document: Callable[..., KnowledgeDocument],
        build_version: Callable[..., DocumentVersion],
    ) -> None:
        """Two writes in one plan is what makes the swap atomic downstream."""
        current = build_version(revision=1, checksum=checksum("old"))
        candidate = build_version(
            revision=2,
            state=VersionState.APPROVED,
            checksum=checksum("new"),
            effective_at=None,
            indexing_state=IndexingState.PENDING,
        )
        document = build_document(versions=(current, candidate))

        plan = document.plan_publication(candidate.version_id, at=NOW)

        assert plan.version_id == candidate.version_id
        assert plan.supersedes_version_id == current.version_id
        assert plan.effective_at == NOW
        assert plan.expires_at is None
        assert not plan.is_rollback

    def test_a_first_publication_supersedes_nothing(
        self,
        build_document: Callable[..., KnowledgeDocument],
        build_version: Callable[..., DocumentVersion],
    ) -> None:
        candidate = build_version(state=VersionState.APPROVED, effective_at=None)
        document = build_document(versions=(candidate,))

        plan = document.plan_publication(candidate.version_id, at=NOW)

        assert plan.supersedes_version_id is None

    def test_publication_can_be_scheduled_and_bounded(
        self,
        build_document: Callable[..., KnowledgeDocument],
        build_version: Callable[..., DocumentVersion],
    ) -> None:
        candidate = build_version(state=VersionState.APPROVED, effective_at=None)
        document = build_document(versions=(candidate,))
        starts = NOW + timedelta(days=1)
        ends = NOW + timedelta(days=30)

        plan = document.plan_publication(
            candidate.version_id, at=NOW, effective_at=starts, expires_at=ends
        )

        assert plan.published_at == NOW
        assert plan.effective_at == starts
        assert plan.expires_at == ends

    def test_a_draft_cannot_be_published(
        self,
        build_document: Callable[..., KnowledgeDocument],
        build_version: Callable[..., DocumentVersion],
    ) -> None:
        """Approval is the review gate; publishing must not be able to skip it."""
        draft = build_version(state=VersionState.DRAFT, effective_at=None)
        document = build_document(versions=(draft,))

        with pytest.raises(InvalidVersionTransitionError) as raised:
            document.plan_publication(draft.version_id, at=NOW)

        assert raised.value.current is VersionState.DRAFT

    def test_a_deleted_version_cannot_be_published(
        self,
        build_document: Callable[..., KnowledgeDocument],
        build_version: Callable[..., DocumentVersion],
    ) -> None:
        withdrawn = build_version(state=VersionState.DELETED)
        document = build_document(versions=(withdrawn,))

        with pytest.raises(InvalidVersionTransitionError):
            document.plan_publication(withdrawn.version_id, at=NOW)

    def test_nothing_can_be_published_on_a_deleted_document(
        self,
        build_document: Callable[..., KnowledgeDocument],
        build_version: Callable[..., DocumentVersion],
    ) -> None:
        """Deleted is indistinguishable from absent, so this is a not-found."""
        candidate = build_version(state=VersionState.APPROVED, effective_at=None)
        document = build_document(versions=(candidate,), deleted=True)

        with pytest.raises(NotFoundError):
            document.plan_publication(candidate.version_id, at=NOW)

    def test_an_inverted_window_is_rejected(
        self,
        build_document: Callable[..., KnowledgeDocument],
        build_version: Callable[..., DocumentVersion],
    ) -> None:
        candidate = build_version(state=VersionState.APPROVED, effective_at=None)
        document = build_document(versions=(candidate,))

        with pytest.raises(ValidationError) as raised:
            document.plan_publication(
                candidate.version_id, at=NOW, expires_at=NOW - timedelta(hours=1)
            )

        assert "expires_at" in rejection(raised)

    def test_republishing_the_current_version_revises_its_window(
        self,
        build_document: Callable[..., KnowledgeDocument],
        build_version: Callable[..., DocumentVersion],
    ) -> None:
        """Extending a live document must not require a revision nobody edited."""
        current = build_version()
        document = build_document(versions=(current,))

        plan = document.plan_publication(
            current.version_id, at=NOW, expires_at=NOW + timedelta(days=90)
        )

        assert plan.supersedes_version_id is None
        assert plan.expires_at == NOW + timedelta(days=90)


class TestRollback:
    def test_publishing_a_superseded_version_restores_it_and_demotes_the_current_one(
        self,
        build_document: Callable[..., KnowledgeDocument],
        build_version: Callable[..., DocumentVersion],
    ) -> None:
        """Rollback is a publication, so no window exists where both are current."""
        previous = build_version(
            revision=1,
            state=VersionState.SUPERSEDED,
            checksum=checksum("good"),
            effective_at=NOW - timedelta(days=10),
        )
        current = build_version(revision=2, checksum=checksum("bad"))
        document = build_document(versions=(previous, current))

        plan = document.plan_publication(previous.version_id, at=NOW)

        assert plan.version_id == previous.version_id
        assert plan.supersedes_version_id == current.version_id
        assert plan.is_rollback

    def test_a_rollback_target_keeps_its_approval(
        self,
        build_document: Callable[..., KnowledgeDocument],
        build_version: Callable[..., DocumentVersion],
    ) -> None:
        """A superseded version was approved once; rollback must not re-review it."""
        previous = build_version(revision=1, state=VersionState.SUPERSEDED, checksum=checksum("a"))
        document = build_document(versions=(previous,))

        assert document.plan_publication(previous.version_id, at=NOW).is_rollback


class TestExpiry:
    def test_expiring_the_current_version_ends_its_window(
        self,
        build_document: Callable[..., KnowledgeDocument],
        build_version: Callable[..., DocumentVersion],
    ) -> None:
        current = build_version()
        document = build_document(versions=(current,))

        plan = document.plan_expiry(current.version_id, at=NOW)

        assert plan.version_id == current.version_id
        assert plan.expires_at == NOW

    def test_expiry_before_the_effective_time_is_rejected(
        self,
        build_document: Callable[..., KnowledgeDocument],
        build_version: Callable[..., DocumentVersion],
    ) -> None:
        current = build_version(effective_at=NOW)
        document = build_document(versions=(current,))

        with pytest.raises(ValidationError) as raised:
            document.plan_expiry(current.version_id, at=NOW - timedelta(hours=1))

        assert "expires_at" in rejection(raised)

    @pytest.mark.parametrize(
        "state", [VersionState.DRAFT, VersionState.APPROVED, VersionState.SUPERSEDED]
    )
    def test_only_the_published_version_can_expire(
        self,
        build_document: Callable[..., KnowledgeDocument],
        build_version: Callable[..., DocumentVersion],
        state: VersionState,
    ) -> None:
        version = build_version(state=state)
        document = build_document(versions=(version,))

        with pytest.raises(InvalidVersionTransitionError) as raised:
            document.plan_expiry(version.version_id, at=NOW)

        assert raised.value.permitted == (VersionState.PUBLISHED,)


class TestIndexingEligibility:
    @pytest.mark.parametrize(
        "state", [VersionState.APPROVED, VersionState.PUBLISHED, VersionState.SUPERSEDED]
    )
    def test_reviewed_versions_may_be_indexed(
        self,
        build_document: Callable[..., KnowledgeDocument],
        build_version: Callable[..., DocumentVersion],
        state: VersionState,
    ) -> None:
        """Superseded content stays indexable so a rollback is immediate."""
        version = build_version(state=state)
        document = build_document(versions=(version,))

        assert document.version_for_indexing(version.version_id) == version

    def test_an_unreviewed_draft_may_not_be_indexed(
        self,
        build_document: Callable[..., KnowledgeDocument],
        build_version: Callable[..., DocumentVersion],
    ) -> None:
        """Indexing a draft leaves unapproved content one publish from answerable."""
        draft = build_version(state=VersionState.DRAFT, effective_at=None)
        document = build_document(versions=(draft,))

        with pytest.raises(InvalidVersionTransitionError) as raised:
            document.version_for_indexing(draft.version_id)

        assert raised.value.current is VersionState.DRAFT

    def test_a_withdrawn_version_may_not_be_indexed(
        self,
        build_document: Callable[..., KnowledgeDocument],
        build_version: Callable[..., DocumentVersion],
    ) -> None:
        withdrawn = build_version(state=VersionState.DELETED)
        document = build_document(versions=(withdrawn,))

        with pytest.raises(InvalidVersionTransitionError):
            document.version_for_indexing(withdrawn.version_id)


class TestPublicView:
    def test_the_public_view_carries_no_operator_fields(
        self,
        build_document: Callable[..., KnowledgeDocument],
        build_version: Callable[..., DocumentVersion],
    ) -> None:
        """A storage key or an indexing error must not reach a cited answer."""
        current = build_version()
        document = build_document(versions=(current,))

        view = document.public_view(current)

        assert view.title == "Financing plan terms"
        assert view.source_name == "Financing partner brochures"
        assert view.revision == current.revision
        assert not {"storage_key", "checksum", "indexing_state", "visibility"} & set(view.__slots__)

    def test_an_unpublished_version_has_nothing_to_cite(
        self,
        build_document: Callable[..., KnowledgeDocument],
        build_version: Callable[..., DocumentVersion],
    ) -> None:
        draft = build_version(state=VersionState.DRAFT, effective_at=None)
        document = build_document(versions=(draft,))

        with pytest.raises(ValidationError) as raised:
            document.public_view(draft)

        assert "never published" in rejection(raised)

    def test_a_version_from_another_document_cannot_be_cited(
        self,
        build_document: Callable[..., KnowledgeDocument],
        build_version: Callable[..., DocumentVersion],
    ) -> None:
        foreign = build_version(document_id=uuid.uuid4())
        document = build_document(versions=(build_version(),))

        with pytest.raises(NotFoundError):
            document.public_view(foreign)
