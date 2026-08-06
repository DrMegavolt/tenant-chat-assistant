"""The reviewer-labelled gold cases `FEAT-015` overlays on a turn.

``gold_cases.json`` is a snapshot of ``evals/fixtures/cases.json`` plus the
chunk texts it anchors to in ``evals/fixtures/corpus.json``, embedded here
because the API service does not ship the evals package. The fixtures are the
source of truth: ``test_trace_explorer.py`` re-derives this snapshot from them
so a drift between the two fails the build.

The gold chunks are synthetic evaluation content, not visitor data, but they
are still evidence-like text, so the routes that serve them sit under the same
trace-read role and audit rules as the inference plane itself.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Final

_GOLD_CASES_PATH: Final = Path(__file__).with_name("gold_cases.json")


@dataclass(frozen=True, slots=True)
class GoldChunk:
    """One reviewer-labelled passage a case is anchored to."""

    source_id: str
    text: str


@dataclass(frozen=True, slots=True)
class GoldCase:
    """One eval fixture case: tenant, query, scenario, and gold anchors."""

    case_id: str
    tenant_id: str
    query: str
    scenario: str | None
    gold_chunks: tuple[GoldChunk, ...]


def load_gold_cases() -> tuple[GoldCase, ...]:
    """The embedded gold cases, parsed once per process.

    Raises:
        OSError: the snapshot file is missing from the package.
    """
    payload = json.loads(_GOLD_CASES_PATH.read_text(encoding="utf-8"))
    return tuple(
        GoldCase(
            case_id=str(case["case_id"]),
            tenant_id=str(case["tenant_id"]),
            query=str(case["query"]),
            scenario=str(case["scenario"]) if case.get("scenario") else None,
            gold_chunks=tuple(
                GoldChunk(source_id=str(chunk["source_id"]), text=str(chunk["text"]))
                for chunk in case.get("gold_chunks", [])
            ),
        )
        for case in payload["cases"]
    )
