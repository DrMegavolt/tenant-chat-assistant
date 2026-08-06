"""The content-safety state a knowledge version may carry (`RAG-007`).

A version is **quarantined** when the ingestion scanner found suspicious
embedded instructions or unsupported active content in it. Quarantine is a
stored state — like :class:`~tenantchat.core.lifecycle.IndexingState` — because
it is decided asynchronously by the ingestion worker, never derivable from the
rows the operator wrote. It withdraws the version from retrieval for every
audience until a reviewer clears it; the review record is the audit trail that
makes "cannot be retrieved until reviewed" operable rather than a comment.

The type carries no content and no detection details: the patterns and hashes
of one detection live in the audit record the worker writes, not on the domain
record.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class SafetyState(StrEnum):
    """Whether a version is available to retrieval.

    :attr:`QUARANTINED` blocks every audience — a hostile document must not
    reach a staff-facing answer either — and survives across re-publishing:
    clearing it is a reviewer decision, not a state transition.
    """

    CLEAR = "clear"
    QUARANTINED = "quarantined"


@dataclass(frozen=True, slots=True)
class QuarantineReviewPlan:
    """The row changes one quarantine review applies.

    ``approved`` distinguishes the two review outcomes. Approval clears the
    quarantine and stamps the reviewer, which is what lets the ingestion job
    re-run without re-flagging the same bytes; a rejection leaves the version
    quarantined (the safe default) and is recorded for the operator.
    """

    tenant_id: str
    document_id: uuid.UUID
    version_id: uuid.UUID
    approved: bool
    reviewed_by: str
    reviewed_at: datetime
