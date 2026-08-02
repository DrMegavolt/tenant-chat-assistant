"""Regression tests for the immutable image and deployment contract."""

from __future__ import annotations

from pathlib import Path

from scripts.verify_image_contracts import (
    DOCKERFILES,
    POSTGRES_FIXTURES,
    ROOT,
    RUNTIME_ENTRYPOINTS,
    local_module_dependencies,
    runtime_copy_sources,
    verify_dockerfiles,
    verify_manifests,
    verify_model,
    verify_model_cache,
    verify_pinned_postgres_images,
    verify_runtime_modules,
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


def test_images_bundle_every_repository_module_their_entrypoint_imports() -> None:
    errors: list[str] = []
    verify_runtime_modules(errors)
    assert errors == []


def test_module_closure_follows_imports_through_internal_auth() -> None:
    """Guards the check above against passing vacuously.

    `internal_auth` is imported directly by every service entrypoint and imports
    `runtime_security` in turn, so an image needs both. A closure that stopped at
    direct imports would accept an image that cannot start.
    """
    for entrypoint in RUNTIME_ENTRYPOINTS.values():
        assert local_module_dependencies(entrypoint) >= {
            "internal_auth.py",
            "runtime_security.py",
        }


def test_final_stage_copies_exclude_the_build_stage(tmp_path: Path) -> None:
    """Only build-context paths count as bundled source.

    `COPY --from=builder /app/.venv` names a path that exists in an earlier stage
    and not in the repository, so counting it would let a missing module look
    satisfied.
    """
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(
        "FROM base AS builder\nCOPY server.py ./\n"
        "FROM base AS runtime\n"
        "COPY --from=builder /app/.venv /app/.venv\n"
        "COPY --chown=10001:10001 internal_auth.py runtime_security.py ./\n",
        encoding="utf-8",
    )
    assert runtime_copy_sources(dockerfile) == {"internal_auth.py", "runtime_security.py"}


def test_every_disposable_postgres_is_the_same_pinned_server() -> None:
    errors: list[str] = []
    verify_pinned_postgres_images(errors)
    assert errors == []
    assert all(path.is_file() for path in POSTGRES_FIXTURES)
    assert ROOT / "tests/repositories/conftest.py" in POSTGRES_FIXTURES


def test_model_cache_mounts_match_the_image_hf_home() -> None:
    errors: list[str] = []
    verify_model_cache(errors)
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
