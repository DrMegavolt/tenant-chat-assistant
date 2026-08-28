"""The Prometheus adapter behind the core metrics port (OBS-002).

Everything the scraper sees is defined here: the collector per metric name, its
kind, its help text, and — enforced at record time — the label contract from
``tenantchat.core.metrics``. The label values a caller may pass are the closed
vocabularies of the core enums, the routing/tool enums, and the tenant
pseudonym; anything else is refused with :class:`MetricLabelError` before it
can become a series.

Exemplars carry the trace ID from the correlation context (`OBS-001`), so a
latency spike in Grafana can drill through to the one turn that caused it. The
exemplar is an identifier only: the turn's content stays in the inference plane
(`ADR-0010`).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    start_http_server,
)
from prometheus_client.openmetrics.exposition import (
    CONTENT_TYPE_LATEST as OPENMETRICS_CONTENT_TYPE,
)
from prometheus_client.openmetrics.exposition import generate_latest as generate_openmetrics

from tenantchat.api.correlation import trace_id as current_trace_id
from tenantchat.core.metrics import (
    METRIC_LABELS,
    MetricKind,
    MetricLabelError,
    MetricName,
    label_value_is_safe,
)

# Kind and help text per metric. The label-name contract lives in core so the
# orchestration package and the API share one vocabulary; the kind and the
# documentation are adapter concerns.
METRIC_DEFINITIONS: Final[Mapping[MetricName, tuple[MetricKind, str]]] = {
    MetricName.TURN_LATENCY: (
        MetricKind.HISTOGRAM,
        "Duration of one completed or paused turn, request to answer.",
    ),
    MetricName.TURN_OUTCOMES: (
        MetricKind.COUNTER,
        "Turns by outcome class: answered, clarified, abstained, handed off, or paused.",
    ),
    MetricName.LLM_CALLS: (
        MetricKind.COUNTER,
        "Model completions by status and prompt template version.",
    ),
    MetricName.LLM_LATENCY: (
        MetricKind.HISTOGRAM,
        "Model completion latency by status and prompt template version.",
    ),
    MetricName.LLM_TOKENS: (
        MetricKind.COUNTER,
        "Tokens consumed per completion by kind and prompt template version.",
    ),
    MetricName.RETRIEVAL_RUNS: (
        MetricKind.COUNTER,
        "Retrieval runs by status and evidence verdict.",
    ),
    MetricName.RETRIEVAL_LATENCY: (
        MetricKind.HISTOGRAM,
        "Retrieval latency by status.",
    ),
    MetricName.RETRIEVAL_CANDIDATES: (
        MetricKind.COUNTER,
        "Evidence passages retrieved and admitted as candidates.",
    ),
    MetricName.TOOL_CALLS: (
        MetricKind.COUNTER,
        "Tool executions by tool and outcome.",
    ),
    MetricName.TOOL_LATENCY: (
        MetricKind.HISTOGRAM,
        "Tool execution latency by tool and outcome.",
    ),
    MetricName.NODE_LATENCY: (
        MetricKind.HISTOGRAM,
        "Graph node execution latency by node and terminal status.",
    ),
    MetricName.ROUTING_DECISIONS: (
        MetricKind.COUNTER,
        "Routing decisions by chosen intent, outcome, and rule.",
    ),
    MetricName.BUSINESS_ACTIONS: (
        MetricKind.COUNTER,
        "Business actions by operation and status (committed, replayed, refused).",
    ),
    MetricName.BUSINESS_LATENCY: (
        MetricKind.HISTOGRAM,
        "Business action latency by operation.",
    ),
    MetricName.CITATION_VALIDATION: (
        MetricKind.COUNTER,
        "Citation-validation verdicts on published answers.",
    ),
    MetricName.FEEDBACK_SUBMITTED: (
        MetricKind.COUNTER,
        "Visitor feedback submissions by rating: up or down.",
    ),
    MetricName.DEPENDENCY_RETRIES: (
        MetricKind.COUNTER,
        "Dependency client retries by dependency and failure reason.",
    ),
    MetricName.CIRCUIT_STATE: (
        MetricKind.GAUGE,
        "Circuit state per dependency, 1 on the active state and 0 elsewhere.",
    ),
    MetricName.POLICY_BLOCKS: (
        MetricKind.COUNTER,
        "Requests or actions refused by budget or content policy, by reason.",
    ),
    MetricName.MODEL_FALLBACKS: (
        MetricKind.COUNTER,
        "Model calls handed to a fallback provider, by failure reason.",
    ),
    MetricName.RESPONSE_CACHE: (
        MetricKind.COUNTER,
        "Safe-response cache lookups by result: hit or miss.",
    ),
    MetricName.BUDGET_ALERTS: (
        MetricKind.COUNTER,
        "Per-tenant spend alerts fired by level: warn or critical.",
    ),
    MetricName.ROUTER_CONFIDENCE: (
        MetricKind.HISTOGRAM,
        "Router confidence distribution across routing decisions.",
    ),
    MetricName.CONTEXT_TRUNCATION: (
        MetricKind.COUNTER,
        "Prompt context items dropped by assembly budget, by kind: history or evidence.",
    ),
    MetricName.TOKEN_COST: (
        MetricKind.COUNTER,
        "Estimated token cost by kind and prompt template, in micro-dollars.",
    ),
}


# Latency-bucket ceilings must exceed real traffic, or p95 quantiles
# saturate at the top edge and render a flat line (observed: model latency
# reaches ~33s against the prometheus_client default ceiling of 10s).
# Only the turn/LLM histograms are widened; ROUTER_CONFIDENCE is a raw-score
# distribution whose dashboards are calibrated to the default le<=10 edges.
WIDE_LATENCY_BUCKETS: Final[tuple[float, ...]] = (
    0.25,
    0.5,
    1.0,
    2.0,
    4.0,
    6.0,
    10.0,
    15.0,
    20.0,
    30.0,
    45.0,
    60.0,
    90.0,
)


class PrometheusMetrics:
    """Implements :class:`MetricsReporter` over one collector registry.

    Collectors are created on first observation, so a deployment that never
    takes a chat turn scrapes no empty series. The label contract is checked on
    every observation: an unknown name, a label name outside the metric's
    contract, or a label value outside the bounded charset raises
    :class:`MetricLabelError` instead of reaching the registry.
    """

    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry or REGISTRY
        self._collectors: dict[MetricName, Counter | Histogram | Gauge] = {}

    def observe(
        self,
        name: MetricName,
        value: float,
        *,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        supplied = dict(labels or {})
        allowed = METRIC_LABELS.get(name)
        if allowed is None:
            raise MetricLabelError(f"no metric registered under name {name.value!r}")
        allowed_names = {label.value for label in allowed}
        unknown = sorted(set(supplied) - allowed_names)
        if unknown:
            raise MetricLabelError(
                f"labels {unknown} are not part of the contract for {name.value}"
            )
        unsafe = [
            label_value
            for key, label_value in supplied.items()
            if not label_value_is_safe(label_value)
        ]
        if unsafe:
            raise MetricLabelError(f"label values {unsafe!r} are not bounded-safe for {name.value}")

        collector = self._collectors.get(name)
        if collector is None:
            collector = self._build(name)
            self._collectors[name] = collector
        exemplar = self._exemplar()
        if isinstance(collector, Counter):
            if supplied:
                collector.labels(**supplied).inc(value, exemplar=exemplar)
            else:
                collector.inc(value, exemplar=exemplar)
        elif isinstance(collector, Gauge):
            # Gauges carry the current state, not per-request observations, so
            # they are set without an exemplar: a state value is not a spike to
            # drill into.
            if supplied:
                collector.labels(**supplied).set(value)
            else:
                collector.set(value)
        else:
            if supplied:
                collector.labels(**supplied).observe(value, exemplar=exemplar)
            else:
                collector.observe(value, exemplar=exemplar)

    def reset(self) -> None:
        """Drop every collector this adapter registered.

        Test-only: returns the shared registry to the state before the first
        observation, so a test can assert on the samples it produced.
        """
        for collector in self._collectors.values():
            self.registry.unregister(collector)
        self._collectors.clear()

    def render(self, *, openmetrics: bool) -> tuple[bytes, str]:
        """The exposition format for the scrape endpoint.

        The classic text format carries no exemplars; a scraper that accepts
        OpenMetrics gets the trace-ID exemplars `OBS-002` requires for the
        drill-through, and every other scraper still gets the classic format.
        """
        if openmetrics:
            return (
                generate_openmetrics(self.registry),  # type: ignore[no-untyped-call]
                OPENMETRICS_CONTENT_TYPE,
            )
        return generate_latest(self.registry), CONTENT_TYPE_LATEST

    def _build(self, name: MetricName) -> Counter | Histogram | Gauge:
        kind, help_text = METRIC_DEFINITIONS[name]
        label_names = tuple(label.value for label in METRIC_LABELS[name])
        if kind is MetricKind.COUNTER:
            return Counter(name.value, help_text, label_names, registry=self.registry)
        if kind is MetricKind.GAUGE:
            return Gauge(name.value, help_text, label_names, registry=self.registry)
        if name in (MetricName.TURN_LATENCY, MetricName.LLM_LATENCY):
            return Histogram(
                name.value,
                help_text,
                label_names,
                buckets=WIDE_LATENCY_BUCKETS,
                registry=self.registry,
            )
        return Histogram(name.value, help_text, label_names, registry=self.registry)

    @staticmethod
    def _exemplar() -> dict[str, str] | None:
        """The current request's trace ID as an exemplar, when one is bound.

        The exemplar is the drill-through handle `OBS-002` requires: it carries
        the trace ID only, and the content that trace ID indexes lives in the
        inference plane.
        """
        trace = current_trace_id()
        return {"trace_id": trace} if trace is not None else None


# The process-wide recorder the API-layer adapters (chat router, action
# services, retrieval adapter) and the composition root use. Tests exercise it
# through the same instance and reset it between assertions.
METRICS = PrometheusMetrics()


def render_metrics(*, openmetrics: bool) -> tuple[bytes, str]:
    """The body and media type of the ``/metrics`` response."""
    return METRICS.render(openmetrics=openmetrics)


def serve_metrics(port: int) -> None:
    """Expose this process's metric plane on ``/metrics`` until it exits.

    The worker records the same `OBS-002` counters as the API but serves no
    application traffic, so without its own scrape endpoint every job, retry,
    and ingestion metric it produces stays in a registry nothing reads. Returns
    once the listener thread is running; the thread is a daemon, so a worker
    shutdown is never held open by a scraper.

    Content negotiation matches the API's route: a scraper that accepts
    OpenMetrics gets the trace-ID exemplars, everything else gets classic text.
    """
    start_http_server(port, registry=METRICS.registry)
