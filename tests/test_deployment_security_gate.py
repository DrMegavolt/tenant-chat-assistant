from __future__ import annotations

from pathlib import Path

import pytest

from scripts import verify_deployment_security as security_gate


@pytest.fixture
def example_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    k8s_dir = tmp_path / "k8s"
    examples_dir = k8s_dir / "examples"
    examples_dir.mkdir(parents=True)
    (tmp_path / ".env.example").write_text("LLM_API_KEY=\n", encoding="utf-8")
    monkeypatch.setattr(security_gate, "ROOT", tmp_path)
    monkeypatch.setattr(security_gate, "K8S_DIR", k8s_dir)
    return examples_dir


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("apiKey", "provider-key-that-must-not-be-committed"),
        ("databaseUrl", "postgresql://tenantchat:live-password@postgres/tenantchat"),
        ("DATABASE_MIGRATION_URL", "postgresql://owner:live-password@postgres/tenantchat"),
    ],
)
def test_examples_reject_literal_credentials(
    example_tree: Path,
    name: str,
    value: str,
) -> None:
    (example_tree / "credentials.env.example").write_text(
        f"{name}={value}\n",
        encoding="utf-8",
    )
    errors: list[str] = []

    security_gate._check_examples(errors)

    assert any(name in error for error in errors)


def test_source_documents_reject_private_endpoint_and_literal_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(security_gate, "ROOT", tmp_path)
    path = tmp_path / "unsafe.yaml"
    document = """\
apiVersion: v1
kind: Secret
metadata:
  name: unsafe
stringData:
  password: committed-value
  endpoint: http://10.20.30.40:1234
"""
    errors: list[str] = []

    security_gate._scan_source_documents(errors, [(path, document)])

    assert any("private network endpoint" in error for error in errors)
    assert any("literal credential" in error for error in errors)


def test_sensitive_environment_literal_value_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(security_gate, "ROOT", tmp_path)
    path = tmp_path / "deployment.yaml"
    document = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: unsafe
spec:
  template:
    spec:
      containers:
        - name: unsafe
          env:
            - name: LLM_API_KEY
              value: committed-value
"""
    errors: list[str] = []

    security_gate._scan_source_documents(errors, [(path, document)])

    assert any("LLM_API_KEY has a literal" in error for error in errors)
