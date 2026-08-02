"""Small, framework-neutral helpers for internal service authentication."""

from __future__ import annotations

import os
import secrets
from collections.abc import Mapping

from runtime_security import (
    RuntimeConfigurationError,
    is_production,
    require_production_environment,
)


def load_internal_credentials(
    environment_identities: Mapping[str, str], *, required: bool = False
) -> dict[str, str]:
    """Load distinct per-caller tokens and fail before the service starts.

    The returned mapping is identity-to-token. Error messages contain environment
    variable names and caller identities only, never credential values.
    """
    environment_names = tuple(environment_identities)
    require_production_environment(environment_names)

    missing = [name for name in environment_names if not os.environ.get(name, "").strip()]
    if missing and (required or is_production()):
        raise RuntimeConfigurationError(
            "internal service authentication is missing required values: "
            + ", ".join(sorted(missing))
        )

    credentials = {
        identity: os.environ[environment_name].strip()
        for environment_name, identity in environment_identities.items()
        if os.environ.get(environment_name, "").strip()
    }
    if len(set(credentials.values())) != len(credentials):
        identities = ", ".join(sorted(credentials))
        raise RuntimeConfigurationError(
            f"internal service credentials must be distinct for callers: {identities}"
        )
    return credentials


def internal_bearer_headers(token: str | None) -> dict[str, str]:
    """Build an internal Authorization header without logging the token."""
    return {"Authorization": f"Bearer {token}"} if token else {}


def reject_external_credential_reuse(
    credentials: Mapping[str, str], environment_names: tuple[str, ...]
) -> None:
    """Reject an internal token reused as an external-provider credential."""
    external_values = {
        value for name in environment_names if (value := os.environ.get(name, "").strip())
    }
    if set(credentials.values()) & external_values:
        names = ", ".join(sorted(environment_names))
        raise RuntimeConfigurationError(
            f"internal service credentials must not reuse external credentials: {names}"
        )


def authenticate_internal_bearer(
    authorization: str | None,
    credentials: Mapping[str, str],
) -> str | None:
    """Return the authenticated caller identity or ``None``.

    Compare every configured token so timing does not disclose which identity
    matched. Only the identity is returned for safe audit/metric attribution.
    """
    presented = ""
    if authorization:
        scheme, separator, value = authorization.partition(" ")
        if separator and scheme.lower() == "bearer":
            presented = value.strip()

    matched_identity: str | None = None
    for identity, expected in credentials.items():
        if secrets.compare_digest(presented, expected):
            matched_identity = identity
    return matched_identity
