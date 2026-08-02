"""States a versioned knowledge record moves through.

Kept in its own module for the same reason as :mod:`tenantchat.core.fields`: the
error taxonomy reports a rejected transition as typed fields, and the knowledge
model raises those errors, so a shared home is what keeps the two from importing
each other.
"""

from __future__ import annotations

from enum import StrEnum


class VersionState(StrEnum):
    """Approval state of one document version.

    At most one version of a document is :attr:`PUBLISHED` at a time. That single
    rule is what makes publishing atomically supersede its predecessor and makes a
    rollback restore a prior version without the two ever answering the same
    question differently.

    :attr:`DELETED` is a tombstone, not a row removal: retrieval must be able to
    tell that a version it indexed earlier is now withdrawn.
    """

    DRAFT = "draft"
    APPROVED = "approved"
    PUBLISHED = "published"
    SUPERSEDED = "superseded"
    DELETED = "deleted"


class IndexingState(StrEnum):
    """Whether the retrieval index reflects this version.

    Separate from :class:`VersionState` because the index is a second datastore
    that fails independently of the publish transaction (ADR-0003). A version is
    publishable before it is indexed, and retrievable only once it is: without
    this distinction, an approved document appears answerable in the moment
    between the publish commit and the indexing job.
    """

    PENDING = "pending"
    INDEXING = "indexing"
    INDEXED = "indexed"
    FAILED = "failed"
