"""Visitor identity: the signed credential every visitor route requires.

The visitor surface (`POST /api/chat`, `GET /api/chat/session`,
`POST /api/chat/confirmation`) is SEC-002's territory. A caller is whoever its
credential says it is — the tenant and session are read from the verified
token, never from a request body, so a body field cannot move a conversation
between tenants and guessing a session UUID is useless without the token that
signs it. The credential is a bearer secret, so it travels in a request header
rather than a query string, where it could end up in an access log.

The dependency fails closed: a missing header and a malformed token produce the
same stable error, so a probing caller learns nothing about which half of the
check failed.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, Request

from tenantchat.core.errors import InvalidVisitorCredentialError
from tenantchat.core.visitor_session import (
    VisitorCredentialSigner,
    VisitorSessionClaims,
)

# Carries the bearer token. A header rather than a query parameter on purpose:
# query strings reach access logs and referrers, and the token is the
# credential.
VISITOR_CREDENTIAL_HEADER = "X-Visitor-Credential"


def get_visitor_signer(request: Request) -> VisitorCredentialSigner:
    """The signer the composition root built, which is the only one that
    verifies anything: a credential is accepted only if it was issued here."""
    signer: VisitorCredentialSigner = request.app.state.visitor_credential_signer
    return signer


def get_visitor_clock(request: Request) -> Callable[[], datetime]:
    """The composition root's clock, so tests can move time without a sleep."""
    clock: Callable[[], datetime] = request.app.state.clock
    return clock


def get_visitor_claims(request: Request) -> VisitorSessionClaims:
    """The identity a presented credential authenticates, or a 401.

    Raises:
        InvalidVisitorCredentialError: no credential, an unverifiable one, or a
            genuine but expired one — indistinguishably, so a caller cannot
            tell a forgery probe what to fix. The error code the client sees
            for a genuine expired token is `visitor_credential_expired`, which
            is stable and says the recovery is to start a fresh session.
    """
    token = request.headers.get(VISITOR_CREDENTIAL_HEADER)
    if token is None:
        raise InvalidVisitorCredentialError(detail="no visitor credential presented")
    return get_visitor_signer(request).verify(token, now=get_visitor_clock(request)())


def issue(
    signer: VisitorCredentialSigner,
    clock: Callable[[], datetime],
    ttl_seconds: int,
    *,
    tenant_id: str,
    session_id: uuid.UUID,
) -> str:
    """Mint a fresh credential token for the bound tenant and session.

    Every credentialed response reissues, so a conversation in active use never
    lets its credential expire and a leaked token is only valid for as long as
    the legitimate visitor keeps talking.
    """
    return signer.issue(tenant_id, session_id, now=clock(), ttl_seconds=ttl_seconds).token


VisitorIdentity = Annotated[VisitorSessionClaims, Depends(get_visitor_claims)]
VisitorSigner = Annotated[VisitorCredentialSigner, Depends(get_visitor_signer)]
VisitorClock = Annotated[Callable[[], datetime], Depends(get_visitor_clock)]


def utc_now() -> datetime:
    return datetime.now(UTC)
