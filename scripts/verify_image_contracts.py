#!/usr/bin/env python3
"""Verify immutable image inputs without contacting a registry or cluster."""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Images whose contents come from the Python lockfile. Every Python workload
# builds from a service image; there is no root Dockerfile.
PYTHON_DOCKERFILES = (
    ROOT / "services/api/Dockerfile",
    ROOT / "services/embedding/Dockerfile",
)
WEB_DOCKERFILE = ROOT / "frontend/Dockerfile"
DOCKERFILES = (*PYTHON_DOCKERFILES, WEB_DOCKERFILE)
RUNTIME_ENTRYPOINTS = {
    ROOT / "services/embedding/Dockerfile": ROOT / "services/embedding/app.py",
}
# The migration Job runs `alembic upgrade head` from the API image itself, so
# the schema tooling has to be in the image the API serves traffic from.
API_RUNTIME_FILES = ("alembic.ini", "services/api/migrations/")
POSTGRES_FIXTURES = (
    ROOT / "tests/migrations/conftest.py",
    ROOT / "tests/repositories/conftest.py",
    ROOT / "scripts/smoke_images.sh",
    ROOT / "docker-compose.yml",
)
K8S_FILES = tuple(sorted((ROOT / "k8s").glob("*.yaml")))
DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")
POSTGRES_IMAGE = re.compile(r"postgres:\d[\w.\-]*(?:@sha256:[0-9a-f]{64})?")
RELEASE_CONTRACT = re.compile(
    r"registry\.example\.invalid/tenantchat/(?:api|embedding|web)"
    r"@sha256:REPLACE_WITH_[A-Z]+_DIGEST$"
)
OAUTH2_PROXY_CONTRACT = re.compile(
    r"quay\.io/oauth2-proxy/oauth2-proxy:v[\d.]+@sha256:REPLACE_WITH_[A-Z0-9_]+_DIGEST$"
)
WEB_SITE_TEMPLATE = ROOT / "frontend/nginx/site.conf.template"
WEB_VITE_CONFIG = ROOT / "frontend/vite.config.ts"
# The build-stage directories each nginx document root is copied from.
WEB_PUBLIC_BUILD = "/build/dist/public/"
WEB_ADMIN_BUILD = "/build/dist/admin/"
# A customer site hard-codes this URL in a script tag, so the build may not
# rename it and the gateway has to answer it with allowlisted CORS.
WEB_EMBED_PATH = "/embed.js"
# The visitor API surface the public listener may forward. `tests/test_web_gateway.py`
# derives the set from the API's routers and asserts it equals this exactly, so a
# route cannot reach a browser until both sides list it.
# Path-parameter routes are nginx regex locations, so their allowlist entries are
# the regex patterns themselves — narrowed to the shapes the API accepts.
API_PATH_TO_GATEWAY = {
    "/api/tenants/{tenant_id}/availability": r"^/api/tenants/[a-z0-9][a-z0-9-]{0,63}/availability$",
    # Chunk ids are the citation source ids the widget renders.
    "/api/chat/sources/{source_id}": r"^/api/chat/sources/[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$",
}
PUBLIC_PROXY_PATHS = frozenset(
    {
        "/api/tenants",
        "/api/chat",
        "/api/chat/session",
        "/api/chat/consent",
        "/api/chat/confirmation",
        # FEAT-008: the visitor's rating of one turn record.
        "/api/chat/feedback",
    }
    | set(API_PATH_TO_GATEWAY.values())
)
# Admin API paths that proxy to the admin listener (auth-gated).
ADMIN_PROXY_PATHS = frozenset({"/api/admin/"})
AUTH_PROXY_PATHS = frozenset({"/_auth", "/oauth2/"})


def verify_dockerfiles(errors: list[str]) -> None:
    """Require locked build inputs, digest-pinned bases, and non-root runtimes."""
    for path in DOCKERFILES:
        text = path.read_text(encoding="utf-8")
        label = path.relative_to(ROOT)
        syntax = text.splitlines()[0]
        if DIGEST.search(syntax) is None:
            errors.append(f"{label}: Dockerfile frontend must use an exact digest")
        # Both ARG spellings are checked: the unquoted form is legal Dockerfile
        # and would otherwise float to any tag without the gate noticing.
        image_args = [
            quoted or unquoted
            for quoted, unquoted in re.findall(
                r'^ARG\s+\w+_IMAGE=(?:"([^"]*)"|(\S+))', text, re.MULTILINE
            )
        ]
        if not image_args or any(DIGEST.search(image) is None for image in image_args):
            errors.append(f"{label}: every base/tool image ARG must end in an exact digest")
        if path in PYTHON_DOCKERFILES and "uv sync --frozen" not in text:
            errors.append(f"{label}: dependency installation must consume uv.lock with --frozen")
        if re.search(r"\bpip(?:3)?\s+install\b", text):
            errors.append(f"{label}: runtime/build pip install is forbidden")
        if "USER 10001:10001" not in text:
            errors.append(f"{label}: final runtime must select numeric non-root uid/gid 10001")


def runtime_copy_sources(dockerfile: Path) -> set[str]:
    """Return the build-context paths the final stage copies into the image.

    Stage-to-stage copies are excluded: they carry the built virtualenv rather
    than repository source.
    """
    final_stage = re.split(r"^FROM .*$", dockerfile.read_text(encoding="utf-8"), flags=re.MULTILINE)
    instructions = re.sub(r"\\\n\s*", " ", final_stage[-1])
    sources: set[str] = set()
    for line in instructions.splitlines():
        arguments = line.split()
        if not arguments or arguments[0] != "COPY":
            continue
        if any(argument.startswith("--from=") for argument in arguments):
            continue
        sources.update(argument for argument in arguments[1:-1] if not argument.startswith("--"))
    return sources


def local_module_dependencies(entrypoint: Path) -> set[str]:
    """Return every repository-root module reachable from an entrypoint's imports."""
    required: set[str] = set()
    pending = [entrypoint]
    visited = {entrypoint}
    while pending:
        tree = ast.parse(pending.pop().read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module is not None:
                imported = [node.module]
            else:
                continue
            for name in imported:
                module = ROOT / f"{name.split('.')[0]}.py"
                if module.is_file() and module not in visited:
                    visited.add(module)
                    required.add(module.name)
                    pending.append(module)
    return required


def verify_runtime_modules(errors: list[str]) -> None:
    """Require each image to carry every repository module its entrypoint imports.

    The service entrypoints import `internal_auth`, which in turn imports
    `runtime_security`. An image that copies only the entrypoint builds and scans
    clean, then fails at container start with `ModuleNotFoundError`.
    """
    for dockerfile, entrypoint in RUNTIME_ENTRYPOINTS.items():
        label = dockerfile.relative_to(ROOT)
        copied = runtime_copy_sources(dockerfile)
        source = str(entrypoint.relative_to(ROOT))
        if source not in copied:
            errors.append(f"{label}: final stage must copy its entrypoint {source}")
        for module in sorted(local_module_dependencies(entrypoint)):
            if module not in copied:
                errors.append(f"{label}: final stage is missing imported module {module}")

    api = ROOT / "services/api/Dockerfile"
    api_copied = runtime_copy_sources(api)
    for required in API_RUNTIME_FILES:
        if required not in api_copied:
            errors.append(f"{api.relative_to(ROOT)}: final stage must copy {required}")


def web_document_roots() -> dict[str, set[str]]:
    """Return the build-context paths each nginx document root is built from."""
    roots: dict[str, set[str]] = {}
    instructions = re.sub(r"\\\n\s*", " ", WEB_DOCKERFILE.read_text(encoding="utf-8"))
    for line in instructions.splitlines():
        arguments = [argument for argument in line.split() if not argument.startswith("--")]
        if not arguments or arguments[0] != "COPY" or not arguments[-1].startswith("/srv/"):
            continue
        root = "/" + "/".join(arguments[-1].split("/")[1:3])
        roots.setdefault(root, set()).update(arguments[1:-1])
    return roots


def web_server_blocks() -> dict[int, list[str]]:
    """Return the rendered site's `server { ... }` bodies grouped by listen port.

    Every block is returned in template order, so a second block on an already
    listened port stays visible to the caller instead of silently winning the
    parse: nginx refuses duplicate default servers, and the gateway checks must
    refuse the template rather than review whichever block parsed last.

    Raises:
        ValueError: a `server {` block carries no bare `listen <port>;`
            directive. Trailing parameters (`default_server`), an IPv6 host, or
            no listen at all — where nginx binds its default port — all take a
            block out of the port checks below (R-63), so the template is
            refused instead of the block going silently unreviewed.
    """
    text = WEB_SITE_TEMPLATE.read_text(encoding="utf-8")
    blocks: dict[int, list[str]] = {}
    for start in (match.end() for match in re.finditer(r"^server\s*\{", text, re.MULTILINE)):
        depth = 1
        index = start
        while depth and index < len(text):
            depth += {"{": 1, "}": -1}.get(text[index], 0)
            index += 1
        body = text[start : index - 1]
        listen = re.search(r"^\s*listen\s+(\d+)\s*;", body, re.MULTILINE)
        if listen is None:
            snippet = " ".join(body.split())[:80]
            raise ValueError(f"server block without a bare `listen <port>;` directive: {snippet!r}")
        blocks.setdefault(int(listen.group(1)), []).append(body)
    return blocks


def location_bodies(block: str) -> dict[str, list[str]]:
    """Return location bodies using brace balancing, including nested `if`s.

    Keys are the matched path: the literal path after `= ` or a bare prefix, and
    the pattern itself for a regex location, so the allowlist can name exactly
    what nginx will match. A duplicated path keeps every body: nginx rejects a
    duplicate location at parse time, and last-parsed-wins would let a second
    unauthenticated block hide behind the reviewed one's checks.
    """
    bodies: dict[str, list[str]] = {}
    pattern = re.compile(r"location\s+(?:=\s*|\^~\s*|~\s*\*\s*|~\s*)?(\S+)\s*\{")
    for match in pattern.finditer(block):
        depth = 1
        index = match.end()
        while depth and index < len(block):
            depth += {"{": 1, "}": -1}.get(block[index], 0)
            index += 1
        if depth == 0:
            # Regex locations are quoted in the template so their quantifier
            # braces do not close the location block; key by the bare pattern.
            bodies.setdefault(match.group(1).strip('"'), []).append(block[match.end() : index - 1])
    return bodies


def proxied_locations(block: str) -> set[str]:
    """Return locations whose complete body proxies upstream."""
    return {
        path
        for path, bodies in location_bodies(block).items()
        if any("proxy_pass" in body for body in bodies)
    }


def verify_web_gateway(errors: list[str]) -> None:
    """Require the single public listener to isolate admin from public.

    Two document roots and one listener are the whole isolation argument: if the
    public root ever receives admin output, or the single server block grows an
    unauthenticated admin location, the operator console becomes internet-facing
    with nothing else in the deployment to stop it.  Admin routes must be
    auth-gated; unknown API paths must 404.

    Public and admin are separate Vite builds, so the two roots share no chunk
    and each is copied whole from its own build directory.
    """
    dockerfile = str(WEB_DOCKERFILE.relative_to(ROOT))
    template = str(WEB_SITE_TEMPLATE.relative_to(ROOT))
    roots = web_document_roots()
    if set(roots) != {"/srv/public", "/srv/admin"}:
        errors.append(f"{dockerfile}: expected exactly a public and an admin document root")
        return
    for expected, root in ((WEB_PUBLIC_BUILD, "/srv/public"), (WEB_ADMIN_BUILD, "/srv/admin")):
        if roots[root] != {expected}:
            got = ", ".join(sorted(roots[root])) or "nothing"
            errors.append(f"{dockerfile}: {root} must be built from exactly {expected}, got {got}")

    try:
        blocks = web_server_blocks()
    except ValueError as error:
        errors.append(f"{template}: {error}")
        return
    duplicated_ports = sorted(port for port, bodies in blocks.items() if len(bodies) > 1)
    if duplicated_ports:
        errors.append(f"{template}: duplicate server listener(s) {duplicated_ports}")
    if set(blocks) != {8080}:
        errors.append(f"{template}: expected a single 8080 listener")
        return
    block = blocks[8080][0]
    locations = location_bodies(block)
    duplicated_locations = sorted(path for path, bodies in locations.items() if len(bodies) > 1)
    if duplicated_locations:
        errors.append(f"{template}: duplicate location declaration(s) {duplicated_locations}")
    # Public visitor API paths must proxy upstream.
    all_proxy = proxied_locations(block)
    public_proxy = all_proxy & set(PUBLIC_PROXY_PATHS)
    missing_public = set(PUBLIC_PROXY_PATHS) - public_proxy
    if missing_public:
        missing = ", ".join(sorted(missing_public))
        errors.append(f"{template}: public listener missing proxy for {missing}")
    # Admin API paths must also proxy upstream (auth-gated, checked below).
    if "/api/admin/" not in all_proxy:
        errors.append(f"{template}: admin API route /api/admin/ is not proxied")
    unexpected = all_proxy - set(PUBLIC_PROXY_PATHS | ADMIN_PROXY_PATHS | AUTH_PROXY_PATHS)
    if unexpected:
        errors.append(f"{template}: unexpected proxied locations {', '.join(sorted(unexpected))}")
    # Unknown /api/ paths must fail closed: the 404 has to live in the
    # catch-all location itself, not anywhere in the listener.
    if "/api/" not in locations or "return 404" not in locations["/api/"][0]:
        errors.append(f"{template}: listener must reject unlisted /api/ paths")
    # Admin location must be auth-gated.
    admin_block = locations.get("/admin/", [""])[0]
    if "auth_request" not in admin_block:
        errors.append(f"{template}: admin location must use auth_request")
    # Admin API must be auth-gated.
    admin_api_block = locations.get("/api/admin/", [""])[0]
    if "auth_request" not in admin_api_block:
        errors.append(f"{template}: admin API location must use auth_request")
    for directive in (
        "auth_request_set $auth_email $upstream_http_x_auth_request_email",
        "auth_request_set $auth_user $upstream_http_x_auth_request_user",
        "auth_request_set $auth_groups $upstream_http_x_auth_request_groups",
        "proxy_set_header X-TenantChat-Gateway-Token",
    ):
        if directive not in admin_api_block:
            errors.append(f"{template}: admin API is missing {directive}")
    for path in PUBLIC_PROXY_PATHS:
        public_body = locations.get(path, [""])[0]
        if "Access-Control-Allow-Origin $widget_cors_origin" not in public_body:
            errors.append(f"{template}: {path} is missing allowlisted CORS responses")
        if "$request_method = OPTIONS" not in public_body:
            errors.append(f"{template}: {path} is missing CORS preflight handling")
    for path in ADMIN_PROXY_PATHS:
        if "Access-Control-Allow-Origin" in locations.get(path, [""])[0]:
            errors.append(f"{template}: admin route {path} must not emit CORS permission")
    # The embed is the only static asset a cross-origin customer site fetches.
    embed_body = locations.get(WEB_EMBED_PATH, [""])[0]
    if "Access-Control-Allow-Origin $widget_cors_origin" not in embed_body:
        errors.append(f"{template}: {WEB_EMBED_PATH} is missing allowlisted CORS responses")
    vite_config = WEB_VITE_CONFIG.read_text(encoding="utf-8")
    if 'entryFileNames: "embed.js"' not in vite_config or "codeSplitting: false" not in vite_config:
        errors.append(
            f"{WEB_VITE_CONFIG.relative_to(ROOT)}: the embed must build to one unhashed embed.js"
        )
    # Widget CORS must not be wildcard.
    if "Access-Control-Allow-Origin *" in block:
        errors.append(f"{template}: must not use wildcard CORS")
    # Vary: Origin must be present for cache correctness.
    if "Vary Origin" not in block:
        errors.append(f"{template}: must emit Vary: Origin for CORS cache correctness")


def verify_pinned_postgres_images(errors: list[str]) -> None:
    """Require every disposable PostgreSQL to be the one reviewed server build.

    Migrations, repository specifications, the API image smoke, and local
    development all assert behavior that depends on server version and collation,
    so a floating tag in any one of them tests a different database.
    """
    references: set[str] = set()
    for path in POSTGRES_FIXTURES:
        found = POSTGRES_IMAGE.findall(path.read_text(encoding="utf-8"))
        label = path.relative_to(ROOT)
        if not found:
            errors.append(f"{label}: expected a pinned PostgreSQL image reference")
        references.update(found)
    unpinned = sorted(image for image in references if DIGEST.search(image) is None)
    if unpinned:
        errors.append(f"PostgreSQL images must be digest-pinned: {', '.join(unpinned)}")
    elif len(references) > 1:
        errors.append(f"PostgreSQL images must all match: {', '.join(sorted(references))}")


def verify_model_cache(errors: list[str]) -> None:
    """Require every model-weight mount to land on the image's own HF_HOME.

    The embedding image runs as uid 10001, which cannot write `/root`; a mount at
    the wrong path leaves the cache unwritable and re-downloads weights forever.
    """
    dockerfile = (ROOT / "services/embedding/Dockerfile").read_text(encoding="utf-8")
    match = re.search(r"HF_HOME=(\S+)", dockerfile)
    if match is None:
        errors.append("services/embedding/Dockerfile: HF_HOME must be set for the runtime user")
        return
    hf_home = match.group(1)
    if hf_home.startswith("/root"):
        errors.append("services/embedding/Dockerfile: HF_HOME must be writable by uid 10001")
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    if f"huggingface-cache:{hf_home}" not in compose:
        errors.append(f"docker-compose.yml: model cache must mount at {hf_home}")
    manifest = (ROOT / "k8s/app.yaml").read_text(encoding="utf-8")
    if f"mountPath: {hf_home}" not in manifest:
        errors.append(f"k8s/app.yaml: model cache must mount at {hf_home}")


def verify_manifests(errors: list[str]) -> None:
    """Require immutable refs and prohibit runtime source/dependency injection."""
    for path in K8S_FILES:
        text = path.read_text(encoding="utf-8")
        label = path.relative_to(ROOT)
        for line in text.splitlines():
            match = re.match(r"^\s*image:\s*(\S+)\s*$", line)
            if match and not (
                DIGEST.search(match.group(1))
                or RELEASE_CONTRACT.fullmatch(match.group(1))
                or OAUTH2_PROXY_CONTRACT.fullmatch(match.group(1))
            ):
                errors.append(f"{label}: mutable or invalid image reference {match.group(1)!r}")
        if re.search(r"\bpip(?:3)?\s+install\b", text):
            errors.append(f"{label}: pods must not install dependencies at startup")
        if re.search(r"name:\s*[\w-]+-code\b", text):
            errors.append(f"{label}: application source ConfigMap mounts are forbidden")

    app = (ROOT / "k8s/app.yaml").read_text(encoding="utf-8")
    if re.search(r"mountPath:\s*/app\b", app):
        errors.append("k8s/app.yaml: application source must come from the image, not /app mounts")
    migration = (ROOT / "k8s/api-migration-job.yaml").read_text(encoding="utf-8")
    if (
        '["/bin/sh", "-eu", "-c"]' not in migration
        or "alembic upgrade head" not in migration
        or "python /app/scripts/setup_checkpoints.py" not in migration
        or "uv run" in migration
    ):
        errors.append(
            "k8s/api-migration-job.yaml: domain and checkpoint migrations must use "
            "the bundled API runtime directly"
        )


def verify_model(errors: list[str]) -> None:
    """Require the reviewed model commit and disable repository Python execution."""
    app = (ROOT / "services/embedding/app.py").read_text(encoding="utf-8")
    manifest = (ROOT / "k8s/app.yaml").read_text(encoding="utf-8")
    revision = "97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3"
    if revision not in app or revision not in manifest:
        errors.append("embedding model revision must be the reviewed immutable commit")
    if "trust_remote_code=False" not in app or "trust_remote_code=True" in app:
        errors.append("embedding model must not execute repository-provided Python")


def main() -> int:
    """Run all checks and report every violation in one pass."""
    errors: list[str] = []
    verify_dockerfiles(errors)
    verify_runtime_modules(errors)
    verify_web_gateway(errors)
    verify_pinned_postgres_images(errors)
    verify_model_cache(errors)
    verify_manifests(errors)
    verify_model(errors)
    if errors:
        sys.stderr.write("".join(f"ERROR: {error}\n" for error in errors))
        return 1
    sys.stdout.write(
        "image contracts passed: locked builds, immutable refs, bundled source, pinned model\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
