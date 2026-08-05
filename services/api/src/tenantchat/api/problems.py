"""Domain errors rendered as RFC 9457 Problem Details.

This is the only place that knows a domain failure has an HTTP status. The
taxonomy in ``tenantchat.core.errors`` carries no transport concern, and adding
one there would make the same rule unusable from a worker or a graph node.

**The naming collision here is load-bearing, so read it once carefully.** RFC 9457
calls its human-readable explanation ``detail``, and ``DomainError`` also has a
``detail``. They are opposites:

- The problem document's ``detail`` is published to an unauthenticated caller and
  is filled from ``DomainError.message``, which is written to be safe to show.
- ``DomainError.detail`` is operator context — it may name tenants, IDs, or the
  value that failed to parse. It goes to the log and never into the response.

Anything a client needs in order to *act* is a typed extension member
(``code``, ``missingFields``, ``offeredServices``, ``offeredSlots``) rather than
prose it would have to parse.
"""

from __future__ import annotations

import logging
from typing import Any, Final

from fastapi import Request
from fastapi.responses import JSONResponse

from tenantchat.core.errors import (
    ConflictError,
    DomainError,
    ExpiredVisitorCredentialError,
    InvalidVersionTransitionError,
    InvalidVisitorCredentialError,
    MissingRequiredFieldsError,
    NotFoundError,
    PolicyViolationError,
    SlotUnavailableError,
    UnknownServiceError,
    ValidationError,
)

PROBLEM_CONTENT_TYPE: Final = "application/problem+json"

logger = logging.getLogger(__name__)

# Most specific first: the first match wins, so a subclass must precede its base.
_STATUS_BY_TYPE: Final[tuple[tuple[type[DomainError], int], ...]] = (
    # 401 rather than 400: presenting no credential, an unverifiable one, or an
    # expired one is an authentication failure. The codes stay distinct
    # (`invalid_visitor_credential` vs `visitor_credential_expired`) so a client
    # can tell a terminal rejection from a recoverable one (SEC-002).
    (InvalidVisitorCredentialError, 401),
    (ExpiredVisitorCredentialError, 401),
    # 422 rather than 400: the body parsed fine, its contents were unacceptable.
    (ValidationError, 422),
    # 403 rather than 404: the tenant exists and the request is well-formed; the
    # capability is switched off. Hiding that behind a 404 would send an operator
    # hunting for a missing route instead of a policy flag.
    (PolicyViolationError, 403),
    (NotFoundError, 404),
    (ConflictError, 409),
)

_DEFAULT_STATUS: Final = 400


def status_for(error: DomainError) -> int:
    for error_type, status in _STATUS_BY_TYPE:
        if isinstance(error, error_type):
            return status
    return _DEFAULT_STATUS


def _extensions(error: DomainError) -> dict[str, Any]:
    """Typed members that let a client recover without parsing prose.

    Every value here is server-owned configuration — field *names*, service
    names, offered time slots — never customer input echoed back.
    """
    if isinstance(error, MissingRequiredFieldsError):
        return {"missingFields": [field.value for field in error.fields]}
    if isinstance(error, UnknownServiceError):
        return {"offeredServices": list(error.offered)}
    if isinstance(error, SlotUnavailableError):
        return {"offeredSlots": list(error.offered)}
    if isinstance(error, InvalidVersionTransitionError):
        return {
            "currentState": error.current.value,
            "permittedStates": [state.value for state in error.permitted],
        }
    return {}


def problem_response(
    error: DomainError, *, request_id: str, status: int | None = None
) -> JSONResponse:
    """Render a domain error as a problem document.

    ``requestId`` is included so a visitor reporting "it said something went
    wrong" can be matched to the log line holding the operator detail.
    """
    resolved_status = status_for(error) if status is None else status
    document: dict[str, Any] = {
        # A relative URN keeps the contract from depending on a hosted docs site
        # that may not exist yet; the code is the stable identifier either way.
        "type": f"/problems/{error.code}",
        "title": type(error).__name__,
        "status": resolved_status,
        "detail": error.message,
        "code": error.code,
        "requestId": request_id,
    } | _extensions(error)

    return JSONResponse(
        status_code=resolved_status,
        content=document,
        media_type=PROBLEM_CONTENT_TYPE,
    )


async def handle_domain_error(request: Request, exc: Exception) -> JSONResponse:
    """Turn any escaped ``DomainError`` into a problem document.

    Registered as a catch-all so a rule raised deep in a domain call still
    produces a typed response, rather than a 500 that tells the caller nothing
    and the operator no more.
    """
    assert isinstance(exc, DomainError)  # noqa: S101 - handler is registered for this type only
    request_id = getattr(request.state, "request_id", "-")

    logger.warning(
        "domain error",
        extra={
            "code": exc.code,
            "request_id": request_id,
            "path": request.url.path,
            # The operator-only half. It reaches a log record, never a response,
            # and OBS-001's redaction filter sits between here and the sink.
            "detail": exc.detail,
        },
    )
    return problem_response(exc, request_id=request_id)
