"""Runtime configuration, read once at startup.

Read from the environment into a frozen object at startup rather than consulted
via ``os.environ`` at call sites, so configuration is visible in one place and a
misconfiguration fails when the process starts instead of on the first request
that happens to touch it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# Large enough for a booking form with a long address, small enough that an
# unauthenticated caller cannot make the process buffer megabytes (SEC-003).
_DEFAULT_MAX_REQUEST_BYTES = 64 * 1024

# The widget is embedded on tenant sites, so cross-origin requests are normal and
# the origin list is the control. Defaulting to localhost keeps development
# working while making a production deployment state its origins explicitly —
# a wildcard default is how a permissive CORS policy reaches production unnoticed.
# The dev port is the `make api` default; the gateway answers same-origin calls
# in the deployed shape, so the allowlist is only the widget's direct path.
_DEFAULT_ALLOWED_ORIGINS = ("http://127.0.0.1:8080", "http://localhost:8080")


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
    # PRIV-001: the erasure role's database. `PRIVACY_DATABASE_URL` names a
    # login with DELETE on sessions and transcripts, which the application role
    # deliberately lacks. The erasure and retention worker connects with it and
    # nothing else does; absent it, the worker refuses to start.
    privacy_database_url: str | None = None
    privacy_database_pool_size: int = 2
    privacy_database_max_overflow: int = 2

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
            max_request_bytes=int(
                os.environ.get("CHAT_API_MAX_REQUEST_BYTES", _DEFAULT_MAX_REQUEST_BYTES)
            ),
            # Off by default: the schema names every field and error code the API
            # accepts, which is a useful map for an attacker and useless to a
            # visitor. Operators turn it on deliberately.
            docs_enabled=os.environ.get("CHAT_API_DOCS_ENABLED", "").lower() == "true",
            database_url=os.environ.get("DATABASE_URL") or None,
            # Named without the `CHAT_API_` prefix because the gateway and this
            # service must be handed the identical values, and the gateway's
            # configuration already uses these names.
            admin_gateway_token=os.environ.get("ADMIN_GATEWAY_TOKEN", "").strip() or None,
            admin_csrf_secret=os.environ.get("ADMIN_CSRF_SECRET", "").strip() or None,
            database_pool_size=int(os.environ.get("CHAT_API_DATABASE_POOL_SIZE", "5")),
            database_max_overflow=int(os.environ.get("CHAT_API_DATABASE_MAX_OVERFLOW", "5")),
            database_pool_timeout_seconds=float(
                os.environ.get("CHAT_API_DATABASE_POOL_TIMEOUT_SECONDS", "5")
            ),
            database_pool_recycle_seconds=int(
                os.environ.get("CHAT_API_DATABASE_POOL_RECYCLE_SECONDS", "1800")
            ),
            # AI-001 provider settings. The same `LLM_*` names the prototype and
            # the financing agent used, so an existing environment configures all
            # three clients identically. Values are trimmed; empty means "not
            # configured", which composes no runtime.
            llm_base_url=os.environ.get("LLM_BASE_URL", "").strip() or None,
            llm_model=os.environ.get("LLM_MODEL", "").strip() or None,
            llm_api_key=os.environ.get("LLM_API_KEY", "").strip(),
            llm_timeout_seconds=int(os.environ.get("LLM_TIMEOUT_SECONDS", "120")),
            privacy_database_url=os.environ.get("PRIVACY_DATABASE_URL", "").strip() or None,
            privacy_database_pool_size=int(
                os.environ.get("CHAT_API_PRIVACY_DATABASE_POOL_SIZE", "2")
            ),
            privacy_database_max_overflow=int(
                os.environ.get("CHAT_API_PRIVACY_DATABASE_MAX_OVERFLOW", "2")
            ),
        )
