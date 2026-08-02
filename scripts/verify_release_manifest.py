#!/usr/bin/env python3
"""Reject mutable image references in a rendered Kubernetes release manifest."""

from __future__ import annotations

import re
import sys
from pathlib import Path

DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")
IMAGE_FIELD = re.compile(r"^\s*(?:-\s*)?image:\s*(\S+)\s*$")
REQUIRED_WORKLOADS = frozenset(
    {
        ("StatefulSet", "elasticsearch"),
        ("Deployment", "kibana"),
        ("StatefulSet", "postgres"),
        ("Deployment", "embedding-service"),
        ("Deployment", "ingestion-service"),
        ("Deployment", "financing-agent"),
        ("Deployment", "chat-backend"),
        ("Deployment", "web"),
    }
)


def _identity(document: str) -> tuple[str, str]:
    """Read kind/name from this repository's conventional Kubernetes YAML."""
    kind = re.search(r"^kind:\s*([^\s#]+)", document, re.MULTILINE)
    name = re.search(
        r"^metadata:\s*\n(?:^[ \t]+.*\n)*?^[ \t]+name:\s*([^\s#]+)",
        document,
        re.MULTILINE,
    )
    return (kind.group(1) if kind else "", name.group(1) if name else "")


def validate_manifest(path: Path) -> list[str]:
    """Return violations for image fields that are not immutable registry refs."""
    errors: list[str] = []
    text = path.read_text(encoding="utf-8")
    for line_number, line in enumerate(text.splitlines(), start=1):
        match = IMAGE_FIELD.match(line)
        if match and DIGEST.search(match.group(1)) is None:
            errors.append(f"{path}:{line_number}: image is not pinned to a 64-hex sha256 digest")
    documents = re.split(r"^---\s*$", text, flags=re.MULTILINE)
    found_workloads: set[tuple[str, str]] = set()
    for document in documents:
        identity = _identity(document)
        if identity not in REQUIRED_WORKLOADS:
            continue
        found_workloads.add(identity)
        image_count = sum(IMAGE_FIELD.match(line) is not None for line in document.splitlines())
        if image_count != 1:
            errors.append(f"{path}: {identity[0]}/{identity[1]} must contain exactly one image")
    for kind, name in sorted(REQUIRED_WORKLOADS - found_workloads):
        errors.append(f"{path}: required release workload {kind}/{name} is missing")
    return errors


def main(argv: list[str]) -> int:
    """Validate exactly one rendered manifest path."""
    if len(argv) != 2:
        sys.stderr.write(f"usage: {argv[0]} <rendered-manifest.yaml>\n")
        return 2
    path = Path(argv[1])
    if not path.is_file():
        sys.stderr.write(f"ERROR: rendered manifest not found: {path}\n")
        return 2
    errors = validate_manifest(path)
    if errors:
        sys.stderr.write("".join(f"ERROR: {error}\n" for error in errors))
        return 1
    sys.stdout.write(
        f"release manifest passed: every image uses an exact registry digest ({path})\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
