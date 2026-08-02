"""Offline characterization of prototype behavior still awaiting replacement.

The production domain and API suites specify tenant policy, tool validation, and
the migrated HTTP contracts. These tests close QA-001's remaining baseline gaps:
fallback routing, ingestion chunk boundaries, and tenant-scoped retrieval. The
prototype modules are loaded dynamically because they are scheduled for deletion,
not suitable as importable application packages.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def load_prototype(name: str, relative_path: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / relative_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load prototype module {relative_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


server = load_prototype("tenantchat_legacy_server", "server.py")
ingestion = load_prototype("tenantchat_legacy_ingestion", "services/ingestion/app.py")
financing = load_prototype("tenantchat_legacy_financing_agent", "services/financing-agent/app.py")


class TestFallbackRouting:
    def test_zip_question_routes_through_the_service_area_tool(self) -> None:
        reply, events = server.fallback_response(
            "apex",
            "session-1",
            server.TENANTS["apex"],
            [{"role": "user", "content": "Do you serve 98103?"}],
        )

        assert "serves 98103" in reply
        assert events == [
            {
                "name": "check_service_area",
                "arguments": {"zip": "98103"},
                "result": {
                    "served": True,
                    "zip": "98103",
                    "phone": "(555) 214-0800",
                },
            }
        ]

    def test_pricing_policy_routes_phone_first_tenant_away_from_a_quote(self) -> None:
        reply, events = server.fallback_response(
            "apex",
            "session-1",
            server.TENANTS["apex"],
            [{"role": "user", "content": "How much does HVAC cost?"}],
        )

        assert "does not provide pricing through chat" in reply
        assert "(555) 214-0800" in reply
        assert events == []

    def test_booking_intent_routes_to_tenant_specific_availability(self) -> None:
        reply, events = server.fallback_response(
            "clearview",
            "session-2",
            server.TENANTS["clearview"],
            [{"role": "user", "content": "Show me HVAC availability"}],
        )

        assert "Hvac openings" in reply
        assert events[0]["name"] == "get_availability"
        assert events[0]["arguments"] == {"service": "hvac"}
        assert events[0]["result"]["slots"] == server.TENANTS["clearview"]["availability"]["hvac"]


class TestIngestionChunking:
    def test_chunks_preserve_the_configured_overlap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ingestion, "CHUNK_TOKENS", 4)
        monkeypatch.setattr(ingestion, "CHUNK_OVERLAP", 2)

        chunks = list(ingestion.chunk_text("one two three four five six seven"))

        assert chunks == [
            "one two three four",
            "three four five six",
            "five six seven",
            "seven",
        ]

    def test_empty_input_produces_no_chunks(self) -> None:
        assert list(ingestion.chunk_text(" \n\t ")) == []


class FakeSearchResponse:
    status_code = 200

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return {
            "hits": {
                "hits": [
                    {
                        "_score": 0.91,
                        "_source": {
                            "title": "Financing Options",
                            "section": "Eligibility",
                            "text": "Subject to lender approval.",
                        },
                    }
                ]
            }
        }


class TestRetrievalFilters:
    def test_search_is_filtered_by_tenant_domain_and_active_status(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}

        def fake_post(url: str, **kwargs: Any) -> FakeSearchResponse:
            captured["url"] = url
            captured.update(kwargs)
            return FakeSearchResponse()

        monkeypatch.setattr(financing, "embed_query", lambda _query: [0.25, 0.75])
        monkeypatch.setattr(financing.requests, "post", fake_post)

        chunks = financing.search_chunks("apex", "Can I finance a repair?")

        assert captured["url"].endswith("/tenant-knowledge-chunks/_search")
        assert captured["json"]["knn"]["filter"] == [
            {"term": {"tenant_id": "apex"}},
            {"term": {"domain": "financing"}},
            {"term": {"active": True}},
        ]
        assert captured["json"]["knn"]["query_vector"] == [0.25, 0.75]
        assert chunks == [
            {
                "title": "Financing Options",
                "section": "Eligibility",
                "text": "Subject to lender approval.",
                "score": 0.91,
            }
        ]

    def test_missing_index_degrades_to_no_results(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class MissingIndexResponse(FakeSearchResponse):
            status_code = 404

        monkeypatch.setattr(financing, "embed_query", lambda _query: [1.0])
        monkeypatch.setattr(
            financing.requests, "post", lambda *_args, **_kwargs: MissingIndexResponse()
        )

        assert financing.search_chunks("apex", "financing") == []
