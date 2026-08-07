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
    ADMIN_PROXY_PATHS,
    API_PATH_TO_GATEWAY,
    PUBLIC_PROXY_PATHS,
    WEB_ADMIN_BUILD,
    WEB_PUBLIC_BUILD,
    location_bodies,
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
    treats as internal is an admin route published to the internet.  The visitor
    surface is derived from the API's routers so neither list can drift, and
    path-parameter routes are mapped to the regex locations that publish them.
    """
    from tenantchat.api.routers import bookings, chat, leads, tenants

    visitor_paths: set[str] = set()
    for module in (chat, tenants, bookings, leads):
        for route in module.router.routes:
            for method in getattr(route, "methods", set()):  # noqa: B007
                if route.path.startswith("/api/") and not route.path.startswith("/api/admin/"):
                    visitor_paths.add(route.path)
    assert visitor_paths == {
        "/api/tenants",
        "/api/tenants/{tenant_id}/availability",
        "/api/chat/session",
        "/api/chat",
        "/api/chat/consent",
        "/api/chat/confirmation",
        "/api/chat/feedback",
        "/api/chat/sources/{source_id}",
        "/api/book",
        "/api/leads",
    }

    gateway_keys = {API_PATH_TO_GATEWAY.get(path, path) for path in visitor_paths}
    assert gateway_keys == set(PUBLIC_PROXY_PATHS)

    all_proxy = proxied_locations(web_server_blocks()[8080])
    assert gateway_keys <= all_proxy
    assert all_proxy >= set(PUBLIC_PROXY_PATHS) | set(ADMIN_PROXY_PATHS)


def test_each_document_root_holds_exactly_one_build() -> None:
    """A chunk shared between the two roots would publish admin code.

    Public and admin are separate Vite builds for this reason: neither root can
    be assembled from files the other one also needs.
    """
    roots = web_document_roots()

    assert roots["/srv/public"] == {WEB_PUBLIC_BUILD}
    assert roots["/srv/admin"] == {WEB_ADMIN_BUILD}


def test_the_embed_keeps_a_stable_cross_origin_url() -> None:
    """Customer sites hard-code this URL, and module imports are CORS-gated.

    Hashing the filename would break every existing embed on the next release;
    splitting it into chunks would make each chunk a separate cross-origin fetch
    that the gateway does not allowlist, so the widget would fail to load.
    """
    config = (ROOT / "frontend/vite.config.ts").read_text(encoding="utf-8")
    assert 'entryFileNames: "embed.js"' in config
    assert "codeSplitting: false" in config

    embed = location_bodies(web_server_blocks()[8080])["/embed.js"]
    assert "Access-Control-Allow-Origin $widget_cors_origin" in embed
    assert "proxy_pass" not in embed


def test_single_listener_has_no_separate_admin_port() -> None:
    """The separate admin listener (8081) is gone; admin is under /admin/."""
    blocks = web_server_blocks()

    assert set(blocks) == {8080}
    # No separate listen 8081 directive.
    assert "listen 8081" not in blocks[8080]
    assert 8081 not in blocks


def test_admin_routes_are_auth_gated() -> None:
    """Admin locations must use auth_request."""
    block = web_server_blocks()[8080]

    locations = location_bodies(block)
    admin_section = locations["/admin/"]
    assert "auth_request" in admin_section

    admin_api_section = locations["/api/admin/"]
    assert "auth_request" in admin_api_section


def test_oauth_redirect_preserves_the_ingress_origin() -> None:
    """TLS terminates at Traefik, so redirects must not expose nginx :8080."""
    template = (ROOT / "frontend/nginx/site.conf.template").read_text(encoding="utf-8")

    assert "map $http_x_forwarded_proto $gateway_scheme" in template
    assert "map $http_x_forwarded_host $gateway_host" in template
    assert "return 302 $gateway_scheme://$gateway_host/oauth2/start?rd=" in template
    assert "proxy_set_header X-Forwarded-Proto $gateway_scheme;" in template


def test_oauth_return_target_is_a_relative_path() -> None:
    """An absolute `rd` is dropped, sending every login to `/` instead of /admin/.

    oauth2-proxy validates an absolute `rd` against --whitelist-domain, which
    this deployment does not set, so it discarded the deep link and fell back
    to the site root. A path needs no whitelist and opens no redirect to
    another host.
    """
    template = (ROOT / "frontend/nginx/site.conf.template").read_text(encoding="utf-8")

    assert "?rd=$request_uri;" in template
    assert "?rd=$gateway_scheme://$gateway_host$request_uri;" not in template


def test_spoofable_identity_headers_are_handled() -> None:
    """Identity must come from oauth2-proxy over an authenticated internal hop."""
    admin_api = location_bodies(web_server_blocks()[8080])["/api/admin/"]
    assert "$upstream_http_x_auth_request_email" in admin_api
    assert "$upstream_http_x_auth_request_user" in admin_api
    assert "$upstream_http_x_auth_request_groups" in admin_api
    assert "X-TenantChat-Gateway-Token" in admin_api


def test_widget_cors_is_not_wildcard() -> None:
    """Widget CORS must never be wildcard."""
    block = web_server_blocks()[8080]
    assert "Access-Control-Allow-Origin *" not in block
    assert "Vary Origin" in block
    locations = location_bodies(block)
    for path in PUBLIC_PROXY_PATHS:
        assert "Access-Control-Allow-Origin $widget_cors_origin" in locations[path]
    for path in ADMIN_PROXY_PATHS:
        assert "Access-Control-Allow-Origin" not in locations[path]


def test_no_kubernetes_route_publishes_a_separate_admin_service() -> None:
    """Port 8081 is gone; there is no web-admin Service."""
    documents = load_documents("k8s/app.yaml")
    services = [document for document in documents if document["kind"] == "Service"]
    service_names = [document["metadata"]["name"] for document in services]

    assert "web-admin" not in service_names
    assert "oauth2-proxy" in service_names

    public = next(document for document in services if document["metadata"]["name"] == "web")
    assert public["metadata"]["labels"]["tenantchat.openai.com/exposure"] == "public-entrypoint"

    policies = load_documents("k8s/network-policies.yaml")
    ingress_policy = next(
        document
        for document in policies
        if document["metadata"]["name"] == "allow-public-ingress-to-web"
    )

    assert ingress_policy["spec"]["ingress"][0]["ports"] == [{"protocol": "TCP", "port": 8080}]


def test_the_gateway_reaches_the_backend_and_auth_proxy() -> None:
    policies = load_documents("k8s/network-policies.yaml")
    egress = next(
        document for document in policies if document["metadata"]["name"] == "allow-web-egress"
    )["spec"]["egress"]

    assert len(egress) == 2
    backend_egress = next(
        e for e in egress if e["to"][0]["podSelector"]["matchLabels"] == {"app": "chat-backend"}
    )
    # The API image serves visitor and admin routes on the single port 8004.
    assert backend_egress["ports"] == [{"protocol": "TCP", "port": 8004}]
    proxy_egress = next(
        e for e in egress if e["to"][0]["podSelector"]["matchLabels"] == {"app": "oauth2-proxy"}
    )
    assert proxy_egress["ports"] == [{"protocol": "TCP", "port": 4180}]
