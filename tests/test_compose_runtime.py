"""The single-host release keeps the Kubernetes runtime's critical ordering."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml  # type: ignore[import-untyped]

ROOT = Path(__file__).resolve().parents[1]


def _compose() -> dict[str, Any]:
    return cast(
        dict[str, Any],
        yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8")),
    )


def test_compose_app_profile_contains_the_complete_visitor_runtime() -> None:
    services = _compose()["services"]

    assert {
        "postgres",
        "elasticsearch",
        "embedding",
        "migrate",
        "api",
        "worker",
        "web",
    } <= services.keys()
    for name in ("embedding", "migrate", "api", "worker", "web"):
        assert "app" in services[name]["profiles"]


def test_compose_runs_both_migrations_before_api_and_worker() -> None:
    services = _compose()["services"]
    migrate = services["migrate"]

    assert migrate["build"]["dockerfile"] == "services/api/Dockerfile"
    command = "\n".join(migrate["command"])
    assert "alembic upgrade head" in command
    assert "scripts/setup_checkpoints.py" in command
    for name in ("api", "worker"):
        assert services[name]["depends_on"]["migrate"]["condition"] == (
            "service_completed_successfully"
        )


def test_compose_uses_service_dns_and_shared_knowledge_storage() -> None:
    services = _compose()["services"]
    api_environment = services["api"]["environment"]
    worker_environment = services["worker"]["environment"]

    assert "@postgres:5432/" in api_environment["DATABASE_URL"]
    assert api_environment["ELASTICSEARCH_URL"] == "http://elasticsearch:9200"
    assert api_environment["EMBEDDING_URL"] == "http://embedding:8001"
    assert worker_environment["ELASTICSEARCH_URL"] == "http://elasticsearch:9200"
    assert worker_environment["EMBEDDING_URL"] == "http://embedding:8001"
    assert {
        "ADMIN_GATEWAY_TOKEN",
        "ADMIN_CSRF_SECRET",
        "CHAT_API_VISITOR_CREDENTIAL_SIGNING_KEY",
        "CHAT_TO_EMBEDDING_TOKEN",
        "LLM_API_KEY",
    }.isdisjoint(worker_environment)
    assert "knowledge-storage:/data/knowledge" in services["api"]["volumes"]
    assert "knowledge-storage:/data/knowledge" in services["worker"]["volumes"]


def test_api_image_seeds_non_root_ownership_for_a_fresh_upload_volume() -> None:
    dockerfile = (ROOT / "services/api/Dockerfile").read_text(encoding="utf-8")

    assert "install -d -o 10001 -g 10001 /data/knowledge" in dockerfile


def test_makefile_exposes_compose_release_and_verification_commands() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    assert "compose-up: ## Build and start the complete local visitor demo" in makefile
    assert "compose-smoke: ## Verify the running Compose application" in makefile
    assert "compose-seed: ## Load governed demo knowledge into the Compose application" in makefile
