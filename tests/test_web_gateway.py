"""Specifications for the nginx image that serves the frontend.

The gateway is what the internet reaches. Its guarantees are structural — which
files exist in which document root, and which upstream paths each listener will
forward — so they are checked against the shipped configuration rather than
against a running container.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml  # type: ignore[import-untyped]

from scripts import verify_image_contracts
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


def _write_template(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str) -> Path:
    """Point the contract checks at a template outside the repository tree.

    The checks label violations with paths relative to ROOT, so ROOT moves to
    the temporary tree and the web Dockerfile gets an identical copy there to
    keep the document-root check reading real content.
    """
    dockerfile_text = verify_image_contracts.WEB_DOCKERFILE.read_text(encoding="utf-8")
    template = tmp_path / "site.conf.template"
    template.write_text(text, encoding="utf-8")
    monkeypatch.setattr(verify_image_contracts, "ROOT", tmp_path)
    monkeypatch.setattr(verify_image_contracts, "WEB_SITE_TEMPLATE", template)
    monkeypatch.setattr(verify_image_contracts, "WEB_DOCKERFILE", tmp_path / "Dockerfile")
    (tmp_path / "Dockerfile").write_text(dockerfile_text, encoding="utf-8")
    return template


def test_a_second_listener_on_the_same_port_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A duplicate `listen 8080` block must fail the gate, not win the parse.

    The template's isolation argument is one public listener; a second block
    parsed after the reviewed one used to silently replace it as the checked
    body, so an unauthenticated block could pass behind the reviewed checks.
    """
    _write_template(
        tmp_path,
        monkeypatch,
        "server {\n    listen 8080;\n}\nserver {\n    listen 8080;\n}\n",
    )
    errors: list[str] = []

    verify_web_gateway(errors)

    assert any("duplicate server listener" in error for error in errors)


@pytest.mark.parametrize(
    "block",
    [
        pytest.param("server {\n    listen 8080 default_server;\n}\n", id="trailing-parameter"),
        pytest.param("server {\n    listen [::]:8080;\n}\n", id="ipv6-host"),
        pytest.param(
            "server {\n    location / {\n        return 404;\n    }\n}\n",
            id="no-listen-at-all",
        ),
    ],
)
def test_a_server_block_without_a_bare_port_listen_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, block: str
) -> None:
    """R-63: a `listen` the checked pattern cannot parse — trailing
    parameters, an IPv6 host, or none at all, where nginx binds its default
    port — used to leave a `server {` block invisible to both the duplicate
    check and the single-8080 assertion. The valid block ahead of it is what
    made that dangerous: the assertions passed on the visible block alone,
    reviewing a configuration that differed from what ships. The gate must
    refuse every block it cannot pin to a port."""
    _write_template(tmp_path, monkeypatch, "server {\n    listen 8080;\n}\n" + block)
    errors: list[str] = []

    verify_web_gateway(errors)

    assert any("without a bare `listen <port>;`" in error for error in errors)


def test_a_duplicate_location_declaration_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """nginx refuses duplicate locations at parse time; the gate must too.

    A second `location /api/admin/` without auth_request parses after the
    reviewed one; last-parsed-wins would check the wrong body and pass.
    """
    _write_template(
        tmp_path,
        monkeypatch,
        "server {\n    listen 8080;\n"
        "    location /api/admin/ {\n        auth_request on;\n    }\n"
        "    location /api/admin/ {\n        proxy_pass http://admin;\n    }\n"
        "}\n",
    )
    errors: list[str] = []

    verify_web_gateway(errors)

    assert any("duplicate location" in error for error in errors)


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
    from tenantchat.api.routers import chat, tenants

    visitor_paths: set[str] = set()
    for module in (chat, tenants):
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
    }

    gateway_keys = {API_PATH_TO_GATEWAY.get(path, path) for path in visitor_paths}
    assert gateway_keys == set(PUBLIC_PROXY_PATHS)

    all_proxy = proxied_locations(web_server_blocks()[8080][0])
    assert gateway_keys <= all_proxy
    assert all_proxy >= set(PUBLIC_PROXY_PATHS) | set(ADMIN_PROXY_PATHS)


CREDENTIAL_HEADER = "X-Visitor-Credential"

# What each public route's own frontend request carries, which is what its
# preflight has to allow. A browser fails the actual request when the preflight
# omits a header it sends, so an under-permissive entry here is an outage for
# every cross-origin embed and a same-origin demo never sees it. For example,
# `/api/chat/consent` once allowed only `Content-Type` while the widget sent the
# credential, so consent could not be granted from a customer site).
PUBLIC_PREFLIGHT_HEADERS = {
    # Unauthenticated reads: the widget attaches no credential.
    "/api/tenants": {"Content-Type"},
    r"^/api/tenants/[a-z0-9][a-z0-9-]{0,63}/availability$": {"Content-Type"},
    # `POST` mints a credential and carries none; `GET` reads the snapshot back
    # with one, and both share this location.
    "/api/chat/session": {"Content-Type", CREDENTIAL_HEADER},
    "/api/chat": {"Content-Type", CREDENTIAL_HEADER},
    "/api/chat/consent": {"Content-Type", CREDENTIAL_HEADER},
    "/api/chat/confirmation": {"Content-Type", CREDENTIAL_HEADER},
    "/api/chat/feedback": {"Content-Type", CREDENTIAL_HEADER},
    r"^/api/chat/sources/[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$": {
        "Content-Type",
        CREDENTIAL_HEADER,
    },
}


def _preflight_allowed_headers(body: str) -> set[str]:
    """The `Access-Control-Allow-Headers` an `OPTIONS` on this location returns."""
    match = re.search(
        r"if\s*\(\$request_method\s*=\s*OPTIONS\s*\).*?"
        r'add_header\s+Access-Control-Allow-Headers\s+"([^"]*)"',
        body,
        re.DOTALL,
    )
    assert match is not None, "location has no OPTIONS Access-Control-Allow-Headers"
    return {header.strip() for header in match.group(1).split(",") if header.strip()}


def test_every_public_route_preflights_the_headers_its_request_sends() -> None:
    """A preflight narrower than the request it guards is a cross-origin outage.

    Checked per route rather than in aggregate: the defect was one location out
    of eight, and an aggregate union would have passed while consent stayed
    broken.
    """
    bodies = location_bodies(web_server_blocks()[8080][0])

    assert set(PUBLIC_PREFLIGHT_HEADERS) == set(
        PUBLIC_PROXY_PATHS
    ), "this table and the public proxy allowlist must name the same routes"
    mismatches = {
        path: (_preflight_allowed_headers(bodies[path][0]), expected)
        for path, expected in PUBLIC_PREFLIGHT_HEADERS.items()
        if _preflight_allowed_headers(bodies[path][0]) != expected
    }
    assert not mismatches, f"preflight header drift: {mismatches}"


def test_the_api_cors_policy_admits_every_header_the_gateway_publishes() -> None:
    """Two allowlists guard one request, so the inner one cannot be narrower.

    The gateway answers the preflight, but the API answers it too when it is
    reached directly, and a request the edge admits must not then fail inside.
    """
    from tenantchat.api.visitor import VISITOR_CREDENTIAL_HEADER

    published = set().union(*PUBLIC_PREFLIGHT_HEADERS.values())

    assert published <= {"Content-Type", VISITOR_CREDENTIAL_HEADER}


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

    embed = location_bodies(web_server_blocks()[8080][0])["/embed.js"][0]
    assert "Access-Control-Allow-Origin $widget_cors_origin" in embed
    assert "proxy_pass" not in embed


def test_single_listener_has_no_separate_admin_port() -> None:
    """The separate admin listener (8081) is gone; admin is under /admin/."""
    blocks = web_server_blocks()

    assert set(blocks) == {8080}
    # No separate listen 8081 directive.
    assert "listen 8081" not in blocks[8080][0]
    assert 8081 not in blocks


def test_admin_routes_are_auth_gated() -> None:
    """Admin locations must use auth_request."""
    block = web_server_blocks()[8080][0]

    locations = location_bodies(block)
    admin_section = locations["/admin/"][0]
    assert "auth_request" in admin_section

    admin_api_section = locations["/api/admin/"][0]
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
    admin_api = location_bodies(web_server_blocks()[8080][0])["/api/admin/"][0]
    assert "$upstream_http_x_auth_request_email" in admin_api
    assert "$upstream_http_x_auth_request_user" in admin_api
    assert "$upstream_http_x_auth_request_groups" in admin_api
    assert "X-TenantChat-Gateway-Token" in admin_api


def test_widget_cors_is_not_wildcard() -> None:
    """Widget CORS must never be wildcard."""
    block = web_server_blocks()[8080][0]
    assert "Access-Control-Allow-Origin *" not in block
    assert "Vary Origin" in block
    locations = location_bodies(block)
    for path in PUBLIC_PROXY_PATHS:
        assert "Access-Control-Allow-Origin $widget_cors_origin" in locations[path][0]
    for path in ADMIN_PROXY_PATHS:
        assert "Access-Control-Allow-Origin" not in locations[path][0]


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
