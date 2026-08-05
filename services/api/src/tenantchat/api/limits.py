"""Rate, concurrency, and identity-key definitions for the SEC-003 guards.

Budgets are fixed windows: one counter per key per window, reset when the clock
crosses a window boundary. No background job, and the same shape for an
in-memory map and a shared table, so a deployment can choose its correctness
model without changing the middleware.

The identity extraction is the SEC-002 seam. The middleware keys its budgets on
the ip / tenant / session triple a :class:`VisitorIdentityExtractor` returns;
at HEAD the session key is the request's ``session_id`` value, and SEC-002
replaces the extractor with one that reads its signed visitor credential. Keys
never reach logs or the store's history: a denial is recorded by scope and
request ID only, and the Postgres table is swept to the current window on every
hit, so a key lives at most two minutes.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from typing import Protocol

from fastapi import Request

from tenantchat.api.visitor import VISITOR_CREDENTIAL_HEADER
from tenantchat.core.errors import DomainError
from tenantchat.core.visitor_session import VisitorCredentialSigner

logger = logging.getLogger(__name__)

# Where the body-limit guard parks the exact bytes it replayed downstream, so
# the rate-limit guard can key on them without reading the channel again.
REQUEST_BODY_STATE: str = "tenantchat_request_body"

# Key length caps independent of the schema: the body's field bounds are not
# in force when the identity is extracted, and a forged 60 KB tenant_id must
# not become a 60 KB primary key.
_TENANT_KEY_MAX = 64
_SESSION_KEY_MAX = 128


@dataclass(frozen=True, slots=True)
class RateLimitPolicy:
    """Budgets for one deployment; every value is per window or per process.

    ``window_seconds`` applies to all three rate scopes. Concurrency budgets
    bound in-flight requests per key in one process: a shared rate counter
    protects the fleet, a per-process in-flight counter protects the node, and
    the two are different kinds of bound with different defaults on purpose.
    """

    ip_requests: int = 600
    ip_concurrency: int = 20
    tenant_requests: int = 3000
    tenant_concurrency: int = 100
    session_requests: int = 60
    session_concurrency: int = 5
    window_seconds: int = 60

    def __post_init__(self) -> None:
        for field in fields(self):
            value = getattr(self, field.name)
            if value < 1:
                raise ValueError(f"{field.name} must be positive, got {value}")


@dataclass(frozen=True, slots=True)
class RequestIdentity:
    """The keys one request's budgets are counted against.

    ``tenant_id`` and ``session_key`` are ``None`` when the request does not
    carry that key, which drops the corresponding budgets rather than mixing
    every anonymous request into one shared tenant counter.
    """

    ip: str
    tenant_id: str | None = None
    session_key: str | None = None

    def budgets(self, policy: RateLimitPolicy) -> tuple[tuple[str, str, int, int], ...]:
        """The (scope, key, rate, concurrency) budget for each present key."""
        budgets: list[tuple[str, str, int, int]] = [
            ("ip", self.ip, policy.ip_requests, policy.ip_concurrency)
        ]
        if self.tenant_id is not None:
            budgets.append(
                (
                    "tenant",
                    self.tenant_id[:_TENANT_KEY_MAX],
                    policy.tenant_requests,
                    policy.tenant_concurrency,
                )
            )
        if self.session_key is not None:
            budgets.append(
                (
                    "session",
                    self.session_key[:_SESSION_KEY_MAX],
                    policy.session_requests,
                    policy.session_concurrency,
                )
            )
        return tuple(budgets)


class VisitorIdentityExtractor(Protocol):
    """SEC-002 seam: maps one request to the identities its budgets key on."""

    def __call__(self, request: Request) -> RequestIdentity: ...


_AVAILABILITY_PATH = re.compile(r"^/api/tenants/([a-z0-9][a-z0-9-]{0,63})/availability$")
# The routes that still name a tenant or session in their body. The credentialed
# chat routes are absent on purpose: since SEC-002 their identity is the signed
# header, and `credential_visitor_identity` reads it before consulting these.
_TENANT_BODY_PATHS = frozenset({"/api/chat/session", "/api/book", "/api/leads"})
_SESSION_BODY_PATHS = frozenset({"/api/book", "/api/leads"})


def default_visitor_identity(request: Request) -> RequestIdentity:
    """The visitor identity of a request that carries no signed credential.

    The uncredentialed surface: opening a session, and the direct booking and
    lead endpoints whose ``session_id`` is a caller-supplied correlation label.
    Those labels are client-chosen, so their session budget is a best-effort
    bound and the per-IP budget is the one an attacker cannot rotate away from.

    Body keys are read from the cached copy the body-limit guard stored, never
    from the receive channel, so extraction costs nothing and consumes nothing.
    """
    client = request.client
    ip = client.host if client is not None else "unknown"
    path = request.url.path

    match = _AVAILABILITY_PATH.fullmatch(path)
    if match is not None:
        return RequestIdentity(ip=ip, tenant_id=match.group(1))

    if path not in _TENANT_BODY_PATHS:
        return RequestIdentity(ip=ip)

    body = getattr(request.state, REQUEST_BODY_STATE, None)
    if not isinstance(body, bytes) or not body:
        return RequestIdentity(ip=ip)
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return RequestIdentity(ip=ip)
    if not isinstance(payload, dict):
        return RequestIdentity(ip=ip)

    tenant_value = payload.get("tenant_id")
    tenant = tenant_value if isinstance(tenant_value, str) and tenant_value else None
    session = None
    if path in _SESSION_BODY_PATHS:
        session_value = payload.get("session_id")
        session = session_value if isinstance(session_value, str) and session_value else None
    return RequestIdentity(ip=ip, tenant_id=tenant, session_key=session)


def credential_visitor_identity(signer: VisitorCredentialSigner) -> VisitorIdentityExtractor:
    """Key budgets on the signed visitor credential, falling back to the body.

    This is what makes the tenant and session budgets binding rather than
    advisory: the keys come from a token only this server can sign, so a caller
    cannot dodge a session budget by editing a field, nor spend another
    tenant's budget by naming it. A request whose credential is absent,
    unverifiable, or expired is keyed by :func:`default_visitor_identity` — the
    route will refuse it anyway, and it must still cost its sender an IP budget.
    """

    def extract(request: Request) -> RequestIdentity:
        token = request.headers.get(VISITOR_CREDENTIAL_HEADER)
        if token is None:
            return default_visitor_identity(request)
        clock = getattr(request.app.state, "clock", None)
        now = clock() if callable(clock) else datetime.now(UTC)
        try:
            claims = signer.verify(token, now=now)
        except DomainError:
            return default_visitor_identity(request)
        client = request.client
        return RequestIdentity(
            ip=client.host if client is not None else "unknown",
            tenant_id=claims.tenant_id,
            session_key=str(claims.session_id),
        )

    return extract


class RateLimitStore(Protocol):
    """Bounded, multi-process-safe accounting for one fixed window.

    ``hit`` records one request against ``key`` in ``window`` and returns the
    window's running count, so the caller compares against its budget. The
    accounting must be atomic per key: two processes hitting the same key in
    the same window must each see the other's increments.
    """

    async def hit(self, key: str, window: int) -> int: ...


class InMemoryRateLimitStore:
    """One process's counters, bounded by entry count and window turnover.

    The fallback for hermetic tests and single-process development: correct
    within one event loop, wrong once a second worker shares the traffic,
    which is exactly what the production Postgres store exists for. Old-window
    entries are replaced on first contact rather than swept, so the map holds
    at most one live entry per key plus whatever fit before the cap evicted it.
    """

    def __init__(self, max_keys: int = 10_000) -> None:
        self._max_keys = max_keys
        # key -> (window, count); insertion order doubles as eviction order.
        self._counters: dict[str, tuple[int, int]] = {}

    async def hit(self, key: str, window: int) -> int:
        entry = self._counters.get(key)
        if entry is None or entry[0] != window:
            if entry is not None:
                del self._counters[key]
            if len(self._counters) >= self._max_keys:
                # Evict the oldest-inserted entry. Dropping a hot key resets
                # its count, which is the acceptable price of a hard memory bound.
                self._counters.pop(next(iter(self._counters)))
            self._counters[key] = (window, 1)
            return 1
        self._counters[key] = (window, entry[1] + 1)
        return entry[1] + 1
