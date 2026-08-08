"""OTel SDK initialization for the API service (L8-OTEL).

Sets up the global tracer provider with an OTLP HTTP exporter pointed at the
collector's HTTP endpoint. The collector is the only fan-out point; the exporter
configuration is operator-facing and never touches content-export settings.

The tracer provider is initialized at startup and flushed at shutdown. A
deployment without an OTLP endpoint configured (``OTEL_EXPORTER_OTLP_ENDPOINT``
unset or empty) runs with a no-op provider, so a local development process
needs nothing extra to start.
"""

from __future__ import annotations

import logging
import os

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

logger = logging.getLogger(__name__)

_SERVICE_NAME = os.environ.get("OTEL_SERVICE_NAME", "chat-backend")
_OTLP_ENDPOINT = os.environ.get(
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "http://otel-gateway-collector.observability:4318/v1/traces",
)


def init_otel() -> TracerProvider:
    resource = Resource.create(
        {
            "service.name": _SERVICE_NAME,
            "service.namespace": "llm-chat",
        }
    )
    provider = TracerProvider(resource=resource)
    if _OTLP_ENDPOINT.strip():
        exporter = OTLPSpanExporter(endpoint=_OTLP_ENDPOINT.strip())
        processor = BatchSpanProcessor(exporter)
        provider.add_span_processor(processor)
        logger.info(
            "otel trace exporter configured",
            extra={"endpoint": _OTLP_ENDPOINT, "service": _SERVICE_NAME},
        )
    else:
        logger.info("otel running with no exporter (no-op)")
    trace.set_tracer_provider(provider)
    return provider


def shutdown_otel() -> None:
    provider = trace.get_tracer_provider()
    if isinstance(provider, TracerProvider):
        provider.shutdown()
