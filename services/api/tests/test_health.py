"""Liveness stays shallow while readiness fails on required dependencies."""

from __future__ import annotations

from dataclasses import replace
from typing import cast

from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_liveness_and_readiness_are_distinct(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").json() == {"status": "ready"}


def test_required_rag_dependency_failure_removes_readiness(client: TestClient) -> None:
    class BrokenEvidence:
        async def ready(self, *, tenant_id: str) -> None:
            del tenant_id
            raise RuntimeError("dependency detail must not escape")

    app = cast(FastAPI, client.app)
    app.state.settings = replace(app.state.settings, rag_required=True)
    app.state.evidence_source = BrokenEvidence()

    response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}
    assert client.get("/healthz").status_code == 200
