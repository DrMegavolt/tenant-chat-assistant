"""Pure-ASGI request guards: body, rate/concurrency, and response size.

Starlette's ``BaseHTTPMiddleware`` reads the request body through its own
receive wrapper, which makes "read the body once, bound it, and let the router
still parse it" impossible to express. These guards are plain ASGI apps so they
see the receive channel directly: the body guard caps what is buffered, replays
the exact bytes downstream, and parks a copy for the rate guard to key on —
one read, one bound, no double parsing.

Refusals are RFC 9457 problem documents like every other error the API emits,
with the same request ID, so a visitor's "it said too many requests" can be
matched to the log line. Rate refusals add a ``Retry-After`` that is at most
the window length, which is the bounded retry guidance `SEC-003` requires.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable
from contextlib import suppress
from typing import Any

from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from tenantchat.api.limits import (
    REQUEST_BODY_STATE,
    RateLimitPolicy,
    RateLimitStore,
    VisitorIdentityExtractor,
)
from tenantchat.api.problems import transport_problem

logger = logging.getLogger(__name__)

RETRY_AFTER_HEADER = "Retry-After"

_BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})


async def _refuse(scope: Scope, send: Send, response: Any) -> None:
    # The response never reads the request channel, so no receive is needed.
    with suppress(Exception):
        # The connection may already be gone; nothing sensible to do but stop.
        await response(scope, None, send)


def _request_id(scope: Scope) -> str:
    state = scope.setdefault("state", {})
    request_id = state.get("request_id", "-")
    return request_id if isinstance(request_id, str) else "-"


class BodySizeLimitMiddleware:
    """Reject oversized bodies before the router reads them, chunked or not.

    A declared ``Content-Length`` over the cap fails before a byte is read. A
    chunked upload declares no length and slips past that check, so the same
    cap is enforced here from a receive-channel counter; without it an
    unauthenticated caller could buffer megabytes against the process. The
    buffered bytes are replayed downstream unchanged and parked in
    ``scope["state"][REQUEST_BODY_STATE]`` for the rate-limit guard.
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self._max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") not in _BODY_METHODS:
            await self.app(scope, receive, send)
            return

        declared = next(
            (int(value) for key, value in scope["headers"] if key == b"content-length"),
            0,
        )
        if declared > self._max_bytes:
            await _refuse(scope, send, self._too_large(scope))
            return

        buffered = bytearray()
        over_limit = False
        while True:
            message = await receive()
            if message["type"] != "http.request":
                continue
            chunk = message.get("body", b"")
            if len(buffered) + len(chunk) > self._max_bytes:
                # Stop reading: the response goes out while the client is still
                # sending, and the server closes the connection rather than
                # reuse it over an unread body.
                over_limit = True
                break
            buffered.extend(chunk)
            if not message.get("more_body", False):
                break
        if over_limit:
            await _refuse(scope, send, self._too_large(scope))
            return

        state = scope.setdefault("state", {})
        state[REQUEST_BODY_STATE] = bytes(buffered)

        replayed = False

        async def replay_receive() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": bytes(buffered), "more_body": False}
            # The channel is exhausted; a second read can only mean the app is
            # about to wait for a body that is already fully delivered.
            return {"type": "http.disconnect"}

        await self.app(scope, replay_receive, send)

    def _too_large(self, scope: Scope) -> Any:
        return transport_problem(
            status=413,
            code="request_too_large",
            title="RequestTooLarge",
            detail="The request body was larger than this endpoint accepts.",
            request_id=_request_id(scope),
            maxBytes=self._max_bytes,
        )


class RateLimitMiddleware:
    """Per-identity rate and concurrency budgets for every non-probe route.

    Concurrency is counted before the rate store is consulted: an in-flight
    request is a resource the process is already spending, so it must be
    bounded even when the shared store is unavailable. The rate store failing
    open is deliberate — the counter is protection, not authorization, and a
    database blip must not take the widget's branding down with it — but the
    gap is logged once per window so an outage is visible without a log storm.

    Denials are logged by scope and request ID only. The key itself — an IP, a
    tenant, a session — is exactly the value that makes a log line useful to
    whoever reads it, and the invariant is that an accidental log consumer
    cannot learn who was throttled.
    """

    def __init__(
        self,
        app: ASGIApp,
        policy: RateLimitPolicy,
        store: RateLimitStore,
        identity: VisitorIdentityExtractor,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.app = app
        self._policy = policy
        self._store = store
        self._identity = identity
        self._now = now
        # In-flight counters are per process by design: the bound protects this
        # node's workers, and a shared counter would let one slow node throttle
        # the fleet. The event loop is single-threaded, so no lock is needed.
        self._in_flight: dict[str, int] = {}
        self._last_store_failure_logged: float = 0.0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") == "/healthz":
            await self.app(scope, receive, send)
            return

        identity = self._identity(Request(scope, receive))
        now = self._now()
        budgets = identity.budgets(self._policy)

        for key_scope, key, _rate, concurrency in budgets:
            storage_key = f"{key_scope}:{key}"
            if self._in_flight.get(storage_key, 0) >= concurrency:
                await self._refuse(scope, send, key_scope, now, retry_after_seconds=1)
                return
        for key_scope, key, _rate, _concurrency in budgets:
            storage_key = f"{key_scope}:{key}"
            self._in_flight[storage_key] = self._in_flight.get(storage_key, 0) + 1

        window = int(now // self._policy.window_seconds)
        try:
            for key_scope, key, rate, _concurrency in budgets:
                storage_key = f"{key_scope}:{key}"
                try:
                    count = await self._store.hit(storage_key, window)
                except Exception:
                    self._note_store_failure(scope, now)
                    continue
                if count > rate:
                    retry_after = math.ceil((window + 1) * self._policy.window_seconds - now)
                    await self._refuse(scope, send, key_scope, now, retry_after_seconds=retry_after)
                    return
            await self.app(scope, receive, send)
        finally:
            for key_scope, key, _rate, _concurrency in budgets:
                storage_key = f"{key_scope}:{key}"
                remaining = self._in_flight.get(storage_key, 0) - 1
                if remaining > 0:
                    self._in_flight[storage_key] = remaining
                else:
                    self._in_flight.pop(storage_key, None)

    async def _refuse(
        self,
        scope: Scope,
        send: Send,
        key_scope: str,
        now: float,
        *,
        retry_after_seconds: int,
    ) -> None:
        logger.warning(
            "request rate-limited",
            extra={
                "scope": key_scope,
                "request_id": _request_id(scope),
                "path": scope.get("path"),
            },
        )
        response = transport_problem(
            status=429,
            code="rate_limited",
            title="TooManyRequests",
            detail="This identity sent more requests than this API allows.",
            request_id=_request_id(scope),
            limitScope=key_scope,
            retryAfterSeconds=retry_after_seconds,
        )
        response.headers[RETRY_AFTER_HEADER] = str(retry_after_seconds)
        await _refuse(scope, send, response)

    def _note_store_failure(self, scope: Scope, now: float) -> None:
        if now - self._last_store_failure_logged < self._policy.window_seconds:
            return
        self._last_store_failure_logged = now
        logger.warning(
            "rate-limit store unavailable; limits fail open",
            extra={"request_id": _request_id(scope)},
        )


class ResponseSizeLimitMiddleware:
    """Refuse a response whose body exceeds the cap.

    The response's ``http.response.start`` is held until the body finishes, so
    a refusal can still replace the status before anything was sent; no
    endpoint in this service streams, and the cap bounds how much is buffered
    either way. A response this large means a data-level bound was missed, so
    the refusal is a hard ``413`` rather than a truncation a client would
    parse as a complete answer.
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self._max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        start: Message | None = None
        buffered = bytearray()
        exceeded = False
        started = False

        async def send_wrapper(message: Message) -> None:
            nonlocal start, buffered, exceeded, started
            if exceeded:
                # Everything after the overflow is swallowed; the refusal is
                # emitted once the inner app has finished.
                return
            if message["type"] == "http.response.start":
                start = message
                return
            if message["type"] == "http.response.body":
                if start is None:
                    await send(message)
                    return
                buffered.extend(message.get("body", b""))
                if len(buffered) > self._max_bytes:
                    exceeded = True
                    return
                if not message.get("more_body", False):
                    started = True
                    await send(start)
                    await send(message)
                return
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            if exceeded and not started:
                await _refuse(scope, send, self._too_large(scope))

    def _too_large(self, scope: Scope) -> Any:
        return transport_problem(
            status=413,
            code="response_too_large",
            title="ResponseTooLarge",
            detail="The response this request produced exceeds what this API serves.",
            request_id=_request_id(scope),
            maxBytes=self._max_bytes,
        )


_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    # Every API response is request-specific — transcripts, availability, turn
    # answers. A shared cache is where a cross-tenant leak starts, and these
    # responses carry PII, so no store of any kind is allowed.
    "Cache-Control": "no-store",
}


class SecurityHeadersMiddleware:
    """Pin the response header posture for every request, early refusals included.

    The API answers JSON only and is never embedded in a page, so the policies
    here are the conservative defaults: no MIME sniffing, no framing, no
    referrer, no caching. Framing and referrer matter at the gateway, which
    serves the widget page, so they are pinned here as well to keep a future
    HTML endpoint from inheriting a weaker default by accident.
    """

    def __init__(self, app: ASGIApp, *, headers: dict[str, str] | None = None) -> None:
        self._app = app
        self._headers = dict(headers or _SECURITY_HEADERS)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        original_send = send

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                extra_headers = [
                    (key.lower().encode("latin-1"), value.encode("latin-1"))
                    for key, value in self._headers.items()
                ]
                message = {
                    **message,
                    "headers": [*message.get("headers", []), *extra_headers],
                }
            await original_send(message)

        await self._app(scope, receive, send_wrapper)
