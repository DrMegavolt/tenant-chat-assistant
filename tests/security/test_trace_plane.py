"""PRIV-002's plane boundary, stated as regressions: content cannot leave the
cluster, and the setting that would move it is refused before startup finishes.

`ADR-0010` draws the boundary in three places and each has a test here. The
deployment gate refuses a manifest that enables `TRACE_CONTENT_EXPORT`; the
application refuses to start with it enabled for a backend outside the trust
boundary; and the collector's redaction processor — the only fan-out point —
drops everything not on its allowlist, so a backend added later cannot widen
what leaves the cluster under either setting.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import cast

import pytest
import yaml  # type: ignore[import-untyped]

from tenantchat.api.settings import Settings, validate_trace_content_export

REPO_ROOT = Path(__file__).resolve().parents[2]

# Content-bearing GenAI attribute keys, by the semantic-convention names a
# trace backend would search by. None of them may appear on the collector's
# allowlist, and a span carrying them must lose them at the redaction processor.
CONTENT_ATTRIBUTES: tuple[str, ...] = (
    "gen_ai.prompt",
    "gen_ai.completion",
    "gen_ai.input",
    "gen_ai.output",
    "gen_ai.request.messages",
    "gen_ai.response.output_text",
    "message.content",
    "conversation.content",
)


def _collector_document() -> dict[str, object]:
    path = REPO_ROOT / "k8s" / "otel-collector.yaml"
    documents = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
    collector = next(
        (
            doc
            for doc in documents
            if doc is not None and doc.get("kind") == "OpenTelemetryCollector"
        ),
        None,
    )
    assert collector is not None, "k8s/otel-collector.yaml must declare the collector"
    return cast(dict[str, object], collector)


def _collector_config() -> dict[str, object]:
    spec = _collector_document()["spec"]
    assert isinstance(spec, dict)
    config = spec["config"]
    assert isinstance(config, dict)
    return config


def _redaction_allowlist() -> list[str]:
    processors = _collector_config()["processors"]
    assert isinstance(processors, dict)
    # The collector 0.127.0 image renamed `allow_attributes` to `allowed_keys`;
    # the manifest ships the new key, so the test must read it.
    allowlist = processors["redaction"]["allowed_keys"]
    assert isinstance(allowlist, list)
    assert all(isinstance(entry, str) for entry in allowlist)
    return allowlist


def _pipelines() -> dict[str, dict[str, object]]:
    service = _collector_config()["service"]
    assert isinstance(service, dict)
    pipelines = service["pipelines"]
    assert isinstance(pipelines, dict)
    return pipelines


def test_content_export_is_disabled_by_default() -> None:
    settings = Settings(
        allowed_origins=(),
        max_request_bytes=1024,
        docs_enabled=False,
    )

    validate_trace_content_export(settings)
    assert settings.trace_content_export is False


def test_content_export_without_an_endpoint_is_refused_at_startup() -> None:
    settings = Settings(
        allowed_origins=(),
        max_request_bytes=1024,
        docs_enabled=False,
        trace_content_export=True,
        trace_content_export_endpoint=None,
    )

    with pytest.raises(ValueError, match="TRACE_CONTENT_EXPORT_ENDPOINT"):
        validate_trace_content_export(settings)


def test_content_export_to_an_external_backend_is_refused_at_startup() -> None:
    """The acceptance criterion: production startup fails for an external backend.

    The trust boundary is loopback or in-cluster service DNS; anything else is
    refused no matter how well-intentioned the deployment is.
    """
    for endpoint in (
        "https://langfuse.example.com:4318",
        "http://viewer.company.com",
        "https://trace.mycorp.io/v1",
    ):
        settings = Settings(
            allowed_origins=(),
            max_request_bytes=1024,
            docs_enabled=False,
            trace_content_export=True,
            trace_content_export_endpoint=endpoint,
        )
        with pytest.raises(ValueError, match="cluster trust boundary"):
            validate_trace_content_export(settings)


def test_content_export_to_an_in_cluster_backend_passes_startup() -> None:
    settings = Settings(
        allowed_origins=(),
        max_request_bytes=1024,
        docs_enabled=False,
        trace_content_export=True,
        trace_content_export_endpoint="http://trace-viewer.observability.svc.cluster.local:4318",
    )

    validate_trace_content_export(settings)


def test_content_export_to_loopback_passes_startup() -> None:
    """Development against a local viewer is the one legitimate external shape."""
    settings = Settings(
        allowed_origins=(),
        max_request_bytes=1024,
        docs_enabled=False,
        trace_content_export=True,
        trace_content_export_endpoint="http://127.0.0.1:4318",
    )

    validate_trace_content_export(settings)


def test_the_collector_redacts_ahead_of_every_exporter() -> None:
    """The redaction processor precedes the batch processor in every pipeline.

    Processors run in order, so a processor ahead of ``batch`` is ahead of
    every exporter a pipeline lists — and any exporter added to the pipeline
    later inherits the same redaction. A pipeline that omits it would be a
    backdoor; the test refuses to let one exist.
    """
    processors = _collector_config()["processors"]
    assert isinstance(processors, dict)
    assert "redaction" in processors, "the collector config must declare the redaction processor"

    for name, pipeline in _pipelines().items():
        chain = pipeline.get("processors")
        assert isinstance(chain, list), name
        assert "redaction" in chain, f"pipeline {name} must run redaction"
        assert chain.index("redaction") < chain.index(
            "batch"
        ), f"pipeline {name} must redact before the batch/export stage"
        assert "batch" in chain, f"pipeline {name} must batch after redaction"


def test_the_redaction_allowlist_carries_no_content_attribute() -> None:
    """The allowlist is the whole story: unlisted keys are dropped.

    Each content-bearing attribute name must not match any allowlist entry, so
    a span carrying prompt or completion text loses it here — under either
    `TRACE_CONTENT_EXPORT` setting, because the setting never widens this list.
    """
    allowlist = _redaction_allowlist()
    assert allowlist

    for content_attribute in CONTENT_ATTRIBUTES:
        assert not any(
            re.fullmatch(_anchor(pattern), content_attribute) for pattern in allowlist
        ), f"allowlist admits content-bearing attribute {content_attribute}"


def _anchor(pattern: object) -> str:
    """Treat a YAML allowlist entry as the regex the processor compiles."""
    assert isinstance(pattern, str)
    return rf"^{pattern}$"


def test_every_content_span_attribute_is_dropped_by_the_allowlist() -> None:
    """A span carrying content in any form leaves the cluster content-free.

    Simulates the redaction processor's contract: an attribute survives only
    if its key matches an allowlist entry. Content keys never match, so the
    values are gone before any exporter sees the span.
    """
    allowlist = _redaction_allowlist()

    span_attributes = {
        "service.name": "chat-backend",
        "gen_ai.prompt": "Please book me at 555-222-1919",
        "gen_ai.completion": "Booked for Dana Ruiz",
        "message.content": "my address is 12 Alder Court",
    }
    surviving = {
        key: value
        for key, value in span_attributes.items()
        if any(re.fullmatch(_anchor(pattern), key) for pattern in allowlist)
    }

    assert surviving == {"service.name": "chat-backend"}
    for value in surviving.values():
        assert isinstance(value, str)
        assert "555-222-1919" not in value
        assert "Dana Ruiz" not in value
        assert "12 Alder Court" not in value


def test_the_manifest_never_enables_content_export() -> None:
    """The tracked deployment ships content export off; enabling it is operator action."""
    from scripts import verify_deployment_security as security_gate

    documents = security_gate.deployment_documents()
    errors: list[str] = []
    security_gate._check_trace_content_export(errors, documents)

    assert not errors, "the tracked deployment must not enable TRACE_CONTENT_EXPORT"


def test_every_emitted_attribute_is_on_the_collector_allowlist() -> None:
    """Every attribute the orchestration OTel module sets must survive redaction.

    The collector is the only fan-out point and its allowlist is the whole story:
    an attribute not on it is dropped before any exporter sees it. If the emitter
    sets a key that the allowlist does not name, the value is lost at the collector
    and the span attribute is effectively dead. This test guarantees the two stay
    in sync.
    """
    from tenantchat.orchestration.otel import EMITTED_ATTRIBUTES

    allowlist = _redaction_allowlist()
    missing = [
        attr
        for attr in EMITTED_ATTRIBUTES
        if not any(re.fullmatch(_anchor(pattern), attr) for pattern in allowlist)
    ]
    assert (
        not missing
    ), f"orchestration OTel module emits attributes not on the collector allowlist: {missing}"


def test_no_content_attribute_is_emitted_by_the_otel_module() -> None:
    """The emitter must never set an attribute whose key is a content one.

    The allowlist test above prevents content from reaching an exporter, but the
    emitter itself must not name a content-bearing key — because naming one that
    the allowlist later drops is still a content-bearing key in source code, and
    a grep of the codebase for those keys must return nothing outside of this
    test and the redaction config comment.
    """
    from tenantchat.orchestration.otel import EMITTED_ATTRIBUTES

    for content_attribute in CONTENT_ATTRIBUTES:
        assert (
            content_attribute not in EMITTED_ATTRIBUTES
        ), f"orchestration OTel module has a content-bearing attribute: {content_attribute}"


def _exporter_endpoints() -> list[str]:
    config = _collector_config()
    exporters = config["exporters"]
    assert isinstance(exporters, dict)
    endpoints: list[str] = []
    for _name, spec in exporters.items():
        if not isinstance(spec, dict):
            continue
        endpoint = spec.get("endpoint")
        if isinstance(endpoint, str):
            endpoints.append(endpoint)
    return endpoints


def test_phoenix_and_mlflow_exporters_are_in_cluster() -> None:
    """The two GenAI trace viewers must be reachable only inside the cluster.

    Phoenix and MLflow sit downstream of redaction, so adding them was a config
    change, not a privacy decision. Their endpoints must name in-cluster
    addresses so no one accidentally widens the trust boundary by editing the
    manifest.
    """
    from urllib.parse import urlparse

    endpoints = _exporter_endpoints()
    viewer_endpoints = [ep for ep in endpoints if "phoenix" in ep or "mlflow" in ep]
    assert viewer_endpoints, "must find at least one Phoenix or MLflow exporter endpoint"

    external: list[str] = []
    for endpoint in viewer_endpoints:
        host = (urlparse(endpoint).hostname or "").lower()
        if (
            host
            and not host.endswith(".svc.cluster.local")
            and host
            not in (
                "localhost",
                "127.0.0.1",
                "::1",
            )
        ):
            external.append(endpoint)
    assert not external, f"Phoenix/MLflow exporter endpoints must be in-cluster, got: {external}"


def test_chat_backend_and_job_worker_have_instrumentation_annotations() -> None:
    """Both the API and the worker must carry the OTel Python injection annotation.

    Without it, the k8s instrumentation operator does not inject the OTel SDK
    and the auto-instrumented HTTP/DB spans never appear. The manual GenAI spans
    from the orchestration layer still work (they use the API, not the SDK), but
    the turn is not followable across services without the base HTTP spans from
    auto-instrumentation.
    """
    app_yaml = REPO_ROOT / "k8s" / "app.yaml"
    documents = list(yaml.safe_load_all(app_yaml.read_text(encoding="utf-8")))
    annotated: set[str] = set()
    for doc in documents:
        if not isinstance(doc, dict) or doc.get("kind") != "Deployment":
            continue
        metadata = doc.get("metadata", {})
        name = metadata.get("name", "") if isinstance(metadata, dict) else ""
        spec = doc.get("spec", {})
        template = spec.get("template", {}) if isinstance(spec, dict) else {}
        template_meta = template.get("metadata", {}) if isinstance(template, dict) else {}
        annotations = (
            template_meta.get("annotations", {}) if isinstance(template_meta, dict) else {}
        )
        if annotations.get("instrumentation.opentelemetry.io/inject-python"):
            annotated.add(str(name))

    expected = {"chat-backend", "job-worker"}
    missing = expected - annotated
    assert (
        not missing
    ), f"these deployments lack the OTel Python instrumentation annotation: {missing}"
