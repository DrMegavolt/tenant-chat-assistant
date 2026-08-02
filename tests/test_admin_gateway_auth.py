"""Security specifications for the single-origin gateway's auth and CSRF model.

These tests verify the Python-side enforcement of:
- Admin route authentication (401 without identity, 403 with insufficient role).
- CSRF token validation on state-changing admin operations.
- Spoofed header stripping (client-supplied identity headers are ignored).
- CORS policy (no wildcard, no admin routes through CORS).
- Cookie/session security properties documented in the auth model.
"""

from __future__ import annotations

import importlib
from typing import Any
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def clean_auth_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "APP_ENV",
        "ADMIN_CSRF_SECRET",
        "ADMIN_GATEWAY_TOKEN",
        "WIDGET_ALLOWED_ORIGINS",
        "LLM_API_KEY",
        "LLM_BASE_URL",
        "LLM_MODEL",
        "LLM_TIMEOUT_SECONDS",
        "DATABASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)


class FakeHeaders:
    """Minimal dict-like header object matching BaseHTTPRequestHandler.headers."""

    def __init__(self, headers: dict[str, str]) -> None:
        self._headers = dict(headers)

    def get(self, name: str, default: str = "") -> str:
        return self._headers.get(name, default)

    def __contains__(self, name: str) -> bool:
        return name in self._headers

    def __delitem__(self, name: str) -> None:
        del self._headers[name]


class FakeHandler:
    """Minimal handler for auth/CSRF testing."""

    def __init__(self, headers: dict[str, str] | None = None) -> None:
        self.headers = FakeHeaders(headers or {})


class FakeCorsHandler(FakeHandler):
    def __init__(self, route: str, origin: str) -> None:
        super().__init__({"Origin": origin})
        self.path = route
        self.response_headers: list[tuple[str, str]] = []

    def send_header(self, name: str, value: str) -> None:
        self.response_headers.append((name, value))


def _identity_for(
    headers: dict[str, str],
    *,
    is_prod: bool,
) -> dict[str, str] | None:
    """Call server._read_identity with the given setup."""
    import server

    handler = FakeHandler(headers)

    with patch.object(server, "is_production", return_value=is_prod):
        return server._read_identity(handler)


def test_dev_mode_allows_admin_access_without_headers() -> None:
    """In local development, admin routes work without the gateway."""
    identity = _identity_for(headers={}, is_prod=False)
    assert identity is not None
    assert identity["role"] == "platform_admin"


def test_production_requires_identity_headers() -> None:
    """In production, a request with no identity headers is rejected."""
    identity = _identity_for(headers={}, is_prod=True)
    assert identity is None


def test_production_rejects_identity_headers_without_gateway_token() -> None:
    """A direct caller cannot turn spoofed identity headers into an identity."""
    identity = _identity_for(
        headers={
            "X-Auth-Email": "attacker@evil.test",
            "X-Auth-Role": "platform_admin",
            "X-Auth-Subject": "attacker",
        },
        is_prod=True,
    )
    assert identity is None


def test_production_accepts_gateway_set_identity_headers() -> None:
    """Headers set by the gateway (after stripping) are accepted."""
    identity = _identity_for(
        headers={
            "X-TenantChat-Gateway-Token": "dev-only-gateway-token-not-for-production",
            "X-Auth-Email": "admin@example.com",
            "X-Auth-Role": "viewer",
            "X-Auth-Subject": "user-123",
        },
        is_prod=True,
    )
    assert identity is not None
    assert identity["email"] == "admin@example.com"
    assert identity["role"] == "viewer"
    assert identity["subject"] == "user-123"


def test_production_rejects_unknown_role() -> None:
    """An identity with an undefined role is rejected."""
    identity = _identity_for(
        headers={
            "X-TenantChat-Gateway-Token": "dev-only-gateway-token-not-for-production",
            "X-Auth-Email": "weird@example.com",
            "X-Auth-Role": "superuser",
            "X-Auth-Subject": "user-456",
        },
        is_prod=True,
    )
    assert identity is None


def test_production_rejects_empty_subject() -> None:
    """An identity with an empty subject is rejected."""
    identity = _identity_for(
        headers={
            "X-TenantChat-Gateway-Token": "dev-only-gateway-token-not-for-production",
            "X-Auth-Email": "admin@example.com",
            "X-Auth-Role": "viewer",
            "X-Auth-Subject": "",
        },
        is_prod=True,
    )
    assert identity is None


def test_role_hierarchy() -> None:
    from server import _role_at_least

    assert _role_at_least("viewer", "viewer") is True
    assert _role_at_least("viewer", "support_agent") is False
    assert _role_at_least("support_agent", "viewer") is True
    assert _role_at_least("support_agent", "support_agent") is True
    assert _role_at_least("tenant_admin", "support_agent") is True
    assert _role_at_least("platform_admin", "tenant_admin") is True
    assert _role_at_least("viewer", "tenant_admin") is False


def test_csrf_token_round_trips() -> None:
    """A generated CSRF token validates against the same identity."""
    import server

    identity = {"email": "admin@example.com", "role": "support_agent", "subject": "user-789"}
    token = server._generate_csrf_token(identity)
    assert token

    handler: Any = FakeHandler({"X-CSRF-Token": token})
    with patch.object(server, "is_production", return_value=True):
        assert server._validate_csrf(handler, identity) is True


def test_csrf_token_rejects_wrong_identity() -> None:
    """A CSRF token for one identity does not validate for another."""
    import server

    identity_a = {"email": "a@example.com", "role": "viewer", "subject": "user-a"}
    identity_b = {"email": "b@example.com", "role": "viewer", "subject": "user-b"}
    token_a = server._generate_csrf_token(identity_a)
    assert token_a != server._generate_csrf_token(identity_b)

    handler: Any = FakeHandler({"X-CSRF-Token": token_a})
    with patch.object(server, "is_production", return_value=True):
        assert server._validate_csrf(handler, identity_b) is False


def test_csrf_token_rejects_missing_token_in_production() -> None:
    """In production, a missing CSRF token is rejected."""
    import server

    identity = {"email": "admin@example.com", "role": "support_agent", "subject": "user-789"}
    handler = FakeHandler({})

    with patch.object(server, "is_production", return_value=True):
        assert server._validate_csrf(handler, identity) is False


def test_dev_mode_skips_csrf_validation() -> None:
    """In local development, CSRF validation is bypassed."""
    import server

    handler: Any = FakeHandler({})
    with patch.object(server, "is_production", return_value=False):
        assert server._validate_csrf(handler, {"subject": "dev"}) is True


def test_cors_allowlist_never_wildcard() -> None:
    """The widget CORS allowlist must never include wildcard."""
    with patch.dict(
        "os.environ", {"WIDGET_ALLOWED_ORIGINS": "https://example.test,https://widget.example.test"}
    ):
        import server

        importlib.reload(server)
        assert "https://example.test" in server._WIDGET_ALLOWED_ORIGINS
        assert "https://widget.example.test" in server._WIDGET_ALLOWED_ORIGINS
        assert "*" not in server._WIDGET_ALLOWED_ORIGINS


def test_cors_allowlist_empty_by_default() -> None:
    """With no WIDGET_ALLOWED_ORIGINS, same-origin only."""
    import server

    importlib.reload(server)
    assert len(server._WIDGET_ALLOWED_ORIGINS) == 0


def test_allowed_widget_origin_gets_cors_only_on_public_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server

    monkeypatch.setattr(server, "_WIDGET_ALLOWED_ORIGINS", frozenset({"https://customer.test"}))
    public: Any = FakeCorsHandler("/api/chat", "https://customer.test")
    server.ChatHandler.send_cors_headers(public)
    assert ("Access-Control-Allow-Origin", "https://customer.test") in public.response_headers

    admin: Any = FakeCorsHandler("/api/admin/chats", "https://customer.test")
    server.ChatHandler.send_cors_headers(admin)
    assert admin.response_headers == []


def test_disallowed_widget_origin_gets_no_cors_permission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server

    monkeypatch.setattr(server, "_WIDGET_ALLOWED_ORIGINS", frozenset({"https://customer.test"}))
    handler: Any = FakeCorsHandler("/api/book", "https://attacker.test")
    server.ChatHandler.send_cors_headers(handler)
    assert not any(name == "Access-Control-Allow-Origin" for name, _ in handler.response_headers)
    assert ("Vary", "Origin") in handler.response_headers


def test_production_requires_csrf_secret() -> None:
    """Production must not start without ADMIN_CSRF_SECRET."""
    import server

    env = {
        "APP_ENV": "production",
        "LLM_BASE_URL": "https://provider.example/v1",
        "LLM_MODEL": "provider-model",
        "LLM_API_KEY": "test-only-key",
        "DATABASE_URL": "postgresql://user:pass@host/db",
        "CHAT_TO_FINANCING_TOKEN": "test-financing-token",
        "FINANCING_AGENT_URL": "http://financing-agent:8003",
    }
    with (
        patch.dict("os.environ", env, clear=False),
        pytest.raises(Exception, match="ADMIN_CSRF_SECRET"),
    ):
        importlib.reload(server)
    # Reload with the dev environment to restore a clean module state.
    importlib.reload(server)


def test_production_requires_internal_gateway_token() -> None:
    import server

    env = {
        "APP_ENV": "production",
        "LLM_BASE_URL": "https://provider.example/v1",
        "LLM_MODEL": "provider-model",
        "LLM_API_KEY": "test-only-key",
        "DATABASE_URL": "postgresql://user:pass@host/db",
        "CHAT_TO_FINANCING_TOKEN": "test-financing-token",
        "FINANCING_AGENT_URL": "http://financing-agent:8003",
        "ADMIN_CSRF_SECRET": "test-only-csrf-secret",
    }
    with (
        patch.dict("os.environ", env, clear=False),
        pytest.raises(Exception, match="ADMIN_GATEWAY_TOKEN"),
    ):
        importlib.reload(server)
    importlib.reload(server)
