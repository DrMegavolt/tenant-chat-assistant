"""PII redaction for the operational plane.

`ADR-0010` keeps customer content out of logs, metrics, and traces, and
`PRIV-001` extends that to tool-event storage. The pattern substitutions here
are the same recognition rules the domain uses, applied to free text and to
JSON trees (tool arguments) where the key alone says a value is contact data.

This module is deliberately a small, import-free utility: the log filter is
installed by the composition root, and the tree redaction is the boundary any
future tool-event writer must push its arguments through.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Final

# The same recognition rules `Contact.parse` applies, in free text: a message
# can carry a phone number or an email without being a well-formed command.
_PHONE = re.compile(r"(?:\+?1[\s.-]?)?(?:\(?\d{3}\)?[\s.-]?)\d{3}[\s.-]?\d{4}")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")

_PHONE_MARKER = "[phone-redacted]"
_EMAIL_MARKER = "[email-redacted]"

# Keys whose value is contact data in a tool-call arguments tree. A marker in
# the key space is not a filter on values: the key itself is what an operator
# would search a tool log by, so replacing only the value keeps the shape
# readable while the data is gone.
_PII_KEYS: Final[frozenset[str]] = frozenset(
    {
        "customer_name",
        "customer_phone_or_email",
        "phone",
        "email",
        "address",
        "service_address",
        "summary",
    }
)


def redact_text(text: str) -> str:
    """Replace phone numbers and email addresses in free text."""
    return _PHONE.sub(_PHONE_MARKER, _EMAIL.sub(_EMAIL_MARKER, text))


def redact_tree(value: object) -> object:
    """Replace PII leaves of a JSON-shaped tree, preserving structure.

    Applies to tool-call ``arguments`` and ``result`` objects: the keys stay
    (so the shape an operator reads is unchanged) and the values of known
    PII-bearing keys become markers. Unknown keys are recursed without
    judgment, because a model can name a contact field anything.
    """
    if isinstance(value, Mapping):
        redacted: dict[object, object] = {}
        for key, item in value.items():
            if isinstance(key, str) and key in _PII_KEYS and isinstance(item, str):
                redacted[key] = redact_text(item)
            else:
                redacted[key] = redact_tree(item)
        return redacted
    if isinstance(value, list):
        return [redact_tree(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


class PiiLogFilter(logging.Filter):
    """Strip phone numbers and email addresses from every log record.

    Defense in depth for the one rule that has no test: an accidental f-string
    that interpolates a contact value. The record's ``msg``, the string-valued
    ``args``, and the formatted traceback are scrubbed before any handler
    formats them, so the record that reaches the sink never carries a dialable
    number or an address.

    Installed on the root logger's *handlers* by the composition root, not on
    the logger itself: a logger consults its own filters only for records
    emitted directly on it, so a filter on the root logger never sees the
    records that propagate up from ``getLogger(__name__)`` — which is every
    module here. Installing twice is harmless because redaction is idempotent.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_text(record.msg)
        args = record.args
        if isinstance(args, tuple):
            record.args = tuple(redact_text(arg) if isinstance(arg, str) else arg for arg in args)
        elif isinstance(args, dict):
            record.args = {
                key: redact_text(value) if isinstance(value, str) else value
                for key, value in args.items()
            }
        if record.exc_info is not None:
            # A traceback can quote the exception message, which for an
            # unexpected failure routinely contains a contact or a credential.
            # Materializing it here lets every handler downstream format the
            # scrubbed text instead of the raw exception.
            record.exc_text = redact_text(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        return True


def install_pii_log_filter() -> None:
    """Attach the filter to every root handler, once per process.

    Handler filters run for propagated records; logger filters do not. A
    deployment that adds a handler after startup must install again, which is
    why this is idempotent per handler rather than a one-shot guard.
    """
    root = logging.getLogger()
    for handler in root.handlers:
        if not any(isinstance(existing, PiiLogFilter) for existing in handler.filters):
            handler.addFilter(PiiLogFilter())
