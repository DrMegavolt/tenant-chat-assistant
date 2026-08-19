# Privacy and data governance (PRIV-001, PRIV-002)

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
| Inference trace | answering-quality analysis; incident investigation |

## Purposes

- `booking` — arrange the appointment.
- `follow_up` — follow up about the work.

A booking requires both `booking` and `follow_up`; capturing a lead requires
`follow_up`. Consent is recorded per session and purpose, keyed by the
conversation the visitor opened, under the sentence the tenant's policy publishes
(see `TenantPolicy.consent_statement`). Withdrawal is a status flip, never a
delete: "consent was given and then withdrawn" stays answerable.

That sentence has exactly one source. `GET /api/tenants` publishes what
`TenantPolicy.consent_statement()` returns, tenant override included, and the
widget renders that string rather than composing its own. This prevents a
tenant override from being displayed as the default it happened to
resemble. An override test asserts the displayed and recorded copy are the same
string.

## Retention

The retention policy in `tenantchat.core.privacy.RetentionPolicy` pairs each
data class with a maximum age. Today the transcript is kept for 90 days and the
inference trace for 30 days; a class with no rule is kept indefinitely. The
erasure worker (`privacy_worker.py`) runs a scheduled pass that, per tenant,
purges expired records and emits an auditable count of what it removed. The two
planes purge independently: an expired turn record is removed while the
transcript it derived from is still within its own, longer, retention.

## The inference trace plane (PRIV-002, ADR-0010)

`ADR-0010` splits telemetry into an operational plane (logs, metrics, spans —
content-free, long-lived) and an inference plane: the **turn record**, one
append-only row per conversation turn holding the prompt, the retrieved
evidence, the model output, and the validator verdicts. This is the only
deliberate home for that content.

### Classification, lawful basis, retention, and access

| Aspect | Rule |
| --- | --- |
| Classification | Prompt, retrieved evidence, and model output are content derived from the visitor's conversation, governed as the `inference_trace` data class. |
| Lawful basis | The same "operating the conversation" basis as the transcript: the trace is a short-lived copy of the conversation made for answering-quality analysis and incident investigation. It is not shared, sold, or used for any other purpose. |
| Retention | 30 days, independently of the 90-day transcript rule, enforced by the same retention pass with auditable counts. |
| Access | The dedicated `trace_viewer` role only — a tenant-scoped grant recorded in `trace_access_grants`, granted and revoked by a platform administrator. Transcript roles confer nothing. `platform_admin` retains access by virtue of the directory role. |
| Reads | Every read is audited to an actor, a turn, and a reason (`quality_review`, `incident_investigation`, `subject_request`, or `tenant_support`). Refused reads are audited too. |
| Erasure | A deletion request removes the subject's turn records; projections derived from them (e.g. evaluation datasets under `FEAT-008`) cascade off their turn record in the same statement. |
| Export | A subject export includes the turn records and projections, complete and untruncated, like every other class. |

### Content export to a trace viewer

Whether a viewer receives prompt and evidence text is one setting,
`TRACE_CONTENT_EXPORT`, **disabled by default**. Enabling it is legitimate only
for a viewer deployed inside the cluster trust boundary — a
`*.svc.cluster.local` service behind the same gateway authentication as the
admin console — and the API refuses to start with it enabled for any backend
outside that boundary (loopback is the development exception). The tracked
deployment inputs never enable it (`scripts/verify_deployment_security.py`
refuses a manifest that does). With it disabled, the collector's redaction
processor allows only operational attributes through, ahead of every exporter,
so adding a backend cannot widen what leaves the cluster. See the
`inference-trace-plane` runbook for the operator path to a viewer.

## Export and erasure

An authorized operator exports everything the platform holds about one subject —
one contact value, matched across transcripts, leads, bookings, handoffs,
consent, and the inference trace — for one tenant, and nothing more. A deletion
request is filed into a queue and fulfilled by the erasure worker under the
erasure role's credentials; completed requests are marked done and their contact
value anonymized.

The application role holds no `DELETE` on sessions, transcripts, consent, or
turn records, so the API cannot erase anything itself. The
`PRIVACY_DATABASE_URL` role is the only one that can. See
`provision_app_role.sql` and `provision_privacy_role.sql`.

## The operational-plane invariant

Phone numbers, email addresses, addresses, and free text reach neither logs,
metrics, nor audit details. The `redaction.py` helpers scrub free text and
tool-event trees, and a logging filter installed at startup redacts an
accidental f-string as a second line of defence. The inference-trace plane is
the only deliberate home for content and is not part of the operational plane.
