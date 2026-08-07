"""Tests for the one-command local MicroK8s release workflow."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.render_local_k8s_release import (
    APPLICATION_IMAGES,
    load_build_digests,
    render_manifests,
)
from scripts.verify_release_manifest import validate_manifest

ROOT = Path(__file__).resolve().parents[1]


def _metadata(metadata_dir: Path) -> dict[str, str]:
    digests: dict[str, str] = {}
    metadata_dir.mkdir()
    for index, image in enumerate(APPLICATION_IMAGES, start=1):
        digest = f"sha256:{index:064x}"
        digests[image] = digest
        (metadata_dir / f"{image}.metadata.json").write_text(
            json.dumps({"containerimage.digest": digest}),
            encoding="utf-8",
        )
    return digests


def test_local_release_renders_the_new_registry_digests(tmp_path: Path) -> None:
    metadata_dir = tmp_path / "metadata"
    expected = _metadata(metadata_dir)
    app_output = tmp_path / "app.release.yaml"
    migration_output = tmp_path / "migration.release.yaml"

    render_manifests(
        app_template=ROOT / "k8s/app.yaml",
        migration_template=ROOT / "k8s/api-migration-job.yaml",
        app_output=app_output,
        migration_output=migration_output,
        pull_repository="localhost:32000/tenantchat",
        oauth2_proxy_digest=f"sha256:{'f' * 64}",
        digests=load_build_digests(metadata_dir),
    )

    app = app_output.read_text(encoding="utf-8")
    migration = migration_output.read_text(encoding="utf-8")
    assert "REPLACE_WITH_" not in app
    assert "REPLACE_WITH_" not in migration
    assert validate_manifest(app_output) == []
    for image, digest in expected.items():
        assert f"localhost:32000/tenantchat/{image}@{digest}" in app
    assert f"localhost:32000/tenantchat/api@{expected['api']}" in migration


def test_local_release_rejects_invalid_buildx_metadata(tmp_path: Path) -> None:
    metadata_dir = tmp_path / "metadata"
    _metadata(metadata_dir)
    (metadata_dir / "api.metadata.json").write_text(
        json.dumps({"containerimage.digest": "sha256:not-a-digest"}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="expected sha256"):
        load_build_digests(metadata_dir)


def test_deploy_wrapper_runs_migration_before_application_rollout() -> None:
    script = (ROOT / "scripts/deploy_local_k8s.sh").read_text(encoding="utf-8")
    backup = "elif ! backup_database; then"
    migration_apply = 'kubectl apply -f "$MIGRATION_RELEASE"'
    migration_wait = "job/tenantchat-api-migrate --timeout=900s"
    deploy = '"$ROOT_DIR/k8s/deploy.sh" "$APP_RELEASE"'

    assert "--platform linux/amd64" in script
    assert '--metadata-file "$metadata"' in script
    assert "pg_dump --format=custom" in script
    assert "pg_restore --list" in script
    assert 'base64 "$archive"' in script
    assert "LOCAL_K8S_SKIP_BUILD" in script
    assert "LOCAL_K8S_SKIP_BACKUP" in script
    assert "LOCAL_K8S_REQUIRE_BACKUP" in script
    assert script.index(backup) < script.index(migration_apply)
    assert script.index(migration_wait) < script.index(deploy)


def test_make_exposes_the_one_line_local_release() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "deploy-local: ## Build, migrate, and deploy" in makefile
    assert "\t./scripts/deploy_local_k8s.sh" in makefile
