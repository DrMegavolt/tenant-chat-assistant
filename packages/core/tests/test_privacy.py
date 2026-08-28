"""The privacy contract's pure rules: retention windows and erasure sentinels.

The lifecycle integration test (``tests/privacy``) proves the worker enforces
these against storage; this module pins the rules themselves, one decision at
a time, so a silent default cannot make a retention or erasure decision.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tenantchat.core.contact import (
    EMAIL_IN_TEXT,
    ERASED_MARKER,
    PHONE_IN_TEXT,
    Contact,
    ContactKind,
)
from tenantchat.core.errors import InvalidContactError
from tenantchat.core.privacy import (
    ANONYMIZED_EMAIL_VALUE,
    ANONYMIZED_PHONE_VALUE,
    DataClass,
    RetentionPolicy,
    RetentionRule,
    anonymize_text,
    anonymized_contact,
)
from tenantchat.core.reviews import payload_contains_pii


class TestRetentionRules:
    def test_every_rule_states_its_window_explicitly(self) -> None:
        """R-49: ``max_age`` once defaulted silently to the transcript window,
        so any class constructed without a window quietly expired at 90 days."""
        rule = RetentionRule(DataClass.BOOKING, timedelta(days=3650))

        assert rule.max_age == timedelta(days=3650)

    def test_a_rule_cannot_be_constructed_without_a_window(self) -> None:
        with pytest.raises(TypeError):
            RetentionRule(DataClass.BOOKING)  # type: ignore[call-arg]

    def test_the_default_policy_declares_each_window_it_carries(self) -> None:
        policy = RetentionPolicy.defaults()

        assert policy.max_age(DataClass.TRANSCRIPT) == timedelta(days=90)
        assert policy.max_age(DataClass.INFERENCE_TRACE) == timedelta(days=30)

    def test_classes_without_an_explicit_rule_never_expire(self) -> None:
        """Business records outlive the transcript by decision, not by default."""
        policy = RetentionPolicy.defaults()
        now = datetime.now(UTC)

        assert policy.max_age(DataClass.BOOKING) is None
        assert not policy.expired(DataClass.BOOKING, now - timedelta(days=10_000), now=now)


class TestAnonymizedContact:
    def test_a_phone_erases_to_an_unreachable_phone_form(self) -> None:
        """R-51: the erased value once kept the email form, so a phone contact
        rendered "(era) sed-@example.invalid" through phone formatting."""
        erased = anonymized_contact(Contact(kind=ContactKind.PHONE, value="+15552221919"))

        assert erased.kind is ContactKind.PHONE
        assert erased.value == ANONYMIZED_PHONE_VALUE
        assert erased.display == "(000) 000-0000"

    def test_an_email_erases_to_the_undeliverable_domain_form(self) -> None:
        erased = anonymized_contact(Contact(kind=ContactKind.EMAIL, value="sam@example.com"))

        assert erased.kind is ContactKind.EMAIL
        assert erased.value == ANONYMIZED_EMAIL_VALUE
        assert erased.display == ANONYMIZED_EMAIL_VALUE

    def test_the_erased_phone_sentinel_is_not_parseable_as_a_contact(self) -> None:
        """The sentinel must never read back as a real, dialable number."""
        with pytest.raises(InvalidContactError):
            Contact.parse(ANONYMIZED_PHONE_VALUE)


class TestOneSourceOfContactPatterns:
    """R-46: the phone/email patterns were duplicated between erasure and the
    promotion PII check, so the two could silently disagree."""

    def test_erasure_replaces_what_the_pii_check_flags(self) -> None:
        text = "reach me at 555-222-1919 or sam@example.com today"

        assert payload_contains_pii({"query": text, "scenario": ""})
        assert anonymize_text(text) == "reach me at [erased] or [erased] today"

    def test_text_without_contact_data_is_untouched_by_both(self) -> None:
        text = "The furnace is loud and the estimate felt high."

        assert not payload_contains_pii({"query": text, "scenario": ""})
        assert anonymize_text(text) == text

    def test_an_address_is_erased_whole_not_half(self) -> None:
        """The phone pattern can match the digits of an address's local part;
        the email pattern must run first or a half-erased address survives."""
        assert anonymize_text("sam.lee5552221919@example.com") == ERASED_MARKER

    def test_the_shared_patterns_are_the_ones_privacy_and_reviews_read(self) -> None:
        assert PHONE_IN_TEXT.search("call (555) 222-1919") is not None
        assert EMAIL_IN_TEXT.search("sam@example.com") is not None
        assert ERASED_MARKER == "[erased]"
