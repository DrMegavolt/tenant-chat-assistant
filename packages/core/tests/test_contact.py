"""Contact parsing, canonicalization, and redaction."""

from __future__ import annotations

import pytest

from tenantchat.core.contact import Contact, ContactKind
from tenantchat.core.errors import InvalidContactError


class TestPhoneParsing:
    @pytest.mark.parametrize(
        "raw",
        [
            "5552221919",
            "555-222-1919",
            "(555) 222-1919",
            "555.222.1919",
            "  555 222 1919  ",
            "+1 555 222 1919",
            "15552221919",
            "1-555-222-1919",
        ],
    )
    def test_accepted_formats_all_canonicalize_identically(self, raw: str) -> None:
        assert Contact.parse(raw).value == "+15552221919"

    def test_parsed_phone_reports_phone_kind(self) -> None:
        assert Contact.parse("555-222-1919").kind is ContactKind.PHONE

    @pytest.mark.parametrize(
        ("raw", "reason"),
        [
            ("555-222", "too few digits"),
            ("222-1919", "seven digits, no area code"),
            ("155522219190", "too many digits"),
            ("", "empty"),
            ("   ", "whitespace only"),
            ("call me maybe", "no digits"),
        ],
    )
    def test_rejects_unparseable_numbers(self, raw: str, reason: str) -> None:
        with pytest.raises(InvalidContactError):
            Contact.parse(raw)

    @pytest.mark.parametrize("raw", ["0001234567", "1112223333", "555-022-1919"])
    def test_rejects_non_dialable_nanp_numbers(self, raw: str) -> None:
        """Area code and exchange must start with 2-9.

        A digit count alone passes all of these, and none is dialable: the lead
        is unreachable and the sales team wastes a callback attempt.
        """
        with pytest.raises(InvalidContactError):
            Contact.parse(raw)

    def test_display_is_human_readable(self) -> None:
        assert Contact.parse("5552221919").display == "(555) 222-1919"

    def test_masked_phone_keeps_only_last_four(self) -> None:
        masked = Contact.parse("5552221919").masked

        assert masked == "(***) ***-1919"
        assert "555222" not in masked


class TestEmailParsing:
    @pytest.mark.parametrize(
        "raw",
        ["sam@example.com", "  sam@example.com  ", "sam.lee+hvac@mail.example.co.uk"],
    )
    def test_accepts_plausible_addresses(self, raw: str) -> None:
        assert Contact.parse(raw).kind is ContactKind.EMAIL

    def test_domain_is_lowercased_but_local_part_is_preserved(self) -> None:
        """The local part is case-sensitive per RFC 5321; the domain is not."""
        parsed = Contact.parse("Sam.Lee@Example.COM")

        assert parsed.value == "Sam.Lee@example.com"

    @pytest.mark.parametrize(
        "raw",
        ["sam@", "@example.com", "sam@example", "sam@@example.com", "sam @example.com"],
    )
    def test_rejects_malformed_addresses(self, raw: str) -> None:
        with pytest.raises(InvalidContactError):
            Contact.parse(raw)

    def test_display_returns_the_canonical_address(self) -> None:
        """Unlike a phone number, an email needs no reformatting to be readable."""
        assert Contact.parse("Sam.Lee@Example.com").display == "Sam.Lee@example.com"

    def test_masked_email_hides_local_part(self) -> None:
        masked = Contact.parse("sam.lee@example.com").masked

        assert masked.startswith("s")
        assert masked.endswith("@example.com")
        assert "sam.lee" not in masked

    def test_single_character_local_part_is_still_masked(self) -> None:
        """A one-character local part must not mask to a bare, revealing string."""
        assert Contact.parse("s@example.com").masked == "s*@example.com"


class TestParsingContract:
    def test_try_parse_returns_none_instead_of_raising(self) -> None:
        assert Contact.try_parse("not a contact") is None

    def test_try_parse_returns_contact_on_success(self) -> None:
        assert Contact.try_parse("sam@example.com") == Contact(
            kind=ContactKind.EMAIL, value="sam@example.com"
        )

    def test_str_is_masked_so_accidental_interpolation_cannot_leak(self) -> None:
        """PRIV-001: an f-string in a log line is the most likely PII leak path."""
        contact = Contact.parse("sam.lee@example.com")

        assert "sam.lee" not in f"lead created for {contact}"

    def test_contact_is_hashable_and_value_comparable(self) -> None:
        """Deduplication and idempotency keys depend on both (FEAT-003)."""
        first = Contact.parse("(555) 222-1919")
        second = Contact.parse("+1 555 222 1919")

        assert first == second
        assert len({first, second}) == 1

    def test_overlong_input_is_rejected_before_regex_evaluation(self) -> None:
        """Bounded input keeps a pathological string away from the regex engine."""
        with pytest.raises(InvalidContactError):
            Contact.parse("a" * 300 + "@example.com")
