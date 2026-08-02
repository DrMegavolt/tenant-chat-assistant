"""Named fields the domain can require from a customer.

A closed set rather than free strings. Field names travel into error payloads and
into the questions the assistant asks, so an open `str` invites two failures: a
typo that renders as a nonsense prompt, and — worse — a caller passing a field
*value* where a name belongs, publishing PII through a channel designed to be safe.

Kept in its own module so both failure paths (:mod:`tenantchat.core.errors`) and
the workflow outcome types that report partial slot state can reference it without
one importing the other.
"""

from __future__ import annotations

from enum import StrEnum


class RequiredField(StrEnum):
    """A field the domain may report as missing."""

    CUSTOMER_NAME = "customer_name"
    CONTACT = "contact"
    SERVICE = "service"
    SUMMARY = "summary"
    ADDRESS = "address"
    SLOT = "slot"
