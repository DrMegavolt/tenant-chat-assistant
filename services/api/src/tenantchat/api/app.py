"""Composition root: builds the app, wires adapters, installs error handling.

Everything with a lifetime longer than a request is constructed here and nowhere
else, so there is one place to look for what a deployment is actually running and
one place for a test to substitute a fake.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

from tenantchat.api.actions import RecordedBookingService
from tenantchat.api.agent import build_conversation_runtime
from tenantchat.api.faults import TransportError
from tenantchat.api.persistence import (
    Database,
    DatabasePoolSettings,
    PostgresBookingStore,
    PostgresConversationStore,
    PostgresHandoffStore,
    PostgresIdempotencyStore,
    PostgresLeadStore,
)
from tenantchat.api.persistence.availability import (
    PostgresAvailabilityProvider,
    seed_demo_availability,
)
from tenantchat.api.problems import PROBLEM_CONTENT_TYPE, handle_domain_error
from tenantchat.api.registry import DemoAvailabilityProvider, TenantRegistry
from tenantchat.api.routers import admin, bookings, chat, health, leads, tenants
from tenantchat.api.settings import Settings
from tenantchat.api.store import (
    BookingStore,
    ConversationStore,
    HandoffStore,
    IdempotencyStore,
    InMemoryBookingStore,
    LeadStore,
)
from tenantchat.core.errors import DomainError
from tenantchat.core.ports import AvailabilityProvider, ConversationRuntime
from tenantchat.orchestration.checkpoints import Checkpointer, postgres_checkpointer
from tenantchat.orchestration.model import ChatModel
from tenantchat.orchestration.providers.openai_compatible import OpenAICompatibleChatModel

REQUEST_ID_HEADER = "X-Request-Id"
ADMIN_PATH_PREFIX = "/api/admin/"

_CORS_RESPONSE_HEADERS = (
    "access-control-allow-origin",
    "access-control-allow-credentials",
    "access-control-allow-methods",
    "access-control-allow-headers",
    "access-control-expose-headers",
    "access-control-max-age",
)

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


async def _handle_api_fault(request: Request, exc: Exception) -> JSONResponse:
    """Render a transport failure as the same problem document a domain error gets.

    The log line carries the path and the request ID and nothing about the
    credential that failed, which is the only detail that would have made it
    useful to whoever sent it.
    """
    assert isinstance(exc, TransportError)  # noqa: S101 - registered for this type only
    request_id = getattr(request.state, "request_id", "-")
    logger.warning(
        "request refused",
        extra={"code": exc.code, "request_id": request_id, "path": request.url.path},
    )
    return _problem(
        status=exc.status,
        code=exc.code,
        title=type(exc).__name__,
        detail=exc.message,
        request_id=request_id,
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

    # Registered after the CORS layer so it runs outside it, on the way out,
    # after those headers have been added.
    @app.middleware("http")
    async def confine_cors_to_the_visitor_surface(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Keep the operator console off the cross-origin surface.

        The origin allowlist exists for the widget, which is embedded on tenant
        sites and cross-origin by design. An admin route reached from one of
        those origins would arrive through the gateway carrying gateway-supplied
        identity, so without this the allowlist would decide who may read
        another tenant's transcripts.
        """
        response = await call_next(request)
        if request.url.path.startswith(ADMIN_PATH_PREFIX):
            for header in _CORS_RESPONSE_HEADERS:
                if header in response.headers:
                    del response.headers[header]
        return response

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


def create_app(
    settings: Settings | None = None,
    *,
    booking_store: BookingStore | None = None,
    lead_store: LeadStore | None = None,
    conversation_store: ConversationStore | None = None,
    handoff_store: HandoffStore | None = None,
    idempotency_store: IdempotencyStore | None = None,
    chat_model: ChatModel | None = None,
    checkpointer: Checkpointer | None = None,
    availability_provider: AvailabilityProvider | None = None,
) -> FastAPI:
    """Build the application.

    The five stores are injected together or not at all. A mixture would run
    half the request against a test double and half against PostgreSQL, which is
    the configuration most likely to make a passing test mean nothing.

    Args:
        settings: Overrides environment-derived configuration.
        booking_store: Explicit test adapter. Production builds PostgreSQL stores.
        lead_store: Explicit test adapter. Production builds PostgreSQL stores.
        conversation_store: Explicit test adapter. Production builds PostgreSQL stores.
        handoff_store: Explicit test adapter. Production builds PostgreSQL stores.
        idempotency_store: Explicit test adapter. Production builds PostgreSQL stores.
        chat_model: The model the agent runtime calls. Injected for tests; when
            omitted, `AI-001` builds an OpenAI-compatible adapter from
            ``LLM_BASE_URL``/``LLM_MODEL``/``LLM_API_KEY`` if both the base URL
            and model are configured. Without either, no runtime is composed and
            the chat routes report themselves unavailable.
        checkpointer: Execution-state store for the agent runtime. Production
            opens a PostgreSQL checkpointer over ``DATABASE_URL`` during startup.
        availability_provider: What the tenant is currently offering. Explicit
            test adapter; production builds the PostgreSQL-backed fake provider.

    Raises:
        ValueError: the stores were injected in part, or production composition
            is missing a setting it cannot run without.
    """
    resolved = settings or Settings.from_environment()
    registry = TenantRegistry.seeded()
    database: Database | None = None
    # AI-001: a provider adapter built from settings for the production
    # composition. Tests inject `chat_model` explicitly and leave this None; a
    # production deployment without a model configured also leaves it None so
    # chat fails closed rather than starting against a guessed endpoint.
    owned_model: OpenAICompatibleChatModel | None = None
    if chat_model is None and resolved.llm_base_url and resolved.llm_model:
        owned_model = OpenAICompatibleChatModel(
            base_url=resolved.llm_base_url,
            model=resolved.llm_model,
            api_key=resolved.llm_api_key,
            timeout_seconds=resolved.llm_timeout_seconds,
        )
    effective_model = chat_model or owned_model

    supplied = (
        booking_store,
        lead_store,
        conversation_store,
        handoff_store,
        idempotency_store,
    )
    if any(store is None for store in supplied):
        if any(store is not None for store in supplied):
            raise ValueError("inject all stores together or let production composition build all")
        if resolved.database_url is None:
            raise ValueError(
                "DATABASE_URL is required unless explicit in-memory test stores are injected"
            )
        # Fail at startup rather than answering every admin request with a 401
        # that looks like a rejected operator instead of a missing secret.
        missing = [
            name
            for name, value in (
                ("ADMIN_GATEWAY_TOKEN", resolved.admin_gateway_token),
                ("ADMIN_CSRF_SECRET", resolved.admin_csrf_secret),
            )
            if not value
        ]
        if missing:
            raise ValueError(f"the admin routes require {', '.join(missing)}")
        database = Database.connect(
            resolved.database_url,
            DatabasePoolSettings(
                size=resolved.database_pool_size,
                max_overflow=resolved.database_max_overflow,
                timeout_seconds=resolved.database_pool_timeout_seconds,
                recycle_seconds=resolved.database_pool_recycle_seconds,
            ),
        )
        booking_store = PostgresBookingStore(database.engine)
        lead_store = PostgresLeadStore(database.engine)
        conversation_store = PostgresConversationStore(database.engine)
        handoff_store = PostgresHandoffStore(database.engine)
        idempotency_store = PostgresIdempotencyStore(database.engine)

    if (
        booking_store is None
        or lead_store is None
        or conversation_store is None
        or handoff_store is None
        or idempotency_store is None
    ):
        raise RuntimeError("composition failed to provide persistence stores")

    if availability_provider is None:
        # In production the offers come from the same database the bookings land
        # in; with injected test stores there is no database, so fall back to the
        # deterministic in-process provider.
        if database is not None:
            availability_provider = PostgresAvailabilityProvider(database.engine)
        else:
            availability_provider = DemoAvailabilityProvider(
                registry,
                # The in-memory provider has no SQL, so the booking store tells
                # it which slots are no longer bookable — the same exclusion the
                # Postgres provider expresses with ``NOT EXISTS``.
                taken=(
                    booking_store.taken_slot_ids
                    if isinstance(booking_store, InMemoryBookingStore)
                    else None
                ),
            )

    booking_service = RecordedBookingService(booking_store, availability_provider)

    bookings_for_agent, leads_for_agent = booking_store, lead_store
    handoffs_for_agent, keys_for_agent = handoff_store, idempotency_store

    def compose_runtime(saver: Checkpointer, model: ChatModel) -> ConversationRuntime:
        return build_conversation_runtime(
            registry=registry,
            model=model,
            bookings=bookings_for_agent,
            leads=leads_for_agent,
            handoffs=handoffs_for_agent,
            idempotency=keys_for_agent,
            checkpointer=saver,
            availability=availability_provider,
        )

    @asynccontextmanager
    async def lifespan(running: FastAPI) -> AsyncIterator[None]:
        async with AsyncExitStack() as closing:
            if database is not None:
                closing.push_async_callback(database.dispose)
                await database.synchronize_tenants(
                    (record.policy.tenant_id, record.policy.name)
                    for record in registry.all().values()
                )
                # Seed the database-backed fake provider now that tenant rows
                # exist; idempotent, so a restart does not duplicate slots.
                await seed_demo_availability(database.engine, registry)
            # Owned provider clients are closed with the app. An injected test
            # model is the caller's to clean up, not the app's.
            if owned_model is not None:
                closing.push_async_callback(owned_model.close)
            # Opened here rather than in the builder because the checkpointer
            # owns a connection pool, and a pool created at import time outlives
            # nothing and belongs to no event loop.
            if effective_model is not None and running.state.conversation_runtime is None:
                if resolved.database_url is None:
                    raise ValueError("the agent runtime requires DATABASE_URL or a checkpointer")
                saver = await closing.enter_async_context(
                    postgres_checkpointer(resolved.database_url)
                )
                running.state.conversation_runtime = compose_runtime(saver, effective_model)
            yield

    app = FastAPI(
        title="Tenant Chat API",
        version="0.1.0",
        docs_url="/docs" if resolved.docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if resolved.docs_enabled else None,
        lifespan=lifespan,
    )

    app.state.settings = resolved
    app.state.registry = registry
    app.state.booking_store = booking_store
    app.state.lead_store = lead_store
    app.state.conversation_store = conversation_store
    app.state.handoff_store = handoff_store
    app.state.idempotency_store = idempotency_store
    app.state.booking_service = booking_service
    app.state.availability_provider = availability_provider
    app.state.conversation_runtime = (
        compose_runtime(checkpointer, effective_model)
        if effective_model is not None and checkpointer is not None
        else None
    )
    # The effective model is public state so a test can assert which adapter
    # composition chose without reaching into construction internals (`AI-001`).
    app.state.chat_model = effective_model

    _install_middleware(app, resolved)

    app.add_exception_handler(DomainError, handle_domain_error)
    app.add_exception_handler(TransportError, _handle_api_fault)
    app.add_exception_handler(RequestValidationError, _handle_request_validation_error)
    app.add_exception_handler(Exception, _handle_unexpected_error)

    app.include_router(health.router)
    app.include_router(tenants.router)
    app.include_router(bookings.router)
    app.include_router(leads.router)
    app.include_router(chat.router)
    app.include_router(admin.router)

    return app
