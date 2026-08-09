#!/usr/bin/env python3
"""Render local MicroK8s release manifests from Buildx registry metadata."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

APPLICATION_IMAGES = ("api", "embedding", "web")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
UNRESOLVED_DIGEST = re.compile(r"REPLACE_WITH_[A-Z0-9_]+_DIGEST")


def _validated_digest(value: object, *, source: str) -> str:
    if not isinstance(value, str) or DIGEST.fullmatch(value) is None:
        raise ValueError(f"{source}: expected sha256:<64 lowercase hex> digest")
    return value


def load_build_digests(metadata_dir: Path) -> dict[str, str]:
    """Load the registry digest emitted by Buildx for every application image."""
    digests: dict[str, str] = {}
    for image in APPLICATION_IMAGES:
        path = metadata_dir / f"{image}.metadata.json"
        try:
            metadata = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise ValueError(f"missing Buildx metadata: {path}") from error
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid Buildx metadata JSON: {path}: {error}") from error
        if not isinstance(metadata, dict):
            raise ValueError(f"{path}: Buildx metadata must be a JSON object")
        digests[image] = _validated_digest(
            metadata.get("containerimage.digest"),
            source=f"{path}:containerimage.digest",
        )
    return digests


def _replace_exact(text: str, old: str, new: str, *, expected: int, source: Path) -> str:
    count = text.count(old)
    if count != expected:
        raise ValueError(f"{source}: expected {expected} occurrence(s) of {old!r}, found {count}")
    return text.replace(old, new)


def render_manifests(
    *,
    app_template: Path,
    migration_template: Path,
    seed_template: Path,
    app_output: Path,
    migration_output: Path,
    seed_output: Path,
    pull_repository: str,
    oauth2_proxy_digest: str,
    digests: dict[str, str],
) -> None:
    """Render application and migration templates using immutable image references."""
    oauth2_proxy_digest = _validated_digest(
        oauth2_proxy_digest,
        source="oauth2-proxy digest",
    )
    missing = set(APPLICATION_IMAGES) - digests.keys()
    if missing:
        raise ValueError(f"missing image digests: {', '.join(sorted(missing))}")

    app = app_template.read_text(encoding="utf-8")
    migration = migration_template.read_text(encoding="utf-8")
    seed = seed_template.read_text(encoding="utf-8")
    for image in APPLICATION_IMAGES:
        digest = _validated_digest(digests[image], source=f"{image} digest")
        old = (
            f"registry.example.invalid/tenantchat/{image}"
            f"@sha256:REPLACE_WITH_{image.upper()}_DIGEST"
        )
        new = f"{pull_repository}/{image}@{digest}"
        app = _replace_exact(
            app,
            old,
            new,
            expected=2 if image == "api" else 1,
            source=app_template,
        )
        if image == "api":
            migration = _replace_exact(
                migration,
                old,
                new,
                expected=1,
                source=migration_template,
            )
            seed = _replace_exact(
                seed,
                old,
                new,
                expected=1,
                source=seed_template,
            )

    oauth2_proxy_placeholder = "sha256:REPLACE_WITH_OAUTH2_PROXY_DIGEST"
    app = _replace_exact(
        app,
        oauth2_proxy_placeholder,
        oauth2_proxy_digest,
        expected=1,
        source=app_template,
    )
    for source, rendered in (
        (app_template, app),
        (migration_template, migration),
        (seed_template, seed),
    ):
        unresolved = UNRESOLVED_DIGEST.search(rendered)
        if unresolved is not None:
            raise ValueError(f"{source}: unresolved digest token {unresolved.group(0)}")

    app_output.parent.mkdir(parents=True, exist_ok=True)
    migration_output.parent.mkdir(parents=True, exist_ok=True)
    app_output.write_text(app, encoding="utf-8")
    migration_output.write_text(migration, encoding="utf-8")
    seed_output.write_text(seed, encoding="utf-8")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metadata-dir", type=Path, required=True)
    parser.add_argument("--app-template", type=Path, required=True)
    parser.add_argument("--migration-template", type=Path, required=True)
    parser.add_argument("--seed-template", type=Path, required=True)
    parser.add_argument("--app-output", type=Path, required=True)
    parser.add_argument("--migration-output", type=Path, required=True)
    parser.add_argument("--seed-output", type=Path, required=True)
    parser.add_argument("--pull-repository", required=True)
    parser.add_argument("--oauth2-proxy-digest", required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        digests = load_build_digests(args.metadata_dir)
        render_manifests(
            app_template=args.app_template,
            migration_template=args.migration_template,
            seed_template=args.seed_template,
            app_output=args.app_output,
            migration_output=args.migration_output,
            seed_output=args.seed_output,
            pull_repository=args.pull_repository.rstrip("/"),
            oauth2_proxy_digest=args.oauth2_proxy_digest,
            digests=digests,
        )
    except (OSError, ValueError) as error:
        raise SystemExit(f"ERROR: {error}") from error
    sys.stdout.write(f"rendered local release manifests in {args.app_output.parent}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
