# Tracing one chat turn

`OBS-001` gives every chat turn one addressable identity end to end. Every
structured log line a service emits during the turn carries the same request
ID and trace ID, and the tenant appears only as a keyed pseudonym. This
walkthrough shows what a turn looks like in the log plane and how to follow
it, then states the redaction and volume contracts and how they are verified.

## The contract

Every JSON line has, at minimum:

| Field         | Meaning                                                        |
| ------------- | -------------------------------------------------------------- |
| `timestamp`   | ISO-8601 UTC when the line was created                         |
| `level`       | `DEBUG`/`INFO`/`WARNING`/`ERROR`/`CRITICAL`                    |
| `service`     | `chat-api`, `chat-job-worker`, or the `OTEL_SERVICE_NAME`      |
| `environment` | `APP_ENV` (`local`, `production`, …)                           |
| `logger`      | the emitting module                                            |
| `event`       | the event name, e.g. `request completed`, `chat turn completed`|

A line emitted under a request also carries `request_id` and `trace_id`
(`X-Request-Id` / `X-Trace-Id` on the wire, both server-minted and echoed on
every response). Once a verified visitor credential has named the tenant, the
line carries `tenant` — the tenant's pseudonym, never the tenant ID.

Structured extras are allowlisted (`logging_setup.py`): a log statement cannot
add a field the allowlist does not name, so content cannot sneak in through a
new `extra` key.

## Following one turn

With `CHAT_API_LOG_ACCESS=true`, a turn through `POST /api/chat` produces at
least two lines sharing one `trace_id`:

```json
{"event": "request completed", "method": "POST", "path": "/api/chat",
 "status": 200, "duration_ms": 6.5, "request_id": "392c…", "trace_id": "1118…",
 "service": "chat-api", "environment": "local", "tenant": "t-cf8f310c626a4349", "…": "…"}
```

```json
{"event": "chat turn completed", "graph_version": "dispatch@1",
 "prompt_version": "dispatch-system@1", "committed_actions": [],
 "request_id": "392c…", "trace_id": "1118…", "tenant": "t-cf8f310c626a4349", "…": "…"}
```

- The **chat hop** is the access line plus the turn event. The turn event
  carries the component versions and the bounded action enum (`book_appointment`,
  `capture_lead`, `handoff`, …), never the answer text.
- **Tool execution** is in-process under the same context: anything the agent
  runtime or a domain service logs during the turn inherits the trace, because
  the context is task-bound, not passed by hand.
- **Background jobs**: an enqueuer stores its trace in the job payload, and the
  worker binds it for the duration of the job, so a privacy-deletion job filed
  during a request logs under the request's trace with the same tenant
  pseudonym. The payload fingerprint ignores the trace field — a retried
  enqueue with a fresh trace is still the same work.
- **Internal services**: every outbound call the API makes to an internal
  service — Elasticsearch and the embedding service — attaches `X-Request-Id`
  and `X-Trace-Id` via `correlation_headers()`. Because the correlation context
  is ambient (a `contextvars.ContextVar`), the infrastructure clients
  (`ElasticsearchSearchIndex` and `EmbeddingServiceClient`) read it at call
  time and merge the headers into each request automatically. A tool or graph
  node that logs inside a request inherits the same trace without new plumbing.
  The job worker uses the same clients; its bound context (from the enqueuing
  request's payload) flows through to every internal call the worker makes.
  Headers are placed only on internal-service calls and never on a third-party
  provider call (e.g. an LLM API).

To follow a turn: take `request_id` from any problem document or response
header, then filter the log store for that `request_id` (or the `trace_id` it
shares with the turn's access line). The same `trace_id` is the join key the
worker and internal services use.

## Redaction — what never appears

A line never carries, by default:

- message content (visitor or assistant text, prompts, model output),
- contact details — phone numbers and email addresses are scrubbed by the PII
  filter from the message, the format args, and the formatted traceback before
  any handler formats the record,
- credentials — bearer tokens, gateway tokens, signing keys, LLM API keys,
- full document chunks (retrieved evidence belongs to the inference plane,
  `ADR-0010`).

`DomainError.detail` is the one deliberate exception: it is operator context
that reaches the line only as an allowlisted extra, after the PII filter, and
never reaches a response.

## Volume and retention

- `CHAT_API_LOG_LEVEL` bounds how much a service emits (`INFO` for
  production). `DEBUG` is for a deliberate, short-lived debugging session.
- `CHAT_API_LOG_ACCESS` toggles the per-request access line (off by default —
  one line per request is real volume).
- `CHAT_API_LOG_JSON=false` turns the structured formatter off, e.g. for a
  plain-text development tail.
- Retention is a log-store property, not a service property: the deployment's
  Loki configuration in `k8s/observability-drilldown-fixes.yaml` (`table_manager`
  retention knobs and `reject_old_samples_max_age`) is where log age is
  bounded.

## Tenant pseudonyms

`tenant` is `t-` plus the first 16 hex characters of an HMAC-SHA256 of the
tenant ID under `CHAT_API_LOG_PSEUDONYM_KEY`. With the key set, the pseudonym
cannot be reversed. Every service sharing a log store must share the key, or
the same tenant gets a different pseudonym in each service's lines; without a
key (development only) the pseudonym is a plain digest.

## Verification

These specifications back this document and run hermetically:

```bash
make test   # or: uv run pytest -m "not integration"
```

- `services/api/tests/test_correlation.py` — server-minted IDs, forged IDs
  ignored, one chat turn sharing one trace with the tenant pseudonym, and the
  visitor message absent from the log plane.
- `services/api/tests/test_logging_setup.py` — the contract field set, the
  extra allowlist, scrubbed tracebacks, and the level/JSON/access
  configuration.
- `services/api/tests/test_worker_correlation.py` — the worker binds the
  enqueuing request's trace, and the payload fingerprint treats the trace as
  attribution rather than work.
- `tests/security/test_privacy_redaction.py` and `tests/test_side_service_contracts.py` —
  the redaction primitives and the side services' header forwarding.
