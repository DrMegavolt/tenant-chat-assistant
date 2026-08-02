from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

ROOT = Path(__file__).resolve().parents[1]


def load_documents(path: str) -> list[dict[str, Any]]:
    return [
        document
        for document in yaml.safe_load_all((ROOT / path).read_text(encoding="utf-8"))
        if document
    ]


def resource(documents: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    return next(
        document
        for document in documents
        if document["kind"] == kind and document["metadata"]["name"] == name
    )


def container_env(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    template = document["spec"]["template"]
    container = template["spec"]["containers"][0]
    return {entry["name"]: entry for entry in container.get("env", [])}


def test_namespace_is_default_deny_for_ingress_and_egress() -> None:
    policies = load_documents("k8s/network-policies.yaml")
    deny = resource(policies, "NetworkPolicy", "default-deny-all")

    assert deny["spec"]["podSelector"] == {}
    assert set(deny["spec"]["policyTypes"]) == {"Ingress", "Egress"}
    assert "ingress" not in deny["spec"]
    assert "egress" not in deny["spec"]


def test_every_required_flow_has_a_named_allow_policy() -> None:
    policies = load_documents("k8s/network-policies.yaml")
    names = {document["metadata"]["name"] for document in policies}

    assert {
        "allow-dns-egress",
        "allow-public-ingress-to-web",
        "allow-web-to-chat",
        "allow-web-to-oauth2-proxy",
        "allow-web-egress",
        "allow-prometheus-chat-metrics",
        "allow-prometheus-embedding-metrics",
        "allow-prometheus-ingestion-metrics",
        "allow-prometheus-financing-metrics",
        "allow-chat-to-financing",
        "allow-rag-callers-to-embedding",
        "allow-seed-to-ingestion",
        "allow-application-to-postgres",
        "allow-approved-callers-to-elasticsearch",
        "allow-chat-egress",
        "allow-financing-egress",
        "allow-ingestion-egress",
        "allow-kibana-egress",
        "allow-kibana-bootstrap-egress",
        "allow-seed-egress",
        "allow-migration-egress",
        "allow-application-telemetry-egress",
        "allow-model-provider-egress",
        "allow-oauth2-proxy-egress",
    } <= names


def test_only_the_web_gateway_is_marked_as_the_public_service() -> None:
    """The backend stopped being internet-facing when nginx took over the edge."""
    documents = load_documents("k8s/app.yaml")
    services = [document for document in documents if document["kind"] == "Service"]
    public = [
        document["metadata"]["name"]
        for document in services
        if document["metadata"].get("labels", {}).get("tenantchat.openai.com/exposure")
        == "public-entrypoint"
    ]

    assert public == ["web"]
    assert all(document["spec"].get("type", "ClusterIP") == "ClusterIP" for document in services)
    # oauth2-proxy exists as an internal service.
    service_names = [document["metadata"]["name"] for document in services]
    assert "oauth2-proxy" in service_names
    assert "web-admin" not in service_names


def test_the_ingress_controller_can_only_reach_the_web_gateway() -> None:
    policies = load_documents("k8s/network-policies.yaml")
    ingress_callers = {
        document["metadata"]["name"]: document["spec"]["podSelector"]["matchLabels"]
        for document in policies
        if document["kind"] == "NetworkPolicy"
        and any(
            source.get("namespaceSelector", {}).get("matchLabels", {})
            == {"kubernetes.io/metadata.name": "ingress"}
            for rule in document["spec"].get("ingress", [])
            for source in rule["from"]
        )
    }

    assert ingress_callers == {"allow-public-ingress-to-web": {"app": "web"}}


def test_workloads_use_distinct_internal_secret_refs() -> None:
    documents = load_documents("k8s/app.yaml")
    expected = {
        "chat-backend": {
            "CHAT_TO_FINANCING_TOKEN": "chat-to-financing-credentials",
        },
        "financing-agent": {
            "CHAT_TO_FINANCING_TOKEN": "chat-to-financing-credentials",
            "FINANCING_TO_EMBEDDING_TOKEN": "financing-to-embedding-credentials",
        },
        "ingestion-service": {
            "SEED_TO_INGESTION_TOKEN": "seed-to-ingestion-credentials",
            "INGESTION_TO_EMBEDDING_TOKEN": "ingestion-to-embedding-credentials",
        },
        "embedding-service": {
            "INGESTION_TO_EMBEDDING_TOKEN": "ingestion-to-embedding-credentials",
            "FINANCING_TO_EMBEDDING_TOKEN": "financing-to-embedding-credentials",
        },
    }

    referenced_secrets: set[str] = set()
    for workload, variables in expected.items():
        env = container_env(resource(documents, "Deployment", workload))
        for variable, secret_name in variables.items():
            secret_ref = env[variable]["valueFrom"]["secretKeyRef"]
            assert secret_ref == {"name": secret_name, "key": "token"}
            referenced_secrets.add(secret_name)

    assert len(referenced_secrets) == 4
    assert "llm-provider-credentials" not in referenced_secrets


def test_internal_data_routes_require_auth_but_health_and_metrics_do_not() -> None:
    protected_markers = {
        "services/embedding/app.py": ("/embed", "Depends(require_embedding_caller)"),
        "services/ingestion/app.py": ("/ingest", "Depends(require_ingestion_caller)"),
        "services/financing-agent/app.py": ("/answer", "Depends(require_chat_backend)"),
    }

    for path, markers in protected_markers.items():
        source = (ROOT / path).read_text(encoding="utf-8")
        assert all(marker in source for marker in markers)
        assert '@app.get("/health")' in source
        assert '@app.get("/metrics")' in source


def test_prometheus_is_the_only_cross_namespace_metrics_caller() -> None:
    policies = load_documents("k8s/network-policies.yaml")
    expected = {
        "allow-prometheus-chat-metrics": ("chat-backend", 8004),
        "allow-prometheus-embedding-metrics": ("embedding-service", 8001),
        "allow-prometheus-ingestion-metrics": ("ingestion-service", 8002),
        "allow-prometheus-financing-metrics": ("financing-agent", 8003),
    }
    for name, (app, port) in expected.items():
        metrics = resource(policies, "NetworkPolicy", name)
        caller = metrics["spec"]["ingress"][0]["from"][0]

        assert metrics["spec"]["podSelector"]["matchLabels"] == {"app": app}
        assert metrics["spec"]["ingress"][0]["ports"] == [{"protocol": "TCP", "port": port}]
        assert caller["namespaceSelector"]["matchLabels"] == {
            "kubernetes.io/metadata.name": "observability"
        }
        assert caller["podSelector"]["matchLabels"] == {"app.kubernetes.io/name": "prometheus"}


def test_public_listener_excludes_admin_and_metrics_routes() -> None:
    from server import is_public_route

    assert is_public_route("GET", "/")
    assert is_public_route("POST", "/api/chat")
    assert is_public_route("POST", "/api/book")
    assert not is_public_route("GET", "/metrics")
    assert not is_public_route("GET", "/admin.html")
    assert not is_public_route("GET", "/admin.js")
    assert not is_public_route("GET", "/api/leads")
    assert not is_public_route("GET", "/api/admin/chats")


def test_public_listener_serves_every_module_the_widget_imports() -> None:
    """A missing entry 403s a widget module and breaks every embed silently."""
    from server import STATIC_ROOT, is_public_route

    entry_points = ["/app.js", "/embed.js"]
    modules = [f"/widget/{path.name}" for path in sorted((STATIC_ROOT / "widget").glob("*.js"))]

    for path in [*entry_points, *modules]:
        assert is_public_route("GET", path), path
