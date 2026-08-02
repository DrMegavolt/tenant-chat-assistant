"""Fail-closed runtime configuration shared by the prototype services.

The deployment still runs the prototype entry points while the production API is
built.  Keeping the security-sensitive environment handling here prevents the
two OpenAI-compatible clients from drifting apart.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlsplit


class RuntimeConfigurationError(RuntimeError):
    """A safe startup error that names configuration, never its value."""


_PRODUCTION_ENVIRONMENTS = frozenset({"prod", "production"})
_PLACEHOLDER_PREFIXES = ("REPLACE_WITH_", "replace-with-")


def is_production() -> bool:
    """Return whether strict production configuration rules apply."""
    return os.environ.get("APP_ENV", "local").strip().lower() in _PRODUCTION_ENVIRONMENTS


def _is_placeholder(value: str) -> bool:
    return any(marker in value for marker in _PLACEHOLDER_PREFIXES) or ".invalid" in value


def require_production_environment(names: tuple[str, ...]) -> None:
    """Require non-placeholder values for ``names`` in production.

    Error text intentionally contains variable names only.  A secret value must
    never be copied into a container log while reporting a startup failure.
    """
    if not is_production():
        return

    missing = [
        name
        for name in names
        if not (value := os.environ.get(name, "").strip()) or _is_placeholder(value)
    ]
    if missing:
        joined = ", ".join(sorted(missing))
        raise RuntimeConfigurationError(
            f"production configuration is missing required values: {joined}"
        )


@dataclass(frozen=True, slots=True)
class OpenAICompatibleSettings:
    """Validated settings for one OpenAI-compatible chat-completions client."""

    base_url: str
    model: str
    api_key: str
    timeout_seconds: int


def load_openai_compatible_settings(
    *,
    local_base_url: str,
    local_model: str = "local-model",
    local_timeout_seconds: int = 120,
) -> OpenAICompatibleSettings:
    """Load LLM configuration, requiring every provider value in production."""
    require_production_environment(("LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY"))

    base_url = os.environ.get("LLM_BASE_URL", local_base_url).strip()
    model = os.environ.get("LLM_MODEL", local_model).strip()
    api_key = os.environ.get("LLM_API_KEY", "").strip()
    raw_timeout = os.environ.get("LLM_TIMEOUT_SECONDS", str(local_timeout_seconds)).strip()

    if base_url:
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise RuntimeConfigurationError(
                "LLM_BASE_URL must be an absolute http:// or https:// URL"
            )

    try:
        timeout_seconds = int(raw_timeout)
    except ValueError as error:
        raise RuntimeConfigurationError("LLM_TIMEOUT_SECONDS must be an integer") from error
    if timeout_seconds <= 0:
        raise RuntimeConfigurationError("LLM_TIMEOUT_SECONDS must be greater than zero")

    return OpenAICompatibleSettings(
        base_url=base_url,
        model=model,
        api_key=api_key,
        timeout_seconds=timeout_seconds,
    )


def openai_request_headers(api_key: str) -> dict[str, str]:
    """Build request headers without logging or otherwise exposing the key."""
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers
