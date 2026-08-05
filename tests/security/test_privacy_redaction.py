"""PRIV-001's core invariant, stated as regressions: PII never reaches the
operational plane.

`ADR-0010`/CLAUDE.md invariant 6: phone numbers, email addresses, and free text
stay out of logs, metrics, and audit details. These tests pin the two layers
that enforce it — the redaction helpers the composition root installs, and the
log filter that scrubs an accidental f-string before a handler formats it.
"""

from __future__ import annotations

import io
import logging
from typing import cast

from tenantchat.api.redaction import PiiLogFilter, install_pii_log_filter, redact_text, redact_tree


def test_free_text_phone_and_email_are_replaced() -> None:
    scrubbed = redact_text("Call Dana at 555-222-1919 or dana@example.com now.")
    assert "555-222-1919" not in scrubbed
    assert "dana@example.com" not in scrubbed
    assert "[phone-redacted]" in scrubbed
    assert "[email-redacted]" in scrubbed


def test_a_vanilla_number_and_domain_set_of_digits_survives() -> None:
    """Redaction must not mangle legitimate short numbers or plain prose."""
    scrubbed = redact_text("We open at 9:00 and the page is at example.com/blog")
    assert scrubbed == "We open at 9:00 and the page is at example.com/blog"


def test_tree_redaction_preserves_shape_and_scrubs_pii_keys() -> None:
    arguments = {
        "service": "HVAC",
        "customer_name": "Dana Ruiz",
        "customer_phone_or_email": "555-222-1919",
        "service_address": "12 Alder Court, Portland, OR 97205",
        "note": "Her email is dana@example.com",
    }
    redacted = cast(dict[str, object], redact_tree(arguments))

    # The shape stays readable: keys are unchanged, only values are replaced.
    assert set(redacted) == set(arguments)
    assert redacted["service"] == "HVAC"
    assert isinstance(redacted["customer_phone_or_email"], str)
    assert "[phone-redacted]" in redacted["customer_phone_or_email"]
    assert "[phone-redacted]." not in cast(str, redacted["service_address"])
    # Unknown keys are recursed, not judged, so a value inside one is scrubbed too.
    assert isinstance(redacted["note"], str)
    assert "[email-redacted]" in redacted["note"]
    assert "555-222-1919" not in str(redacted)
    assert "dana@example.com" not in str(redacted)


def test_the_log_filter_scrubs_a_formatted_record() -> None:
    record = logging.LogRecord(
        name="privacy",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="booking %s uses %s",
        args=("BK-1", "dana@example.com / 555-222-1919"),
        exc_info=None,
    )
    PiiLogFilter().filter(record)

    message = record.getMessage()
    assert "dana@example.com" not in message
    assert "555-222-1919" not in message
    assert "[email-redacted]" in message
    assert "[phone-redacted]" in message


def test_a_module_logger_record_is_scrubbed_at_the_handler() -> None:
    """The filter must catch records from `getLogger(__name__)`, not just root.

    A filter on the root *logger* is consulted only for records logged directly
    on it. Every module here logs through its own logger, whose records reach
    the root's handlers by propagation and skip the root logger's filters
    entirely — so installing there left the safety net catching nothing.
    """
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    root = logging.getLogger()
    root.addHandler(handler)
    previous_level = root.level
    root.setLevel(logging.INFO)
    try:
        install_pii_log_filter()
        logging.getLogger("tenantchat.api.routers.bookings").info(
            "booking for dana@example.com at 555-222-1919"
        )
        handler.flush()
        written = stream.getvalue()
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)

    assert "dana@example.com" not in written
    assert "555-222-1919" not in written
    assert "[email-redacted]" in written
    assert "[phone-redacted]" in written


def test_installing_the_filter_is_idempotent() -> None:
    handler = logging.StreamHandler(io.StringIO())
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        install_pii_log_filter()
        install_pii_log_filter()
        installed = [f for f in handler.filters if isinstance(f, PiiLogFilter)]
    finally:
        root.removeHandler(handler)

    assert len(installed) == 1
