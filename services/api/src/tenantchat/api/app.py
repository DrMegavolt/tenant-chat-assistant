"""Composition root: builds the app, wires adapters, installs error handling.

Everything with a lifetime longer than a request is constructed here and nowhere
else, so there is one place to look for what a deployment is actually running and
one place for a test to substitute a fake.
"""

from __future__ import annotations

import logging
import secrets
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

from tenantchat.api.agent import build_conversation_runtime
from tenantchat.api.faults import TransportError
from tenantchat.api.guards import (
    BodySizeLimitMiddleware,
    RateLimitMiddleware,
    ResponseSizeLimitMiddleware,
    SecurityHeadersMiddleware,
)
from tenantchat.api.limits import (
    InMemoryRateLimitStore,
    RateLimitStore,
    VisitorIdentityExtractor,
    credential_visitor_identity,
)
from tenantchat.api.persistence import (
    Database,
    DatabasePoolSettings,
    PostgresAuditStore,
    PostgresBookingStore,
    PostgresConversationStore,
    PostgresHandoffStore,
    PostgresIdempotencyStore,
    PostgresLeadStore,
    PostgresMembershipStore,
)
from tenantchat.api.persistence.rate_limits import PostgresRateLimitStore
from tenantchat.api.problems import (
    REQUEST_ID_HEADER,
    handle_domain_error,
    transport_problem,
)
from tenantchat.api.registry import TenantRegistry
from tenantchat.api.routers import admin, bookings, chat, health, leads, tenants
from tenantchat.api.settings import Settings, loopback_database
from tenantchat.api.store import (
    AuditStore,
    BookingStore,
    ConversationStore,
    HandoffStore,
    IdempotencyStore,
    LeadStore,
    MembershipStore,
)
from tenantchat.api.visitor import VISITOR_CREDENTIAL_HEADER, utc_now
from tenantchat.core.errors import DomainError
from tenantchat.core.ports import ConversationRuntime
from tenantchat.core.visitor_session import (
    HmacVisitorCredentialSigner,
    VisitorCredentialSigner,
)
from tenantchat.orchestration.checkpoints import Checkpointer, postgres_checkpointer
from tenantchat.orchestration.model import ChatModel
from tenantchat.orchestration.providers.openai_compatible import OpenAICompatibleChatModel

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

    return transport_problem(
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
    return transport_problem(
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
    query fragments, or row data. ``CHAT_API_DEBUG`` is the explicit opt-out —
    a development deployment publishes the message so a breakpoint-free debug
    session works against a real stack.
    """
    request_id = getattr(request.state, "request_id", "-")
    logger.exception("unhandled error", extra={"request_id": request_id, "path": request.url.path})
    # A handler can be invoked with a bare Request in tests; the app's settings
    # are the opt-in for publishing exception text, so their absence must not
    # itself blow up the handler.
    app = request.scope.get("app", None)
    settings: Settings | None = getattr(app.state, "settings", None) if app is not None else None
    detail = (
        f"{type(exc).__name__}: {exc}"
        if settings is not None and settings.debug
        else ("The request could not be completed.")
    )
    return transport_problem(
        status=500,
        code="internal_error",
        title="InternalServerError",
        detail=detail,
        request_id=request_id,
    )


def _install_middleware(
    app: FastAPI,
    settings: Settings,
    rate_limit_store: RateLimitStore,
    visitor_identity: VisitorIdentityExtractor,
) -> None:
    # Starlette wraps the most recently registered middleware around the earlier
    # layers, so the registration order below is inner-to-outer:
    # response-size -> rate/concurrency -> body-size -> CORS -> confine -> id.
    # The three guards sit inside CORS so their 413/429 documents carry the
    # same cross-origin headers as a success; the body guard must sit outside
    # the rate guard because it produces the bytes the rate guard keys on.
    app.add_middleware(ResponseSizeLimitMiddleware, max_bytes=settings.max_response_bytes)
    app.add_middleware(
        RateLimitMiddleware,
        policy=settings.rate_limits,
        store=rate_limit_store,
        identity=visitor_identity,
    )
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_bytes)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.allowed_origins),
        allow_methods=["GET", "POST", "OPTIONS"],
        # The widget's visitor identity is a custom bearer header, so it is
        # CORS-allowed like Content-Type is. Cookies and Authorization never
        # travel cross-origin from the widget; leaving credentials off means a
        # permissive origin list cannot become a session-riding bug.
        allow_headers=["Content-Type", VISITOR_CREDENTIAL_HEADER],
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

    # Registered last so it wraps everything registered so far: the security
    # header posture applies to guard refusals and handler errors as well as
    # successful responses.
    app.add_middleware(SecurityHeadersMiddleware)

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
    membership_store: MembershipStore | None = None,
    audit_store: AuditStore | None = None,
    chat_model: ChatModel | None = None,
    checkpointer: Checkpointer | None = None,
    rate_limit_store: RateLimitStore | None = None,
    visitor_identity: VisitorIdentityExtractor | None = None,
) -> FastAPI:
    """Build the application.

    The seven stores are injected together or not at all. A mixture would run
    half the request against a test double and half against PostgreSQL, which is
    the configuration most likely to make a passing test mean nothing.

    Args:
        settings: Overrides environment-derived configuration.
        booking_store: Explicit test adapter. Production builds PostgreSQL stores.
        lead_store: Explicit test adapter. Production builds PostgreSQL stores.
        conversation_store: Explicit test adapter. Production builds PostgreSQL stores.
        handoff_store: Explicit test adapter. Production builds PostgreSQL stores.
        idempotency_store: Explicit test adapter. Production builds PostgreSQL stores.
        membership_store: Explicit test adapter. Production builds PostgreSQL stores.
        audit_store: Explicit test adapter. Production builds PostgreSQL stores.
        chat_model: The model the agent runtime calls. Injected for tests; when
            omitted, `AI-001` builds an OpenAI-compatible adapter from
            ``LLM_BASE_URL``/``LLM_MODEL``/``LLM_API_KEY`` if both the base URL
            and model are configured. Without either, no runtime is composed and
            the chat routes report themselves unavailable.
        checkpointer: Execution-state store for the agent runtime. Production
            opens a PostgreSQL checkpointer over ``DATABASE_URL`` during startup.
        rate_limit_store: Shared rate-limit accounting. Injected for tests;
            production composes a ``PostgresRateLimitStore`` over the same
            engine the stores use, which is what keeps a budget meaningful
            across replicas. ``InMemoryRateLimitStore`` is the test/development
            fallback and is correct for one process only.
        visitor_identity: Maps each request to the ip/tenant/session keys its
            budgets are counted against. `SEC-002` replaces the default with
            an extractor that reads its signed visitor credential; the default
            uses the body's and path's ``session_id``.

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
        membership_store,
        audit_store,
    )
    if any(store is None for store in supplied):
        if any(store is not None for store in supplied):
            raise ValueError("inject all stores together or let production composition build all")
        if resolved.database_url is None:
            raise ValueError(
                "DATABASE_URL is required unless explicit in-memory test stores are injected"
            )
        # Development auth is a loopback-only convenience (SEC-001). A remote
        # database is the shape every production deployment has, so refusing
        # here is what makes "no production deployment can start with
        # development auth enabled" structural rather than a convention.
        if resolved.dev_auth and not loopback_database(resolved.database_url):
            raise ValueError(
                "CHAT_API_DEV_AUTH is enabled but DATABASE_URL is not a loopback address; "
                "development auth must never run against a remote database"
            )
        # Fail at startup rather than answering every admin request with a 401
        # that looks like a rejected operator instead of a missing secret.
        # Development auth needs none of these: it mints its own CSRF secret,
        # trusts the identity headers directly, and signs visitor credentials
        # with the ephemeral key below.
        if not resolved.dev_auth:
            missing = [
                name
                for name, value in (
                    ("ADMIN_GATEWAY_TOKEN", resolved.admin_gateway_token),
                    ("ADMIN_CSRF_SECRET", resolved.admin_csrf_secret),
                    (
                        "CHAT_API_VISITOR_CREDENTIAL_SIGNING_KEY",
                        resolved.visitor_credential_signing_key,
                    ),
                )
                if not value
            ]
            if missing:
                raise ValueError(f"the visitor and admin routes require {', '.join(missing)}")
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
        membership_store = PostgresMembershipStore(database.engine)
        audit_store = PostgresAuditStore(database.engine)

    if (
        booking_store is None
        or lead_store is None
        or conversation_store is None
        or handoff_store is None
        or idempotency_store is None
        or membership_store is None
        or audit_store is None
    ):
        raise RuntimeError("composition failed to provide persistence stores")

    # SEC-002: the visitor credential signer. Production composition required
    # the key above, so every real deployment signs with a shared secret it
    # holds. A test composition without a key gets an ephemeral one: sessions
    # minted by one test app are meaningless to the next, which is exactly the
    # isolation a hermetic suite wants. The signer is public state so tests and
    # `SEC-003` rate limiting can verify or mint through the same instance.
    visitor_credentials: VisitorCredentialSigner
    if resolved.visitor_credential_signing_key is not None:
        visitor_credentials = HmacVisitorCredentialSigner(resolved.visitor_credential_signing_key)
    else:
        visitor_credentials = HmacVisitorCredentialSigner(secrets.token_urlsafe(48))

    # A shared store follows the database the stores already use; the
    # in-memory fallback is the single-process development shape. Whichever is
    # composed, the middleware talks to it through the same port, so the
    # replication decision is made in exactly one place.
    if rate_limit_store is None:
        rate_limit_store = (
            PostgresRateLimitStore(database.engine)
            if database is not None
            else InMemoryRateLimitStore()
        )
    # The rate-limit keys come from the verified credential (SEC-002), so the
    # tenant and session budgets bind to an identity a caller cannot forge or
    # rotate by editing a request body.
    effective_identity = visitor_identity or credential_visitor_identity(visitor_credentials)

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
    app.state.membership_store = membership_store
    app.state.audit_store = audit_store
    app.state.visitor_credential_signer = visitor_credentials
    # The one clock every visitor credential is verified against, so a test can
    # move time by reassigning state rather than sleeping.
    app.state.clock = utc_now
    app.state.conversation_runtime = (
        compose_runtime(checkpointer, effective_model)
        if effective_model is not None and checkpointer is not None
        else None
    )
    # The effective model is public state so a test can assert which adapter
    # composition chose without reaching into construction internals (`AI-001`).
    app.state.chat_model = effective_model

    _install_middleware(app, resolved, rate_limit_store, effective_identity)

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
