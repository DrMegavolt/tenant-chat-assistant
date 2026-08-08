"""Composition root: builds the app, wires adapters, installs error handling.

Everything with a lifetime longer than a request is constructed here and nowhere
else, so there is one place to look for what a deployment is actually running and
one place for a test to substitute a fake.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from tenantchat.api import otel_setup
from tenantchat.api.actions import RecordedBookingService
from tenantchat.api.agent import build_conversation_runtime
from tenantchat.api.correlation import CorrelationMiddleware
from tenantchat.api.evidence import RetrievalEvidenceSource
from tenantchat.api.faults import TransportError
from tenantchat.api.guards import (
    BodySizeLimitMiddleware,
    RateLimitMiddleware,
    ResponseSizeLimitMiddleware,
    SecurityHeadersMiddleware,
)
from tenantchat.api.index_integrity import IndexIntegrityStore, InMemoryIndexIntegrityStore
from tenantchat.api.jobs import InMemoryJobStore, JobStore
from tenantchat.api.limits import (
    InMemoryRateLimitStore,
    RateLimitStore,
    VisitorIdentityExtractor,
    credential_visitor_identity,
)
from tenantchat.api.logging_setup import SERVICE_NAME, configure_logging, resolve_service
from tenantchat.api.metrics import METRICS
from tenantchat.api.persistence import (
    Database,
    DatabasePoolSettings,
    PostgresAuditStore,
    PostgresBookingStore,
    PostgresConsentStore,
    PostgresConversationStore,
    PostgresHandoffStore,
    PostgresIdempotencyStore,
    PostgresJobStore,
    PostgresLeadStore,
    PostgresMembershipStore,
    PostgresPrivacyStore,
    PostgresReviewQueueStore,
    PostgresTraceAccessStore,
    PostgresTurnFeedbackStore,
    PostgresTurnRecordStore,
    PostgresWorkflowStore,
)
from tenantchat.api.persistence.availability import (
    PostgresAvailabilityProvider,
    seed_demo_availability,
)
from tenantchat.api.persistence.index_integrity import PostgresIndexIntegrityStore
from tenantchat.api.persistence.knowledge import PostgresKnowledgeStore
from tenantchat.api.persistence.rate_limits import PostgresRateLimitStore
from tenantchat.api.problems import (
    handle_domain_error,
    transport_problem,
)
from tenantchat.api.redaction import install_pii_log_filter
from tenantchat.api.registry import DemoAvailabilityProvider, TenantRegistry
from tenantchat.api.retrieval import HybridRetrieverConfig
from tenantchat.api.routers import (
    admin,
    bookings,
    chat,
    handoffs,
    health,
    jobs,
    knowledge,
    leads,
    metrics,
    privacy,
    reviews,
    tenants,
    traces,
)
from tenantchat.api.search import (
    ElasticsearchSearchIndex,
    Embedder,
    EmbeddingServiceClient,
    InMemorySearchIndex,
    SearchIndex,
)
from tenantchat.api.settings import Settings, loopback_database, validate_trace_content_export
from tenantchat.api.storage import DiskObjectStore, MemoryObjectStore, ObjectStore
from tenantchat.api.store import (
    AuditStore,
    BookingStore,
    ConsentStore,
    ConversationStore,
    HandoffStore,
    IdempotencyStore,
    InMemoryBookingStore,
    InMemoryKnowledgeStore,
    InMemoryReviewQueueStore,
    InMemoryTraceAccessStore,
    InMemoryTurnFeedbackStore,
    InMemoryTurnRecordStore,
    InMemoryWorkflowStore,
    KnowledgeStore,
    LeadStore,
    MembershipStore,
    PrivacyStore,
    ReviewQueueStore,
    TraceAccessStore,
    TurnFeedbackStore,
    TurnRecordStore,
    WorkflowStore,
)
from tenantchat.api.visitor import VISITOR_CREDENTIAL_HEADER, utc_now
from tenantchat.core.budgets import BudgetEnforcer, BudgetLedger
from tenantchat.core.errors import DomainError
from tenantchat.core.ports import (
    AvailabilityProvider,
    ConversationRuntime,
    EvidenceSource,
)
from tenantchat.core.visitor_session import (
    HmacVisitorCredentialSigner,
    VisitorCredentialSigner,
)
from tenantchat.orchestration.checkpoints import Checkpointer, postgres_checkpointer
from tenantchat.orchestration.model import ChatModel
from tenantchat.orchestration.otel import SpanRecordingChatModel
from tenantchat.orchestration.providers.cache import CachingChatModel
from tenantchat.orchestration.providers.fallback import FallbackChatModel
from tenantchat.orchestration.providers.openai_compatible import OpenAICompatibleChatModel
from tenantchat.orchestration.providers.recording import MetricRecordingChatModel

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


async def _shutdown_otel() -> None:
    otel_setup.shutdown_otel()


def _provider_name(base_url: str) -> str:
    """Derive a ``gen_ai.system`` value from the LLM provider URL.

    ``gen_ai.system`` is a GenAI semantic-convention attribute that names the
    provider, not the model. The model goes into ``gen_ai.request.model``.
    """
    if not base_url:
        return ""
    lowered = base_url.lower()
    if "lm-studio" in lowered or "localhost" in lowered or "127.0.0.1" in lowered:
        return "lm_studio"
    if "openai.com" in lowered:
        return "openai"
    return "openai_compatible"


class AdminCorsConfineMiddleware:
    """Keep the operator console off the cross-origin surface.

    The origin allowlist exists for the widget, which is embedded on tenant
    sites and cross-origin by design. An admin route reached from one of those
    origins would arrive through the gateway carrying gateway-supplied
    identity, so without this the allowlist would decide who may read another
    tenant's transcripts.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        original_send = send

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start" and scope.get("path", "").startswith(
                ADMIN_PATH_PREFIX
            ):
                message = {
                    **message,
                    "headers": [
                        (name, value)
                        for name, value in message.get("headers", [])
                        if name.decode("latin-1").lower() not in _CORS_RESPONSE_HEADERS
                    ],
                }
            await original_send(message)

        await self.app(scope, receive, send_wrapper)


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
    # after those headers have been added. Pure ASGI, not `BaseHTTPMiddleware`:
    # the latter runs the application in a child task whose context-variable
    # writes are discarded, which would lose the correlation tenant binding
    # (`OBS-001`).
    app.add_middleware(AdminCorsConfineMiddleware)

    # Registered last so it wraps everything registered so far: the security
    # header posture applies to guard refusals and handler errors as well as
    # successful responses.
    app.add_middleware(SecurityHeadersMiddleware)

    # Starlette wraps the most recently registered middleware around the earlier
    # layers. Register request correlation last so even an early body-limit or
    # CORS response receives the same usable IDs as an endpoint response. The
    # IDs are always server-minted: an ID supplied by an unauthenticated client
    # can be repeated or forged, which makes it useless for correlation and
    # misleading in an audit trail.
    app.add_middleware(CorrelationMiddleware, log_access=settings.log_access)


def create_app(
    settings: Settings | None = None,
    *,
    registry: TenantRegistry | None = None,
    booking_store: BookingStore | None = None,
    lead_store: LeadStore | None = None,
    conversation_store: ConversationStore | None = None,
    handoff_store: HandoffStore | None = None,
    idempotency_store: IdempotencyStore | None = None,
    membership_store: MembershipStore | None = None,
    consent_store: ConsentStore | None = None,
    privacy_store: PrivacyStore | None = None,
    audit_store: AuditStore | None = None,
    job_store: JobStore | None = None,
    turn_record_store: TurnRecordStore | None = None,
    trace_access_store: TraceAccessStore | None = None,
    feedback_store: TurnFeedbackStore | None = None,
    review_store: ReviewQueueStore | None = None,
    workflow_store: WorkflowStore | None = None,
    chat_model: ChatModel | None = None,
    checkpointer: Checkpointer | None = None,
    rate_limit_store: RateLimitStore | None = None,
    visitor_identity: VisitorIdentityExtractor | None = None,
    availability_provider: AvailabilityProvider | None = None,
    knowledge_store: KnowledgeStore | None = None,
    generation_findings: IndexIntegrityStore | None = None,
    object_store: ObjectStore | None = None,
    search_index: SearchIndex | None = None,
    evidence_source: EvidenceSource | None = None,
    budgets: BudgetLedger | None = None,
) -> FastAPI:
    """Build the application.

    The stores are injected together or not at all. A mixture would run
    half the request against a test double and half against PostgreSQL, which is
    the configuration most likely to make a passing test mean nothing.

    Args:
        settings: Overrides environment-derived configuration.
        registry: The tenants this deployment serves. Injected for tests that
            need a tenant with a specific plan (e.g. a tiny budget to exhaust);
            production uses the seeded registry.
        booking_store: Explicit test adapter. Production builds PostgreSQL stores.
        lead_store: Explicit test adapter. Production builds PostgreSQL stores.
        conversation_store: Explicit test adapter. Production builds PostgreSQL stores.
        handoff_store: Explicit test adapter. Production builds PostgreSQL stores.
        idempotency_store: Explicit test adapter. Production builds PostgreSQL stores.
        membership_store: Explicit test adapter. Production builds PostgreSQL stores.
        consent_store: Explicit test adapter. Production builds PostgreSQL stores.
        privacy_store: Explicit test adapter. Production composes one over the
            application engine plus the erasure role's engine.
        audit_store: Explicit test adapter. Production builds a PostgreSQL store.
        job_store: Durable job adapter. Production always builds the PostgreSQL
            implementation; explicit HTTP tests default to an in-memory fake.
        turn_record_store: The PRIV-002 inference-plane envelope. Production
            builds the PostgreSQL implementation; explicit-store compositions
            default to an in-memory fake, like the job store.
        trace_access_store: The PRIV-002 dedicated trace-read grants. Production
            builds the PostgreSQL implementation; explicit-store compositions
            default to an in-memory fake.
        feedback_store: The FEAT-008 visitor ratings. Production builds the
            PostgreSQL implementation; explicit-store compositions default to
            an in-memory fake.
        review_store: The FEAT-008 review queue. Production builds the
            PostgreSQL implementation; explicit-store compositions default to
            an in-memory fake.
        workflow_store: The AGENT-001 routing and workflow records. Production
            builds the PostgreSQL implementation; explicit-store compositions
            default to an in-memory fake.
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
        availability_provider: What the tenant is currently offering. Explicit
            test adapter; production builds the PostgreSQL-backed fake provider.
        knowledge_store: The knowledge system of record (RAG-001). Injected
            for tests; production builds the PostgreSQL store.
        generation_findings: Index generations and integrity findings (RAG-002).
            Injected for tests; production builds the PostgreSQL store.
        object_store: Tenant-isolated object storage for uploads (RAG-002).
            Injected for tests; production requires ``INGESTION_STORAGE_ROOT``.
        search_index: The retrieval index (RAG-002). Injected for tests;
            production builds the Elasticsearch adapter when
            ``ELASTICSEARCH_URL`` is configured.
        evidence_source: The `RAG-005` retrieval port the agent runtime grounds
            turns in. Injected explicitly, like the chat model: a composition
            without it runs the pre-`RAG-005` graph, with no abstention and no
            citations.
        budgets: The `AI-002` per-tenant spend and action ledger. Injected for
            tests that need to observe or preload usage; production composes one
            and holds it on ``app.state``. A deployment can never run without
            one — the composition builds it when omitted.

    Raises:
        ValueError: the stores were injected in part, or production composition
            is missing a setting it cannot run without.
    """
    resolved = settings or Settings.from_environment()
    # L8-OTEL: initialise the OTel tracer provider before the first span is
    # created, so startup probes and HTTP auto-instrumentation record traces.
    otel_setup.init_otel()
    # The log plane is configured before anything else can log: the first line
    # the process emits is already structured and carries the service name.
    configure_logging(
        service=resolve_service(SERVICE_NAME),
        environment=resolved.app_env,
        level=resolved.log_level,
        json_enabled=resolved.log_json,
    )
    # PRIV-002: fail a deployment that would export trace content to a backend
    # outside the trust boundary before any turn is recorded, not on the first
    # export. Refuses any external endpoint regardless of environment, so
    # "production startup fails" is structural rather than a convention.
    validate_trace_content_export(resolved)
    resolved_registry = registry or TenantRegistry.seeded()
    database: Database | None = None
    privacy_database: Database | None = None
    # The one per-tenant budget ledger the whole app enforces against, shared
    # across every conversation so usage accumulates per tenant. A caller that
    # injected one (a test observing or preloading usage) owns it; otherwise
    # one is built here, so a deployment can never silently run without it.
    resolved_budgets = budgets if budgets is not None else BudgetEnforcer(metrics=METRICS)
    # AI-001/AI-002: the provider chain built from settings. Tests inject
    # `chat_model` explicitly and leave this empty; a production deployment
    # without a model configured also leaves it empty so chat fails closed
    # rather than starting against a guessed endpoint. When both a primary and
    # a fallback are configured, the two are wrapped in a fallback chain; when
    # the response cache is enabled, the chain is cached.
    owned_models: list[OpenAICompatibleChatModel] = []
    if chat_model is None and resolved.llm_base_url and resolved.llm_model:
        primary = OpenAICompatibleChatModel(
            base_url=resolved.llm_base_url,
            model=resolved.llm_model,
            api_key=resolved.llm_api_key,
            policy=resolved.llm_resilience,
            metrics=METRICS,
        )
        owned_models.append(primary)
        if resolved.llm_fallback_base_url and resolved.llm_fallback_model:
            owned_models.append(
                OpenAICompatibleChatModel(
                    base_url=resolved.llm_fallback_base_url,
                    model=resolved.llm_fallback_model,
                    api_key=resolved.llm_fallback_api_key,
                    policy=resolved.llm_fallback_resilience,
                    metrics=METRICS,
                )
            )
    effective_model = chat_model
    if owned_models:
        effective_model = owned_models[0]
        if len(owned_models) > 1:
            effective_model = FallbackChatModel(owned_models, metrics=METRICS)
    # L8-OTEL: wrap the model chain in a content-free GenAI-convention span
    # recorder *before* the metrics wrapper so both sit at the same observation
    # level around the actual provider calls.
    if effective_model is not None:
        provider = _provider_name(resolved.llm_base_url or "")
        effective_model = SpanRecordingChatModel(
            effective_model,
            gen_ai_system=provider,
        )
    # The metrics wrapper is observation only: it delegates every call and
    # records latency, outcome, and token counts around it (`OBS-002`). A
    # provider failure still escapes to the graph exactly as before.
    if effective_model is not None:
        effective_model = MetricRecordingChatModel(effective_model, METRICS)
    # The cache sits outside the recorder so a hit never records a model call:
    # the recording wrapper stands for an actual provider completion, and a
    # cached answer is not one.
    if effective_model is not None and resolved.llm_response_cache:
        effective_model = CachingChatModel(
            effective_model,
            metrics=METRICS,
            ttl_seconds=float(resolved.llm_response_cache_ttl_seconds),
        )

    supplied = (
        booking_store,
        lead_store,
        conversation_store,
        handoff_store,
        idempotency_store,
        membership_store,
        consent_store,
        privacy_store,
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
        consent_store = PostgresConsentStore(database.engine)
        audit_store = PostgresAuditStore(database.engine)
        # The erasure engine is optional here: it exists for the worker, and the
        # API never calls the operations that need it. A deployment without
        # PRIVACY_DATABASE_URL serves export and the queue but runs no erasure.
        erasure_engine = None
        if resolved.privacy_database_url is not None:
            privacy_database = Database.connect(
                resolved.privacy_database_url,
                DatabasePoolSettings(
                    size=resolved.privacy_database_pool_size,
                    max_overflow=resolved.privacy_database_max_overflow,
                    timeout_seconds=resolved.database_pool_timeout_seconds,
                    recycle_seconds=resolved.database_pool_recycle_seconds,
                ),
            )
            erasure_engine = privacy_database.engine
        privacy_store = PostgresPrivacyStore(database.engine, erasure_engine)
        job_store = PostgresJobStore(database.engine)
        turn_record_store = PostgresTurnRecordStore(database.engine)
        trace_access_store = PostgresTraceAccessStore(database.engine)
        feedback_store = PostgresTurnFeedbackStore(database.engine)
        review_store = PostgresReviewQueueStore(database.engine)
        workflow_store = PostgresWorkflowStore(database.engine)
        knowledge_store = PostgresKnowledgeStore(database.engine)
        generation_findings = PostgresIndexIntegrityStore(database.engine)
        # The upload surface exists only where the ingestion pipeline exists. A
        # deployment that configured no ingestion dependency gets no object
        # store either, and the upload route answers `storage_unavailable` —
        # the same fail-closed shape the search-index surface uses, and the
        # same condition the worker requires before composing an ingestion
        # handler. Requiring the root unconditionally would make an unrelated
        # booking-only deployment refuse to start.
        if resolved.elasticsearch_url is not None or resolved.embedding_url is not None:
            if not resolved.ingestion_storage_root:
                raise ValueError(
                    "INGESTION_STORAGE_ROOT is required for tenant-isolated upload storage"
                )
            object_store = DiskObjectStore(Path(resolved.ingestion_storage_root))
        if resolved.elasticsearch_url is not None:
            search_index = ElasticsearchSearchIndex(
                base_url=resolved.elasticsearch_url,
                username=resolved.elasticsearch_username,
                password=resolved.elasticsearch_password,
                index_name=resolved.elasticsearch_index,
                policy=resolved.elasticsearch_resilience,
                metrics=METRICS,
            )

    if (
        booking_store is None
        or lead_store is None
        or conversation_store is None
        or handoff_store is None
        or idempotency_store is None
        or membership_store is None
        or consent_store is None
        or privacy_store is None
        or audit_store is None
    ):
        raise RuntimeError("composition failed to provide persistence stores")

    if job_store is None:
        # Explicit-store compositions are unit-test shapes. A deployed app took
        # the database branch above and can never silently run an in-memory queue.
        job_store = InMemoryJobStore()

    if turn_record_store is None:
        turn_record_store = InMemoryTurnRecordStore()
    if trace_access_store is None:
        trace_access_store = InMemoryTraceAccessStore()
    if feedback_store is None:
        feedback_store = InMemoryTurnFeedbackStore()
    if review_store is None:
        review_store = InMemoryReviewQueueStore()
    if workflow_store is None:
        # Explicit-store compositions are unit-test shapes; a deployed app took
        # the database branch above and can never silently run in-memory.
        workflow_store = InMemoryWorkflowStore()

    rag_stores = (knowledge_store, generation_findings, object_store, search_index)
    if (
        database is None
        and any(store is None for store in rag_stores)
        and any(store is not None for store in rag_stores)
    ):
        raise ValueError("inject all RAG stores together or let production composition build all")
    if not any(store is not None for store in rag_stores) and database is None:
        # The no-database shape is a unit test, and the in-memory fakes are its
        # complete implementation, exactly like `InMemoryJobStore` above.
        knowledge_store = InMemoryKnowledgeStore()
        generation_findings = InMemoryIndexIntegrityStore()
        object_store = MemoryObjectStore()
        search_index = InMemorySearchIndex()

    # `RAG-005`: without this the runtime is handed `evidence=None` and answers
    # every turn from the prompt alone — no retrieval, no citations, and no
    # abstention — while the index beside it holds the tenant's approved
    # knowledge. A caller that injected its own source keeps it.
    owned_query_embedder: Embedder | None = None
    if (
        evidence_source is None
        and search_index is not None
        and knowledge_store is not None
        and resolved.embedding_url is not None
    ):
        owned_query_embedder = EmbeddingServiceClient(
            base_url=resolved.embedding_url,
            token=resolved.embedding_token,
            policy=resolved.embedding_resilience,
            metrics=METRICS,
        )
        evidence_source = RetrievalEvidenceSource(
            index=search_index,
            embedder=owned_query_embedder,
            knowledge=knowledge_store,
            config=HybridRetrieverConfig(),
            metrics=METRICS,
        )

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

    if availability_provider is None:
        # In production the offers come from the same database the bookings land
        # in; with injected test stores there is no database, so fall back to the
        # deterministic in-process provider.
        if database is not None:
            availability_provider = PostgresAvailabilityProvider(database.engine)
        else:
            availability_provider = DemoAvailabilityProvider(
                resolved_registry,
                # The in-memory provider has no SQL, so the booking store tells
                # it which slots are no longer bookable — the same exclusion the
                # Postgres provider expresses with ``NOT EXISTS``.
                taken=(
                    booking_store.taken_slot_ids
                    if isinstance(booking_store, InMemoryBookingStore)
                    else None
                ),
            )

    booking_service = RecordedBookingService(
        booking_store, availability_provider, consent_store, metrics=METRICS
    )

    bookings_for_agent, leads_for_agent = booking_store, lead_store
    handoffs_for_agent, keys_for_agent = handoff_store, idempotency_store
    consent_for_agent = consent_store

    def compose_runtime(saver: Checkpointer, model: ChatModel) -> ConversationRuntime:
        return build_conversation_runtime(
            registry=resolved_registry,
            model=model,
            bookings=bookings_for_agent,
            leads=leads_for_agent,
            handoffs=handoffs_for_agent,
            idempotency=keys_for_agent,
            consent=consent_for_agent,
            workflows=workflow_store,
            checkpointer=saver,
            availability=availability_provider,
            evidence=evidence_source,
            metrics=METRICS,
            budgets=resolved_budgets,
        )

    @asynccontextmanager
    async def lifespan(running: FastAPI) -> AsyncIterator[None]:
        async with AsyncExitStack() as closing:
            if database is not None:
                closing.push_async_callback(database.dispose)
                await database.synchronize_tenants(
                    (record.policy.tenant_id, record.policy.name)
                    for record in resolved_registry.all().values()
                )
                # Seed the database-backed fake provider now that tenant rows
                # exist; idempotent, so a restart does not duplicate slots.
                await seed_demo_availability(database.engine, resolved_registry)
            if privacy_database is not None:
                closing.push_async_callback(privacy_database.dispose)
            # Owned provider clients are closed with the app. An injected test
            # model is the caller's to clean up, not the app's.
            for owned in owned_models:
                closing.push_async_callback(owned.close)
            if isinstance(search_index, ElasticsearchSearchIndex):
                closing.push_async_callback(search_index.close)
            # Only the client this composition built: an injected evidence
            # source owns whatever it was given.
            if isinstance(owned_query_embedder, EmbeddingServiceClient):
                closing.push_async_callback(owned_query_embedder.close)
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
            closing.push_async_callback(_shutdown_otel)
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
    app.state.registry = resolved_registry
    app.state.budgets = resolved_budgets
    app.state.booking_store = booking_store
    app.state.lead_store = lead_store
    app.state.conversation_store = conversation_store
    app.state.handoff_store = handoff_store
    app.state.idempotency_store = idempotency_store
    app.state.membership_store = membership_store
    app.state.audit_store = audit_store
    app.state.consent_store = consent_store
    app.state.privacy_store = privacy_store
    app.state.job_store = job_store
    app.state.turn_record_store = turn_record_store
    app.state.trace_access_store = trace_access_store
    app.state.feedback_store = feedback_store
    app.state.review_store = review_store
    app.state.knowledge_store = knowledge_store
    app.state.generation_findings = generation_findings
    app.state.object_store = object_store
    app.state.search_index = search_index
    app.state.evidence_source = evidence_source
    app.state.visitor_credential_signer = visitor_credentials
    # The one clock every visitor credential is verified against, so a test can
    # move time by reassigning state rather than sleeping.
    app.state.clock = utc_now
    app.state.booking_service = booking_service
    app.state.availability_provider = availability_provider
    app.state.metrics = METRICS
    app.state.conversation_runtime = (
        compose_runtime(checkpointer, effective_model)
        if effective_model is not None and checkpointer is not None
        else None
    )
    # The effective model is public state so a test can assert which adapter
    # composition chose without reaching into construction internals (`AI-001`).
    app.state.chat_model = effective_model

    _install_middleware(app, resolved, rate_limit_store, effective_identity)
    install_pii_log_filter()

    app.add_exception_handler(DomainError, handle_domain_error)
    app.add_exception_handler(TransportError, _handle_api_fault)
    app.add_exception_handler(RequestValidationError, _handle_request_validation_error)
    app.add_exception_handler(Exception, _handle_unexpected_error)

    app.include_router(health.router)
    app.include_router(metrics.router)
    app.include_router(tenants.router)
    app.include_router(bookings.router)
    app.include_router(leads.router)
    app.include_router(chat.router)
    app.include_router(privacy.router)
    app.include_router(jobs.router)
    app.include_router(traces.router)
    app.include_router(reviews.router)
    app.include_router(handoffs.router)
    app.include_router(knowledge.router)
    app.include_router(admin.router)

    return app
