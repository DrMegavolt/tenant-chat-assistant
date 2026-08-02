#!/usr/bin/env python3
"""Verify immutable image inputs without contacting a registry or cluster."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILES = (
    ROOT / "Dockerfile",
    ROOT / "services/api/Dockerfile",
    ROOT / "services/embedding/Dockerfile",
    ROOT / "services/ingestion/Dockerfile",
    ROOT / "services/financing-agent/Dockerfile",
)
K8S_FILES = tuple(sorted((ROOT / "k8s").glob("*.yaml")))
DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")
RELEASE_CONTRACT = re.compile(
    r"registry\.example\.invalid/tenantchat/(?:prototype|api|embedding|ingestion|financing)"
    r"@sha256:REPLACE_WITH_[A-Z]+_DIGEST$"
)


def verify_dockerfiles(errors: list[str]) -> None:
    """Require locked build inputs, digest-pinned bases, and non-root runtimes."""
    for path in DOCKERFILES:
        text = path.read_text(encoding="utf-8")
        label = path.relative_to(ROOT)
        syntax = text.splitlines()[0]
        if DIGEST.search(syntax) is None:
            errors.append(f"{label}: Dockerfile frontend must use an exact digest")
        image_args = re.findall(r'^ARG\s+\w+_IMAGE="([^"]+)"', text, re.MULTILINE)
        if len(image_args) != 2 or any(DIGEST.search(image) is None for image in image_args):
            errors.append(f"{label}: every base/tool image ARG must end in an exact digest")
        if "uv sync --frozen" not in text:
            errors.append(f"{label}: dependency installation must consume uv.lock with --frozen")
        if re.search(r"\bpip(?:3)?\s+install\b", text):
            errors.append(f"{label}: runtime/build pip install is forbidden")
        if "USER 10001:10001" not in text:
            errors.append(f"{label}: final runtime must select numeric non-root uid/gid 10001")


def verify_manifests(errors: list[str]) -> None:
    """Require immutable refs and prohibit runtime source/dependency injection."""
    for path in K8S_FILES:
        text = path.read_text(encoding="utf-8")
        label = path.relative_to(ROOT)
        for line in text.splitlines():
            match = re.match(r"^\s*image:\s*(\S+)\s*$", line)
            if match and not (
                DIGEST.search(match.group(1)) or RELEASE_CONTRACT.fullmatch(match.group(1))
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
    if '["alembic", "upgrade", "head"]' not in migration or "uv run" in migration:
        errors.append("k8s/api-migration-job.yaml: migration must use bundled API runtime directly")


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
