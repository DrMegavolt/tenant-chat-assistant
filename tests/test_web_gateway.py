"""Specifications for the nginx image that serves the frontend.

The gateway is what the internet reaches. Its guarantees are structural — which
files exist in which document root, and which upstream paths each listener will
forward — so they are checked against the shipped configuration rather than
against a running container.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from scripts.verify_image_contracts import (
    PUBLIC_PROXY_PATHS,
    proxied_locations,
    verify_web_gateway,
    web_document_roots,
    web_server_blocks,
)

ROOT = Path(__file__).resolve().parents[1]


def load_documents(path: str) -> list[dict[str, Any]]:
    return [
        document
        for document in yaml.safe_load_all((ROOT / path).read_text(encoding="utf-8"))
        if document
    ]


def test_web_gateway_contract_holds() -> None:
    errors: list[str] = []
    verify_web_gateway(errors)
    assert errors == []


def test_public_listener_forwards_exactly_the_backend_public_api() -> None:
    """The edge allowlist and the backend allowlist have to agree.

    A path the backend treats as public but nginx does not forward is a feature
    that silently 404s for every visitor; a path nginx forwards but the backend
    treats as internal is an admin route published to the internet.
    """
    from server import _PUBLIC_GET_PATHS, _PUBLIC_POST_PATHS

    backend_api = {
        path for path in _PUBLIC_GET_PATHS | _PUBLIC_POST_PATHS if path.startswith("/api/")
    }

    assert proxied_locations(web_server_blocks()[8080]) == backend_api == set(PUBLIC_PROXY_PATHS)


def test_public_listener_serves_every_module_the_widget_imports() -> None:
    """A module missing from the public root 404s and breaks every embed."""
    public_root = web_document_roots()["/srv/public"]

    assert "frontend/public/widget/" in public_root
    assert "frontend/public/embed.js" in public_root


def test_the_console_is_unreachable_from_the_public_listener() -> None:
    blocks = web_server_blocks()

    assert "admin" not in blocks[8080]
    assert not any(source.startswith("admin") for source in web_document_roots()["/srv/public"])
    assert "/api/admin/" in proxied_locations(blocks[8081])


def test_no_kubernetes_route_publishes_the_console_listener() -> None:
    """Port 8081 must stay reachable only through an explicit port-forward."""
    documents = load_documents("k8s/app.yaml")
    services = [document for document in documents if document["kind"] == "Service"]
    admin = next(document for document in services if document["metadata"]["name"] == "web-admin")

    assert admin["metadata"]["labels"]["tenantchat.openai.com/exposure"] == "internal"
    assert admin["spec"].get("type", "ClusterIP") == "ClusterIP"

    policies = load_documents("k8s/network-policies.yaml")
    public = next(
        document
        for document in policies
        if document["metadata"]["name"] == "allow-public-ingress-to-web"
    )

    assert public["spec"]["ingress"][0]["ports"] == [{"protocol": "TCP", "port": 8080}]


def test_the_gateway_reaches_the_backend_and_nothing_else() -> None:
    policies = load_documents("k8s/network-policies.yaml")
    egress = next(
        document for document in policies if document["metadata"]["name"] == "allow-web-egress"
    )["spec"]["egress"]

    assert len(egress) == 1
    assert egress[0]["to"] == [{"podSelector": {"matchLabels": {"app": "chat-backend"}}}]
    assert egress[0]["ports"] == [
        {"protocol": "TCP", "port": 8000},
        {"protocol": "TCP", "port": 8004},
    ]
