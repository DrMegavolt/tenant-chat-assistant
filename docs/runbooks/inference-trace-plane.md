# The inference trace plane (PRIV-002)

The turn record is the only deliberate home for prompt, evidence, and output
content (`ADR-0010`). This runbook is the operator's manual for that plane:
what lives where, who may read it, how it expires, and how to answer a rights
request about it.

## What the plane holds

One `turn_records` row per conversation turn: the opaque `content` object
(prompt, retrieved evidence, model output, verdicts — the shape `OBS-004`
fills in), a `trace_id` correlating with the operational plane's spans, and the
timestamp retention is computed from. `turn_record_projections` rows are
derived datasets pinned to a turn record (evaluation datasets under
`FEAT-008`); they cascade off their turn record, so erasing the record erases
the projection. The tables carry no content-bearing columns beyond `content` —
the schema never parses it.

The operational plane (logs, metrics, spans) is content-free by construction:
the application never writes content there, and the collector's redaction
processor drops anything not on its allowlist ahead of every exporter.

## Retention

Turn records expire at 30 days, independently of the 90-day transcript rule.
The `privacy_worker` pass purges them per tenant and writes one
`privacy.retention_purged` audit row per tenant carrying only counts —
observable without exposing what was purged. A purge that removed nothing is
not audited, exactly like the transcript purge.

To check what a purge did:

```sql
SELECT tenant_id, details->>'turn_records_deleted' AS turns
FROM audit_events
WHERE action = 'privacy.retention_purged'
ORDER BY occurred_at DESC;
```

## Access

Turn-record reads require the dedicated `trace_viewer` grant
(`trace_access_grants`), not a transcript role. Grant and revoke through the
admin API as a platform administrator (CSRF token required):

```bash
curl -X POST $ADMIN/api/admin/trace-access \
  -H "X-CSRF-Token: $CSRF" -H "$GATEWAY_IDENTITY_HEADERS" \
  -d '{"tenant_id":"clearview","subject":"operator-7"}'
curl -X DELETE "$ADMIN/api/admin/trace-access?tenant_id=clearview&subject=operator-7" \
  -H "X-CSRF-Token: $CSRF" -H "$GATEWAY_IDENTITY_HEADERS"
```

Read one turn record:

```bash
curl "$ADMIN/api/admin/traces/$TURN_ID?tenant_id=clearview&reason=incident_investigation" \
  -H "$GATEWAY_IDENTITY_HEADERS"
```

Every read is audited with the actor, turn, and reason; refusals are audited as
`trace.read_refused`. Reasons are closed: `quality_review`,
`incident_investigation`, `subject_request`, `tenant_support`.

## Rights requests

Export (`/api/admin/privacy/export`) includes turn records and projections,
complete and untruncated. Deletion requests remove them: the worker deletes the
turn records for the subject's sessions under the erasure role, and the
projection cascade removes everything derived from them in the same statement.
The `privacy.erased` audit row carries `turn_records_deleted`. Verification:

```sql
SELECT count(*) FROM turn_records WHERE tenant_id = :tenant;
SELECT count(*) FROM turn_record_projections WHERE tenant_id = :tenant;
```

## Enabling content export to a trace viewer

Content export is off by default and the tracked deployment never enables it.
An operator adopting a self-hosted viewer (see `ADR-0010`) must, in one review
session:

1. Add the viewer's exporter and a dedicated pipeline to the collector config,
   keeping the operational pipelines on the redaction allowlist. Never widen
   the allowlist to carry content.
2. Set `TRACE_CONTENT_EXPORT_ENDPOINT` to the viewer's in-cluster
   `*.svc.cluster.local` URL and `TRACE_CONTENT_EXPORT=true` in the deployment
   environment. The API refuses to start with content export enabled for any
   endpoint outside the trust boundary, and
   `scripts/verify_deployment_security.py` refuses a manifest that enables it.
3. Keep the viewer's UI behind the same gateway authentication as the admin
   console, and treat it as part of the same 30-day retention surface — its
   store is a disposable projection, not the system of record.

## Verification

```bash
make test-privacy      # turn-record export, expiry, and erasure lifecycles
make test-repositories # envelope and grant stores against real PostgreSQL
make test-migrations   # schema and role provisioning
```
