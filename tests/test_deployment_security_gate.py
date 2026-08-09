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


def test_chat_backend_deployment_rejects_development_auth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SEC-001: a manifest that turns off gateway auth is a manifest the gate refuses."""
    monkeypatch.setattr(security_gate, "ROOT", tmp_path)
    path = tmp_path / "chat-backend.yaml"
    document = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: chat-backend
spec:
  template:
    spec:
      containers:
        - name: chat-backend
          env:
            - name: CHAT_API_DEV_AUTH
              value: "true"
"""
    errors: list[str] = []

    security_gate._check_workload_refs(errors, [(path, document)])

    assert any("CHAT_API_DEV_AUTH must never be enabled" in error for error in errors)


def test_a_manifest_that_enables_content_export_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PRIV-002: content export is operator action, never a tracked manifest value."""
    monkeypatch.setattr(security_gate, "ROOT", tmp_path)
    path = tmp_path / "trace-export.yaml"
    document = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: chat-backend
spec:
  template:
    spec:
      containers:
        - name: chat-backend
          env:
            - name: TRACE_CONTENT_EXPORT
              value: "true"
"""
    errors: list[str] = []

    security_gate._check_trace_content_export(errors, [(path, document)])

    assert any("TRACE_CONTENT_EXPORT must never be enabled" in error for error in errors)


def test_a_manifest_that_exports_to_an_external_backend_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(security_gate, "ROOT", tmp_path)
    path = tmp_path / "trace-export.yaml"
    document = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: chat-backend
spec:
  template:
    spec:
      containers:
        - name: chat-backend
          env:
            - name: TRACE_CONTENT_EXPORT_ENDPOINT
              value: "https://langfuse.example.com:4318"
"""
    errors: list[str] = []

    security_gate._check_trace_content_export(errors, [(path, document)])

    assert any("must be a literal in-cluster URL" in error for error in errors)


def test_an_endpoint_reference_is_refused_because_it_cannot_be_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A config-map reference could name anything; the gate cannot see the boundary."""
    monkeypatch.setattr(security_gate, "ROOT", tmp_path)
    path = tmp_path / "trace-export.yaml"
    document = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: chat-backend
spec:
  template:
    spec:
      containers:
        - name: chat-backend
          env:
            - name: TRACE_CONTENT_EXPORT_ENDPOINT
              valueFrom:
                configMapKeyRef:
                  name: trace-export
                  key: endpoint
"""
    errors: list[str] = []

    security_gate._check_trace_content_export(errors, [(path, document)])

    assert any("must be a literal in-cluster URL" in error for error in errors)


class TestServicePortDrift:
    def test_matching_port_passes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(security_gate, "ROOT", tmp_path)
        svc = tmp_path / "svc.yaml"
        dep = tmp_path / "dep.yaml"
        svc_doc = """\
apiVersion: v1
kind: Service
metadata:
  name: chat-admin
spec:
  selector:
    app: chat-backend
  ports:
    - name: http
      port: 8004
      targetPort: 8004
"""
        dep_doc = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: chat-backend
spec:
  selector:
    matchLabels:
      app: chat-backend
  template:
    metadata:
      labels:
        app: chat-backend
    spec:
      containers:
        - name: chat-backend
          ports:
            - containerPort: 8004
              name: http
"""
        errors: list[str] = []

        security_gate._check_service_port_drift(errors, [(svc, svc_doc), (dep, dep_doc)])

        assert not errors

    def test_mismatched_target_port_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(security_gate, "ROOT", tmp_path)
        svc = tmp_path / "svc.yaml"
        dep = tmp_path / "dep.yaml"
        svc_doc = """\
apiVersion: v1
kind: Service
metadata:
  name: chat-backend
spec:
  selector:
    app: chat-backend
  ports:
    - name: http
      port: 8000
      targetPort: 8000
"""
        dep_doc = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: chat-backend
spec:
  selector:
    matchLabels:
      app: chat-backend
  template:
    metadata:
      labels:
        app: chat-backend
    spec:
      containers:
        - name: chat-backend
          ports:
            - containerPort: 8004
              name: http
"""
        errors: list[str] = []

        security_gate._check_service_port_drift(errors, [(svc, svc_doc), (dep, dep_doc)])

        assert len(errors) == 1
        assert "targetPort 8000" in errors[0]
        assert "containerPort in" in errors[0]

    def test_named_target_port_matches_named_container_port(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(security_gate, "ROOT", tmp_path)
        svc = tmp_path / "svc.yaml"
        dep = tmp_path / "dep.yaml"
        svc_doc = """\
apiVersion: v1
kind: Service
metadata:
  name: chat-admin
spec:
  selector:
    app: chat-backend
  ports:
    - name: http
      port: 8004
      targetPort: http
"""
        dep_doc = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: chat-backend
spec:
  selector:
    matchLabels:
      app: chat-backend
  template:
    metadata:
      labels:
        app: chat-backend
    spec:
      containers:
        - name: chat-backend
          ports:
            - containerPort: 8004
              name: http
"""
        errors: list[str] = []

        security_gate._check_service_port_drift(errors, [(svc, svc_doc), (dep, dep_doc)])

        assert not errors

    def test_service_matching_no_workload_produces_no_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(security_gate, "ROOT", tmp_path)
        svc = tmp_path / "svc.yaml"
        svc_doc = """\
apiVersion: v1
kind: Service
metadata:
  name: external-service
spec:
  selector:
    app: external-workload
  ports:
    - name: http
      port: 9999
      targetPort: 9999
"""
        errors: list[str] = []

        security_gate._check_service_port_drift(errors, [(svc, svc_doc)])

        assert not errors

    def test_statefulset_container_ports_are_checked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(security_gate, "ROOT", tmp_path)
        svc = tmp_path / "svc.yaml"
        sts = tmp_path / "sts.yaml"
        svc_doc = """\
apiVersion: v1
kind: Service
metadata:
  name: postgres
spec:
  selector:
    app: postgres
  ports:
    - name: postgres
      port: 5432
      targetPort: 9999
"""
        sts_doc = """\
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: postgres
spec:
  serviceName: postgres
  selector:
    matchLabels:
      app: postgres
  template:
    metadata:
      labels:
        app: postgres
    spec:
      containers:
        - name: postgres
          ports:
            - containerPort: 5432
              name: postgres
"""
        errors: list[str] = []

        security_gate._check_service_port_drift(errors, [(svc, svc_doc), (sts, sts_doc)])

        assert len(errors) == 1
        assert "targetPort 9999" in errors[0]


def test_an_in_cluster_export_endpoint_literal_passes_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(security_gate, "ROOT", tmp_path)
    path = tmp_path / "trace-export.yaml"
    document = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: chat-backend
spec:
  template:
    spec:
      containers:
        - name: chat-backend
          env:
            - name: TRACE_CONTENT_EXPORT_ENDPOINT
              value: "http://trace-viewer.observability.svc.cluster.local:4318"
"""
    errors: list[str] = []

    security_gate._check_trace_content_export(errors, [(path, document)])

    assert not errors
