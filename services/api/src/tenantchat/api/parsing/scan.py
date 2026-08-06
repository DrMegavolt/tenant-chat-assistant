"""Content scanning: the refusals that keep hostile bytes out of parsing.

Scanning is the budget-and-shape check, parsing the structure check. Both
raise :class:`~tenantchat.core.errors.ValidationError` with content-free
details, which the ingestion handler maps to the permanent
``ingestion_scan_rejected`` job failure.
"""

from __future__ import annotations

from tenantchat.core.errors import ValidationError

# The per-document byte budget; the upload route enforces the same bound
# before staging, so an oversized document fails here too when the upload
# path is bypassed.
MAX_DOCUMENT_BYTES = 10 * 1024 * 1024


def scan_bytes(content: bytes) -> None:
    """Reject content over the size budget.

    Raises:
        ValidationError: the document exceeds the size budget.
    """
    if len(content) > MAX_DOCUMENT_BYTES:
        raise ValidationError(detail="document exceeds the size budget")


def scan_text(content: bytes) -> None:
    """Reject text content the parsers cannot safely read.

    Raises:
        ValidationError: the document is oversized, is not valid UTF-8,
            contains NUL bytes, or has no scannable text.
    """
    scan_bytes(content)
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError(detail="document is not valid UTF-8") from exc
    if "\x00" in text:
        raise ValidationError(detail="document contains NUL bytes")
    if not text.strip():
        raise ValidationError(detail="document has no text content")
