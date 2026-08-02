"""Regression tests for the immutable image and deployment contract."""

from __future__ import annotations

from pathlib import Path

from scripts.verify_image_contracts import (
    DOCKERFILES,
    verify_dockerfiles,
    verify_manifests,
    verify_model,
)
from scripts.verify_release_manifest import REQUIRED_WORKLOADS, validate_manifest


def test_all_five_deployable_images_are_covered() -> None:
    assert len(DOCKERFILES) == 5
    assert all(path.is_file() for path in DOCKERFILES)


def test_dockerfiles_are_locked_and_non_root() -> None:
    errors: list[str] = []
    verify_dockerfiles(errors)
    assert errors == []


def test_kubernetes_uses_images_as_immutable_artifacts() -> None:
    errors: list[str] = []
    verify_manifests(errors)
    assert errors == []


def test_embedding_model_code_and_revision_are_pinned() -> None:
    errors: list[str] = []
    verify_model(errors)
    assert errors == []


def test_rendered_release_rejects_tags_and_unresolved_digest_tokens(tmp_path: Path) -> None:
    manifest = tmp_path / "release.yaml"
    manifest.write_text(
        """\
containers:
  - image: example.invalid/tenantchat/api:latest
  - image: example.invalid/tenantchat/api@sha256:REPLACE_WITH_API_DIGEST
""",
        encoding="utf-8",
    )
    errors = validate_manifest(manifest)
    assert any("not pinned" in error for error in errors)


def test_rendered_release_rejects_a_zero_image_noop(tmp_path: Path) -> None:
    manifest = tmp_path / "release.yaml"
    manifest.write_text(
        "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: noop\n", encoding="utf-8"
    )
    errors = validate_manifest(manifest)
    assert len(errors) == len(REQUIRED_WORKLOADS)


def test_rendered_release_accepts_only_exact_registry_digests(tmp_path: Path) -> None:
    manifest = tmp_path / "release.yaml"
    documents = (
        f"apiVersion: apps/v1\nkind: {kind}\nmetadata:\n  name: {name}\n"
        f"spec:\n  template:\n    spec:\n      containers:\n"
        f"        - image: example.invalid/tenantchat/{name}@sha256:{'a' * 64}\n"
        for kind, name in sorted(REQUIRED_WORKLOADS)
    )
    manifest.write_text("---\n".join(documents), encoding="utf-8")
    assert validate_manifest(manifest) == []
