"""Operator identity and tenant-scoped authorization (SEC-001).

The API never speaks OIDC. The gateway completes the login, and nginx forwards
what oauth2-proxy established as three headers, alongside a shared token that
authenticates the hop itself. Both halves are required: the identity headers are
trivially forgeable by anything that can reach the port directly, so without the
token they authenticate nothing.

The service re-checks the role rather than trusting the gateway's routing. A
proxy rule is a deployment artifact that can be edited, reordered, or bypassed by
a second ingress; the role check here travels with the route it protects.

Tenant scoping is a second authority on top of the gateway's. The gateway maps
provider groups to one coarse directory role; the membership table (SEC-001)
decides what each operator may do *inside* a tenant. The effective role for a
tenant is the tighter of the two, so a membership row can narrow an operator's
access but never widen it beyond their directory role — the identity provider
remains the privilege ceiling. `platform_admin` is the exception: it spans every
tenant by definition and is granted only by the directory role, never by a
membership row.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Annotated, Final

from fastapi import Depends, Request

from tenantchat.api.dependencies import (
    get_audit_store,
    get_membership_store,
    get_trace_access_store,
)
from tenantchat.api.faults import (
    CsrfValidationError,
    ForbiddenError,
    TenantAccessDeniedError,
    UnauthenticatedError,
)
from tenantchat.api.registry import SYSTEM_TENANT_ID, TenantRegistry
from tenantchat.api.settings import Settings
from tenantchat.api.store import (
    AuditActorType,
    AuditEvent,
    AuditStore,
    MembershipStore,
    TraceAccessStore,
)
from tenantchat.core.errors import NotFoundError

GATEWAY_TOKEN_HEADER: Final = "X-TenantChat-Gateway-Token"  # noqa: S105 - a header name
EMAIL_HEADER: Final = "X-Auth-Email"
ROLE_HEADER: Final = "X-Auth-Role"
SUBJECT_HEADER: Final = "X-Auth-Subject"
CSRF_HEADER: Final = "X-CSRF-Token"

# How long a minted CSRF token may be presented. Long enough for an operator
# tab that fetched the token once at load and never reloads; short enough that
# a token copied out of a response cannot be replayed forever.
CSRF_TOKEN_TTL: Final = timedelta(hours=12)
# A minting clock slightly ahead of the verifying one must not fail.
CSRF_CLOCK_SKEW: Final = 60

# Ordered by privilege; each role holds everything the roles before it hold.
# `platform_admin` is the one role that is also a tenant in its own right: it
# grants access to every tenant, which no per-tenant membership row can.
ROLES: Final[tuple[str, ...]] = ("viewer", "support_agent", "tenant_admin", "platform_admin")

# Roles a membership row may grant. `platform_admin` is deliberately absent:
# it spans tenants and is decided by the provider directory, so a tenant-scoped
# record can never confer it (see the migration's CHECK constraint).
TENANT_ROLES: Final[tuple[str, ...]] = ("viewer", "support_agent", "tenant_admin")

# PRIV-002: the dedicated role for reading turn records. Not a directory role
# and not part of the ordered transcript hierarchy — an operator holds it as a
# tenant-scoped grant (`trace_access_grants`), and neither a transcript role
# nor a membership row confers it. It names the role in refusal audits.
TRACE_READER_ROLE: Final = "trace_viewer"

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AdminIdentity:
    """One authenticated operator.

    ``subject`` is the identity provider's pseudonymous ID and is what audit
    records should carry. ``email`` is personal data: it is reachable as an
    attribute for a record that is meant to hold it, and never appears in this
    object's string forms, so an f-string in a log statement cannot leak one.
    ``role`` is the directory role the gateway mapped from provider groups; the
    per-tenant role is resolved per request from the membership store.
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


def _constant_time_equals(presented: str, expected: str) -> bool:
    """Compare two presented secrets without timing or encoding leaks.

    ``hmac.compare_digest`` raises ``TypeError`` on strings holding non-ASCII
    codepoints, and HTTP headers decode as latin-1 — so a header carrying a
    raw byte ≥ 0x80 would turn a 401 into a 500. Comparing the UTF-8 bytes
    keeps the same equality (equal strings encode equally) and never raises;
    an encoding failure is contained as "did not match".
    """
    try:
        return hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))
    except UnicodeEncodeError:
        return False


def authenticate(request: Request, settings: Settings) -> AdminIdentity:
    """Read the operator identity the gateway established.

    With ``settings.dev_auth`` the gateway token is not required — the explicit
    local-development mode trusts the identity headers directly. The production
    composition refuses to start in that mode against a non-loopback database,
    so a deployment cannot accidentally run it.

    Raises:
        UnauthenticatedError: the deployment has no gateway token configured,
            the presented token did not match, or an identity header was
            missing.
        ForbiddenError: the gateway authenticated the operator, but no
            application role (or an unknown one) reached the service.
    """
    expected = settings.admin_gateway_token
    presented = request.headers.get(GATEWAY_TOKEN_HEADER, "").strip()
    # An unconfigured deployment fails closed. The alternative — treating an
    # absent token as "no gateway in front of us, so trust the headers" — is the
    # configuration mistake that turns the admin API into an open one.
    gateway_ok = False
    if expected is not None:
        gateway_ok = _constant_time_equals(presented, expected)
    if not gateway_ok and not settings.dev_auth:
        raise UnauthenticatedError

    subject = request.headers.get(SUBJECT_HEADER, "").strip()
    email = request.headers.get(EMAIL_HEADER, "").strip()
    role = request.headers.get(ROLE_HEADER, "").strip()
    if not subject or not email:
        raise UnauthenticatedError
    if role not in ROLES:
        logger.warning(
            "admin identity has no recognized role",
            extra={"subject": subject, "path": request.url.path},
        )
        raise ForbiddenError

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


def effective_role(identity: AdminIdentity, membership_role: str | None) -> str | None:
    """The role an operator actually holds inside one tenant.

    ``platform_admin`` is granted by the directory and spans every tenant. Any
    other operator is bound by the tighter of their directory role and their
    membership row, so an assignment can restrict access but never grant more
    than the identity provider allows.
    """
    if identity.role == "platform_admin":
        return "platform_admin"
    if membership_role is None:
        return None
    return min((identity.role, membership_role), key=ROLES.index)


async def authorize_tenant_access(
    identity: AdminIdentity,
    memberships: MembershipStore,
    tenant_id: str,
    *,
    minimum: str,
    path: str,
) -> AdminIdentity:
    """Refuse a tenant-scoped operation this operator may not perform.

    The membership check happens before anything else touches the tenant: no
    tenant record is looked up, so the refusal is identical whether the tenant
    exists or not, and the detail names no tenant ID.

    Raises:
        ForbiddenError: the operator's effective role is below *minimum*.
        TenantAccessDeniedError: no membership row grants access to this tenant.
    """
    membership_role = await memberships.role_for(tenant_id, identity.subject)
    effective = effective_role(identity, membership_role)
    if effective is None:
        logger.warning(
            "tenant-scoped access refused",
            extra={"subject": identity.subject, "role": identity.role, "path": path},
        )
        raise TenantAccessDeniedError
    if ROLES.index(effective) < ROLES.index(minimum):
        logger.warning(
            "tenant-scoped authorization refused",
            extra={
                "subject": identity.subject,
                "role": identity.role,
                "effective_role": effective,
                "required_role": minimum,
                "path": path,
            },
        )
        raise ForbiddenError
    return identity


def tenant_scoped(
    minimum: str,
) -> Callable[
    [Request, Annotated[MembershipStore, Depends(get_membership_store)]], Awaitable[AdminIdentity]
]:
    """Build a dependency for GET-style routes carrying ``tenant_id`` in the query.

    Raises:
        ValueError: at import time, if *minimum* is not a defined role.
    """
    if minimum not in ROLES:
        raise ValueError(f"{minimum!r} is not one of {ROLES}")

    async def dependency(
        request: Request, memberships: Annotated[MembershipStore, Depends(get_membership_store)]
    ) -> AdminIdentity:
        settings: Settings = request.app.state.settings
        identity = authenticate(request, settings)
        tenant_id = request.query_params.get("tenant_id", "")
        await authorize_tenant_access(
            identity, memberships, tenant_id, minimum=minimum, path=request.url.path
        )
        return identity

    return dependency


def _csrf_digest(secret: str, subject: str, issued_at: int) -> str:
    message = f"{subject}:{issued_at}".encode()
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def csrf_token(identity: AdminIdentity, settings: Settings) -> str:
    """Mint the double-submit token for one operator.

    The token binds the operator's subject to the minting instant and expires
    after :data:`CSRF_TOKEN_TTL`, so each fetch rotates it and a token copied
    from an old response stops working. It still authorizes nothing alone: a
    write also demands the role and the tenant membership.

    Raises:
        CsrfValidationError: no CSRF secret is configured, so no token this
            service would accept can be produced.
    """
    secret = settings.admin_csrf_secret
    if not secret:
        raise CsrfValidationError
    issued_at = int(time.time())
    return f"{issued_at}.{_csrf_digest(secret, identity.subject, issued_at)}"


def verify_csrf(request: Request, identity: AdminIdentity, settings: Settings) -> None:
    """Check the double-submit token on a state-changing admin request.

    The gateway's session cookie is ambient browser credential material, so a
    cross-origin page can cause an authenticated request to be sent even though
    it cannot read the response. This token is the second defence: it must be
    read from a previous response, which the same-origin policy prevents.

    Raises:
        CsrfValidationError: the token is absent, malformed, expired, or not
            the one this operator's subject derives for its minting instant.
    """
    presented = request.headers.get(CSRF_HEADER, "").strip()
    secret = settings.admin_csrf_secret
    if not presented or not secret:
        raise CsrfValidationError
    issued_at_text, separator, digest = presented.partition(".")
    if not separator or not issued_at_text.isdigit() or not digest:
        raise CsrfValidationError
    issued_at = int(issued_at_text)
    now = int(time.time())
    expired = now - issued_at > int(CSRF_TOKEN_TTL.total_seconds())
    not_yet_valid = issued_at - now > CSRF_CLOCK_SKEW
    if expired or not_yet_valid:
        raise CsrfValidationError
    expected = _csrf_digest(secret, identity.subject, issued_at)
    if not _constant_time_equals(digest, expected):
        raise CsrfValidationError


def _refusal_audit_tenant(request: Request, tenant_id: str) -> str:
    """The tenant an access-refusal audit row can be written under.

    ``audit_events`` is foreign-keyed to ``tenants``, so a refusal naming an
    empty or unknown tenant id cannot be recorded verbatim. A tenant this
    deployment actually serves is recorded under its real id; anything else is
    recorded under the bootstrapped system tenant, which stands in for "no
    tenant". The raw id never lands in the row either way, so the refusal
    stays unusable for tenant enumeration.
    """
    registry: TenantRegistry = request.app.state.registry
    try:
        registry.get(tenant_id)
    except NotFoundError:
        return SYSTEM_TENANT_ID
    return tenant_id


def require_trace_read() -> Callable[..., Awaitable[AdminIdentity]]:
    """Build the dependency gating every turn-record read.

    The gate is the dedicated `PRIV-002` role — a tenant-scoped grant — or the
    ``platform_admin`` ceiling. The ordered transcript hierarchy is irrelevant
    here on purpose: a tenant admin with no trace grant is refused exactly like
    a viewer is. The refusal is itself audited, so an operator cannot probe the
    trace plane silently, and the detail names no tenant, so the refusal is
    identical whether the tenant exists or not.

    Raises:
        UnauthenticatedError: no usable operator identity.
        ForbiddenError: the operator holds no trace-read grant for the tenant.
    """

    async def dependency(
        request: Request,
        grants: Annotated[TraceAccessStore, Depends(get_trace_access_store)],
        audit: Annotated[AuditStore, Depends(get_audit_store)],
    ) -> AdminIdentity:
        settings: Settings = request.app.state.settings
        identity = authenticate(request, settings)
        tenant_id = request.query_params.get("tenant_id", "")
        if identity.role == "platform_admin" or await grants.has_access(
            tenant_id, identity.subject
        ):
            return identity
        await audit.record(
            AuditEvent(
                tenant_id=_refusal_audit_tenant(request, tenant_id),
                actor_type=AuditActorType.STAFF,
                principal_id=identity.subject,
                action="trace.read_refused",
                resource_type="turn_record",
                resource_id=None,
                request_id=getattr(request.state, "request_id", None),
                details={
                    "reason": request.query_params.get("reason", ""),
                    "required_role": TRACE_READER_ROLE,
                },
            )
        )
        logger.warning(
            "trace read refused",
            extra={
                "subject": identity.subject,
                "role": identity.role,
                "path": request.url.path,
            },
        )
        raise ForbiddenError

    return dependency
