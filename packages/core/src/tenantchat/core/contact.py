"""Customer contact details as a parsed value object.

A ``Contact`` cannot exist unless it is valid, so nothing downstream re-checks and
no unparseable value can travel disguised as a normalized one. Normalizing and
validating in two separate functions is the alternative, and it lets the two
disagree: whatever the validator rejects, the normalizer has already passed on.

Valid means *reachable*, not *well-shaped*. ``0001234567`` is ten digits and is
not a number anyone can dial.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from tenantchat.core.errors import InvalidContactError

# Deliberately conservative. Full RFC 5322 is not a useful target: it admits
# addresses no mail provider accepts, and syntactic validity never implies
# deliverability. Confirming an address requires sending to it.
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+$")

# North American Numbering Plan: area code and central-office code both begin
# with 2-9, which is what rejects 000/111-style filler.
_NANP_RE = re.compile(r"^(?P<area>[2-9]\d{2})(?P<exchange>[2-9]\d{2})(?P<line>\d{4})$")

_MAX_RAW_LENGTH = 254  # RFC 5321 limit on a forward path; also bounds phone input.


class ContactKind(StrEnum):
    EMAIL = "email"
    PHONE = "phone"


@dataclass(frozen=True, slots=True)
class Contact:
    """A validated means of reaching a customer.

    ``value`` is canonical, not as-typed: E.164 for phones, lowercased for email.
    Storing the canonical form is what makes deduplication and CRM idempotency
    keys reliable (FEAT-003).
    """

    kind: ContactKind
    value: str

    @classmethod
    def parse(cls, raw: str) -> Contact:
        """Parse a customer-supplied string.

        Raises:
            InvalidContactError: if the value is neither a plausible email nor a
                dialable NANP phone number.
        """
        candidate = raw.strip()
        if not candidate or len(candidate) > _MAX_RAW_LENGTH:
            raise InvalidContactError(detail=f"length {len(candidate)} outside accepted bounds")

        if "@" in candidate:
            return cls._parse_email(candidate)
        return cls._parse_phone(candidate)

    @classmethod
    def _parse_email(cls, candidate: str) -> Contact:
        if not _EMAIL_RE.match(candidate):
            raise InvalidContactError(detail="value contains '@' but is not a valid address")
        local, _, domain = candidate.rpartition("@")
        # The local part is case-sensitive per spec; the domain is not.
        return cls(kind=ContactKind.EMAIL, value=f"{local}@{domain.lower()}")

    @classmethod
    def _parse_phone(cls, candidate: str) -> Contact:
        digits = re.sub(r"\D", "", candidate)
        if len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]
        if len(digits) != 10:
            raise InvalidContactError(detail=f"expected 10 NANP digits, got {len(digits)}")
        if not _NANP_RE.match(digits):
            raise InvalidContactError(detail="area code or exchange outside the 2-9 NANP range")
        return cls(kind=ContactKind.PHONE, value=f"+1{digits}")

    @classmethod
    def try_parse(cls, raw: str) -> Contact | None:
        """Parse, returning ``None`` instead of raising.

        For probing untrusted free text, where failure is expected and unexceptional.
        """
        try:
            return cls.parse(raw)
        except InvalidContactError:
            return None

    @property
    def display(self) -> str:
        """Human-readable form, for confirmations shown back to the customer."""
        if self.kind is ContactKind.EMAIL:
            return self.value
        digits = self.value.removeprefix("+1")
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"

    @property
    def masked(self) -> str:
        """Redacted form for logs, traces, metrics, and admin previews.

        OBS-001 and PRIV-001 both require that contact details never reach a log
        line. Exposing the masked form as a property makes the safe choice the
        convenient one at the call site.
        """
        if self.kind is ContactKind.EMAIL:
            local, _, domain = self.value.rpartition("@")
            head = local[0] if local else ""
            return f"{head}{'*' * max(len(local) - 1, 1)}@{domain}"
        return f"(***) ***-{self.value[-4:]}"

    def __str__(self) -> str:
        """Masked by default so an accidental f-string cannot leak PII."""
        return self.masked
