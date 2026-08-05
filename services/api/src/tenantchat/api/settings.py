"""Runtime configuration, read once at startup.

Read from the environment into a frozen object at startup rather than consulted
via ``os.environ`` at call sites, so configuration is visible in one place and a
misconfiguration fails when the process starts instead of on the first request
that happens to touch it.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from urllib.parse import urlparse

from tenantchat.api.limits import RateLimitPolicy

# Large enough for a booking form with a long address, small enough that an
# unauthenticated caller cannot make the process buffer megabytes (SEC-003).
_DEFAULT_MAX_REQUEST_BYTES = 64 * 1024
# Generous enough for a full bounded transcript (100 messages at 4000
# characters), small enough that the buffered-response guard stays cheap.
_DEFAULT_MAX_RESPONSE_BYTES = 512 * 1024
# The transcript is served truncated to this many of the most recent messages,
# which bounds what a session read costs whether the conversation is ten turns
# old or ten thousand (SEC-003).
_DEFAULT_MAX_HISTORY_MESSAGES = 100

# The widget is embedded on tenant sites, so cross-origin requests are normal and
# the origin list is the control. Defaulting to localhost keeps development
# working while making a production deployment state its origins explicitly —
# a wildcard default is how a permissive CORS policy reaches production unnoticed.
# The dev port is the `make api` default; the gateway answers same-origin calls
# in the deployed shape, so the allowlist is only the widget's direct path.
_DEFAULT_ALLOWED_ORIGINS = ("http://127.0.0.1:8080", "http://localhost:8080")

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def loopback_database(database_url: str | None) -> bool:
    """Whether the deployment's database is on this machine.

    The boundary for `CHAT_API_DEV_AUTH`: development auth may only run against
    a database that lives on the same host, because that is the one shape a
    production deployment can never legitimately have. A URL with no host
    (unix-socket driver default) counts as loopback.
    """
    if database_url is None:
        return True
    host = (urlparse(database_url).hostname or "").lower()
    return host in _LOOPBACK_HOSTS


# The cluster-internal service DNS zone. A backend named inside it is reached
# only through the cluster's own networking; anything else is outside the
# trust boundary `ADR-0010` draws around the trace plane.
_IN_CLUSTER_SUFFIX = ".svc.cluster.local"


def validate_trace_content_export(settings: Settings) -> None:
    """Refuse a deployment that would export trace content outside the boundary.

    `TRACE_CONTENT_EXPORT` is off by default. Enabling it is only legitimate
    for a viewer inside the cluster trust boundary — a service the deployment
    itself runs and protects with the same admin authentication as the console.
    An external backend is refused here, at startup, so a misconfigured
    deployment fails before any turn is recorded, never on the first export.

    Raises:
        ValueError: content export is enabled without an endpoint, or the
            endpoint's host is neither loopback nor in-cluster service DNS.
    """
    if not settings.trace_content_export:
        return
    endpoint = settings.trace_content_export_endpoint
    if not endpoint:
        raise ValueError(
            "TRACE_CONTENT_EXPORT is enabled but TRACE_CONTENT_EXPORT_ENDPOINT is not set"
        )
    host = (urlparse(endpoint).hostname or "").lower()
    if host in _LOOPBACK_HOSTS or host.endswith(_IN_CLUSTER_SUFFIX):
        return
    raise ValueError(
        "TRACE_CONTENT_EXPORT_ENDPOINT must name a backend inside the cluster trust "
        f"boundary (loopback or *{_IN_CLUSTER_SUFFIX}); content export is refused for "
        "any external backend"
    )


def _int_env(name: str, default: int) -> int:
    return int(os.environ.get(name, str(default)))


@dataclass(frozen=True, slots=True)
class Settings:
    allowed_origins: tuple[str, ...]
    max_request_bytes: int
    docs_enabled: bool
    database_url: str | None = None
    # Both admin credentials are optional here and required by the production
    # composition in `create_app`. Absent, every admin route fails closed, so a
    # deployment that forgets one loses the console rather than opening it.
    admin_gateway_token: str | None = None
    admin_csrf_secret: str | None = None
    # Explicit non-production local development auth (SEC-001): when enabled,
    # the service trusts the gateway identity headers without the shared token,
    # and the production composition refuses to start unless the database is
    # loopback. Never set in a deployment: `scripts/verify_deployment_security.py`
    # rejects a manifest that enables it.
    dev_auth: bool = False
    database_pool_size: int = 5
    database_max_overflow: int = 5
    database_pool_timeout_seconds: float = 5.0
    database_pool_recycle_seconds: int = 1800
    # AI-001: OpenAI-compatible provider settings. Both the base URL and the
    # model are required to compose a chat runtime; absent either, no runtime is
    # composed and chat fails closed (503). `api_key` stays optional so a local,
    # unauthenticated endpoint keeps working in development.
    llm_base_url: str | None = None
    llm_model: str | None = None
    llm_api_key: str = ""
    llm_timeout_seconds: int = 120
    # SEC-002: the key that signs visitor credentials, and their lifetime. The
    # key is required by the production composition — a deployment without it
    # cannot open sessions (fail closed), exactly like the admin credentials.
    # The TTL bounds how long a credential works without renewal; every
    # credentialed chat response reissues one, so an actively used conversation
    # never expires while an abandoned one becomes unusable after the TTL.
    visitor_credential_signing_key: str | None = None
    visitor_credential_ttl_seconds: int = 24 * 60 * 60
    # SEC-003: size and history bounds. The rate/concurrency budgets live in
    # `rate_limits`; the request-size bound above is the body limit.
    max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES
    max_history_messages: int = _DEFAULT_MAX_HISTORY_MESSAGES
    rate_limits: RateLimitPolicy = field(default_factory=RateLimitPolicy)
    # Off by default: when set, unexpected errors publish their exception text
    # to the caller. The text routinely contains connection strings, query
    # fragments, or row data, which is why the response hides it otherwise —
    # the flag is for a developer attached to a local deployment, not for an
    # operator who wants better error messages in production.
    debug: bool = False
    # PRIV-001: the erasure role's database. `PRIVACY_DATABASE_URL` names a
    # login with DELETE on sessions and transcripts, which the application role
    # deliberately lacks. The erasure and retention worker connects with it and
    # nothing else does; absent it, the worker refuses to start.
    privacy_database_url: str | None = None
    privacy_database_pool_size: int = 2
    privacy_database_max_overflow: int = 2
    # OBS-001: the structured log plane. `log_json` shapes the stream, `level`
    # bounds volume (a production deployment runs DEBUG only when it must),
    # `log_access` is the per-request access line, off by default for the same
    # reason, and `log_pseudonym_key` makes tenant pseudonyms irreversible.
    # Services that share a log store must share the key, or the same tenant
    # gets a different pseudonym in each service's lines.
    log_level: str = "INFO"
    log_json: bool = True
    log_access: bool = False
    log_pseudonym_key: str | None = None
    # The deployment environment (`APP_ENV`, as the side services already
    # read it). Log lines carry it so a shared log store can be split by
    # deployment; a deployment that never sets it logs as `local` until it does.
    app_env: str = "local"
    # PRIV-002/ADR-0010: whether the inference plane may reach a trace viewer
    # as content. Disabled by default; when enabled, `trace_content_export_endpoint`
    # must name a viewer inside the cluster trust boundary (loopback for
    # development, `*.svc.cluster.local` in the cluster), and `create_app`
    # refuses to start otherwise. The application itself never exports content
    # — the collector is the fan-out point — but the setting travels with the
    # deployment and this process is the fail-closed enforcement point.
    trace_content_export: bool = False
    trace_content_export_endpoint: str | None = None
    # RAG-002: the ingestion pipeline's external dependencies. None is required
    # for the API to serve visitor routes; each is required for the surface
    # that uses it. The worker composes the ingestion handler only when all of
    # them are configured, so a partial configuration fails closed instead of
    # indexing into a guessed endpoint.
    elasticsearch_url: str | None = None
    elasticsearch_username: str | None = None
    elasticsearch_password: str | None = None
    elasticsearch_index: str = "tenant-knowledge-chunks"
    embedding_url: str | None = None
    embedding_token: str | None = None
    ingestion_storage_root: str | None = None

    @classmethod
    def from_environment(cls) -> Settings:
        """Build settings from ``CHAT_API_*`` variables, falling back to dev defaults.

        Raises:
            ValueError: if a numeric variable is set to something unparseable.
        """
        raw_origins = os.environ.get("CHAT_API_ALLOWED_ORIGINS", "")
        origins = tuple(item.strip() for item in raw_origins.split(",") if item.strip())

        return cls(
            allowed_origins=origins or _DEFAULT_ALLOWED_ORIGINS,
            max_request_bytes=_int_env("CHAT_API_MAX_REQUEST_BYTES", _DEFAULT_MAX_REQUEST_BYTES),
            # Off by default: the schema names every field and error code the API
            # accepts, which is a useful map for an attacker and useless to a
            # visitor. Operators turn it on deliberately.
            docs_enabled=os.environ.get("CHAT_API_DOCS_ENABLED", "").lower() == "true",
            database_url=os.environ.get("DATABASE_URL") or None,
            # Named without the `CHAT_API_` prefix because the gateway and this
            # service must be handed the identical values, and the gateway's
            # configuration already uses these names.
            admin_gateway_token=os.environ.get("ADMIN_GATEWAY_TOKEN", "").strip() or None,
            # A development process mints its own CSRF secret so `make api` needs
            # no shared secrets at all; the loopback-database guard keeps that
            # convenience from reaching a deployment.
            admin_csrf_secret=os.environ.get("ADMIN_CSRF_SECRET", "").strip()
            or (
                secrets.token_hex(32)
                if os.environ.get("CHAT_API_DEV_AUTH", "").lower() == "true"
                else None
            ),
            dev_auth=os.environ.get("CHAT_API_DEV_AUTH", "").lower() == "true",
            database_pool_size=_int_env("CHAT_API_DATABASE_POOL_SIZE", 5),
            database_max_overflow=_int_env("CHAT_API_DATABASE_MAX_OVERFLOW", 5),
            database_pool_timeout_seconds=float(
                os.environ.get("CHAT_API_DATABASE_POOL_TIMEOUT_SECONDS", "5")
            ),
            database_pool_recycle_seconds=_int_env("CHAT_API_DATABASE_POOL_RECYCLE_SECONDS", 1800),
            # AI-001 provider settings. The same `LLM_*` names the prototype and
            # the financing agent used, so an existing environment configures all
            # three clients identically. Values are trimmed; empty means "not
            # configured", which composes no runtime.
            llm_base_url=os.environ.get("LLM_BASE_URL", "").strip() or None,
            llm_model=os.environ.get("LLM_MODEL", "").strip() or None,
            llm_api_key=os.environ.get("LLM_API_KEY", "").strip(),
            llm_timeout_seconds=_int_env("LLM_TIMEOUT_SECONDS", 120),
            visitor_credential_signing_key=os.environ.get(
                "CHAT_API_VISITOR_CREDENTIAL_SIGNING_KEY", ""
            ).strip()
            or None,
            visitor_credential_ttl_seconds=_int_env(
                "CHAT_API_VISITOR_CREDENTIAL_TTL_SECONDS", 86400
            ),
            # SEC-003 bounds. The window applies to every rate scope; a budget
            # is per window, so a 60-second window with 600 IP requests is 10
            # per second sustained.
            max_response_bytes=_int_env("CHAT_API_MAX_RESPONSE_BYTES", _DEFAULT_MAX_RESPONSE_BYTES),
            max_history_messages=_int_env(
                "CHAT_API_MAX_HISTORY_MESSAGES", _DEFAULT_MAX_HISTORY_MESSAGES
            ),
            rate_limits=RateLimitPolicy(
                ip_requests=_int_env("CHAT_API_IP_RATE_LIMIT", 600),
                ip_concurrency=_int_env("CHAT_API_IP_CONCURRENCY", 20),
                tenant_requests=_int_env("CHAT_API_TENANT_RATE_LIMIT", 3000),
                tenant_concurrency=_int_env("CHAT_API_TENANT_CONCURRENCY", 100),
                session_requests=_int_env("CHAT_API_SESSION_RATE_LIMIT", 60),
                session_concurrency=_int_env("CHAT_API_SESSION_CONCURRENCY", 5),
                window_seconds=_int_env("CHAT_API_RATE_WINDOW_SECONDS", 60),
            ),
            debug=os.environ.get("CHAT_API_DEBUG", "").lower() == "true",
            privacy_database_url=os.environ.get("PRIVACY_DATABASE_URL", "").strip() or None,
            privacy_database_pool_size=_int_env("CHAT_API_PRIVACY_DATABASE_POOL_SIZE", 2),
            privacy_database_max_overflow=_int_env("CHAT_API_PRIVACY_DATABASE_MAX_OVERFLOW", 2),
            log_level=os.environ.get("CHAT_API_LOG_LEVEL", "").strip() or "INFO",
            log_json=os.environ.get("CHAT_API_LOG_JSON", "true").strip().lower() != "false",
            log_access=os.environ.get("CHAT_API_LOG_ACCESS", "").lower() == "true",
            log_pseudonym_key=os.environ.get("CHAT_API_LOG_PSEUDONYM_KEY", "").strip() or None,
            app_env=os.environ.get("APP_ENV", "").strip() or "local",
            trace_content_export=(
                os.environ.get("TRACE_CONTENT_EXPORT", "").strip().lower() == "true"
            ),
            trace_content_export_endpoint=(
                os.environ.get("TRACE_CONTENT_EXPORT_ENDPOINT", "").strip() or None
            ),
            elasticsearch_url=os.environ.get("ELASTICSEARCH_URL", "").strip() or None,
            elasticsearch_username=os.environ.get("ES_USERNAME", "").strip() or None,
            elasticsearch_password=os.environ.get("ES_PASSWORD", "").strip() or None,
            elasticsearch_index=os.environ.get("KNOWLEDGE_INDEX", "tenant-knowledge-chunks"),
            embedding_url=os.environ.get("EMBEDDING_URL", "").strip() or None,
            embedding_token=os.environ.get("INGESTION_TO_EMBEDDING_TOKEN", "").strip() or None,
            ingestion_storage_root=os.environ.get("INGESTION_STORAGE_ROOT", "").strip() or None,
        )
