"""The live harness is a semantic gate, not an HTTP-success printer."""

from __future__ import annotations

import pytest

from scripts.harness_live import _connection, _request_path, _validate_turn

CASE = {"outcomes": ("answered",), "min_citations": 1}


def test_live_case_accepts_the_expected_grounded_shape() -> None:
    _validate_turn(
        CASE,
        {
            "reply": "Financing may be available.",
            "turn_id": "turn-1",
            "citations": [{"source_id": "chunk-1"}],
        },
    )


def test_live_case_accepts_a_pending_confirmation_instead_of_a_reply() -> None:
    """A booking or lead pause still earns a turn record for the explorer."""
    _validate_turn(
        CASE,
        {
            "reply": "",
            "turn_id": "turn-1",
            "citations": [],
            "pending": {"awaiting": "booking_confirmation", "slot": "Tomorrow 09:00"},
        },
    )


@pytest.mark.parametrize(
    "turn",
    [
        {"reply": "No", "turn_id": "turn-1", "citations": []},
        {"reply": "", "turn_id": "turn-1", "citations": [{}]},
        {"reply": "Yes", "turn_id": "", "citations": [{}]},
        {"reply": "Yes", "turn_id": "turn-1", "citations": []},
        {"reply": "Yes", "turn_id": "turn-1", "citations": [{}], "pending": {}},
    ],
)
def test_live_case_rejects_semantic_false_positives(turn: dict[str, object]) -> None:
    with pytest.raises(RuntimeError):
        _validate_turn(CASE, turn)


def test_live_harness_supports_https_and_api_path_prefixes() -> None:
    connection = _connection("https://chat.example.test:8443/tenant-chat/")
    assert connection.host == "chat.example.test"
    assert connection.port == 8443
    assert _request_path("https://chat.example.test/tenant-chat/", "/readyz") == (
        "/tenant-chat/readyz"
    )


def test_live_harness_rejects_non_http_api_urls() -> None:
    with pytest.raises(ValueError, match="absolute http"):
        _connection("chat.example.test")
