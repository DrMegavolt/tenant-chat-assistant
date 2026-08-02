from __future__ import annotations

import pytest

from internal_auth import (
    authenticate_internal_bearer,
    internal_bearer_headers,
    load_internal_credentials,
    reject_external_credential_reuse,
)
from runtime_security import RuntimeConfigurationError


@pytest.fixture(autouse=True)
def clean_internal_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("APP_ENV", "CALLER_A_TOKEN", "CALLER_B_TOKEN", "LLM_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def test_local_standalone_can_disable_internal_dependencies() -> None:
    assert (
        load_internal_credentials({"CALLER_A_TOKEN": "caller-a", "CALLER_B_TOKEN": "caller-b"})
        == {}
    )
    assert internal_bearer_headers(None) == {}


def test_configured_local_dependency_requires_credential() -> None:
    with pytest.raises(RuntimeConfigurationError, match="CALLER_A_TOKEN"):
        load_internal_credentials({"CALLER_A_TOKEN": "caller-a"}, required=True)


def test_production_missing_credentials_fail_without_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("CALLER_A_TOKEN", "never-log-this")

    with pytest.raises(RuntimeConfigurationError) as caught:
        load_internal_credentials({"CALLER_A_TOKEN": "caller-a", "CALLER_B_TOKEN": "caller-b"})

    assert "CALLER_B_TOKEN" in str(caught.value)
    assert "CALLER_A_TOKEN" not in str(caught.value)
    assert "never-log-this" not in str(caught.value)


def test_internal_callers_must_have_distinct_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("CALLER_A_TOKEN", "same-test-token")
    monkeypatch.setenv("CALLER_B_TOKEN", "same-test-token")

    with pytest.raises(RuntimeConfigurationError, match="must be distinct"):
        load_internal_credentials({"CALLER_A_TOKEN": "caller-a", "CALLER_B_TOKEN": "caller-b"})


def test_internal_token_cannot_reuse_external_provider_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_API_KEY", "shared-test-value")

    with pytest.raises(RuntimeConfigurationError, match="LLM_API_KEY"):
        reject_external_credential_reuse({"caller-a": "shared-test-value"}, ("LLM_API_KEY",))


def test_bearer_authentication_returns_only_the_caller_identity() -> None:
    credentials = {"caller-a": "test-token-a", "caller-b": "test-token-b"}

    assert authenticate_internal_bearer("Bearer test-token-b", credentials) == "caller-b"
    assert authenticate_internal_bearer("Basic test-token-b", credentials) is None
    assert authenticate_internal_bearer("Bearer wrong-token", credentials) is None
    assert internal_bearer_headers("test-token-a") == {"Authorization": "Bearer test-token-a"}
