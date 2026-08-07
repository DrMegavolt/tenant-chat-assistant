"""Specifications for the realm the Keycloak chart imports.

These render the chart, so they need the `helm` CLI that `make check` is
deliberately kept free of. The `chart` marker keeps them out of the hermetic
suite; `make keycloak-check` and CI's `charts` job run them.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml  # type: ignore[import-untyped]

pytestmark = pytest.mark.chart

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "k8s/helm/keycloak"
EXAMPLE_VALUES = CHART / "values.local.example.yaml"

# Keycloak creates these itself, and only for a realm representation that omits
# `clientScopes`. A representation carrying that key is authoritative: Keycloak
# assigns exactly the listed scopes and creates none of these.
BUILTIN_CLIENT_SCOPES = frozenset(
    {"acr", "basic", "email", "profile", "roles", "web-origins"},
)

# OIDC's marker that a request is an authentication request. Keycloak resolves
# it without a client scope of that name.
OIDC_MARKER_SCOPE = "openid"

_CREATED_CLIENT_SCOPE = re.compile(r"kc create client-scopes\b[^\n]*?-s name=([A-Za-z0-9_-]+)")


def _render_chart() -> list[dict[str, Any]]:
    helm = shutil.which("helm")
    if helm is None:
        pytest.fail(
            "helm is not on PATH. These tests render the chart rather than reading "
            "its templates as text; install helm or run `make keycloak-check` in CI.",
        )
    completed = subprocess.run(  # noqa: S603
        [
            helm,
            "template",
            "keycloak",
            str(CHART),
            "--namespace",
            "identity",
            "--values",
            str(EXAMPLE_VALUES),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, f"helm template failed:\n{completed.stderr}"
    return [document for document in yaml.safe_load_all(completed.stdout) if document]


def _configmap_entry(documents: list[dict[str, Any]], suffix: str) -> str:
    """The one rendered ConfigMap value whose key ends in `suffix`.

    Keyed on the suffix rather than the resource name because the release name
    prefixes every name in the chart.
    """
    for document in documents:
        if document.get("kind") != "ConfigMap":
            continue
        for key, value in document.get("data", {}).items():
            if key.endswith(suffix):
                return str(value)
    raise AssertionError(f"no rendered ConfigMap holds a key ending in {suffix!r}")


def _scopes_created_by_bootstrap(script: str) -> set[str]:
    """Client scope names the bootstrap Job adds to the realm Keycloak built."""
    return set(_CREATED_CLIENT_SCOPE.findall(script.replace("\\\n", " ")))


def _scopes_the_realm_ends_up_with(realm: dict[str, Any], bootstrap_script: str) -> set[str]:
    declared = (
        {scope["name"] for scope in realm["clientScopes"]}
        if "clientScopes" in realm
        else set(BUILTIN_CLIENT_SCOPES)
    )
    return declared | _scopes_created_by_bootstrap(bootstrap_script)


def _gateway_requested_scopes() -> set[str]:
    """The scopes oauth2-proxy names in its authorization request."""
    documents = [
        document
        for document in yaml.safe_load_all((ROOT / "k8s/app.yaml").read_text(encoding="utf-8"))
        if document
    ]
    for document in documents:
        if document.get("kind") != "Deployment":
            continue
        for container in document["spec"]["template"]["spec"]["containers"]:
            if container["name"] != "oauth2-proxy":
                continue
            for entry in container.get("env", []):
                if entry["name"] == "OAUTH2_PROXY_SCOPE":
                    return set(str(entry["value"]).split())
    raise AssertionError("no oauth2-proxy container in k8s/app.yaml sets OAUTH2_PROXY_SCOPE")


@pytest.fixture(scope="module")
def rendered() -> list[dict[str, Any]]:
    return _render_chart()


@pytest.fixture(scope="module")
def realm(rendered: list[dict[str, Any]]) -> dict[str, Any]:
    parsed: dict[str, Any] = json.loads(_configmap_entry(rendered, "-realm.json"))
    return parsed


@pytest.fixture(scope="module")
def bootstrap_script(rendered: list[dict[str, Any]]) -> str:
    return _configmap_entry(rendered, "bootstrap.sh")


def test_realm_import_leaves_the_builtin_client_scopes_in_place(realm: dict[str, Any]) -> None:
    """`clientScopes` in a realm representation is authoritative, not additive.

    Keycloak assigns exactly the listed scopes instead of creating its built-in
    ones, so declaring a single custom scope here deletes `basic`, `profile`,
    `email`, `roles`, and `web-origins` from the realm. Every authorization
    request then comes back `invalid_scope` and /admin/ is unreachable for every
    user. Realm import has no additive mode for the key, so it has to be absent
    and the scope has to be created against the live realm afterwards.
    """
    assert "clientScopes" not in realm


def test_client_default_scopes_all_exist_while_the_realm_is_imported(
    realm: dict[str, Any],
) -> None:
    """Import drops an unresolvable scope name here silently rather than failing.

    Only the built-ins exist at that point. A scope the bootstrap Job creates is
    attached afterwards, against the live realm, so naming it here buys nothing
    and hides the omission.
    """
    declared = set(realm["clients"][0]["defaultClientScopes"])

    assert not declared - BUILTIN_CLIENT_SCOPES, (
        f"{sorted(declared - BUILTIN_CLIENT_SCOPES)} do not exist yet when the realm is "
        "imported; the bootstrap Job has to attach them to the client instead"
    )


def test_bootstrap_job_creates_the_groups_scope_and_attaches_it(bootstrap_script: str) -> None:
    """Keeping `clientScopes` out of the realm must not take the group claim with it.

    The gateway maps a group to an application role, so a realm with no `groups`
    scope logs users in carrying no role and every admin route answers 403.
    """
    assert "groups" in _scopes_created_by_bootstrap(bootstrap_script)
    assert "oidc-group-membership-mapper" in bootstrap_script
    assert "clients/$client_uuid/default-client-scopes/" in bootstrap_script


def test_every_scope_the_gateway_requests_resolves_in_the_realm(
    realm: dict[str, Any],
    bootstrap_script: str,
) -> None:
    """Keycloak refuses the whole request over one unresolvable scope.

    A scope the realm does not have is not ignored, so the mismatch takes login
    down rather than degrading a claim. This pins the chart against the manifest
    that consumes it: the two are edited independently.
    """
    available = _scopes_the_realm_ends_up_with(realm, bootstrap_script)
    requested = _gateway_requested_scopes() - {OIDC_MARKER_SCOPE}

    assert not requested - available, (
        f"oauth2-proxy requests {sorted(requested - available)}, which the realm does not "
        "have; Keycloak answers the authorization request with invalid_scope"
    )
