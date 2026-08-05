"""The privacy contract: what is stored, why, for how long, and how it ends.

Everything a visitor agrees to is pinned to server-owned copy here, so the
widget, the API, the retention worker, and the erasure worker all read the same
statement instead of each rephrasing it.

The two enforcement points live in this module. ``ConsentGrant.require`` is the
gate `PRIV-001` puts in front of any action that stores contact data, and
``RetentionPolicy.expired`` is the boundary the retention worker purges on. Both
are pure functions of their inputs, so a lifecycle integration test can
exercise them without a database.
"""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final

from tenantchat.core.contact import Contact
from tenantchat.core.errors import PolicyViolationError


class ConsentRequiredError(PolicyViolationError):
    """Contact data cannot be stored because the visitor never agreed to the purpose.

    A policy refusal, not a validation failure: the request is complete and
    well-formed, but `PRIV-001` gates every action that stores contact data on
    a recorded consent grant for the purposes it uses. Carries the missing
    purposes so the caller can ask for exactly the consent that is outstanding
    instead of re-asking for what was already given.

    Defined here rather than in :mod:`tenantchat.core.errors` because this
    module is its natural home and the direction of imports is one-way: this
    module imports the error taxonomy, never the other way around.
    """

    code = "consent_required"
    message = (
        "The visitor must first agree to how their details will be used before "
        "this can be arranged."
    )

    def __init__(
        self, missing_purposes: Collection[ConsentPurpose], detail: str | None = None
    ) -> None:
        if not missing_purposes:
            raise ValueError("ConsentRequiredError requires at least one missing purpose")
        self.missing_purposes = tuple(missing_purposes)
        super().__init__(detail)


class DataClass(StrEnum):
    """What kind of personal data a stored record is.

    The value is what retention is configured against: one policy row per
    class, rather than one per table.
    """

    CONTACT = "contact"
    ADDRESS = "address"
    TRANSCRIPT = "transcript"
    BOOKING = "booking"
    LEAD = "lead"
    HANDOFF = "handoff"
    CONSENT = "consent"
    # PRIV-002/ADR-0010: the inference plane, one row per conversation turn.
    # Shorter-lived than the transcript by design, narrowly authorized, and
    # audited on every read (see `TurnRecordReadReason`).
    INFERENCE_TRACE = "inference_trace"


# The documentation a data-subject rights request starts from: which classes of
# data the platform stores, and for which purposes each is used. Values are
# human prose, because this document exists to be quoted back.
PERMITTED_USES: Final[dict[str, tuple[str, ...]]] = {
    DataClass.TRANSCRIPT.value: ("operating the conversation", "staff follow-up"),
    DataClass.CONTACT.value: ("arranging appointments", "staff follow-up"),
    DataClass.ADDRESS.value: ("arranging service appointments",),
    DataClass.BOOKING.value: ("arranging appointments", "delivering the booked service"),
    DataClass.LEAD.value: ("staff follow-up",),
    DataClass.HANDOFF.value: ("staff takeover of the conversation",),
    DataClass.CONSENT.value: ("proving consent and withdrawal",),
    # The inference plane is a derived copy of the conversation's content for
    # the purposes of answering-quality analysis and incident investigation.
    # It is governed separately from the transcript: shorter retention, a
    # dedicated access role, and a mandatory read reason.
    DataClass.INFERENCE_TRACE.value: (
        "answering-quality analysis",
        "incident investigation",
    ),
}


class ConsentPurpose(StrEnum):
    """One thing a tenant may do with stored contact data.

    An enum rather than a free string so the set of purposes is closed: a
    tenant cannot write a purpose the visitor cannot have agreed to.
    """

    BOOKING = "booking"
    FOLLOW_UP = "follow_up"


class ConsentStatus(StrEnum):
    GRANTED = "granted"
    WITHDRAWN = "withdrawn"


# The clauses of the consent statement, one per purpose. Declared together with
# the purposes so a purpose cannot gain a store-and-use clause without a
# sentence for it; statement copy lives here, server-owned.
_PURPOSE_CLAUSES: Final[dict[ConsentPurpose, str]] = {
    ConsentPurpose.BOOKING: "arrange the appointment",
    ConsentPurpose.FOLLOW_UP: "follow up about the work",
}


def consent_statement(tenant_name: str, purposes: Collection[ConsentPurpose]) -> str:
    """The sentence a visitor agrees to, built from the tenant's name.

    One statement per tenant (a visitor grants purposes together in the
    widget), so the builder names every purpose it is given.
    """
    # Declaration order, regardless of input order, so the sentence is stable.
    ordered = [_PURPOSE_CLAUSES[purpose] for purpose in ConsentPurpose if purpose in purposes]
    if not ordered:
        raise ValueError("a consent statement needs at least one purpose")
    uses = ", ".join(ordered)
    return (
        f"I agree that {tenant_name} may store the name, address, and contact "
        f"details I enter here in order to {uses}."
    )


@dataclass(frozen=True, slots=True)
class ConsentGrant:
    """Proof that a session granted purposes under a given statement.

    Immutable, so it can travel between the store that assembled it and the
    service that enforces it without being re-derived from untrusted input.
    """

    tenant_id: str
    session_id: str
    purposes: frozenset[ConsentPurpose]
    statement: str
    granted_at: datetime

    def require(self, *purposes: ConsentPurpose) -> frozenset[ConsentPurpose]:
        """Refuse the action unless every purpose was granted.

        Raises:
            ConsentRequiredError: one or more purposes were never granted, with
                the missing set named for the caller.
        """
        missing = frozenset(purpose for purpose in purposes if purpose not in self.purposes)
        if missing:
            raise ConsentRequiredError(missing)
        return self.purposes

    def granted(self, purpose: ConsentPurpose) -> bool:
        return purpose in self.purposes


_DEFAULT_TRANSCRIPT_RETENTION: Final = timedelta(days=90)
# PRIV-002: turn records are content-bearing and therefore shorter-lived than
# the transcript they derive from. One policy rule, independently purgeable.
_DEFAULT_TRACE_RETENTION: Final = timedelta(days=30)


@dataclass(frozen=True, slots=True)
class RetentionRule:
    """How long one data class is kept before the worker may purge it.

    No rule means "kept indefinitely": the policy intentionally carries no
    default rule for bookings and leads, which are business records the tenant
    may need long after the transcript is gone.
    """

    data_class: DataClass
    max_age: timedelta = _DEFAULT_TRANSCRIPT_RETENTION

    def expired(self, recorded_at: datetime, *, now: datetime) -> bool:
        return now - recorded_at >= self.max_age


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """The per-class retention schedule the purge worker enforces.

    Declared in source for now; `FEAT-006` moves tenant policy into the
    database, and this type is the shape that migration will fill from.
    """

    rules: tuple[RetentionRule, ...] = (
        RetentionRule(DataClass.TRANSCRIPT),
        RetentionRule(DataClass.INFERENCE_TRACE, _DEFAULT_TRACE_RETENTION),
    )

    @classmethod
    def defaults(cls) -> RetentionPolicy:
        return cls()

    def max_age(self, data_class: DataClass) -> timedelta | None:
        for rule in self.rules:
            if rule.data_class is data_class:
                return rule.max_age
        return None

    def expired(self, data_class: DataClass, recorded_at: datetime, *, now: datetime) -> bool:
        """Whether a record of this class has passed its retention.

        A class with no rule never expires; the policy must say so explicitly
        rather than the worker guessing.
        """
        rule = self.max_age(data_class)
        if rule is None:
            return False
        return now - recorded_at >= rule


class TurnRecordReadReason(StrEnum):
    """Why one operator read one turn record, recorded on every read.

    A closed set rather than a free string so an audit trail cannot carry a
    reason nobody agreed to, and so a dashboard can group reads by cause. The
    `PRIV-002` read surface refuses a request without one of these.
    """

    QUALITY_REVIEW = "quality_review"
    INCIDENT_INVESTIGATION = "incident_investigation"
    SUBJECT_REQUEST = "subject_request"
    TENANT_SUPPORT = "tenant_support"


# Replacement values for irreversible anonymization. ``erased`` reads as
# deliberate where an empty string could be a data-entry hole.
ANONYMIZED_NAME: Final = "erased"
ANONYMIZED_ADDRESS: Final = "erased"
ANONYMIZED_SUMMARY: Final = "erased"
ANONYMIZED_CONTACT_VALUE: Final = "erased@example.invalid"

# The same recognition rules `Contact.parse` applies, in free text: a message
# can carry a phone number or an address without being a well-formed command.
_PHONE_IN_TEXT = re.compile(r"(?:\+?1[\s.-]?)?(?:\(?\d{3}\)?[\s.-]?)\d{3}[\s.-]?\d{4}")
_EMAIL_IN_TEXT = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_ANONYMIZED_TEXT_MARKER = "[erased]"


def anonymized_contact(contact: Contact) -> Contact:
    """The replacement for a real contact value, preserving the kind.

    The kind is kept so the column that held it stays type-consistent; the
    value is a sentinel no parser will ever confuse with a real address.
    """
    return Contact(kind=contact.kind, value=ANONYMIZED_CONTACT_VALUE)


def anonymize_text(text: str) -> str:
    """Replace phone numbers and email addresses in free text.

    The surrounding text survives, which matters for transcripts: the point of
    erasure is that the data is unrecoverable, not that the sentence it lived
    in disappears.
    """
    return _PHONE_IN_TEXT.sub(
        _ANONYMIZED_TEXT_MARKER, _EMAIL_IN_TEXT.sub(_ANONYMIZED_TEXT_MARKER, text)
    )
