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
    allowlist = processors["redaction"]["allow_attributes"]
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
