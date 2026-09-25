# Admin chat-to-turn navigation

## Trigger

An operator starts in **Chat queue**, copies the visible chat UUID, and pastes it
into **AI turn explorer**. The current explorer accepts only a turn UUID or an
OpenTelemetry trace ID, so the valid chat UUID returns “not found”. Queue rows
also hide the chat UUID, and a trace-search result is styled as a card without
an explicit action, making its drill-down easy to miss or misread as inert.

## Expected result

- Every chat-queue row visibly identifies its chat with a shortened chat ID;
  chat search continues to match the complete ID.
- The selected chat shows its complete chat ID and a trace-access-gated list of
  its recorded AI turns. Each turn names its turn number, outcome, turn ID, and
  trace ID (or says that no trace ID was recorded).
- Each queue turn has an explicit **Open in AI turn explorer** button. It opens
  that exact turn without making the operator copy an identifier or issuing a
  duplicate audited turn-record read.
- The explorer lookup accepts a turn ID, trace ID, or chat ID. A chat ID produces
  the content-free turns for that chat; those results have explicit **Open turn**
  buttons with ordinary keyboard focus and activation behavior.
- The explorer labels each identifier by kind instead of showing an unlabeled
  hash, and a queue-originated drill-down offers a clear return to Chat queue.

## HTTP contract

`GET /api/admin/traces/by-session/{session_id}` returns a
`TraceSearchResponsePage` containing the session's content-free turn-record
projections, oldest first. Query parameters are:

- `tenant_id`: required tenant scope.
- `reason`: required governed-read reason.
- `limit`: optional, `1..200`, default `200`.

The route returns the existing trace-search fields only: turn ID, session ID,
trace ID, recorded time, outcome, manifest hash, diagnosis causes/statuses,
turn index, schema version, and source-generation IDs. The response never
contains prompts, messages, evidence, model output, or tool arguments.

Compatibility is additive: existing queue, trace-search, and single-turn
responses are unchanged.

## Tenant and authorization boundary

- The route uses the existing dedicated trace-read grant, not the ordinary chat
  viewer role. An operator may read a transcript without automatically gaining
  inference-plane identifiers.
- Tenant qualification is applied to both session and turn-record lookup; an
  absent session and a session belonging to another tenant are indistinguishable.
- A successful session lookup writes one content-free audit event naming the
  session, reason, match count, and bound. Opening a returned turn still creates
  the existing audited `trace.read` event exactly once.

## Failure behavior

- Without a trace-read grant, the queue's AI-turn panel explains that access is
  unavailable and does not hide the rest of the conversation.
- An absent or cross-tenant chat ID returns the same empty result as a chat with
  no retained turn records, so the route cannot enumerate another tenant's
  sessions. The explorer says that no retained turns were found for that chat.
- Transport failures retain the current queue and show a retryable panel error.
- A chat with no retained turn records shows an explicit empty state; retention
  may remove turn records before the conversation itself.

## Exclusions

- No prompt, transcript, evidence, model output, or tool payload is added to a
  list response.
- The six Gate B trace-search filter dimensions do not change; chat ID is a
  direct lookup/navigation path, not a seventh filter.
- This change does not alter retention, trace grants, replay behavior, polling,
  or the operational/inference telemetry boundary from ADR-0010.
