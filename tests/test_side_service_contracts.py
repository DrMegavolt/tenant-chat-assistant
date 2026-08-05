"""Characterization of the ingestion and financing side-service contracts.

These behaviors predate the shared packages and are still exercised through the
loose-file service entrypoints, which are excluded from lint and type checking
but shipped in their images. The modules are loaded dynamically because they are
scheduled to move into the packages, not because they are importable packages.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def load_service(name: str, relative_path: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / relative_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load service module {relative_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ingestion = load_service("tenantchat_legacy_ingestion", "services/ingestion/app.py")
financing = load_service("tenantchat_legacy_financing_agent", "services/financing-agent/app.py")


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
        embed_calls: list[tuple[Any, ...]] = []

        def fake_post(url: str, **kwargs: Any) -> FakeSearchResponse:
            captured["url"] = url
            captured.update(kwargs)
            return FakeSearchResponse()

        def fake_embed(*args: Any) -> list[float]:
            embed_calls.append(args)
            return [0.25, 0.75]

        monkeypatch.setattr(financing, "embed_query", fake_embed)
        monkeypatch.setattr(financing.requests, "post", fake_post)

        chunks = financing.search_chunks("apex", "Can I finance a repair?", "req-1", "trace-1")

        assert captured["url"].endswith("/tenant-knowledge-chunks/_search")
        assert captured["json"]["knn"]["filter"] == [
            {"term": {"tenant_id": "apex"}},
            {"term": {"domain": "financing"}},
            {"term": {"active": True}},
        ]
        assert captured["json"]["knn"]["query_vector"] == [0.25, 0.75]
        # The correlation IDs the caller supplied ride along to the embedding hop.
        assert embed_calls == [("Can I finance a repair?", "req-1", "trace-1")]
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

        monkeypatch.setattr(
            financing, "embed_query", lambda _query, _request_id=None, _trace_id=None: [1.0]
        )
        monkeypatch.setattr(
            financing.requests, "post", lambda *_args, **_kwargs: MissingIndexResponse()
        )

        assert financing.search_chunks("apex", "financing") == []
