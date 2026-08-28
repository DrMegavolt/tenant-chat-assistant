"""Versioned tenant knowledge: what may be retrieved, and when.

**Retrievability is derived, never stored.** There is no ``is_retrievable``
column and no ``active`` flag, because a flag is a second copy of the truth that
drifts from the state it summarizes — a document expires at midnight and nothing
runs to flip the bit, or a rollback restores an old version and the flag stays on
the withdrawn one. :meth:`KnowledgeDocument.retrievable_version` computes it from
approval state, indexing state, the effective window, source ownership, the
asking audience, and the content-safety state, so there is exactly one
definition and it cannot go stale. The retrieval adapter's index filter
(`RAG-004`) implements the same predicate; this module is the specification
it must match. Quarantine (`RAG-007`) is an *input* to that predicate, decided
by the ingestion worker and stored like indexing state — never a derived bit.

**Transitions are planned, not applied.** Approving, publishing, and expiring
each return a plan describing the exact rows to change, because a publish is two
writes — supersede the current version, promote the new one — that must land in
one transaction or not at all. Returning a plan keeps the rule here, where it is
testable without a database, and leaves atomicity to the adapter that owns the
transaction. The database enforces the same one-published-version invariant with
a partial unique index, so a plan built from stale state fails loudly instead of
producing two current versions.

A version's effective window belongs to its *publication*, not to its draft: a
draft that has sat unapproved for a month should not become live the instant
someone approves it because of a date typed in weeks ago. Drafts therefore carry
no dates at all, and :meth:`KnowledgeDocument.plan_publication` takes them.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from tenantchat.core.errors import (
    InvalidVersionTransitionError,
    NotFoundError,
    ValidationError,
)
from tenantchat.core.lifecycle import IndexingState, VersionState
from tenantchat.core.safety import QuarantineReviewPlan, SafetyState

# Matches the tenant ID format, so a domain reads the same everywhere it appears:
# storage, index filters, metric labels, and object-storage prefixes.
_DOMAIN_RE = re.compile(r"^[a-z][a-z0-9-]{1,62}$")
_CHECKSUM_RE = re.compile(r"^[0-9a-f]{64}$")

_MAX_DISPLAY_NAME = 200
_MAX_EXTERNAL_KEY = 512
_MAX_MEDIA_TYPE = 120
_MAX_STORAGE_KEY = 1024
_MAX_TITLE = 300

_PUBLISHABLE_FROM = (VersionState.APPROVED, VersionState.PUBLISHED, VersionState.SUPERSEDED)


def require_aware(name: str, moment: datetime) -> datetime:
    """Reject a naive datetime.

    Comparing a naive datetime against an aware one raises, and comparing two
    naive ones silently assumes they share a zone. An effective window that is
    wrong by the deployment's UTC offset publishes content hours early.

    Shared by every domain type that carries a wall-clock bound, so one rule
    decides what "a real moment" means.

    Raises:
        ValidationError: if ``moment`` carries no UTC offset.
    """
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ValidationError(detail=f"{name} must be timezone-aware")
    return moment


def _require_text(name: str, raw: str, limit: int) -> str:
    """Collapse whitespace and enforce a bound.

    Raises:
        ValidationError: if the value is blank or longer than ``limit``.
    """
    value = " ".join(raw.split())
    if not value:
        raise ValidationError(detail=f"{name} is blank")
    if len(value) > limit:
        raise ValidationError(detail=f"{name} is {len(value)} characters, limit {limit}")
    return value


@dataclass(frozen=True, slots=True)
class ContentChecksum:
    """SHA-256 of the exact bytes a version was created from.

    Content *identity*, not integrity checking: it is what makes re-ingesting an
    unchanged document a no-op (`RAG-002`) rather than a new revision nobody
    reviewed and a re-embedding nobody needed.
    """

    value: str

    @classmethod
    def of(cls, content: bytes) -> ContentChecksum:
        """Checksum raw document bytes."""
        return cls(hashlib.sha256(content).hexdigest())

    @classmethod
    def parse(cls, raw: str) -> ContentChecksum:
        """Parse a stored or transmitted checksum.

        Raises:
            ValidationError: if the value is not 64 lowercase hex characters.
        """
        candidate = raw.strip().lower()
        if not _CHECKSUM_RE.match(candidate):
            raise ValidationError(
                detail=f"checksum is {len(candidate)} characters, expected 64 hex"
            )
        return cls(candidate)

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class KnowledgeDomain:
    """A tenant's partition of its knowledge, such as ``financing``.

    Parsed rather than carried as free text because it is a retrieval *filter*: a
    value differing only in case or trailing whitespace matches nothing, and an
    empty result set looks exactly like a tenant with no content on the subject.
    """

    value: str

    @classmethod
    def parse(cls, raw: str) -> KnowledgeDomain:
        """Parse a domain slug.

        Raises:
            ValidationError: if the value is not a lowercase slug of 2-63
                characters starting with a letter.
        """
        candidate = raw.strip().lower()
        if not _DOMAIN_RE.match(candidate):
            # The raw value is the caller's own words, so it is content: the
            # detail names the failure, never the rejected text (ADR-0010).
            raise ValidationError(detail="value is not a valid knowledge domain slug")
        return cls(candidate)

    def __str__(self) -> str:
        return self.value


class SourceKind(StrEnum):
    """Where a source's documents come from.

    The kind constrains what `RAG-002` may do to refresh a source, and never
    names a filesystem path: the prototype's caller-supplied path is the
    vulnerability that task exists to remove.
    """

    UPLOAD = "upload"
    URL = "url"
    MANUAL = "manual"


class Visibility(StrEnum):
    """Who a version may be shown to once it is retrievable."""

    PUBLIC = "public"
    """May ground an answer to an unauthenticated visitor."""

    INTERNAL = "internal"
    """Staff-facing only: rate sheets, escalation procedures, internal policy."""


class RetrievalAudience(StrEnum):
    """Who is asking, for the purpose of visibility filtering."""

    VISITOR = "visitor"
    STAFF = "staff"

    def may_read(self, visibility: Visibility) -> bool:
        """Whether this audience may be shown content with that visibility."""
        return visibility is Visibility.PUBLIC or self is RetrievalAudience.STAFF


@dataclass(frozen=True, slots=True)
class KnowledgeSource:
    """A tenant-owned origin that documents belong to.

    ``enabled`` withdraws every document under a source in one act, without
    rewriting version history: a tenant that pulls a financing partner's brochure
    set needs it to stop answering questions now, and needs the audit trail of
    what it used to say intact.
    """

    source_id: uuid.UUID
    tenant_id: str
    domain: KnowledgeDomain
    kind: SourceKind
    display_name: str
    enabled: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "display_name",
            _require_text("display_name", self.display_name, _MAX_DISPLAY_NAME),
        )


@dataclass(frozen=True, slots=True)
class DocumentVersion:
    """One immutable revision of a document's content.

    Content is referenced by ``storage_key``, never carried here: a version is a
    control record that a policy decision is made about, and the domain has no
    reason to hold megabytes of parsed PDF to decide whether it may be answered
    from.

    ``storage_key``, ``checksum``, and ``indexing_state`` are operator-facing.
    Nothing on this type may be serialized to a visitor; use
    :meth:`KnowledgeDocument.public_view`.
    """

    version_id: uuid.UUID
    tenant_id: str
    document_id: uuid.UUID
    revision: int
    state: VersionState
    indexing_state: IndexingState
    visibility: Visibility
    checksum: ContentChecksum
    byte_size: int
    media_type: str
    storage_key: str
    safety_state: SafetyState = SafetyState.CLEAR
    effective_at: datetime | None = None
    expires_at: datetime | None = None
    # Operator-facing provenance, denormalized like the effective window: the
    # approval and publication decisions a console displays without re-deriving
    # them from history.
    approved_at: datetime | None = None
    approved_by: str | None = None
    published_at: datetime | None = None
    superseded_at: datetime | None = None
    indexed_at: datetime | None = None
    index_error_code: str | None = None

    def __post_init__(self) -> None:
        if self.revision < 1:
            raise ValidationError(detail=f"revision {self.revision} is not positive")
        if self.byte_size < 1:
            raise ValidationError(detail=f"byte_size {self.byte_size} is not positive")
        object.__setattr__(
            self, "media_type", _require_text("media_type", self.media_type, _MAX_MEDIA_TYPE)
        )
        object.__setattr__(
            self, "storage_key", _require_text("storage_key", self.storage_key, _MAX_STORAGE_KEY)
        )
        if self.effective_at is not None:
            require_aware("effective_at", self.effective_at)
        if self.expires_at is not None:
            require_aware("expires_at", self.expires_at)
        if self.state is VersionState.PUBLISHED and self.effective_at is None:
            raise ValidationError(detail="a published version has no effective_at")
        if self.expires_at is not None:
            if self.effective_at is None:
                raise ValidationError(detail="expires_at set without effective_at")
            if self.expires_at <= self.effective_at:
                raise ValidationError(detail="expires_at is not after effective_at")

    def is_effective_at(self, moment: datetime) -> bool:
        """Whether ``moment`` falls inside this version's published window.

        The window is half-open: a version expiring at 09:00 does not answer a
        question asked at 09:00.
        """
        if self.effective_at is None:
            return False
        return self.effective_at <= moment and (self.expires_at is None or moment < self.expires_at)


@dataclass(frozen=True, slots=True)
class PublicDocumentView:
    """Document metadata safe to return with an answer.

    Every field here is world-readable by design, and the type exists so that
    adding a field to :class:`DocumentVersion` — a storage key, an indexing
    error, a reviewer's name — cannot reach a visitor by omission. `RAG-005`
    extends this with per-claim citation locations.
    """

    document_id: uuid.UUID
    title: str
    source_name: str
    revision: int
    effective_at: datetime


@dataclass(frozen=True, slots=True)
class RetrievalContext:
    """The question a retrieval filter is answering: content for whom, when.

    Passed as one value rather than four arguments so that a caller cannot supply
    a tenant and forget the audience, silently widening what an unauthenticated
    visitor can be told.
    """

    tenant_id: str
    domain: KnowledgeDomain
    audience: RetrievalAudience
    moment: datetime

    def __post_init__(self) -> None:
        require_aware("moment", self.moment)


@dataclass(frozen=True, slots=True)
class ApprovalPlan:
    """The single row change that marks a draft reviewed."""

    tenant_id: str
    document_id: uuid.UUID
    version_id: uuid.UUID
    approved_by: str
    approved_at: datetime


@dataclass(frozen=True, slots=True)
class PublicationPlan:
    """The row changes that make one version current, applied as one transaction.

    ``supersedes_version_id`` is the version losing currency, or ``None`` when the
    document has none — the first publication, or a republication of the version
    that is already current with a new effective window.
    """

    tenant_id: str
    document_id: uuid.UUID
    version_id: uuid.UUID
    supersedes_version_id: uuid.UUID | None
    effective_at: datetime
    expires_at: datetime | None
    published_at: datetime
    is_rollback: bool


@dataclass(frozen=True, slots=True)
class ExpiryPlan:
    """The row change that ends a published version's effective window.

    The version stays :attr:`~tenantchat.core.lifecycle.VersionState.PUBLISHED`:
    it is still the document's current version, it has simply stopped being
    effective. Demoting it would make "expired" and "superseded" indistinguishable
    in history, and would leave a rollback target that never existed.
    """

    tenant_id: str
    document_id: uuid.UUID
    version_id: uuid.UUID
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class KnowledgeDocument:
    """A document and every revision of it, as the domain reasons about them.

    Loaded whole because the rules are about the *set*: publishing needs to know
    which version is current, rollback needs the superseded ones, and idempotent
    re-ingestion needs every checksum. Version counts are bounded by how often a
    human edits a policy document, so this stays small.
    """

    document_id: uuid.UUID
    tenant_id: str
    source: KnowledgeSource
    external_key: str
    title: str
    versions: tuple[DocumentVersion, ...] = ()
    deleted: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "title", _require_text("title", self.title, _MAX_TITLE))
        object.__setattr__(
            self,
            "external_key",
            _require_text("external_key", self.external_key, _MAX_EXTERNAL_KEY),
        )
        if self.source.tenant_id != self.tenant_id:
            raise ValidationError(detail="document and source belong to different tenants")

        seen: set[int] = set()
        published = 0
        for version in self.versions:
            if version.tenant_id != self.tenant_id or version.document_id != self.document_id:
                raise ValidationError(detail="version belongs to a different document or tenant")
            if version.revision in seen:
                raise ValidationError(detail=f"duplicate revision {version.revision}")
            seen.add(version.revision)
            if version.state is VersionState.PUBLISHED:
                published += 1
        if published > 1:
            raise ValidationError(detail=f"{published} published versions on one document")

    @property
    def domain(self) -> KnowledgeDomain:
        """The domain this document is filtered by, owned by its source."""
        return self.source.domain

    def next_revision(self) -> int:
        """The revision number a new draft would take."""
        return max((version.revision for version in self.versions), default=0) + 1

    def version(self, version_id: uuid.UUID) -> DocumentVersion:
        """Look up one version.

        Raises:
            NotFoundError: if no version of this document has that ID.
        """
        for candidate in self.versions:
            if candidate.version_id == version_id:
                return candidate
        raise NotFoundError(
            detail=f"version {version_id} is not part of document {self.document_id}"
        )

    def version_with_checksum(self, checksum: ContentChecksum) -> DocumentVersion | None:
        """The existing version holding this exact content, if there is one.

        Re-ingestion calls this first: identical bytes must reuse the existing
        revision rather than create one, or every scheduled crawl of an unchanged
        page produces a version awaiting approval.
        """
        for candidate in self.versions:
            if candidate.checksum == checksum and candidate.state is not VersionState.DELETED:
                return candidate
        return None

    def published(self) -> DocumentVersion | None:
        """The current version, whether or not it is presently effective."""
        for candidate in self.versions:
            if candidate.state is VersionState.PUBLISHED:
                return candidate
        return None

    def retrievable_version(self, context: RetrievalContext) -> DocumentVersion | None:
        """The version that may ground an answer right now, or ``None``.

        ``None`` covers every reason at once — wrong tenant or domain, deleted
        document, disabled source, nothing published, outside the effective
        window, quarantined, not yet indexed, or visibility above the audience —
        because a caller must not branch on *why* content is unavailable.
        Distinguishing "another tenant owns this" from "this tenant has nothing"
        is the leak.
        """
        if self.deleted or not self.source.enabled:
            return None
        if self.tenant_id != context.tenant_id or self.domain != context.domain:
            return None

        current = self.published()
        if current is None:
            return None
        if current.indexing_state is not IndexingState.INDEXED:
            return None
        if current.safety_state is not SafetyState.CLEAR:
            return None
        if not current.is_effective_at(context.moment):
            return None
        if not context.audience.may_read(current.visibility):
            return None
        return current

    def public_view(self, version: DocumentVersion) -> PublicDocumentView:
        """Project a version to the citation-safe subset.

        Raises:
            NotFoundError: if the version is not part of this document.
            ValidationError: if the version was never published and so has no
                effective date to cite.
        """
        held = self.version(version.version_id)
        if held.effective_at is None:
            raise ValidationError(detail="cannot cite a version that was never published")
        return PublicDocumentView(
            document_id=self.document_id,
            title=self.title,
            source_name=self.source.display_name,
            revision=held.revision,
            effective_at=held.effective_at,
        )

    def version_for_indexing(self, version_id: uuid.UUID) -> DocumentVersion:
        """The version an indexing job may report a result against.

        A draft has not been reviewed and a deleted version has been withdrawn;
        indexing either would put unapproved or retracted content one publish away
        from being answerable.

        Raises:
            NotFoundError: if the document is deleted or the version is unknown.
            InvalidVersionTransitionError: if the version is a draft or deleted.
        """
        version = self._live_version(version_id)
        if version.state is VersionState.DRAFT:
            raise InvalidVersionTransitionError(
                current=version.state,
                permitted=_PUBLISHABLE_FROM,
                detail=f"version {version_id} is not reviewed and must not be indexed",
            )
        return version

    def plan_approval(
        self, version_id: uuid.UUID, *, approved_by: str, at: datetime
    ) -> ApprovalPlan:
        """Mark a draft reviewed and publishable.

        Raises:
            NotFoundError: if the document is deleted or the version is unknown.
            InvalidVersionTransitionError: if the version is not a draft.
            ValidationError: ``approved_by`` is blank, or ``at`` is naive.
        """
        version = self._live_version(version_id)
        if version.state is not VersionState.DRAFT:
            raise InvalidVersionTransitionError(
                current=version.state,
                permitted=(VersionState.DRAFT,),
                detail=f"version {version_id} cannot be approved from {version.state.value}",
            )
        return ApprovalPlan(
            tenant_id=self.tenant_id,
            document_id=self.document_id,
            version_id=version_id,
            approved_by=_require_text("approved_by", approved_by, _MAX_DISPLAY_NAME),
            approved_at=require_aware("at", at),
        )

    def plan_publication(
        self,
        version_id: uuid.UUID,
        *,
        at: datetime,
        effective_at: datetime | None = None,
        expires_at: datetime | None = None,
    ) -> PublicationPlan:
        """Make one approved version current, superseding whichever version was.

        Publishing a superseded version is a rollback and needs no separate
        operation: the plan supersedes the current version the same way, so there
        is no window in which both are retrievable. Publishing the already-current
        version is permitted and revises its effective window, which is how an
        operator extends or shortens a live document without a new revision.

        Args:
            at: When the publication happens. Also the default effective time.
            effective_at: When the version starts answering, for a scheduled
                publication. Defaults to ``at``.
            expires_at: When it stops. ``None`` means it stays current until
                superseded.

        Raises:
            NotFoundError: if the document is deleted or the version is unknown.
            InvalidVersionTransitionError: if the version is a draft or deleted.
            ValidationError: if a datetime is naive or the window is inverted.
        """
        version = self._live_version(version_id)
        if version.state not in _PUBLISHABLE_FROM:
            raise InvalidVersionTransitionError(
                current=version.state,
                permitted=_PUBLISHABLE_FROM,
                detail=f"version {version_id} cannot be published from {version.state.value}",
            )

        published_at = require_aware("at", at)
        effective = (
            published_at if effective_at is None else require_aware("effective_at", effective_at)
        )
        if expires_at is not None:
            require_aware("expires_at", expires_at)
            if expires_at <= effective:
                raise ValidationError(detail="expires_at is not after effective_at")

        current = self.published()
        supersedes = (
            current.version_id if current is not None and current.version_id != version_id else None
        )
        return PublicationPlan(
            tenant_id=self.tenant_id,
            document_id=self.document_id,
            version_id=version_id,
            supersedes_version_id=supersedes,
            effective_at=effective,
            expires_at=expires_at,
            published_at=published_at,
            is_rollback=version.state is VersionState.SUPERSEDED,
        )

    def plan_expiry(self, version_id: uuid.UUID, *, at: datetime) -> ExpiryPlan:
        """End the current version's effective window.

        Raises:
            NotFoundError: if the document is deleted or the version is unknown.
            InvalidVersionTransitionError: if the version is not the published one.
            ValidationError: if ``at`` is naive or not after the effective time.
        """
        version = self._live_version(version_id)
        if version.state is not VersionState.PUBLISHED:
            raise InvalidVersionTransitionError(
                current=version.state,
                permitted=(VersionState.PUBLISHED,),
                detail=f"version {version_id} cannot expire from {version.state.value}",
            )
        expires_at = require_aware("at", at)
        # `effective_at` is never None on a published version; __post_init__ rejects it.
        if version.effective_at is not None and expires_at <= version.effective_at:
            raise ValidationError(detail="expires_at is not after effective_at")
        return ExpiryPlan(
            tenant_id=self.tenant_id,
            document_id=self.document_id,
            version_id=version_id,
            expires_at=expires_at,
        )

    def plan_quarantine_review(
        self,
        version_id: uuid.UUID,
        *,
        approved: bool,
        reviewed_by: str,
        at: datetime,
    ) -> QuarantineReviewPlan:
        """Record a reviewer's decision on a quarantined version.

        The version must already be quarantined: approving a clear version is a
        no-op and rejecting one is meaningless, so both are refused rather than
        quietly ignored. Approval clears the quarantine, which is what permits
        the ingestion worker to re-run and embed the reviewed bytes; rejection
        keeps the version out of retrieval, the safe default.

        Raises:
            NotFoundError: if the document is deleted or the version is unknown.
            InvalidVersionTransitionError: if the version is not quarantined.
            ValidationError: ``reviewed_by`` is blank, or ``at`` is naive.
        """
        version = self._live_version(version_id)
        if version.safety_state is not SafetyState.QUARANTINED:
            raise InvalidVersionTransitionError(
                current=version.safety_state,
                permitted=(SafetyState.QUARANTINED,),
                detail=f"version {version_id} is not quarantined",
            )
        return QuarantineReviewPlan(
            tenant_id=self.tenant_id,
            document_id=self.document_id,
            version_id=version_id,
            approved=approved,
            reviewed_by=_require_text("reviewed_by", reviewed_by, _MAX_DISPLAY_NAME),
            reviewed_at=require_aware("at", at),
        )

    def _live_version(self, version_id: uuid.UUID) -> DocumentVersion:
        """Resolve a version on a document that still exists.

        Raises:
            NotFoundError: if the document is deleted or the version is unknown.
            InvalidVersionTransitionError: if the version itself is deleted.
        """
        if self.deleted:
            raise NotFoundError(detail=f"document {self.document_id} is deleted")
        version = self.version(version_id)
        if version.state is VersionState.DELETED:
            raise InvalidVersionTransitionError(
                current=version.state,
                permitted=_PUBLISHABLE_FROM,
                detail=f"version {version_id} is deleted",
            )
        return version
