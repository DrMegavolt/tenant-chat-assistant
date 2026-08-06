"""Content-safety scanning: suspicious embedded instructions and active content.

The ingestion-time half of `RAG-007`. The assembly boundary and the runtime
guards assume hostile documents are *already* neutralized; this scan is what
neutralizes them at the door. It is deterministic pattern detection over the
extracted text and returns only bounded metadata — signal kinds and a content
fingerprint — so the ingestion worker can quarantine a document and record the
decision without ever copying the hostile text into a log or audit row.

Deliberately narrow patterns: the signals below are distinctive multi-word or
markup shapes that are rare in legitimate business documents, and false
positives cost an operator a review pass rather than an answer. A document that
fails is quarantined for a human review, not rejected outright — a reviewer
clears it when the pattern is benign.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import StrEnum

# Instructions that tell the assistant to discard or replace its standing
# rules. Paired with the boundary prose a hostile doc would quote or defeat.
_OVERRIDE = re.compile(
    r"(?:ignore|disregard|forget|overwrite|delete|erase)"
    r"(?: (?:all|any|your|the|previous|prior)){1,4}"
    r" (?:instructions|rules|limits|policy|guidelines)",
    re.IGNORECASE,
)

# Attempts to redefine the assistant's role or make it act as another party.
_ROLE = re.compile(
    r"(?:you are now|from now on you are|pretend you are|your new role is|"
    r"act as if you were|you will now act as|disregard your role)",
    re.IGNORECASE,
)

# Demands to disclose the assistant's instructions or system configuration.
_EXTRACT = re.compile(
    r"(?:reveal|show|share|output|print|repeat|display|leak|expose)"
    r" (?:me |us )?(?:your|the|my)? (?:system )?(?:prompt|instructions|"
    r"configuration|initial message)",
    re.IGNORECASE,
)

# Embedded active content: scripts, event handlers, and command invocations.
_ACTIVE = re.compile(
    r"<script|</script|<iframe|<object|<embed|onclick\s*=|onload\s*=|"
    r"javascript:|data:text/html|vbscript:|powershell\s+-|cmd\s+/c|"
    r"bash\s+-[ci]|curl\s+[\"']?[-a-z]*[Ua-z]*http|/bin/(?:sh|bash)\b",
    re.IGNORECASE,
)

# Statements that the assistant's capabilities or permissions have changed.
_PERMISSION = re.compile(
    r"(?:you (?:may|can) now|you are (?:now )?allowed to|new permissions?|"
    r"permissions? (?:have been|were) (?:updated|granted)|access (?:has )?been granted)",
    re.IGNORECASE,
)

_SIGNALS: tuple[tuple[InjectionSignal, re.Pattern[str]], ...]


class InjectionSignal(StrEnum):
    """The bounded detection kinds a document may flag. Never carries text."""

    INSTRUCTION_OVERRIDE = "instruction_override"
    ROLE_USURPATION = "role_usurpation"
    PROMPT_EXTRACTION = "prompt_extraction"
    ACTIVE_CONTENT = "active_content"
    PERMISSION_CHANGE = "permission_change"


_SIGNALS = (
    (InjectionSignal.INSTRUCTION_OVERRIDE, _OVERRIDE),
    (InjectionSignal.ROLE_USURPATION, _ROLE),
    (InjectionSignal.PROMPT_EXTRACTION, _EXTRACT),
    (InjectionSignal.ACTIVE_CONTENT, _ACTIVE),
    (InjectionSignal.PERMISSION_CHANGE, _PERMISSION),
)


@dataclass(frozen=True, slots=True)
class ContentSafetyReport:
    """The scan outcome: a flag, the signal kinds, and a content fingerprint.

    ``fingerprint`` is SHA-256 of the scanned text, the only content-derived
    value allowed off the inference plane: an audit row or a review queue can
    say "this exact content was flagged" without holding the content.
    """

    flagged: bool
    signals: tuple[InjectionSignal, ...]
    fingerprint: str


def content_fingerprint(text: str) -> str:
    """SHA-256 of the text: the content-free identifier for audit records."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def scan_for_injection(text: str) -> ContentSafetyReport:
    """Scan extracted document text for suspicious embedded instructions.

    A document is flagged when any signal matches. The report never carries
    the matched text — only the kinds and the fingerprint — because the
    verdict must be auditable without the content becoming a log line.
    """
    detected = tuple(signal for signal, pattern in _SIGNALS if pattern.search(text) is not None)
    return ContentSafetyReport(
        flagged=bool(detected),
        signals=detected,
        fingerprint=content_fingerprint(text),
    )
