# Privacy and data governance (PRIV-001)

This document is the machine-readable contract the consent gate, the retention
worker, and the export/erasure workflows all implement. The server-owned copy of
the classification lives in `tenantchat.core.privacy`; anything that rephrases a
purpose or a data class here without changing that module is documentation that
has drifted.

## Data classes and permitted uses

A data class is what retention is configured against, and the operating plane
stores one class per purpose. A purpose is something a tenant may do with stored
contact data; a visitor agrees to a purpose under a server-derived statement
before an action that stores contact data is allowed.

| Data class | Permitted uses |
| --- | --- |
| Transcript | operating the conversation; staff follow-up |
| Contact details | arranging appointments; staff follow-up |
| Address | arranging service appointments |
| Booking | arranging appointments; delivering the booked service |
| Lead | staff follow-up |
| Handoff | staff takeover of the conversation |
| Consent | proving consent and withdrawal |

## Purposes

- `booking` — arrange the appointment.
- `follow_up` — follow up about the work.

A booking requires both `booking` and `follow_up`; capturing a lead requires
`follow_up`. Consent is recorded per session and purpose, keyed by the
conversation the visitor opened, under the sentence the tenant's policy publishes
(see `TenantPolicy.consent_statement`). Withdrawal is a status flip, never a
delete: "consent was given and then withdrawn" stays answerable.

## Retention

The retention policy in `tenantchat.core.privacy.RetentionPolicy` pairs each
data class with a maximum age. Today the transcript is kept for 90 days; a class
with no rule is kept indefinitely. The erasure worker (`privacy_worker.py`) runs
a scheduled pass that, per tenant, purges expired records and emits an auditable
count of what it removed.

## Export and erasure

An authorized operator exports everything the platform holds about one subject —
one contact value, matched across transcripts, leads, bookings, handoffs, and
consent — for one tenant, and nothing more. A deletion request is filed into a
queue and fulfilled by the erasure worker under the erasure role's credentials;
completed requests are marked done and their contact value anonymized.

The application role holds no `DELETE` on sessions, transcripts, or consent, so
the API cannot erase anything itself. The `PRIVACY_DATABASE_URL` role is the
only one that can. See `provision_app_role.sql` and `provision_privacy_role.sql`.

## The operational-plane invariant

Phone numbers, email addresses, addresses, and free text reach neither logs,
metrics, nor audit details. The `redaction.py` helpers scrub free text and
tool-event trees, and a logging filter installed at startup redacts an
accidental f-string as a second line of defence. The inference-trace plane
(`PRIV-002`, `ADR-0010`) is the only deliberate home for content and is not part
of this task.
