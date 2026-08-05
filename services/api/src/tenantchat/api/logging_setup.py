"""Structured JSON logging configuration for the operational plane.

`ADR-0010` gives the log plane identifiers, enums, counts, and versions — and
nothing else. This module is the formatter that enforces the shape: every line
is one JSON object carrying the timestamp, level, service, environment, the
correlation context, the event, and a fixed allowlist of structured extra
fields. An extra key that is not on the allowlist never reaches a line, so a
future ``extra={"chunk": ...}`` cannot quietly become log content; the message
itself and any traceback are scrubbed of contact data by the PII filter that
ships on every handler this module builds (``redaction.py``).

The composition root calls :func:`configure_logging` once per process — the
API and the job worker each do, with their own service name — and uvicorn's
own loggers are absorbed into the root handler so a deployment gets one JSON
stream instead of a plain-text one beside it.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime
from typing import Final, TextIO

from tenantchat.api.correlation import context_extra
from tenantchat.api.redaction import PiiLogFilter, redact_text

SERVICE_NAME: Final = "chat-api"

# The only ``extra`` keys that may reach a structured line. Everything a log
# statement adds must be on this list or it is dropped: the list is the
# boundary between "structured operator context" and "content", and adding a
# key is a deliberate act with a test asserting it appears.
_EXTRA_FIELDS: Final[frozenset[str]] = frozenset(
    {
        # Correlation.
        "request_id",
        "trace_id",
        "tenant",
        # Safe error codes.
        "code",
        "error_code",
        # Operator context, already passed through the PII filter.
        "detail",
        # Request shape.
        "path",
        "method",
        "status",
        "duration_ms",
        # Job and rate context.
        "job_id",
        "scope",
        "attempt",
        # Admin identity (the subject is the provider's pseudonymous ID).
        "subject",
        "role",
        "required_role",
        "effective_role",
        # Turn context: versions and the bounded action enum only.
        "graph_version",
        "prompt_version",
        "committed_actions",
    }
)


class JsonLogFormatter(logging.Formatter):
    """Render one record as a JSON object with the fixed field set.

    The correlation fields come from the ambient context when the record does
    not carry them explicitly, so anything logged during a request — inside
    the agent runtime, a tool, a worker handler — lines up under the same
    request and trace ID without passing the IDs through every call.
    """

    def __init__(self, *, service: str, environment: str) -> None:
        super().__init__()
        self._service = service
        self._environment = environment

    def format(self, record: logging.LogRecord) -> str:
        line: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "service": self._service,
            "environment": self._environment,
            "logger": record.name,
            "event": _message(record),
        }
        line.update(context_extra())
        for key in _EXTRA_FIELDS:
            if key in record.__dict__:
                line[key] = record.__dict__[key]
        if record.exc_text is not None:
            line["exception"] = record.exc_text
        elif record.exc_info is not None:
            # A handler without the PII filter still must not emit a traceback
            # that names a contact; the formatted text is scrubbed here.
            line["exception"] = redact_text(self.formatException(record.exc_info))
        return json.dumps(line, sort_keys=True, default=str)


def _message(record: logging.LogRecord) -> str:
    try:
        return record.getMessage()
    except (ValueError, TypeError):
        # A mismatched format string is a bug in the caller; the record must
        # still produce a line rather than take the sink down with it.
        return str(record.msg)


def build_json_handler(
    *, service: str, environment: str, stream: TextIO | None = None
) -> logging.Handler:
    """A handler emitting scrubbed JSON lines, ready for the root logger."""
    handler = logging.StreamHandler(stream=stream or sys.stderr)
    handler.setFormatter(JsonLogFormatter(service=service, environment=environment))
    handler.addFilter(PiiLogFilter())
    return handler


def resolve_service(default: str) -> str:
    """The deployed service name, honoring ``OTEL_SERVICE_NAME``.

    The deployment manifests already set ``OTEL_SERVICE_NAME`` for the API
    image; honoring it keeps the log plane's service field aligned with the
    trace plane's resource attributes.
    """
    configured = os.environ.get("OTEL_SERVICE_NAME", "").strip()
    return configured or default


def configure_logging(
    *,
    service: str,
    environment: str,
    level: str = "INFO",
    json_enabled: bool = True,
) -> logging.Handler | None:
    """Install structured logging on the root logger, once per process.

    Attaches one JSON handler (with the PII filter) when none exists, sets the
    root level, and absorbs uvicorn's own handlers so startup, error, and
    access records flow through the same structured stream instead of a
    plain-text one beside it.

    Raises:
        ValueError: *level* is not a name ``logging`` knows.
    """
    level_name = level.strip().upper()
    resolved = logging.getLevelNamesMapping().get(level_name)
    if resolved is None:
        raise ValueError(f"unknown log level {level!r}")

    root = logging.getLogger()
    root.setLevel(resolved)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True

    for handler in root.handlers:
        if isinstance(handler.formatter, JsonLogFormatter):
            return None
    if not json_enabled:
        return None
    handler = build_json_handler(service=service, environment=environment)
    root.addHandler(handler)
    return handler
