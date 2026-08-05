"""Request correlation: server-issued IDs, the tenant pseudonym, and their context.

`ADR-0010` lets the operational plane carry identifiers and no content. These
IDs are the identifiers: one request ID and one trace ID per HTTP request,
both minted by the server (an ID supplied by an unauthenticated client can be
repeated or forged, which makes it useless for correlation), echoed back on
the response, and carried by the log records the request produces.

The IDs live in a ``contextvars`` context so any code that runs under the
request — the router, the agent runtime, a tool — logs the same correlation
fields without passing them through every call. Background work binds its own
context from its durable record (`job_worker` does this from the job payload),
and a service calling another service forwards the IDs through
:func:`correlation_headers`.

The tenant field is a pseudonym, not the tenant ID: a stable, bounded digest
that cannot be reversed when a deployment key is configured. The digest of
``tenant_id`` is the same in every service that shares the key, so a chat turn
and the background job it enqueues name the same tenant without any log line
carrying a raw tenant ID.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Final

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from tenantchat.api.problems import REQUEST_ID_HEADER

TRACE_ID_HEADER: Final = "X-Trace-Id"

logger = logging.getLogger(__name__)

# The bound on a tenant pseudonym, asserted by a test: a `t-` prefix plus the
# first 16 hex characters of the digest, so it can never exceed a few dozen
# bytes no matter how the tenant ID is spelled.
_PSEUDONYM_PREFIX: Final = "t-"
_PSEUDONYM_HEX_LENGTH: Final = 16

_context: ContextVar[CorrelationContext | None] = ContextVar(
    "tenantchat_correlation_context", default=None
)


@dataclass(frozen=True, slots=True)
class CorrelationContext:
    """The correlation fields one unit of work carries.

    ``tenant_id`` is memory-only: it is what the pseudonym derives from, and
    only the pseudonym may reach a log line. ``request_id`` and ``trace_id``
    are minted by the caller of :func:`bind`.
    """

    request_id: str
    trace_id: str
    tenant_id: str | None = None
    tenant_pseudonym: str | None = None


def tenant_pseudonym(tenant_id: str, *, key: str | None) -> str:
    """A stable, bounded pseudonym for a tenant ID.

    With a key, the pseudonym is an HMAC and cannot be reversed. Without one —
    the development fallback — it is a plain digest, which is enough to keep a
    tenant ID out of the log plane but not enough to hide it from someone who
    guesses the tenant set.

    Raises:
        ValueError: the tenant ID cannot be pseudonymized.
    """
    if not tenant_id:
        raise ValueError("cannot pseudonymize an empty tenant ID")
    material = tenant_id.encode("utf-8")
    if key:
        digest = hmac.new(key.encode("utf-8"), material, hashlib.sha256).digest()
    else:
        digest = hashlib.sha256(material).digest()
    return f"{_PSEUDONYM_PREFIX}{digest[: _PSEUDONYM_HEX_LENGTH // 2].hex()}"


def bind(context: CorrelationContext) -> None:
    """Make *context* the correlation context of this task."""
    _context.set(context)


def bind_tenant(tenant_id: str, *, key: str | None) -> None:
    """Attach the tenant to the current context once its identity is known.

    The tenant is not part of the HTTP request until a verified credential or
    gateway identity names it, so it is bound where verification happens, not
    in the middleware.
    """
    current_context = _context.get()
    if current_context is None:
        return
    _context.set(
        replace(
            current_context,
            tenant_id=tenant_id,
            tenant_pseudonym=tenant_pseudonym(tenant_id, key=key),
        )
    )


def reset() -> None:
    """Clear the correlation context when the unit of work finishes."""
    _context.set(None)


def current() -> CorrelationContext | None:
    return _context.get()


def trace_id() -> str | None:
    current_context = _context.get()
    return None if current_context is None else current_context.trace_id


def context_extra() -> dict[str, str]:
    """The correlation fields for a ``logging`` ``extra`` dict.

    Merged at log sites that run outside a request (the job worker), so the
    record is self-contained even for a handler that is not correlation-aware.
    """
    current_context = _context.get()
    if current_context is None:
        return {}
    extra: dict[str, str] = {
        "request_id": current_context.request_id,
        "trace_id": current_context.trace_id,
    }
    if current_context.tenant_pseudonym is not None:
        extra["tenant"] = current_context.tenant_pseudonym
    return extra


def correlation_headers() -> dict[str, str]:
    """The headers to attach to an outbound internal-service call.

    This is the propagation contract the walkthrough documents: the callee
    reads the same header names and binds them into its own context, so one
    trace crosses service boundaries.
    """
    current_context = _context.get()
    if current_context is None:
        return {}
    return {
        REQUEST_ID_HEADER: current_context.request_id,
        TRACE_ID_HEADER: current_context.trace_id,
    }


class CorrelationMiddleware:
    """Mint per-request IDs, bind them for the request, and emit an access line.

    A pure-ASGI middleware rather than ``BaseHTTPMiddleware`` because the
    contextvar must be visible to the endpoint: ``call_next`` runs the
    application in another task whose copied context cannot be written back,
    so the tenant binding inside a dependency would be lost by the time the
    response returns. In the same task, the binding survives.

    The access line is off by default: every request would otherwise emit one
    record, and volume is a configuration decision (`CHAT_API_LOG_ACCESS`).
    """

    def __init__(self, app: ASGIApp, *, log_access: bool) -> None:
        self.app = app
        self._log_access = log_access

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = uuid.uuid4().hex
        trace_id = uuid.uuid4().hex
        state = scope.setdefault("state", {})
        state["request_id"] = request_id
        state["trace_id"] = trace_id
        bind(CorrelationContext(request_id=request_id, trace_id=trace_id))

        started = time.monotonic()
        status_code: int | None = None
        sent_start = False

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code, sent_start
            if message["type"] == "http.response.start" and not sent_start:
                sent_start = True
                status_code = int(message.get("status", 0))
                headers = list(message.get("headers", []))
                names = {name.decode("latin-1").lower() for name, _value in headers}
                for name, value in (
                    (REQUEST_ID_HEADER, request_id),
                    (TRACE_ID_HEADER, trace_id),
                ):
                    if name.lower() not in names:
                        headers.append((name.encode("latin-1"), value.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            if self._log_access:
                logger.info(
                    "request completed",
                    extra={
                        **context_extra(),
                        "method": scope.get("method"),
                        "path": scope.get("path"),
                        "status": status_code,
                        "duration_ms": round((time.monotonic() - started) * 1000, 3),
                    },
                )
            reset()
