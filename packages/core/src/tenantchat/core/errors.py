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

from typing import ClassVar

from tenantchat.core.fields import RequiredField


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
