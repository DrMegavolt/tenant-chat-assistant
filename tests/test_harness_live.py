"""The live harness is a semantic gate, not an HTTP-success printer."""

from __future__ import annotations

import pytest

from scripts.harness_live import (
    _connection,
    _recorded_outcome,
    _request_path,
    _validate_turn,
    run_cases,
)

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


RECORD_CONTENT = {
    "outcome": {"status": "answered"},
    "diagnoses": [{"cause": "retrieval_miss"}],
}


class TestRecordedOutcomeShapes:
    """The admin trace-detail API has served two record layouts; both must parse.

    When the endpoint dropped the ``{"record": ...}`` envelope and returned the
    record itself at the top level, every harness check raised "carried no
    record" and a healthy cluster reported 20/20 failures — the exact
    false alarm this function must never produce again (N-03).
    """

    def _get_direct(self, path: str, **_kwargs: object) -> dict[str, object]:
        return {
            "turn_id": "turn-1",
            "tenant_id": "apex",
            "session_id": "sess-1",
            "trace_id": "trc-1",
            "recorded_at": "2026-08-28T00:00:00Z",
            "content": dict(RECORD_CONTENT),
            "projections": {},
        }

    def _get_envelope(self, path: str, **_kwargs: object) -> dict[str, object]:
        return {"record": self._get_direct(path)}

    def test_reads_the_record_returned_directly_at_the_top_level(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("scripts.harness_live._api_get", self._get_direct)
        assert _recorded_outcome("apex", "turn-1") == ("answered", ("retrieval_miss",))

    def test_still_accepts_a_response_wrapped_in_a_record_envelope(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("scripts.harness_live._api_get", self._get_envelope)
        assert _recorded_outcome("apex", "turn-1") == ("answered", ("retrieval_miss",))

    def test_a_response_in_neither_shape_is_an_error_naming_the_keys_seen(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "scripts.harness_live._api_get", lambda path, **_: {"detail": "not found"}
        )
        with pytest.raises(RuntimeError, match="carried no record.*detail"):
            _recorded_outcome("apex", "turn-1")


class TestRunCasesExitCode:
    """A failed check must fail the run: an operator trusting exit code 0 would
    present a broken demo (N-03)."""

    @staticmethod
    def _stub_happy_path(monkeypatch: pytest.MonkeyPatch, failing_turn_ids: set[str]) -> None:
        def recorded_outcome(tenant_id: str, turn_id: str) -> tuple[str, tuple[str, ...]]:
            return ("unknown", ()) if turn_id in failing_turn_ids else ("answered", ())

        monkeypatch.setattr("scripts.harness_live.ADMIN_GATEWAY_TOKEN", "token")
        monkeypatch.setattr("scripts.harness_live._verify_health", lambda: None)
        monkeypatch.setattr(
            "scripts.harness_live._open_session", lambda tenant_id: ("sess-1", "cred")
        )
        monkeypatch.setattr(
            "scripts.harness_live._send_message",
            lambda credential, message: {
                "reply": "Here is what I found.",
                "turn_id": "turn-1",
                "citations": [{"source_id": "chunk-1"}],
            },
        )
        monkeypatch.setattr("scripts.harness_live._recorded_outcome", recorded_outcome)
        monkeypatch.setattr("scripts.harness_live.report", lambda line: None)

    def test_reports_success_only_when_every_check_passes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._stub_happy_path(monkeypatch, failing_turn_ids=set())
        assert run_cases() == 0

    def test_returns_a_failure_exit_code_when_any_check_errors(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._stub_happy_path(monkeypatch, failing_turn_ids={"turn-1"})
        assert run_cases() == 1
