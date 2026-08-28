"""OBS-001: the structured JSON line and its configuration.

The formatter is the enforcement point of `ADR-0010`'s operational plane: a
fixed field set, a fixed allowlist of extra keys, contact-scrubbed messages
and tracebacks, and the correlation context merged in. Configuration is what
"volume is configurable" means in code — level, JSON shape, and the access
line — plus the documented retention knobs in the observability stack.
"""

from __future__ import annotations

import io
import json
import logging
from typing import cast

import pytest

from tenantchat.api.correlation import (
    CorrelationContext,
    bind,
    reset,
    tenant_pseudonym,
)
from tenantchat.api.logging_setup import (
    _EXTRA_FIELDS,
    JsonLogFormatter,
    build_json_handler,
    configure_logging,
)
from tenantchat.api.redaction import _STRUCTURED_EXTRA_KEYS

_CONTRACT_FIELDS = (
    "timestamp",
    "level",
    "service",
    "environment",
    "logger",
    "event",
)


def _record(
    message: str,
    *,
    level: int = logging.INFO,
    extra: dict[str, object] | None = None,
) -> logging.LogRecord:
    record = logging.LogRecord(
        name="tenantchat.api.obs",
        level=level,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )
    if extra:
        for key, value in extra.items():
            setattr(record, key, value)
    return record


class TestJsonLine:
    def test_every_contract_field_is_present(self) -> None:
        formatter = JsonLogFormatter(service="chat-api", environment="staging")
        line = cast(dict[str, object], json.loads(formatter.format(_record("booking rejected"))))

        for field in _CONTRACT_FIELDS:
            assert field in line, line
        assert line["service"] == "chat-api"
        assert line["environment"] == "staging"
        assert line["level"] == "INFO"
        assert line["event"] == "booking rejected"
        assert cast(str, line["timestamp"]).endswith("+00:00")

    def test_the_correlation_context_is_merged_into_every_line(self) -> None:
        pseudonym = tenant_pseudonym("clearview", key="key")
        bind(
            CorrelationContext(
                request_id="req-1",
                trace_id="trace-1",
                tenant_id="clearview",
                tenant_pseudonym=pseudonym,
            )
        )
        try:
            formatter = JsonLogFormatter(service="s", environment="e")
            line = cast(dict[str, object], json.loads(formatter.format(_record("turn done"))))
        finally:
            reset()

        assert line["request_id"] == "req-1"
        assert line["trace_id"] == "trace-1"
        assert line["tenant"] == pseudonym

    def test_unknown_extra_keys_are_dropped_and_known_ones_pass(self) -> None:
        formatter = JsonLogFormatter(service="s", environment="e")
        line = cast(
            dict[str, object],
            json.loads(
                formatter.format(
                    _record(
                        "rate limited",
                        extra={"code": "rate_limited", "status": 429, "leaky": "secret"},
                    )
                )
            ),
        )

        assert line["code"] == "rate_limited"
        assert line["status"] == 429
        assert "leaky" not in line

    def test_explicit_extra_request_ids_win_over_the_context(self) -> None:
        bind(CorrelationContext(request_id="ctx-1", trace_id="ctx-t"))
        try:
            formatter = JsonLogFormatter(service="s", environment="e")
            line = cast(
                dict[str, object],
                json.loads(formatter.format(_record("m", extra={"request_id": "explicit"}))),
            )
        finally:
            reset()

        assert line["request_id"] == "explicit"

    def test_a_traceback_never_carries_a_contact_value(self) -> None:
        stream = io.StringIO()
        handler = build_json_handler(service="chat-api", environment="test", stream=stream)
        logger = logging.getLogger("tenantchat.api.obs")
        logger.addHandler(handler)
        try:
            try:
                raise RuntimeError("provider said dana@example.com / 555-222-1919")
            except RuntimeError:
                logger.error("handler failed", exc_info=True)
            logger.removeHandler(handler)
            written = stream.getvalue()
        finally:
            logger.removeHandler(handler)

        assert "dana@example.com" not in written
        assert "555-222-1919" not in written
        line = json.loads(written.splitlines()[0])
        exception = cast(str, line["exception"])
        assert "[email-redacted]" in exception
        assert "[phone-redacted]" in exception

    def test_an_extra_field_never_carries_a_contact_value(self) -> None:
        """The operator detail rides in ``extra=``; the filter must scrub it too.

        ``problems.py`` logs a domain error's ``detail`` so the request ID can
        be matched to what the caller sent. That detail may quote visitor
        input (a phone number inside a rejected command), and ``detail`` is on
        the formatter's extra allowlist — so redaction has to happen on the
        record, or the allowlist itself becomes the leak.
        """
        stream = io.StringIO()
        handler = build_json_handler(service="chat-api", environment="test", stream=stream)
        logger = logging.getLogger("tenantchat.api.obs")
        logger.addHandler(handler)
        try:
            logger.warning(
                "domain error",
                extra={
                    "code": "validation_error",
                    "detail": "rejected contact dana@example.com / 555-222-1919",
                },
            )
        finally:
            logger.removeHandler(handler)
        written = stream.getvalue()

        assert "dana@example.com" not in written
        assert "555-222-1919" not in written
        line = json.loads(written.splitlines()[0])
        assert line["detail"] == "rejected contact [email-redacted] / [phone-redacted]"

    def test_structured_extras_pass_through_untouched(self) -> None:
        """A hex trace id is an identifier, not prose; the filter must not mangle it.

        The phone pattern matches any run of ten digits, so redacting every
        string extra would corrupt the correlation fields themselves — a
        32-hex trace id would log as ``[phone-redacted]abcdef…`` and the
        access line could never be joined back to a trace. Structured extras
        pass verbatim; only free-text extras are scrubbed.
        """
        stream = io.StringIO()
        handler = build_json_handler(service="chat-api", environment="test", stream=stream)
        logger = logging.getLogger("tenantchat.api.obs")
        logger.addHandler(handler)
        trace_id = "1234567890abcdef1234567890abcdef"
        try:
            logger.warning(
                "request completed",
                extra={"trace_id": trace_id, "job_id": "9f8e7d6c", "detail": "call 555-222-1919"},
            )
        finally:
            logger.removeHandler(handler)
        line = json.loads(stream.getvalue().splitlines()[0])

        assert line["trace_id"] == trace_id
        assert line["job_id"] == "9f8e7d6c"
        assert "[phone-redacted]" in line["detail"]


class TestExtraAllowlistBoundary:
    def test_the_published_allowlist_is_partitioned_by_the_filter(self) -> None:
        """Every published extra is either structured or the scrubbed ``detail``.

        The formatter's allowlist and the redaction filter's structured set
        are two halves of one boundary: a key published to a line is either a
        content-free identifier (passes verbatim) or free text (scrubbed).
        Adding a key to the allowlist without classifying it here would leave
        the new field's fate to whatever the filter defaults to, so this test
        fails until the decision is made on both sides.
        """
        structured = _STRUCTURED_EXTRA_KEYS | {"detail"}
        assert structured == _EXTRA_FIELDS


class TestConfiguration:
    def test_configure_logging_is_idempotent_and_sets_the_level(self) -> None:
        # Earlier tests (and every create_app) install the JSON handler on
        # root, so the property is "a second call adds nothing", not "the
        # first call adds one" — start from the post-startup state.
        configure_logging(service="chat-api", environment="test", level="WARNING")
        first = configure_logging(service="chat-api", environment="test", level="WARNING")
        second = configure_logging(service="chat-api", environment="test", level="WARNING")

        assert first is None
        assert second is None
        assert logging.getLogger().level == logging.WARNING
        json_handlers = [
            handler
            for handler in logging.getLogger().handlers
            if isinstance(handler.formatter, JsonLogFormatter)
        ]
        assert len(json_handlers) == 1

    def test_an_unknown_level_is_refused(self) -> None:
        with pytest.raises(ValueError, match="unknown log level"):
            configure_logging(service="s", environment="e", level="VERBOSE")

    def test_json_disabled_adds_no_handler_but_still_sets_the_level(self) -> None:
        root = logging.getLogger()
        for existing in list(root.handlers):
            if isinstance(existing.formatter, JsonLogFormatter):
                root.removeHandler(existing)
        handler = configure_logging(
            service="chat-api", environment="test", level="WARNING", json_enabled=False
        )

        assert handler is None
        assert logging.getLogger().level == logging.WARNING
        assert not any(
            isinstance(handler.formatter, JsonLogFormatter)
            for handler in logging.getLogger().handlers
        )

    def test_uvicorn_loggers_flow_through_the_root_stream(self) -> None:
        access = logging.getLogger("uvicorn.access")
        access.addHandler(logging.StreamHandler(io.StringIO()))
        try:
            configure_logging(service="chat-api", environment="test")
            assert access.handlers == []
            assert access.propagate is True
        finally:
            access.handlers = []
            access.propagate = True
