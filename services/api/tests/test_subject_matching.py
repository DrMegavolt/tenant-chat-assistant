"""The subject-contact matching policy that selects sessions for export and
erasure (R-04).

Discovery is the front door of both rights requests, so these tests specify
what may and may not match. The failure being prevented: the previous
implementation stripped every non-digit character from the whole turn-record
JSON — or from a whole message — and substring-matched a ten-digit probe, so
a timestamp, an epoch value, a score, a hash, or an identifier carrying the
probe's digits as a subsequence pulled unrelated sessions into export and,
worse, into irreversible erasure. Matching is now structured: contact-typed
fields are compared canonically, free conversation text is recognized with
the domain's own patterns and canonicalized, and everything else — metadata,
unrecognized shapes, unparseable content — is invisible to discovery.
"""

from __future__ import annotations

from typing import Any

from tenantchat.api.subject_match import (
    contact_value_matches,
    text_holds_contact,
    trace_holds_contact,
)
from tenantchat.core.contact import Contact

DANA = Contact.parse("555-222-1919")
BORIS = Contact.parse("555-333-4444")


def test_a_phone_inside_trace_metadata_is_not_a_subject_match() -> None:
    """A hash, an epoch value, or a score may contain the probe's digits as a
    digit run; metadata is not conversation content, so none of it matches."""
    content: dict[str, Any] = {
        "schema_version": "4",
        "turn_index": 3,
        "manifest_hash": "9d1755522219190000" + "0" * 48,
        "executed_graph": {
            "started_at": "17700005552221919000",
            "ended_at": "17700005552221919999",
            "duration_ms": 173,
        },
        "retrieval": {
            "sufficient": True,
            "min_evidence_score": 0.5552221919,
            "generation_id": "gen-175552221919",
        },
        "component_manifest": {"model": {"id": "scripted", "parameters": {}}},
        "output": {"answer": "On my way.", "raw": ""},
    }
    assert trace_holds_contact(content, DANA) is False


def test_a_phone_in_a_tool_contact_argument_is_a_subject_match() -> None:
    """The booking/lead argument is a contact-typed field: its value is
    canonicalized the way storage canonicalizes it and compared exactly."""
    content: dict[str, Any] = {
        "tools": {
            "tool_calls": [
                {
                    "call_id": "call-1",
                    "name": "create_lead",
                    "arguments": {"customer_phone_or_email": "(555) 222-1919"},
                }
            ]
        }
    }
    assert trace_holds_contact(content, DANA) is True


def test_a_phone_in_the_prompt_tool_arguments_is_a_subject_match() -> None:
    content: dict[str, Any] = {
        "prompt": {
            "template_ref": "dispatch@3",
            "messages": [
                {
                    "role": "assistant",
                    "segments": [["seg-1", "instruction", "Propose the action."]],
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "name": "book_appointment",
                            "arguments": {"customer_phone_or_email": "555.222.1919"},
                        }
                    ],
                }
            ],
        }
    }
    assert trace_holds_contact(content, DANA) is True


def test_a_phone_in_the_visitor_message_section_is_a_subject_match() -> None:
    """The retrieval section preserves the visitor's own words, which can name
    the subject without any tool call ever carrying them."""
    content: dict[str, Any] = {
        "retrieval": {
            "query": "Please call 555-222-1919 back",
            "original_message": "Please call 555-222-1919 back",
        }
    }
    assert trace_holds_contact(content, DANA) is True


def test_an_email_in_trace_content_is_a_subject_match() -> None:
    """The email path shares the mechanism: recognized in content fields,
    canonicalized, compared exactly — never substring-matched. Exactness is
    the core canonical equality, local-part case included, the same equality
    leads and bookings already match on."""
    subject = Contact.parse("dana.ruiz@example.com")
    content: dict[str, Any] = {
        "output": {"answer": "We will email dana.ruiz@example.com to confirm.", "raw": ""}
    }
    assert trace_holds_contact(content, subject) is True
    assert text_holds_contact("never mailed Dana.Ruiz@example.com", subject) is False


def test_an_email_inside_trace_metadata_is_not_a_subject_match() -> None:
    subject = Contact.parse("dana.ruiz@example.com")
    content: dict[str, Any] = {
        "component_manifest": {"model": {"id": "dana.ruiz@example.com", "parameters": {}}},
        "output": {"answer": "Noted.", "raw": ""},
    }
    assert trace_holds_contact(content, subject) is False


def test_an_unparseable_trace_content_is_never_a_match() -> None:
    """Fail closed: content that is not the trace's JSON object at all cannot
    widen discovery, whatever text it happens to carry."""
    assert trace_holds_contact(None, DANA) is False
    assert trace_holds_contact("prompt about 555-222-1919", DANA) is False
    assert trace_holds_contact([("prompt", "call 555-222-1919")], DANA) is False


def test_a_section_with_an_unrecognized_shape_is_not_a_match() -> None:
    """A field that does not have the section's documented structure is
    invisible to discovery even when its text names the contact: matching
    widens only along the designed path, never through a shape surprise."""
    content: dict[str, Any] = {"prompt": "Please call 555-222-1919 back"}
    assert trace_holds_contact(content, DANA) is False


def test_a_digit_subsequence_spaced_through_a_message_is_not_a_match() -> None:
    """The whole-content digit scan matched the probe as a subsequence of any
    digits in the text. Recognition requires the phone's own shape, so a
    reference number whose digit groups happen to interleave the probe's
    digits is not the subject."""
    assert text_holds_contact("ticket 17 55 52 22 19 19 closed", DANA) is False


def test_a_formatted_phone_in_a_message_is_a_match() -> None:
    assert text_holds_contact("call me at (555) 222-1919 today", DANA) is True
    assert text_holds_contact("my number is +1 555 222 1919", DANA) is True


def test_a_contact_typed_argument_must_parse_as_a_contact() -> None:
    """Contact-typed fields are never digit-normalized: a long number that
    merely contains the subject's digits does not canonicalize to it, and a
    value that parses to no contact at all matches nothing."""
    content: dict[str, Any] = {
        "tools": {
            "tool_calls": [
                {
                    "call_id": "call-1",
                    "name": "create_lead",
                    "arguments": {"customer_phone_or_email": "17555221919000"},
                }
            ]
        }
    }
    assert trace_holds_contact(content, DANA) is False
    assert contact_value_matches("555-222-1919", DANA) is True
    assert contact_value_matches("+15552221919", DANA) is True
    assert contact_value_matches("not a contact", DANA) is False
    assert contact_value_matches(17555221919000, DANA) is False


def test_another_subjects_contact_is_not_a_match() -> None:
    content: dict[str, Any] = {
        "retrieval": {"original_message": "Please call 555-333-4444 back"},
        "tools": {
            "tool_calls": [
                {
                    "call_id": "call-1",
                    "name": "create_lead",
                    "arguments": {"customer_phone_or_email": "555-333-4444"},
                }
            ]
        },
    }
    assert trace_holds_contact(content, DANA) is False
