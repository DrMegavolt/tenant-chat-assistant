"""Operator identity, as it arrives from the authenticating gateway.

The API never speaks OIDC. The gateway completes the login, and nginx forwards
what oauth2-proxy established as three headers, alongside a shared token that
authenticates the hop itself. Both halves are required: the identity headers are
trivially forgeable by anything that can reach the port directly, so without the
token they authenticate nothing.

The service re-checks the role rather than trusting the gateway's routing. A
proxy rule is a deployment artifact that can be edited, reordered, or bypassed by
a second ingress; the role check here travels with the route it protects.

Tenant-scoped authorization — which tenants *this* operator may read — is
`SEC-001` and is not implemented. Every authenticated operator can currently read
every tenant's conversations, which is why the routes using this module are not
exposed to the public internet.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from fastapi import Request

from tenantchat.api.faults import CsrfValidationError, ForbiddenError, UnauthenticatedError
from tenantchat.api.settings import Settings

GATEWAY_TOKEN_HEADER: Final = "X-TenantChat-Gateway-Token"  # noqa: S105 - a header name
EMAIL_HEADER: Final = "X-Auth-Email"
ROLE_HEADER: Final = "X-Auth-Role"
SUBJECT_HEADER: Final = "X-Auth-Subject"
CSRF_HEADER: Final = "X-CSRF-Token"

# Ordered by privilege; each role holds everything the roles before it hold.
ROLES: Final[tuple[str, ...]] = ("viewer", "support_agent", "tenant_admin", "platform_admin")

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AdminIdentity:
    """One authenticated operator.

    ``subject`` is the identity provider's pseudonymous ID and is what audit
    records should carry. ``email`` is personal data: it is reachable as an
    attribute for a record that is meant to hold it, and never appears in this
    object's string forms, so an f-string in a log statement cannot leak one.
    """

    subject: str
    email: str
    role: str

    def holds(self, minimum: str) -> bool:
        """Whether this role is at least *minimum* in the privilege order."""
        return ROLES.index(self.role) >= ROLES.index(minimum)

    def __str__(self) -> str:
        return f"operator {self.subject} ({self.role})"

    def __repr__(self) -> str:
        return f"AdminIdentity(subject={self.subject!r}, role={self.role!r})"


def authenticate(request: Request, settings: Settings) -> AdminIdentity:
    """Read the operator identity the gateway established.

    Raises:
        UnauthenticatedError: the deployment has no gateway token configured,
            the presented token did not match, an identity header was missing,
            or the role is not one this service defines.
    """
    expected = settings.admin_gateway_token
    presented = request.headers.get(GATEWAY_TOKEN_HEADER, "").strip()
    # An unconfigured deployment fails closed. The alternative — treating an
    # absent token as "no gateway in front of us, so trust the headers" — is the
    # configuration mistake that turns the admin API into an open one.
    if not expected or not hmac.compare_digest(presented, expected):
        raise UnauthenticatedError

    subject = request.headers.get(SUBJECT_HEADER, "").strip()
    email = request.headers.get(EMAIL_HEADER, "").strip()
    role = request.headers.get(ROLE_HEADER, "").strip()
    if not subject or not email or role not in ROLES:
        raise UnauthenticatedError

    return AdminIdentity(subject=subject, email=email, role=role)


def require_role(minimum: str) -> Callable[[Request], AdminIdentity]:
    """Build a dependency admitting operators at or above *minimum*.

    Raises:
        ValueError: at import time, if *minimum* is not a defined role. A typo
            in a route's requirement would otherwise widen it silently.
    """
    if minimum not in ROLES:
        raise ValueError(f"{minimum!r} is not one of {ROLES}")

    def dependency(request: Request) -> AdminIdentity:
        settings: Settings = request.app.state.settings
        identity = authenticate(request, settings)
        if not identity.holds(minimum):
            logger.warning(
                "admin authorization refused",
                extra={
                    "subject": identity.subject,
                    "role": identity.role,
                    "required_role": minimum,
                    "path": request.url.path,
                },
            )
            raise ForbiddenError
        return identity

    return dependency


def csrf_token(identity: AdminIdentity, settings: Settings) -> str:
    """Mint the double-submit token for one operator.

    Raises:
        CsrfValidationError: no CSRF secret is configured, so no token this
            service would accept can be produced.
    """
    secret = settings.admin_csrf_secret
    if not secret:
        raise CsrfValidationError
    return hmac.new(
        secret.encode("utf-8"), identity.subject.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def verify_csrf(request: Request, identity: AdminIdentity, settings: Settings) -> None:
    """Check the double-submit token on a state-changing admin request.

    The gateway's session cookie is ambient browser credential material, so a
    cross-origin page can cause an authenticated request to be sent even though
    it cannot read the response. This token is the second defence: it must be
    read from a previous response, which the same-origin policy prevents.

    Raises:
        CsrfValidationError: the token is absent, malformed, or not the one this
            operator's subject derives.
    """
    presented = request.headers.get(CSRF_HEADER, "").strip()
    if not presented or not hmac.compare_digest(presented, csrf_token(identity, settings)):
        raise CsrfValidationError
