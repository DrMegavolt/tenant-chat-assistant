from __future__ import annotations

import pytest

from runtime_security import (
    RuntimeConfigurationError,
    load_openai_compatible_settings,
    openai_request_headers,
    require_production_environment,
)

_RUNTIME_VARIABLES = (
    "APP_ENV",
    "DATABASE_URL",
    "ES_PASSWORD",
    "LLM_API_KEY",
    "LLM_BASE_URL",
    "LLM_MODEL",
    "LLM_TIMEOUT_SECONDS",
)


@pytest.fixture(autouse=True)
def clean_runtime_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _RUNTIME_VARIABLES:
        monkeypatch.delenv(name, raising=False)


def test_local_provider_may_be_unauthenticated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "local")

    settings = load_openai_compatible_settings(local_base_url="http://localhost:1234/v1")

    assert settings.base_url == "http://localhost:1234/v1"
    assert settings.api_key == ""
    assert openai_request_headers(settings.api_key) == {"Content-Type": "application/json"}


def test_production_names_every_missing_value_without_echoing_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DATABASE_URL", "REPLACE_WITH_DATABASE_URL")
    monkeypatch.setenv("ES_PASSWORD", "do-not-log-this-value")

    with pytest.raises(RuntimeConfigurationError) as caught:
        require_production_environment(("DATABASE_URL", "ES_PASSWORD", "LLM_API_KEY"))

    message = str(caught.value)
    assert "DATABASE_URL" in message
    assert "LLM_API_KEY" in message
    assert "ES_PASSWORD" not in message
    assert "do-not-log-this-value" not in message


def test_production_rejects_placeholder_embedded_in_database_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://tenantchat:REPLACE_WITH_GENERATED_POSTGRES_PASSWORD@postgres/tenantchat",
    )

    with pytest.raises(RuntimeConfigurationError, match="DATABASE_URL"):
        require_production_environment(("DATABASE_URL",))


def test_production_provider_requires_key_endpoint_and_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")

    with pytest.raises(RuntimeConfigurationError) as caught:
        load_openai_compatible_settings(local_base_url="http://localhost:1234/v1")

    assert str(caught.value).endswith("LLM_API_KEY, LLM_BASE_URL, LLM_MODEL")


def test_production_provider_builds_bearer_authentication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("LLM_BASE_URL", "https://provider.example/v1")
    monkeypatch.setenv("LLM_MODEL", "provider-model")
    monkeypatch.setenv("LLM_API_KEY", "test-only-key")
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "45")

    settings = load_openai_compatible_settings(local_base_url="")

    assert settings.timeout_seconds == 45
    assert openai_request_headers(settings.api_key) == {
        "Authorization": "Bearer test-only-key",
        "Content-Type": "application/json",
    }


@pytest.mark.parametrize("base_url", ["provider.example/v1", "file:///tmp/provider"])
def test_provider_url_must_be_absolute_http(monkeypatch: pytest.MonkeyPatch, base_url: str) -> None:
    monkeypatch.setenv("LLM_BASE_URL", base_url)

    with pytest.raises(RuntimeConfigurationError, match="LLM_BASE_URL"):
        load_openai_compatible_settings(local_base_url="")
