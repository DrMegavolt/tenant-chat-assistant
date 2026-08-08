"""Domain error taxonomy.

An error here answers one question: *what went wrong, in domain terms.* No HTTP
status, no JSON shape, no serializer — a domain rule must be equally raisable
from a worker, a test, or a graph node, none of which have a response to shape.
The API layer maps this taxonomy onto RFC 9457 Problem Details.

Each error carries:

- ``code``    — a stable machine-readable string. Clients branch on it, so it is
                part of the public contract and must not change casually.
- ``message`` — prose safe to show an unauthenticated visitor.
- ``detail``  — operator context, for **structured logging only**.

``detail`` is absent from ``str()`` and ``repr()`` by design. Python builds
``str(exception)`` from the exception's args, so anything placed there surfaces in
every f-string, traceback, and careless log line — which is exactly what ``detail``
exists to avoid. Operators pass it explicitly, as
``extra={"code": err.code, "detail": err.detail}``, so it travels *through*
OBS-001's redaction filter rather than around it.

Upstream causes are preserved by chaining (``raise ConflictError(...) from exc``),
never by interpolating them into a message: the traceback keeps the original, and
the domain error stays publishable.

**Not every unhappy branch is an error.** A booking that lacks a phone number is
an ordinary conversational state — the assistant asks for one. Expected workflow
states belong in typed outcome values, not exceptions. These types are for
operations that were *rejected or failed*: policy refusal, conflict, missing
aggregate, broken invariant, dependency failure.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from tenantchat.core.fields import RequiredField
from tenantchat.core.lifecycle import VersionState
from tenantchat.core.safety import SafetyState

if TYPE_CHECKING:
    from tenantchat.core.workflows import WorkflowStatus, WorkflowTransition


class DomainError(Exception):
    """Base class for every expected domain failure.

    Unexpected failures stay ordinary exceptions and become a generic 500 at the
    API edge. Subclassing this is a statement that the failure is part of the
    domain's contract and safe to describe to a caller.
    """

    code: ClassVar[str] = "domain_error"
    message: ClassVar[str] = "The request could not be completed."

    def __init__(self, detail: str | None = None) -> None:
        self.detail = detail
        # Only the safe message reaches the exception args, so `str(error)` is
        # publishable. `detail` is available on the instance for structured logs.
        super().__init__(self.message)

    def __repr__(self) -> str:
        # Signals that detail is present without disclosing it, so a debugger or
        # pytest failure shows there is more to look at in the structured log.
        redacted = "<redacted>" if self.detail is not None else None
        return f"{type(self).__name__}(code={self.code!r}, detail={redacted!r})"


class ValidationError(DomainError):
    """Caller-supplied data is malformed or contradictory."""

    code = "validation_error"
    message = "Some of the supplied details were not valid."


class MissingRequiredFieldsError(ValidationError):
    """A command arrived without fields it cannot execute without.

    This is for *commands*, not conversation. A direct API call omitting a
    required field is a malformed request; an assistant that has not yet asked
    for a phone number is mid-workflow, and reports that through an outcome value
    instead.
    """

    code = "missing_required_fields"
    message = "Some required details are still missing."

    def __init__(self, fields: tuple[RequiredField, ...], detail: str | None = None) -> None:
        if not fields:
            raise ValueError("MissingRequiredFieldsError requires at least one field")
        self.fields = fields
        super().__init__(detail)


class InvalidContactError(ValidationError):
    """A phone number or email address could not be parsed."""

    code = "invalid_contact"
    message = (
        "Provide a valid email address or a complete 10-digit US phone number "
        "including the area code."
    )


class UnknownServiceError(ValidationError):
    """The requested service did not resolve against the tenant catalog.

    Carries the tenant's offered service names so the caller can ask a precise
    clarifying question. These come from :meth:`ServiceCatalog.offered_names`,
    which is server-owned configuration — never visitor input.
    """

    code = "unknown_service"
    message = "That service was not recognized."

    def __init__(self, offered: tuple[str, ...] = (), detail: str | None = None) -> None:
        self.offered = offered
        super().__init__(detail)


class PolicyViolationError(DomainError):
    """Well-formed, but this tenant's policy forbids it.

    Distinct from an authorization failure: the caller is who they claim to be and
    the request makes sense, but the tenant has not enabled the capability.
    """

    code = "policy_violation"
    message = "This assistant is not able to help with that request."


class BookingNotPermittedError(PolicyViolationError):
    code = "booking_not_permitted"
    message = "This company does not take bookings through chat."


class PricingNotPermittedError(PolicyViolationError):
    code = "pricing_not_permitted"
    message = "This company does not quote pricing through chat."


class LeadCaptureNotPermittedError(PolicyViolationError):
    code = "lead_capture_not_permitted"
    message = "This company is not accepting follow-up requests through chat."


class NotFoundError(DomainError):
    """A referenced record does not exist, or the caller may not see it.

    Deliberately conflates the two. SEC-001 requires that an unauthorized
    cross-tenant read not confirm whether a resource exists.
    """

    code = "not_found"
    message = "That record was not found."


class TraceReplayError(DomainError):
    """A turn record cannot be replayed: it names no reconstructible prompt.

    The record exists and is readable; only the replay step is impossible,
    because the stored prompt section is absent or malformed. ``detail`` names
    the field that failed so the operator log can be specific without the
    published message disclosing any content.
    """

    code = "trace_replay_error"
    message = "This turn record cannot be replayed."


class GenerationUnavailableError(DomainError):
    """The index generation this turn was retrieved against is no longer available.

    A replay that quietly changes its evidence is worse than no replay — when
    the generation is gone, the service must refuse the reproducibility claim
    rather than silently replaying against current data. ``detail`` carries the
    generation id so the operator log is specific.
    """

    code = "generation_unavailable"
    message = "The index generation this turn was retrieved against is no longer available."


class InvalidVisitorCredentialError(DomainError):
    """A presented visitor credential is not one this server issued.

    The ``detail`` never says *why* the token was rejected — the reason is a
    forgery probe's free intelligence — and the message is stable so clients can
    branch on the ``code`` and clear their stored credential.
    """

    code = "invalid_visitor_credential"
    message = "Your session is no longer recognized. Please refresh to start again."


class ExpiredVisitorCredentialError(DomainError):
    """A visitor credential was genuine but past its expiry.

    The conversation row is untouched; the visitor just needs a fresh
    credential for it.
    """

    code = "visitor_credential_expired"
    message = "Your session has expired. Please refresh to continue."


class ConflictError(DomainError):
    """The action collided with concurrent state, such as a slot already taken."""

    code = "conflict"
    message = "That action conflicted with a more recent change."


class SlotUnavailableError(ConflictError):
    """The requested appointment slot is not one the tenant is currently offering.

    Carries the current offers so the caller can re-prompt in the same turn
    instead of sending the customer back to check availability.
    """

    code = "slot_unavailable"
    message = "That appointment time is no longer available."

    def __init__(self, offered: tuple[str, ...] = (), detail: str | None = None) -> None:
        self.offered = offered
        super().__init__(detail)


class InvalidVersionTransitionError(ConflictError):
    """A knowledge version is not in a state this change is allowed from.

    A conflict rather than a validation failure: the request was well-formed and
    would have succeeded a moment earlier. Two reviewers publishing different
    versions of the same document arrive here, and the loser needs to reload
    rather than correct anything.

    Carries the states so an operator console can re-render the document's real
    situation instead of asking the operator to guess what changed.
    """

    code = "invalid_version_transition"
    message = "That document version has changed. Reload it and try again."

    def __init__(
        self,
        *,
        current: VersionState | SafetyState,
        permitted: tuple[VersionState | SafetyState, ...],
        detail: str | None = None,
    ) -> None:
        if not permitted:
            raise ValueError("InvalidVersionTransitionError requires at least one permitted state")
        self.current = current
        self.permitted = permitted
        super().__init__(detail)


class ReviewTransitionError(ConflictError):
    """A review queue entry is not in a state the requested transition may leave.

    A conflict rather than a validation failure: the queue moved under the
    caller — another reviewer took it, or an evaluation run already closed it.
    Carries the current status and the transitions the entry would have
    accepted so the caller can reload the queue instead of guessing.
    """

    code = "invalid_review_transition"
    message = "That review has moved on. Reload it and try again."

    def __init__(
        self,
        *,
        current: str = "",
        permitted: frozenset[str] = frozenset(),
        detail: str | None = None,
    ) -> None:
        self.current = current
        self.permitted = frozenset(permitted)
        super().__init__(detail)


class PromotionPrivacyError(PolicyViolationError):
    """A reviewed turn cannot become an evaluation case: the anonymized payload
    would still carry contact data.

    The refusal is the privacy check (acceptance 6): promotion never silently
    redacts a case the reviewer approved, it sends the reviewer back to make
    the query releasable.
    """

    code = "promotion_privacy_refused"
    message = "That turn cannot be promoted until its query is anonymized."


class HandoffTransitionError(ConflictError):
    """A handoff is not in a state the requested transition may leave.

    A conflict rather than a validation failure: the queue moved under the
    caller — another staff member accepted first, or the handoff closed. The
    race-to-accept rule is enforced here: a refused accept means a different
    owner already holds the conversation, and the loser reloads the queue
    rather than overwriting the winner.
    """

    code = "invalid_handoff_transition"
    message = "That handoff has moved on. Reload the queue and try again."

    def __init__(
        self,
        *,
        current: str = "",
        permitted: frozenset[str] = frozenset(),
        detail: str | None = None,
    ) -> None:
        self.current = current
        self.permitted = frozenset(permitted)
        super().__init__(detail)


class HandoffOwnershipError(HandoffTransitionError):
    """A staff transition was refused because the caller is not the owner.

    Distinct from :class:`HandoffTransitionError`: the current status *admits*
    the transition, so "reload the queue" is wrong advice — the queue did not
    move, the caller simply holds no ownership of this conversation. A
    non-owner, non-supervisor resolving an assigned handoff lands here, and the
    client should tell them they cannot take that action rather than implying
    the handoff changed under them.
    """

    code = "handoff_ownership_refused"
    message = "Only the staff member who owns this conversation can take that action."

    def __init__(
        self,
        *,
        current: str = "",
        permitted: frozenset[str] = frozenset(),
        detail: str | None = None,
    ) -> None:
        self.current = current
        self.permitted = frozenset(permitted)
        super().__init__(current=current, permitted=permitted, detail=detail)


class WorkflowTransitionError(ConflictError):
    """A workflow is not in a state the requested transition may leave.

    A conflict rather than a validation failure: the workflow was in the right
    state a moment ago and moved under the caller. Carries the current status
    and the transitions it would have accepted, so the caller can reload the
    workflow instead of guessing what changed.
    """

    code = "invalid_workflow_transition"
    message = "That workflow has moved on. Reload it and try again."

    def __init__(
        self,
        *,
        current: WorkflowStatus,
        transition: WorkflowTransition,
        permitted: frozenset[WorkflowTransition],
        detail: str | None = None,
    ) -> None:
        # `permitted` is legitimately empty: a terminal workflow accepts nothing.
        self.current = current
        self.transition = transition
        self.permitted = frozenset(permitted)
        super().__init__(detail)
