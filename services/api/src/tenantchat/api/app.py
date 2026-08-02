"""Composition root: builds the app, wires adapters, installs error handling.

Everything with a lifetime longer than a request is constructed here and nowhere
else, so there is one place to look for what a deployment is actually running and
one place for a test to substitute a fake.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

from tenantchat.api.problems import PROBLEM_CONTENT_TYPE, handle_domain_error
from tenantchat.api.registry import TenantRegistry
from tenantchat.api.routers import bookings, health, leads, tenants
from tenantchat.api.settings import Settings
from tenantchat.api.store import InMemoryBookingStore, InMemoryLeadStore
from tenantchat.core.errors import DomainError

REQUEST_ID_HEADER = "X-Request-Id"

logger = logging.getLogger(__name__)


def _problem(
    *,
    status: int,
    code: str,
    title: str,
    detail: str,
    request_id: str,
    **extensions: object,
) -> JSONResponse:
    """A problem document for failures that never became a ``DomainError``."""
    return JSONResponse(
        status_code=status,
        content={
            "type": f"/problems/{code}",
            "title": title,
            "status": status,
            "detail": detail,
            "code": code,
            "requestId": request_id,
        }
        | extensions,
        headers={REQUEST_ID_HEADER: request_id},
        media_type=PROBLEM_CONTENT_TYPE,
    )


async def _handle_request_validation_error(request: Request, exc: Exception) -> JSONResponse:
    """Report *where* a request was malformed, never *what* was in it.

    Pydantic's error list carries the rejected value in ``input``. For a booking
    that value is a phone number or a home address, and this response is one of
    the most-logged objects in the system, so only the field location and the
    rule that failed are published.
    """
    assert isinstance(exc, RequestValidationError)  # noqa: S101 - registered for this type only
    request_id = getattr(request.state, "request_id", "-")
    fields = [
        {"location": ".".join(str(part) for part in error["loc"]), "rule": error["type"]}
        for error in exc.errors()
    ]

    return _problem(
        status=422,
        code="malformed_request",
        title="RequestValidationError",
        detail="The request body did not match the expected shape.",
        request_id=request_id,
        invalidFields=fields,
    )


async def _handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    """Last resort for anything that is not part of the domain contract.

    The response says nothing beyond the request ID: an unexpected exception's
    message is written for a developer and routinely contains connection strings,
    query fragments, or row data.
    """
    request_id = getattr(request.state, "request_id", "-")
    logger.exception("unhandled error", extra={"request_id": request_id, "path": request.url.path})
    return _problem(
        status=500,
        code="internal_error",
        title="InternalServerError",
        detail="The request could not be completed.",
        request_id=request_id,
    )


def _install_middleware(app: FastAPI, settings: Settings) -> None:
    @app.middleware("http")
    async def enforce_body_limit(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Reject oversized bodies before they are read.

        Content-Length only. A chunked upload declares no length and slips past
        this, which is why the ingress in `DEP-003` also sets a body limit — this
        check exists so the bound holds when the app is reached directly.
        """
        declared = request.headers.get("content-length")
        if (
            declared is not None
            and declared.isdigit()
            and int(declared) > settings.max_request_bytes
        ):
            return _problem(
                status=413,
                code="request_too_large",
                title="RequestTooLarge",
                detail="The request body was larger than this endpoint accepts.",
                request_id=getattr(request.state, "request_id", "-"),
                maxBytes=settings.max_request_bytes,
            )
        return await call_next(request)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.allowed_origins),
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type"],
        # No cookies or Authorization header travel cross-origin from the widget;
        # visitor identity is a body-carried session token (`SEC-002`). Leaving
        # credentials off means a permissive origin list cannot become a
        # session-riding bug.
        allow_credentials=False,
    )

    # Starlette wraps the most recently registered middleware around the earlier
    # layers. Register request correlation last so even an early body-limit or
    # CORS response receives the same usable ID as an endpoint response.
    @app.middleware("http")
    async def assign_request_id(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Give every request an ID and echo it back.

        Generated here rather than accepted from the caller: an ID supplied by an
        unauthenticated client can be repeated across requests or forged to match
        someone else's, which makes it useless for correlation and misleading in
        an audit trail. `OBS-001` extends this to accept a trusted upstream
        header once there is an authenticated edge to trust.
        """
        request.state.request_id = uuid.uuid4().hex
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request.state.request_id
        return response


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application.

    Args:
        settings: Overrides the environment-derived configuration. Tests pass
            explicit settings so a stray variable in the shell cannot change
            what is being asserted.
    """
    resolved = settings or Settings.from_environment()

    app = FastAPI(
        title="Tenant Chat API",
        version="0.1.0",
        docs_url="/docs" if resolved.docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if resolved.docs_enabled else None,
    )

    app.state.settings = resolved
    app.state.registry = TenantRegistry.seeded()
    app.state.booking_store = InMemoryBookingStore()
    app.state.lead_store = InMemoryLeadStore()

    _install_middleware(app, resolved)

    app.add_exception_handler(DomainError, handle_domain_error)
    app.add_exception_handler(RequestValidationError, _handle_request_validation_error)
    app.add_exception_handler(Exception, _handle_unexpected_error)

    app.include_router(health.router)
    app.include_router(tenants.router)
    app.include_router(bookings.router)
    app.include_router(leads.router)

    return app
