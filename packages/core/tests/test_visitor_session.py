"""Visitor credential signing: bound identity, tamper-proof, redacted.

The behaviour under test is the SEC-002 contract: a credential is only as good
as the server's signature, an expired credential is a *different, recoverable*
failure from a forged one, and no printable form of the credential carries the
token itself.
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from tenantchat.core.errors import (
    ExpiredVisitorCredentialError,
    InvalidVisitorCredentialError,
)
from tenantchat.core.visitor_session import HmacVisitorCredentialSigner, VisitorCredential

NOW = datetime(2026, 8, 4, 12, 0, 0, tzinfo=UTC)
SIGNING_KEY = "visitor-test-secret-" + "x" * 32
SESSION_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")


def signer(key: str = SIGNING_KEY) -> HmacVisitorCredentialSigner:
    return HmacVisitorCredentialSigner(key)


def issue_credential(
    *, tenant_id: str = "clearview", ttl: int = 3600
) -> tuple[HmacVisitorCredentialSigner, VisitorCredential]:
    issuer = signer()
    credential = issuer.issue(tenant_id, SESSION_ID, now=NOW, ttl_seconds=ttl)
    return issuer, credential


def _reencode(payload: dict[str, object], template: str) -> str:
    """Re-encode a payload under the *old* signature of ``template``.

    The result is a token whose signature does not cover its claims, which is
    exactly the shape a forger can produce without the key.
    """
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    prefix, _, signature = template.partition(".")
    return f"{prefix}.{encoded}.{signature}"


class TestIssueAndVerifyRoundTrip:
    def test_issued_credentials_verify_to_their_own_claims(self) -> None:
        issuer, credential = issue_credential()

        assert issuer.verify(credential.token, now=NOW + timedelta(minutes=5)) == credential.claims

    def test_claims_carry_the_bound_identity_and_expiry(self) -> None:
        _, credential = issue_credential(ttl=1800)

        assert credential.claims.tenant_id == "clearview"
        assert credential.claims.session_id == SESSION_ID
        assert credential.claims.expires_at == NOW + timedelta(seconds=1800)

    def test_naive_clock_input_is_normalized_to_utc(self) -> None:
        issuer = signer()
        credential = issuer.issue(
            "clearview", SESSION_ID, now=NOW.replace(tzinfo=None), ttl_seconds=60
        )

        assert issuer.verify(credential.token, now=NOW.replace(tzinfo=None)) == credential.claims


class TestExpiry:
    def test_token_verifies_through_the_second_before_expiry(self) -> None:
        issuer, credential = issue_credential()

        assert (
            issuer.verify(credential.token, now=credential.claims.expires_at - timedelta(seconds=1))
            == credential.claims
        )

    def test_expired_token_is_a_recoverable_state_not_a_forgery(self) -> None:
        """`exp` names the instant the credential stops working; at or after it
        the failure is expiry, which clients recover from by starting over."""
        issuer, credential = issue_credential()

        for moment in (
            credential.claims.expires_at,
            credential.claims.expires_at + timedelta(seconds=1),
        ):
            with pytest.raises(ExpiredVisitorCredentialError):
                issuer.verify(credential.token, now=moment)

    def test_rejection_never_teaches_a_caller_what_to_fix(self) -> None:
        """A rejected token must not disclose why (forgery probe feedback)."""
        issuer, credential = issue_credential()

        with pytest.raises(InvalidVisitorCredentialError) as excinfo:
            issuer.verify(credential.token[:-2] + "aa", now=NOW)
        assert "signature" not in str(excinfo.value)


class TestTamperAndForgery:
    def test_swapped_tenant_is_rejected(self) -> None:
        """The whole hijack class: a session bound to one tenant cannot be
        re-bound to another by editing the token."""
        issuer, credential = issue_credential()
        forged = _reencode(
            {
                "v": 1,
                "tenant": "apex",
                "session": str(SESSION_ID),
                "iat": int(NOW.timestamp()),
                "exp": int(NOW.timestamp()) + 3600,
            },
            credential.token,
        )

        with pytest.raises(InvalidVisitorCredentialError):
            issuer.verify(forged, now=NOW + timedelta(seconds=30))

    def test_swapped_session_is_rejected(self) -> None:
        issuer, credential = issue_credential()
        forged = _reencode(
            {"v": 1, "tenant": "clearview", "session": str(uuid.uuid4())},
            credential.token,
        )

        with pytest.raises(InvalidVisitorCredentialError):
            issuer.verify(forged, now=NOW + timedelta(seconds=30))

    def test_wrong_signing_key_is_rejected(self) -> None:
        _, credential = issue_credential()
        other = signer(SIGNING_KEY + "-other")

        with pytest.raises(InvalidVisitorCredentialError):
            other.verify(credential.token, now=NOW)

    def test_flipped_signature_byte_is_rejected(self) -> None:
        issuer, credential = issue_credential()
        flipped = "A" if credential.token[-1] == "B" else "B"

        with pytest.raises(InvalidVisitorCredentialError):
            issuer.verify(credential.token[:-1] + flipped, now=NOW)

    def test_unknown_claim_version_is_rejected(self) -> None:
        """Forward-compatible claims must not be accepted under the old format."""
        issuer, credential = issue_credential()
        forged = _reencode(
            {"v": 2, "tenant": "clearview", "session": str(SESSION_ID)}, credential.token
        )

        with pytest.raises(InvalidVisitorCredentialError):
            issuer.verify(forged, now=NOW + timedelta(seconds=30))

    def test_extra_claims_are_rejected(self) -> None:
        issuer, credential = issue_credential()
        forged = _reencode(
            {
                "v": 1,
                "tenant": "clearview",
                "session": str(SESSION_ID),
                "iat": int(NOW.timestamp()),
                "exp": int(NOW.timestamp()) + 3600,
                "sub": "admin",
            },
            credential.token,
        )

        with pytest.raises(InvalidVisitorCredentialError):
            issuer.verify(forged, now=NOW + timedelta(seconds=30))

    def test_garbage_and_wrong_prefix_are_rejected(self) -> None:
        issuer = signer()
        for garbage in ("", "plain-text", "tc.v2." + "x" * 40, "tc.v1." + "x" * 40):
            with pytest.raises(InvalidVisitorCredentialError):
                issuer.verify(garbage, now=NOW)

    def test_payload_that_is_not_json_is_rejected(self) -> None:
        issuer, credential = issue_credential()
        not_json = credential.token.rpartition(".")[0] + ".AA"

        with pytest.raises(InvalidVisitorCredentialError):
            issuer.verify(not_json, now=NOW)

    def test_expiry_preceding_issue_is_rejected_as_invalid(self) -> None:
        issuer, credential = issue_credential()
        forged = _reencode(
            {
                "v": 1,
                "tenant": "clearview",
                "session": str(SESSION_ID),
                "iat": int(NOW.timestamp()) + 3600,
                "exp": int(NOW.timestamp()),
            },
            credential.token,
        )

        with pytest.raises(InvalidVisitorCredentialError):
            issuer.verify(forged, now=NOW)


class TestIssueGuardrails:
    @pytest.mark.parametrize(
        "tenant",
        ("Clearview", "clear view", "", "clearview_extra", "a" * 64, "über"),
    )
    def test_tenant_outside_the_slug_shape_cannot_be_signed(self, tenant: str) -> None:
        issuer = signer()

        with pytest.raises(ValueError, match="tenant"):
            issuer.issue(tenant, SESSION_ID, now=NOW, ttl_seconds=3600)

    def test_short_signing_key_is_refused_at_construction(self) -> None:
        """A guessable key would make every token in the fleet forgeable."""
        with pytest.raises(ValueError, match="at least 32 bytes"):
            HmacVisitorCredentialSigner("short-key")

    def test_non_positive_ttl_is_refused(self) -> None:
        issuer = signer()

        with pytest.raises(ValueError, match="TTL"):
            issuer.issue("clearview", SESSION_ID, now=NOW, ttl_seconds=0)


class TestSecretsStayOutOfPrintables:
    def test_credential_str_contains_no_token(self) -> None:
        _, credential = issue_credential()

        assert credential.token not in str(credential)

    def test_credential_repr_redacts_the_token(self) -> None:
        _, credential = issue_credential()

        assert "<redacted>" in repr(credential)
        assert credential.token not in repr(credential)

    def test_claims_str_names_only_the_bound_identity(self) -> None:
        _, credential = issue_credential()

        assert str(credential.claims) == f"session {SESSION_ID} in tenant clearview"
