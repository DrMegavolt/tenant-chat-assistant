"""OTel SDK initialization for the API service (L8-OTEL).

Sets up the global tracer provider with an OTLP HTTP exporter pointed at the
collector's HTTP endpoint. The collector is the only fan-out point; the exporter
configuration is operator-facing and never touches content-export settings.

The tracer provider is initialized at startup and flushed at shutdown. When the
``OTEL_EXPORTER_OTLP_ENDPOINT`` environment variable is unset or empty the
function returns a no-op provider with no exporter, so a local development
process or hermetic test suite needs nothing extra to start.
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


def init_otel() -> TracerProvider:
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    service_name = os.environ.get("OTEL_SERVICE_NAME", "chat-backend").strip()
    resource = Resource.create(
        {
            "service.name": service_name,
            "service.namespace": "llm-chat",
        }
    )
    provider = TracerProvider(resource=resource)
    if endpoint:
        exporter = OTLPSpanExporter(endpoint=endpoint)
        processor = BatchSpanProcessor(exporter)
        provider.add_span_processor(processor)
        logger.info(
            "otel trace exporter configured",
            extra={"endpoint": endpoint, "service": service_name},
        )
    else:
        logger.info("otel running with no exporter (no-op)")
    trace.set_tracer_provider(provider)
    return provider


def shutdown_otel() -> None:
    provider = trace.get_tracer_provider()
    if isinstance(provider, TracerProvider):
        provider.shutdown()
