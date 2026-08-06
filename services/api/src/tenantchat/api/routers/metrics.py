"""The scrape endpoint for the OBS-002 metrics plane.

An operations surface, not a client contract: it is served outside the OpenAPI
document on purpose, exactly like the side services' ``/metrics`` routes. The
labels it exports are the bounded vocabulary of
:mod:`tenantchat.api.metrics`, so scraping it leaks no content (`ADR-0010`).
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

from tenantchat.api.metrics import render_metrics

router = APIRouter(tags=["operations"])


@router.get("/metrics", include_in_schema=False)
def metrics(request: Request) -> PlainTextResponse:
    """The Prometheus exposition of the process's metric plane.

    Served as OpenMetrics when the scraper accepts it — the only format that
    carries the trace-ID exemplars — and as classic text otherwise.
    """
    openmetrics = "openmetrics" in request.headers.get("accept", "")
    body, content_type = render_metrics(openmetrics=openmetrics)
    return PlainTextResponse(body, media_type=content_type)
