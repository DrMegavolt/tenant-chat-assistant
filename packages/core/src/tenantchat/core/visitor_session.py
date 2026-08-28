"""Signed visitor credentials: the only identity a conversation accepts.

`SEC-002` makes the unguessable session ID a *credential*: a self-contained,
server-signed token that names exactly one tenant and one session and carries
its own expiry. Nothing else may authorize a visitor request — the tenant and
session are read from the verified token, never from a request body — so a
body field cannot move a conversation between tenants, and guessing a session
UUID is useless without the token that signs it.

The format is deliberately simple and versioned. A token is

``tc.v1.<base64url(payload)>.<base64url(hmac_sha256(payload))>``

where the payload is a compact JSON object of ``v`` (format version), ``tenant``,
``session``, ``iat``, and ``exp`` epoch seconds. The signature covers the
base64url payload bytes verbatim, so there is no ambiguity about what was
signed. Verification is stateless: no per-token storage, no database read, so a
deployment of any size verifies a token with one HMAC.

Expiry is per-credential and enforced at verification. A visitor whose token
expires mid-conversation is told so with a stable error and can open a fresh
session; the conversation row itself is untouched, so nothing is lost
server-side (`PRIV-001` owns server-side retention, not this module).

Rotating the signing key invalidates every outstanding token at once: the
widget's recovery path — open a new session when a token is refused — is the
rotation story, which is exactly the blast radius a shared key rotation should
have.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from tenantchat.core.errors import (
    ExpiredVisitorCredentialError,
    InvalidVisitorCredentialError,
    VisitorCredentialRejection,
)

# The wire prefix of every credential this server issues, fixed at format v1.
_CREDENTIAL_PREFIX = "tc.v1."
_SIGNING_KEY_MIN_BYTES = 32
# The tenant slug shape the API already publishes for request bodies; a token
# signed for a tenant outside it could never be routed, so it is rejected here
# rather than failing later with a surprising error.
_TENANT_PATTERN = re.compile(r"\A[a-z0-9][a-z0-9-]{0,62}\Z")
_PAYLOAD_VERSION = 1
# The exact claims a version-1 payload may carry. Rejected strictly: the payload
# is signed, so any other shape is a bug in an issuer, not a caller, and a bug
# should fail loudly instead of being ignored.
_PAYLOAD_KEYS = frozenset({"v", "tenant", "session", "iat", "exp"})


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64url(encoded: str) -> bytes:
    padding = "=" * (-len(encoded) % 4)
    return base64.urlsafe_b64decode(encoded + padding)


@dataclass(frozen=True, slots=True)
class VisitorSessionClaims:
    """The identity a verified credential authenticates.

    ``expires_at`` is when the credential stops being usable, not when the
    conversation is deleted. The conversation outlives every credential that
    names it; `PRIV-001` decides how long the record itself lives.
    """

    tenant_id: str
    session_id: uuid.UUID
    issued_at: datetime
    expires_at: datetime

    def __str__(self) -> str:
        return f"session {self.session_id} in tenant {self.tenant_id}"

    def __repr__(self) -> str:
        return (
            f"VisitorSessionClaims(tenant_id={self.tenant_id!r}, session_id={self.session_id!r}, "
            f"expires_at={self.expires_at.isoformat()!r})"
        )


@dataclass(frozen=True, slots=True)
class VisitorCredential:
    """A signed token together with the claims it authenticates.

    The token is a bearer secret: anyone holding it can act as the session. Its
    string forms therefore redact it — the token is passed around as an opaque
    value and must not reach a log line through an f-string.
    """

    token: str
    claims: VisitorSessionClaims

    def __str__(self) -> str:
        # The token is the credential; only the claims are publishable.
        return str(self.claims)

    def __repr__(self) -> str:
        return f"VisitorCredential(claims={self.claims!r}, token=<redacted>)"


class VisitorCredentialSigner(Protocol):
    """Mints and verifies visitor credentials.

    The one public seam `SEC-003` (per-session rate limits) and `PRIV-001`
    (customer export and deletion) build on: verify a presented token to learn
    which tenant and session the caller is, without touching the database.
    """

    def issue(
        self,
        tenant_id: str,
        session_id: uuid.UUID,
        *,
        now: datetime,
        ttl_seconds: int,
    ) -> VisitorCredential:
        """Mint a credential bound to one tenant and session.

        Raises:
            ValueError: the tenant or session cannot be bound.
        """
        ...

    def verify(self, token: str, *, now: datetime) -> VisitorSessionClaims:
        """Return the claims of a token the server signed.

        Raises:
            InvalidVisitorCredentialError: the token is not one this server
                signed, or is structurally malformed. The refusal is identical
                to the visitor whichever check failed — ``detail`` stays empty
                — because the reason is a forgery probe's free intelligence.
                Operators read the bounded ``reason`` on the error instead.
            ExpiredVisitorCredentialError: the signature is valid and the
                credential is past its expiry.
        """
        ...


class HmacVisitorCredentialSigner:
    """Signs credentials with HMAC-SHA256 under one deployment-wide key.

    The key is a system secret: rotating it invalidates every outstanding
    credential at once, which is the supported way to kill sessions fleet-wide
    before their expiry.
    """

    def __init__(self, key: str) -> None:
        encoded = key.encode("utf-8")
        if len(encoded) < _SIGNING_KEY_MIN_BYTES:
            raise ValueError(
                f"the visitor token signing key must be at least {_SIGNING_KEY_MIN_BYTES} bytes"
            )
        self._key = encoded

    def issue(
        self,
        tenant_id: str,
        session_id: uuid.UUID,
        *,
        now: datetime,
        ttl_seconds: int,
    ) -> VisitorCredential:
        if not _TENANT_PATTERN.fullmatch(tenant_id):
            raise ValueError(f"cannot sign a credential for tenant {tenant_id!r}")
        if ttl_seconds <= 0:
            raise ValueError(f"credential TTL must be positive, got {ttl_seconds}")
        issued_at = int(now.astimezone(UTC).timestamp())
        claims = VisitorSessionClaims(
            tenant_id=tenant_id,
            session_id=session_id,
            issued_at=datetime.fromtimestamp(issued_at, tz=UTC),
            expires_at=datetime.fromtimestamp(issued_at + ttl_seconds, tz=UTC),
        )
        payload = json.dumps(
            {
                "v": _PAYLOAD_VERSION,
                "tenant": tenant_id,
                "session": str(session_id),
                "iat": issued_at,
                "exp": issued_at + ttl_seconds,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        encoded = _b64url(payload.encode("utf-8"))
        signature = self._signature(encoded)
        return VisitorCredential(token=f"{_CREDENTIAL_PREFIX}{encoded}.{signature}", claims=claims)

    def verify(self, token: str, *, now: datetime) -> VisitorSessionClaims:
        try:
            encoded, signature = _split(token)
        except ValueError:
            raise InvalidVisitorCredentialError(VisitorCredentialRejection.MALFORMED) from None
        if not hmac.compare_digest(self._signature(encoded), signature):
            raise InvalidVisitorCredentialError(VisitorCredentialRejection.BAD_SIGNATURE)

        try:
            payload = json.loads(_unb64url(encoded))
            version = payload["v"]
            tenant_id = payload["tenant"]
            session_id = uuid.UUID(payload["session"])
            issued_at = int(payload["iat"])
            expires_at = int(payload["exp"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise InvalidVisitorCredentialError(
                VisitorCredentialRejection.UNUSABLE_PAYLOAD
            ) from error

        if (
            version != _PAYLOAD_VERSION
            or set(payload) != _PAYLOAD_KEYS
            or not _TENANT_PATTERN.fullmatch(tenant_id)
            or expires_at <= issued_at
        ):
            raise InvalidVisitorCredentialError(VisitorCredentialRejection.CLAIMS_REJECTED)

        issued = datetime.fromtimestamp(issued_at, tz=UTC)
        expires = datetime.fromtimestamp(expires_at, tz=UTC)
        # Genuine-but-old is a different, recoverable failure from a forgery:
        # the class's own message carries it, and a prober holding an expired
        # token already knew its signature was valid.
        if expires <= now.astimezone(UTC):
            raise ExpiredVisitorCredentialError(detail="visitor credential past expiry")
        return VisitorSessionClaims(
            tenant_id=tenant_id,
            session_id=session_id,
            issued_at=issued,
            expires_at=expires,
        )

    def _signature(self, encoded_payload: str) -> str:
        digest = hmac.new(self._key, encoded_payload.encode("ascii"), hashlib.sha256).digest()
        return _b64url(digest)


def _split(token: str) -> tuple[str, str]:
    """The payload and signature halves of a token, structurally checked.

    Raises:
        ValueError: the token is not the ``tc.v1.<payload>.<signature>`` shape.
    """
    # Both halves are base64url, so a non-ASCII character cannot occur in a
    # token this server signed. It is rejected here because the two operations
    # downstream — `str.encode("ascii")` and `hmac.compare_digest` — both raise
    # on non-ASCII input, which would turn a malformed header into a 500 on an
    # unauthenticated route rather than the stable refusal this module promises.
    if not token.isascii():
        raise ValueError("credential carries non-ASCII characters")
    if not token.startswith(_CREDENTIAL_PREFIX):
        raise ValueError("wrong credential prefix")
    rest = token[len(_CREDENTIAL_PREFIX) :]
    encoded, separator, signature = rest.rpartition(".")
    if not separator or not encoded or not signature:
        raise ValueError("credential missing payload or signature")
    return encoded, signature
