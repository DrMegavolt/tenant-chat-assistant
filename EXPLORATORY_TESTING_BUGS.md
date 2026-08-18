# Exploratory Testing Defect Dossier

> Current defect register and historical exploratory evidence. The status table
> below is authoritative. Dated revalidation tables preserve what was observed
> at discovery time; they do not override the current status.

## Current status — 2026-08-17

`Guarded by` is what fails if the defect returns. A resolved row whose guard is
operational rather than a build test says so, because that distinction decides
whether a regression is caught before or after a deploy.

| ID | Current status | Backlog owner | Guarded by | Current interpretation |
|---|---|---|---|---|
| BUG-001 | **Resolved** | `FEAT-004` / `SEC-002` | `tests/repositories/test_handoff_repository.py` | Handoff binds to the real visitor session; the live accept/reply/resolve journey passed. |
| BUG-002 | **Resolved** | `DATA-003` / `AGENT-001` | `packages/core/tests/test_intent_routing.py` (continuation rule); single commit by `DATA-003`'s idempotency tests | An active booking workflow survives a service-area detour instead of being displaced into escalation, and commits once. |
| BUG-003 | **Resolved** | `AGENT-001` | `tests/agent_runtime/test_routing_workflows.py`, `tests/agent_runtime/test_tool_dispatch.py` | Lead confirmation and idempotent commit nodes are in `dispatch@3`, which is now the only lead ingress (BUG-024). |
| BUG-004 | **Resolved** | `RAG-005` | `services/api/tests/test_citations.py`, `packages/core/tests/test_claims.py` | An answer resting only on tool verdicts publishes no document citation. The prose fallback that could still bypass this closed with BUG-020. |
| BUG-005 | **Resolved** | `RAG-002` | `services/api/tests/test_search_query_fields.py` | Every retrieval query is held to the adapter's own index mapping, which is the fault the live cluster hid. |
| BUG-006 | **Resolved** | Demo data / `RAG-001` | Operational only: knowledge index-integrity check | The contaminated source was renamed/tombstoned. Demo data cannot be guarded by a unit test; re-check after any reseed. |
| BUG-007 | **Resolved** | `PRIV-001` / `FEAT-013` | `frontend/tests/privacy.test.tsx` | The general storage disclosure matches free-text behavior. |
| BUG-008 | **Resolved** | `SEC-002` / widget | `frontend/tests/session.test.tsx` | A rejected stored credential is discarded and a fresh session is opened. |
| BUG-009 | **Resolved** | `RAG-007` | `services/api/tests/test_citations.py`, `services/api/tests/test_harness_cases.py` | Trusted tenant configuration can support business-hours answers. |
| BUG-010 | **Open — not deterministically revalidated** | `REL-001` / `QA-005` | — | Safe replay after a forced model timeout still needs a fault-injection proof. |
| BUG-011 | **Resolved** | `OBS-003` | Operational: `make dashboard-check`, `make grafana-smoke` | All repository dashboards are generated from one source and verified against the deployment. |
| BUG-012 | **Open — audit incomplete** | `OBS-003` | — | Tenant Chat dashboards exist; the wider Tempo/Phoenix/MLflow/Pyroscope quality audit was not repeated. |
| BUG-013 | **Open — not revalidated** | `SEC-002` / `FEAT-010` | — | Old-widget/new-server compatibility has not been exercised in a controlled deployment window. |
| BUG-014 | **Resolved** | `DEP-001` | `tests/test_local_k8s_release.py` | The orphan Service is gone and reconciliation fails the release if one recurs. |
| BUG-015 | **Resolved** | `FEAT-013` | Presentation only: `white-space: pre-wrap` in `frontend/src/widget/widget.css` | Availability choices render as separate lines. No test asserts the line breaks; a CSS change could regress it silently. |
| BUG-016 | **Resolved** | `OBS-005` / database | `tests/repositories/test_trace_governance.py`, `frontend/tests/session.test.tsx` | Turn outcomes accept `refused`/`failed`, and unrelated integrity errors are no longer mapped to session 404. |
| BUG-017 | **Resolved** | `FEAT-004` | `frontend/tests/admin.test.tsx` | Handoff lifecycle notices render with a System author. |
| BUG-018 | **Resolved** | `FEAT-016` | `tests/test_audit_taxonomy.py` | The audit action vocabulary has one backend source and a completeness test. |
| BUG-019 | **Resolved** | `AGENT-001` | `packages/core/tests/test_intent_routing.py`, `tests/agent_runtime/test_routing_workflows.py` | Booking and availability are removed when tenant policy disables booking. |
| BUG-020 | **Resolved** | `RAG-007` | `packages/core/tests/test_claims.py` | A ZIP the tool decided is grounded on that verdict alone; retrieval can neither overrule it nor stand in for a ZIP the tool never checked, and affirmative idioms no longer read as refusals. |
| BUG-021 | **Resolved** | `SEC-002` | `tests/test_web_gateway.py`, `services/api/tests/test_openapi_contract.py` | `POST /api/book` and `POST /api/leads` were retired. Every booking and lead now commits through the graph, under the signed visitor credential; no route accepts body identity. |
| BUG-022 | **Resolved** | `SEC-003` | `tests/test_web_gateway.py` | The consent preflight allows the credential header the widget sends, and a per-route table now pins every public route's preflight to the headers its own request carries. |
| BUG-023 | **Resolved** | `PRIV-001` | `packages/core/tests/test_tenant.py`, `frontend/tests/privacy.test.tsx` | `GET /api/tenants` publishes the server's statement and the widget renders it verbatim; the widget's local copy of the sentence was deleted. |
| BUG-024 | **Resolved** | `DATA-004` | `services/api/tests/test_actions.py`, `tests/repositories/test_postgres_repositories.py` | The non-idempotent ingress is gone with the route. The one remaining lead path is `RecordedLeadService`, whose replay contract was already covered. |
| BUG-025 | **Resolved** | `FEAT-007` | `frontend/tests/admin.test.tsx` | Every admin read claims a generation before its first await and publishes only while newest, so a superseded tenant queue or transcript is discarded. |
| BUG-026 | **Open** | `RAG-007` | — | A model's own disclaimer ("we cannot guarantee approval") is kinded as a coverage claim, fails token overlap, and refuses the whole answer. Found live on 2026-08-17; fires only when the model phrases the hedge that way. |
| BUG-027 | **Open** | `OBS-004` | — | A turn record's `tools` section is session-cumulative, not turn-scoped: every turn replays all prior turns' tool calls and committed effects. Found live on 2026-08-18. |
| BUG-028 | **Open** | `FEAT-007` | — | The admin session detail's Bookings, Lead info, and Tool calls cards read fields the API never returns, so all three are permanently empty. Found live on 2026-08-18. |
| BUG-029 | **Open** | `FEAT-007` | — | The chat-queue Leads and Messages tiles sum fields the session list never carries, so both always read 0. Found live on 2026-08-18. |
| BUG-030 | **Open** | `RAG-005` | — | Stripping `[evidence:...]` markers leaves the preceding space, so published answers show " ." wherever a citation was cited. Found live on 2026-08-18. |
| BUG-031 | **Open** | `PRIV-001` | — | The generated consent sentence joins its purpose clauses with a comma and no conjunction, and the visitor agrees to that text verbatim. Found live on 2026-08-18. |
| BUG-032 | **Open** | `RAG-005` | — | The widget renders a cited source's effective date in UTC, so a visitor west of UTC sees tomorrow's date on a source published today. Found live on 2026-08-18. |
| BUG-033 | **Open** | `FEAT-016` | — | `handoff.*`, `knowledge.version_*`, and `trace.replay_*` are offered in the audit filter but absent from the authorizing-permission map, so the console prints the bare action name. Found live on 2026-08-18. |

Every defect found by the 2026-08-17 repository review is closed. BUG-010,
BUG-012, and BUG-013 remain unverified rather than unfixed — each awaits fresh
fault-injection, observability-audit, or deployment-window evidence. BUG-026 is
new, found by the post-fix live run. BUG-027 through BUG-033 were found by the
2026-08-18 demo walkthrough against the same cluster; BUG-027 is the one that
touches a stated invariant rather than presentation. See `BACKLOG.md` for gate
ownership and dispatch order.

## Test context

- Test date: **2026-08-09**
- Deployment: local MicroK8s, latest deployed application version
- Visitor app: `https://chat.192.168.1.170.nip.io/`
- Admin console: `https://chat.192.168.1.170.nip.io/admin/`
- Tenants exercised: **Apex** (`apex`) and **Clearview** (`clearview`)
- Admin identity observed: `tenantchat-operator`
- The admin endpoint uses a demo TLS certificate. Bypass the certificate warning only for this local demo environment.
- Do not put passwords, bearer credentials, cookies, or Kubernetes Secret values in source, logs, screenshots, test fixtures, or commits. Obtain demo credentials through the normal local operator/Secret workflow.

The cluster was healthy during testing. Application pods had zero restarts and there were no current warning events. Elasticsearch and PostgreSQL showed five older restarts each, but no active failure was observed.

## Live revalidation — 2026-08-17, after the BUG-020…BUG-025 fixes

Run against the local MicroK8s cluster carrying the fixes (`chat-backend` and
`web` images rebuilt and rolled, migrations applied, knowledge reseeded).

| Run | Result | Notes |
|---|---|---|
| `make harness-live` (default `HARNESS_TIMEOUT=60`) | 20 checks, 7 failures | Six were `ERROR: timed out`; one was apex `case-1-grounded` returning zero citations, which is BUG-026. |
| Re-run with `HARNESS_TIMEOUT=180` | **20 checks, 0 failures** | Both tenants, all ten cases. |

The six timeouts were the harness's own 60-second per-request default, not the
system under test: the same cases passed unchanged at 180 seconds. The local
Qwen model regularly needs longer than 60 seconds for a retrieval turn, so a
default-timeout run reports failures that say nothing about the build. Set
`HARNESS_TIMEOUT` to at least 180 on this hardware before reading a run as a
Gate B result.

BUG-026 is the one genuine finding from the run and is recorded below.

## Historical revalidation snapshot — 2026-08-09

This pass restarted from fresh HTTPS tabs and exercised both tenants, the admin console, tool-backed flows, consent gates, handoff, audit, knowledge, AI turn details, and the live Grafana deployment. The local Qwen model was allowed up to roughly 50 seconds per turn before treating a response as failed.

| ID | Revalidation status | Fresh evidence |
|---|---|---|
| BUG-001 | **Pass** | Clearview handoff `HO-6F43D410E8FE46F3BAEEAAD019FE8D6D` was accepted, stopped AI invocation, delivered a staff reply to the visitor, and resolved cleanly on session `cd8d72e3-47a8-4303-8a3d-cbd9f38e8b7e`. |
| BUG-002 | **Pass** | Clearview HVAC booking committed exactly once as `BK-48AB2F4EE2414B60AB2D1CD6C4F0D24E`; trace `8f382f827465221541af53db3f8cfc56` showed one committed `book_appointment` effect. |
| BUG-003 | **Partial / fail** | The assistant no longer claimed a false success, but lead creation still failed after explicit consent and told the visitor to call instead. |
| BUG-004 | **Blocked by BUG-016** | Every fresh service-area probe failed at the session boundary before a supported answer/citation could be validated. |
| BUG-005 | **Fail** | Clearview documents still report `{indexed: 0}` while their chunks are retrievable in a live booking trace; running the integrity check did not reconcile the counts. |
| BUG-006 | **Fail** | Clearview still lists and retrieves `Northline Service Policy`; a Clearview citation modal also labels its publication as Northline. |
| BUG-007 | **Pass** | The disclosure now states that all typed messages are transmitted/stored, staff can read them, closing the tab does not delete them, and browser-local conversation credentials can be removed. |
| BUG-008 | **Fail** | A settled Clearview→Apex switch worked, but a fresh Apex→Clearview switch followed by a turn repeatedly returned `session absent or outside tenant` and did not self-recover. |
| BUG-009 | **Pass** | Apex correctly returned configured weekday and Saturday hours. |
| BUG-010 | **Not deterministically revalidated** | Slow Qwen turns completed or surfaced a separate session failure; safe replay deadline behavior was not forced independently. |
| BUG-011 | **Pass after deployment repair** | All five ConfigMaps and all five Grafana UIDs were verified live through authenticated in-pod API calls. |
| BUG-012 | **Partial** | Tenant Chat dashboards are present; the wider Tempo/Phoenix/MLflow/Pyroscope quality gaps were not re-audited in this pass. |
| BUG-013 | **Not revalidated** | No old-bundle/new-server deployment window was available during the pass. |
| BUG-014 | **Fail** | Live cluster still has orphan `chat-backend` Service port 8000 while `chat-admin` correctly uses 8004. |
| BUG-015 | **Pass** | Twenty booking slots rendered as distinct numbered lines at widget width. |

New defects found in this pass are BUG-016 through BUG-019 below.

## Historical revalidation snapshot — 2026-08-17

Verified against the repository and the live MicroK8s cluster: the applied
migration head, the Elasticsearch mapping and contents, the Postgres knowledge
and audit tables, Prometheus target health, and a full `make harness-live` run
across both tenants.

| ID | Status | Evidence |
|---|---|---|
| BUG-003 | **Pass** | `confirm_lead`/`commit_lead` are registered graph nodes; the lead pauses for consent and commits through the idempotent service. Graph is `dispatch@3`. |
| BUG-004 | **Pass** | Regressed and re-fixed during this pass — see below. Final live probes `877d32e0-f756-451f-848f-f3067a797454` (clearview) and `da8b8938-6375-41c7-b68e-c8896ba4e8fe` (apex) publish the true answer with zero citations. |
| BUG-005 | **Pass** | Root cause was the index mapping, not the counter: `version_id` was `text`, so the integrity check's `term` query matched nothing and reported `indexed: 0`. After the index was recreated, per-version counts match the database (6/9/7) and the check reports no findings. |
| BUG-006 | **Pass** | The source is renamed `Clearview Service Policy`, the Northline document is tombstoned, and the index returns zero hits for `northline` in either text or title. |
| BUG-008 | **Pass** | A 404 on a stored credential now discards it, opens a fresh session, and delivers the message once. |
| BUG-010 | **Not revalidated** | Forcing a model timeout deterministically still needs a fault-injection hook. |
| BUG-012 | **Not revalidated** | Tenant Chat dashboards are provisioned; the wider Tempo/Phoenix/MLflow/Pyroscope audit was not repeated. |
| BUG-013 | **Not revalidated** | No old-bundle/new-server deployment window was available. BUG-008's credential recovery may cover it; unconfirmed. |
| BUG-014 | **Pass** | The orphan `chat-backend` Service is gone. `scripts/reconcile_local_k8s.py` now fails the release if an orphaned Service or monitor survives. |
| BUG-016 | **Pass** | Migration `0019` admits `refused` and `failed`; the store no longer relabels a non-foreign-key `IntegrityError` as a session 404. Both outcomes are present in `turn_records`. |
| BUG-017 | **Pass** | Handoff lifecycle notices carry a system author and render as System. |
| BUG-018 | **Pass** | The action taxonomy has one home in `tenantchat.api.store.AUDIT_ACTIONS`; `tests/test_audit_taxonomy.py` fails if a router emits outside it or the console omits any of it. |
| BUG-019 | **Pass** | Booking and availability leave the routing candidate set when the tenant has booking disabled. |
| BUG-020 | **Partial** | The original true-answer refusal was fixed and verified live, but repository review found two deterministic residual failures documented below. |

### Found in this pass

- **Retrieval was failing on every live turn.** `active_chunks` sorted on
  `chunk_id`, which is the Elasticsearch `_id` and not a stored field, so
  Elasticsearch rejected the whole search and each turn recorded
  `retriever_version: "unavailable"` with no evidence and no citations. A
  long-lived index carried a stale dynamically-mapped `chunk_id`, so the fault
  only appeared once the index was recreated from the adapter's own mapping —
  the BUG-005 repair is what exposed it. Fixed; ordering moved into Python.
- **The job worker exposed no `/metrics`** while a Service and ServiceMonitor
  scraped port 8005, leaving a permanently down Prometheus target and no
  durable-job metrics. Fixed.
- **Financing questions could not be routed.** The demo's only governed
  knowledge is financing policy, and the router had no financing vocabulary, so
  "what financing options are available" scored highest as `availability`
  picking up "available" and clarified instead of retrieving. Fixed.
- **BUG-020**, below: a service-area claim confirmed by its own tool is refused.

After these fixes `make harness-live` runs **20 checks, 0 failures** across both
tenants, with case 1 returning a grounded answer and two citations.

Both faults now fail the build rather than only the cluster: every query the
search adapter issues is held to the adapter's own index mapping, and each
document the seed loads must route to the general agent, which is the only one
that retrieves.

---

## BUG-020 — Medium: a tool-confirmed service-area claim is refused as unsupported — **Resolved**

Retained in full because it is the one defect whose fix moved a trust boundary,
and because BUG-004 is only intelligible alongside it.

### Impact

"Do you serve my ZIP?" — a core home-services question — always ends in the
server-written refusal, on both tenants. The system is safe but wrong: it
withholds a true answer its own deterministic tool just produced.

### Observed evidence

Turn `5118b2f2-46ad-44af-ab2e-937d7b86ba2e` (`clearview`, "Do you serve ZIP
97205?"):

- routed `service_area`; `retriever_version: v1`
- `check_service_area(zip=97205)` returned `{"served": true, "zip": "97205", ...}`
- the model answered "Yes, we serve ZIP code 97205."
- `verdicts.claims_invalid`: `[{"kind": "service_area", "value": "Yes, we serve ZIP code 97205."}]`
- outcome `refused`

`f86dbac7-3217-4a60-bcdd-78f103c7b295` (`apex`, ZIP 98103) is identical.

### Working theory

`nodes.py` calls `validate_sensitive_claims` with `evidence_texts` drawn only
from admitted retrieval evidence. Tool results are never offered as grounding,
so a claim that only a tool can support has nothing to match and fails
`_sentence_supported`'s 0.7 token-overlap threshold. A deterministic tool result
is stronger grounding than retrieved prose, and the validator cannot see it.

Note that simply appending the tool-result JSON to `evidence_texts` does **not**
fix it: the claim sentence and the JSON share too few tokens to clear the
threshold, and it would let unrelated claims borrow support from any tool
output. The service area needs an authoritative channel of its own, in the
shape of the existing `trusted_prices` argument.

### Acceptance criteria

- A service-area claim whose ZIP the service-area tool confirmed is supported,
  and the answer is published.
- A service-area claim the tool contradicted, or one naming a ZIP the tool was
  never asked about, is still refused.
- Tool results do not become general-purpose grounding for other claim kinds.

### Resolution

Resolved in two passes. The first gave claim validation the turn's service-area
verdicts keyed by ZIP, which fixed the original true-answer refusal and was
verified live on both tenants. It left two deterministic holes, closed in the
second pass:

- **Retrieval could overrule the tool.** A claim counted as grounded when the
  tool verdict agreed **or** `_sentence_supported` matched retrieved prose, so a
  matching passage could publish a claim the tool had answered `served: false`,
  or vouch for a ZIP the tool was never asked about. A sentence naming a ZIP is
  now decided by the tool verdict alone; only a sentence naming no ZIP ("we
  serve your area") falls back to evidence, because the tool decided nothing
  about it. Every existing test used empty `evidence_texts`, which is why the
  fallback survived the first pass — the new tests supply contradicting prose.
- **Affirmative idioms read as refusals.** The negation scan matched the bare
  `no` in "No problem, we serve ZIP 97205" and refused a true answer. A closed
  set of affirmative idioms is removed before the scan, so a match of the
  negation pattern keeps exactly one meaning.

Regression coverage is in `packages/core/tests/test_claims.py`: contradictory
tool-plus-passage evidence, an unasked ZIP with supporting prose, an affirmative
idiom over a `served: true` verdict, and an idiom that must not mask the real
negation that follows it.

**The first pass re-opened BUG-004**, which the over-refusal had been hiding: once
the answer became publishable, the model cited the financing document's
lead-capture paragraph beside "yes, we serve 97205", because that paragraph
mentions "ZIP code". Citation validation only ever checked that a cited source
was in the prompt context, never that it supported anything.

Relevance scoring cannot separate these. Measured over live turns, the bad
pairing scores **0.40** against the answer's content words while citations that
are genuinely earned score **0.38** and **0.14** — a threshold that drops the
first drops real ones too. The usable distinction for BUG-004 is structural:
when every
sensitive claim in an answer was decided by a tool verdict, no document earned
a citation. An answer that also makes a claim a passage supported keeps its
citations, so an ordinary grounded answer is untouched.

### BUG-004 versus BUG-020

These are adjacent regressions, not duplicate defect IDs:

| Defect | Contract | Current state |
|---|---|---|
| BUG-020 | Decide whether a service-area claim is true enough to publish. | **Resolved:** the tool's verdict on a named ZIP is authoritative in both directions. |
| BUG-004 | Decide whether a published answer earned each document citation. | **Resolved:** an answer resting only on tool verdicts publishes no document citations. |

BUG-020's over-refusal temporarily masked BUG-004 because no service-area answer
reached publication. Fixing that symptom exposed BUG-004.

The two fixes also had to be closed in this order. `answer_rests_only_on_tool_verdicts`
asks whether the tool decided every sensitive claim, using the strict verdict
check; while validation still had its prose fallback, a claim published through
that fallback answered "no" and kept its document citations. So BUG-020's
fallback was simultaneously the last route back to BUG-004's exact failure —
a service-area answer the tool never authorized, published with a citation that
did not support it. Closing the fallback removed both.

## Defects found by the 2026-08-17 repository review — all resolved

### BUG-021 — High: direct actions bypass the visitor credential — **Resolved**

`POST /api/book` and `POST /api/leads` accepted `tenant_id` and `session_id`
from the request body and did not depend on `VisitorIdentity`, contradicting the
SEC-002 boundary every chat, consent, feedback, and citation route already
honoured. Booking also checked `find_replay(tenant_id, idempotency_key)` before
any ownership check, so knowledge of a tenant/key pair returned the original
booking response without proving ownership.

**Resolved by retiring both routes** rather than binding them. Nothing called
them: the shipped widget's whole surface is `/api/tenants`, `/api/chat`, and
`/api/chat/*`, and both actions already commit through the graph under the
signed credential. Binding them would have kept a second ingress to the same
effects, permanently needing the same proofs as the first. Removing them means
there is no route that can accept body identity, which is a stronger guarantee
than a test that no route does.

Removed: `routers/bookings.py`, `routers/leads.py`, their `BookingRequest`,
`LeadRequest`, `BookingResponse`, and `LeadResponse` schemas, and their gateway
locations. `tests/test_web_gateway.py` pins the visitor path set against the
API's own routers and `services/api/tests/test_openapi_contract.py` pins the
published operations, so neither can return unnoticed.

The route tests moved to the layer that still owns the behaviour: confirmation
echo and reference uniqueness to `services/api/tests/test_actions.py`, the
availability/booking agreement to `services/api/tests/test_tenants.py`, and the
problem-details mapping to a contact-bearing admin route plus a direct assertion
on `problem_response`. The privacy and repository integration suites now plant
their subjects through the graph, so what they assert about is what a real
conversation writes.

### BUG-022 — High: cross-origin action preflights omit required headers — **Resolved**

The nginx preflight for `/api/chat/consent` allowed only `Content-Type`, but the
request carries `X-Visitor-Credential`, so a real customer-site embed could not
grant consent. A same-origin demo never shows this: the preflight only runs
cross-origin.

Resolved: the consent location allows the credential header, and the two
`/api/book` and `/api/leads` locations that needed `Idempotency-Key` are gone
with BUG-021. FastAPI's CORS list already carried exactly `Content-Type` and the
credential header, so nothing was needed there once the direct actions left.

`tests/test_web_gateway.py` now holds a per-route table of the headers each
public route's own frontend request sends, asserts every location's `OPTIONS`
answer matches it exactly, and requires that table to name the same routes as
the proxy allowlist. Checked per route deliberately: the defect was one location
out of eight, and an aggregate union would have passed while consent stayed
broken.

### BUG-023 — High: displayed consent copy is not the recorded consent copy — **Resolved**

`TenantPolicy.public_view()` carried `contact_consent_statement`, but
`TenantSummary` omitted it. The widget instead called its own
`consentStatement(tenantName)`. The default strings happened to match, so a
tenant override was silently dropped and the UI could display one statement
while the server recorded another.

Resolved: `TenantSummary` carries the field, the widget renders
`config.contactConsentStatement` verbatim, and the widget's local
`consentStatement()` was deleted rather than left as a second source. The
frontend fixture now uses one override constant for both `GET /api/tenants` and
the `POST /api/chat/consent` echo, which is faithful — on the server both read
`TenantPolicy.consent_statement()` — so a widget that rebuilds the copy renders
the default and fails.

Guards: `packages/core/tests/test_tenant.py` (override and composed-default
projection), `services/api/tests/test_tenants.py` (the field is in the response
the widget's `normalizeTenant` reads), `frontend/tests/privacy.test.tsx`
(rendered label equals the served string, and equals the recorded grant).

### BUG-024 — Medium: direct lead capture is not idempotent — **Resolved**

`POST /api/leads` called `LeadStore.record` directly and accepted no idempotency
key, so a retry could create duplicate callbacks, while the graph path committed
through `RecordedLeadService` and a durable idempotency store.

Resolved with BUG-021: the non-idempotent ingress went away with the route. The
one remaining lead path is `RecordedLeadService`, whose replay contract
`services/api/tests/test_actions.py` already covers, and
`tests/repositories/test_postgres_repositories.py` now proves the production
composition persists a lead through that path against real PostgreSQL.

### BUG-025 — Medium: admin polling can publish stale tenant data — **Resolved**

`useAdminConsole.refresh` read tenant/selection refs, awaited the list, read the
selection again, then published results without a request generation or abort.
Tenant switches, manual selections, interval ticks, and post-send refreshes can
overlap, so a slower earlier response could overwrite the newer tenant's
sessions or selected transcript — an operator reading one conversation under
another's heading.

Resolved: every read claims a generation before its first await and publishes
only while it is still the newest. `select` shares the same counter as
`refresh`, because the two race each other as readily as either races itself.

`frontend/tests/admin.test.tsx` covers both shapes with hand-released promises
rather than real timers: a superseded tenant queue answering last, and a
superseded transcript answering after a newer selection. Both were confirmed to
fail without the guard.

## BUG-026 — Medium: a model's own disclaimer is refused as an unsupported claim

Found by the live harness run on 2026-08-17, after the BUG-020 through BUG-025
fixes were deployed.

### Impact

`case-1-grounded` refused on `apex` while passing on `clearview` with the same
query. The answer was correct and grounded — two evidence chunks were admitted —
but it closed with a hedge, and the hedge is what refused it. The visitor sees
the server-written refusal instead of a good financing answer, and the case
looks like a retrieval failure in the harness output when retrieval worked.

### Observed evidence

Turn `5eb54c51-8978-4554-a67b-53d04a2bd2a8` (`apex`, "What financing options are
available for a major HVAC replacement?"):

- `retrieval.evidence`: 2 chunks admitted
- `verdicts.claims_invalid`: one `coverage` claim — *"Our team can explain the
  available options and help you start an application, though we cannot
  guarantee approval."*
- `verdicts.citations`: `[]`, and the answer was not published

### Working theory

`guarantee` is in `_SENSITIVE_KEYWORDS`, so any sentence containing it becomes a
COVERAGE claim requiring 70% token overlap with one admitted passage. The
sentence here is the model declining to promise something — the safest thing it
could have said — and it is mostly the model's own connective prose, so it
cannot clear the threshold against any passage.

The keyword set cannot distinguish "we guarantee X" from "we cannot guarantee
X". The first is a business commitment that must be evidenced; the second is a
disclaimer that commits to nothing and needs no support. This is the same shape
as BUG-020 — a validator refusing a true, safe answer — but in the COVERAGE
family, where BUG-020's fix does not reach.

### Acceptance criteria

- A sentence that declines to promise a covered/guaranteed/warranted outcome is
  not treated as asserting one, and does not by itself refuse an answer.
- An actual coverage assertion is still refused without supporting evidence.
- The distinction is deterministic and tested in both polarities, including
  "we guarantee approval" against the same evidence.

### Reproduction is output-dependent, not flaky

A second run of the same case on the same revision passed with two citations,
because that answer did not include the hedge. The defect is deterministic given
the recorded output — `sensitive_claims` on that sentence returns a COVERAGE
claim every time — and intermittent only in whether the model writes such a
sentence. Reproduce from the recorded turn, not by re-running the harness.

### Note on scope

The negation machinery this needs already exists — `_NEGATION_RE` and
`_AFFIRMATIVE_IDIOM_RE` were built for service-area polarity in BUG-020 — but
polarity means something different here. For service area, a negated claim is
still a claim and must match the tool's verdict. For coverage, a negated claim
is usually not a claim at all. Do not reuse the service-area rule directly.

## Defects found by the 2026-08-18 demo walkthrough

Driven through Chrome against the local MicroK8s cluster carrying `8b00db8`:
both tenants, the visitor widget, and every admin tab. One Apex session
(`a9cc827d-f743-48be-b290-6d1f553a31da`) carried the grounded-answer,
service-area, lead-capture, and escalation beats and is the evidence for
BUG-027 through BUG-030.

## BUG-027 — High: a turn record reports tool calls and effects the turn never made

### Impact

The turn record is the demo's third claim — "every answer is reconstructible
from its turn record". It is not, for tools. Opening the escalation turn in the
explorer shows an executed graph of two nodes (`route` → `escalate`, 21 ms)
beside a tool table listing `check_service_area` and `create_lead`, and a
committed-effects list crediting that turn with the lead the *previous* turn
created. Two panels of the same record contradict each other, and the one an
operator would read as "what this turn did" is the wrong one.

### Observed evidence

`turn_records` for the session above, `content->'tools'`:

| Turn | Outcome | `tool_calls` recorded | Actually called |
|---|---|---|---|
| 2 | answered | `check_service_area` | `check_service_area` |
| 3 | answered | `check_service_area` (same `call_id`) | none |
| 4 | answered | `check_service_area` (same `call_id`) | none |
| 5 | answered | `check_service_area`, `create_lead` | `create_lead` |
| 6 | escalated | `check_service_area`, `create_lead` | none |

Turn 6's `committed` holds both `LD-B6E217C39B3E4D9287749D19058C261E` (turn 5)
and `HO-1195FABC8A6A43AC9F7E237CEA7EE5CE` (turn 6), each with `replayed: false`.

### Cause

`_tools_section` in `packages/orchestration/src/tenantchat/orchestration/trace.py`
walks the whole conversation `history` and the whole accumulated
`state["committed"]`. Neither is narrowed to the turn being recorded, while
`_executed_graph_section` is — which is exactly why the two disagree.

### Acceptance criteria

- A turn record's `tool_calls`, `tool_results`, and `committed` contain only
  what that turn produced.
- A turn that called no tool records none, whatever earlier turns did.
- A committed effect appears on exactly one turn record.
- Covered by a multi-turn test where an early tool call must not reappear.

### Note on scope

`nodes.py` already has `_current_turn(transcript)` — "the entries written since
the visitor's most recent message" — used by `_confirmed_service_areas` for this
same reason. The scoping rule exists; the trace assembler does not apply it.

## BUG-028 — Medium: the session detail's Bookings, Lead info, and Tool calls cards can never fill

### Impact

A dispatcher opening the conversation that captured a lead sees "No captured
leads for this chat yet." The lead exists, on that exact session. The operator
has no in-console path from a conversation to what it produced.

### Observed evidence

Session `a9cc827d-f743-48be-b290-6d1f553a31da` shows all three cards empty while
`leads` holds `b6e217c3-9b3e-4d92-8774-9d19058c261e` with that
`chat_session_id`, and the turn record holds two tool calls.

### Cause

`SessionDetail.tsx` renders `session.leads`, `session.bookings`, and
`session.toolEvents`. `GET /api/admin/chats/{session_id}` returns only `session`
and `messages`; `adminApi.session()` maps only those. Nothing in the application
or its tests ever assigns the three fields — they are optional in `types.ts` and
permanently `undefined`.

### Acceptance criteria

- The session detail endpoint returns the session's leads, bookings, and tool
  events, or the three cards are removed.
- A session with a committed lead renders it, asserted against a session whose
  lead was created through the graph.

## BUG-029 — Medium: the Leads and Messages tiles always read zero

### Impact

The chat queue's summary bar reported `LEADS 0` and `MESSAGES 0` against 49 live
chats, a visible lead, and 481 rows in `messages`. Four of six tiles are
trustworthy and two are not, which is worse than omitting them.

### Cause

`StatBar` sums `row.leadCount ?? 0` and `row.messageCount ?? 0` over session
summaries. `sessionSummaryFromWire` never sets either field. Only the *detail*
mapping sets `messageCount`, and the bar is not built from details.

### Acceptance criteria

- Both tiles reflect the tenant's real counts, or are removed.
- A test fails if a tile's source field stops being populated.

## BUG-030 — Low: stripped citation markers leave a space before the punctuation

### Impact

Published answers read "...including major system replacements ." Visible in the
widget and in the admin transcript, on exactly the grounded answers the demo
opens with.

### Cause

`strip_citation_markers` substitutes `[evidence:...]` with the empty string and
only `.strip()`s the ends, so an interior " [evidence:x]." becomes " .".

### Acceptance criteria

- Removing a marker leaves no doubled or orphaned whitespace, at any position.
- Tested for a marker before punctuation, mid-sentence, and at the end.

## BUG-031 — Low: the generated consent sentence has no conjunction

### Impact

The visitor ticks: "I agree that Apex Home Services may store the name, address,
and contact details I enter here in order to arrange the appointment, follow up
about the work." This is the sentence the consent grant stores verbatim.

### Cause

`consent_statement` in `packages/core/src/tenantchat/core/privacy.py` builds the
purpose list with `", ".join(ordered)`, which is correct for one clause and
ungrammatical for two.

### Acceptance criteria

- Two purposes join with "and"; three or more use a serial list.
- One purpose is unchanged.

## BUG-032 — Low: a cited source's effective date is rendered in UTC

### Impact

A source published at 18:30 PDT displays "Revision 1 · effective 2026-08-18" to
a Pacific visitor on 2026-08-17 — a citation dated tomorrow, in the panel whose
whole job is provenance.

### Cause

`effectiveDate` in `frontend/src/widget/components/SourceViewer.tsx` formats with
`date.toISOString().slice(0, 10)`, which is the UTC calendar date regardless of
the viewer's zone.

### Acceptance criteria

- The date shown is the one in effect for the viewer, or is explicitly labelled
  UTC.
- Tested from a zone where the local and UTC dates differ.

## BUG-033 — Low: the audit console cannot name the permission for a third of its actions

### Impact

The audit trail's "permission that authorized it" column prints the bare action
name for `handoff.accepted`, `handoff.released`, `handoff.resolved`,
`knowledge.version_approved`, `knowledge.version_published`,
`knowledge.version_reindexed`, `knowledge.version_expired`,
`trace.replay_trials`, `trace.replay_retrieval`, and `trace.replay_template` —
including the handoff actions a governance walkthrough would demonstrate.

### Cause

`_AUTHORIZING_PERMISSION` in `services/api/src/tenantchat/api/access.py` is a
narrower set than the action vocabulary the filter offers, and
`authorizing_permission` falls back to the action name. BUG-018 gave the filter
one source of truth; the permission map was not brought along.

### Acceptance criteria

- Every action in the audit vocabulary has an authorizing-permission entry.
- A test fails when an action is added without one.

## How implementation agents should use this document

1. Reproduce the assigned defect on the current deployment and capture the new session, turn, and trace identifiers.
2. Prove or disprove the working theory before implementing a fix.
3. Preserve the tenant-isolation and authorization behavior listed in [Verified security invariants](#verified-security-invariants).
4. Prefer one focused change and one focused pull request per numbered defect.
5. Add a deterministic regression test at the lowest useful layer plus an integration or browser test when the defect crosses boundaries.
6. Run the targeted test suites and the repository's standard checks before handoff.
7. Do not make model prose the source of truth for committed actions. Success language should be derived from committed server-side effects.

## What each defect was, as first filed (historical)

Severity, area, and confidence at filing time. Since the resolved BUG-001
through BUG-019 narratives were pruned, this table is the surviving one-line
record of what each of those defects actually was — the fix and its guard are in
[Current status](#current-status--2026-08-17), which is what decides whether
anything remains open.

| ID | Severity | Area | Summary | Confidence |
|---|---|---|---|---|
| BUG-001 | Critical | Handoff/session identity | Handoff is attached to a shadow session, so accepted conversations continue invoking AI | Confirmed; root cause strongly localized |
| BUG-002 | High | Booking/routing | Booking loses workflow ownership when a ZIP is supplied and eventually escalates | Confirmed |
| BUG-003 | High | Lead capture | Assistant promises a callback without committing a lead | Confirmed |
| BUG-004 | High | Retrieval/citations | Service-area answer cites unrelated financing text and passes validation | Confirmed |
| BUG-005 | High | Knowledge integrity | Integrity check reports zero indexed chunks for content that live retrieval can fetch | Confirmed symptom; cause unproven |
| BUG-006 | High | Knowledge data | Clearview contains and retrieves a Northline policy | Confirmed data contamination, not an auth bypass |
| BUG-007 | Medium | Privacy/consent | Disclosure says PII is sent only after a form and consent, but free-text chat sends it immediately | Confirmed contract mismatch |
| BUG-008 | Medium | Tenant switching | UI clears transcript while retaining the server session and hidden model context | Confirmed |
| BUG-009 | Medium | Grounding | The bot refuses to answer business hours even though trusted tenant configuration contains them | Confirmed |
| BUG-010 | Medium | Timeouts/replay | A live turn exceeded the configured model timeout and safe replay failed without a useful reason | Confirmed symptom; cause unproven |
| BUG-011 | Medium | Grafana | Repository dashboards were not provisioned in the deployed Grafana | Confirmed |
| BUG-012 | Medium | OTEL/APM | Tempo has traces, but standard APM panels and Phoenix/MLflow/Pyroscope views are incomplete or low-value | Confirmed gaps; some may be intentional |
| BUG-013 | Low | Deployment compatibility | An already-open widget sent the old session payload to the new API and received 422 until reload | Confirmed once |
| BUG-014 | Low | Kubernetes hygiene | A stale live `chat-backend` Service targets port 8000 while the current pod listens on 8004 | Confirmed in cluster; source manifest no longer creates it |
| BUG-015 | Low | Widget UX | Availability choices render as a dense run-on list | Confirmed visually |
| BUG-016 | High | Service area/session | Service-area turns end in a delayed session-ownership 404 | Confirmed across tenants and sessions |
| BUG-017 | Medium | Admin transcript | Handoff system notices are attributed to Visitor | Confirmed |
| BUG-018 | Low | Admin audit | Action filter omits event types present in the table | Confirmed |
| BUG-019 | Medium | Policy/config | Apex offers booking even though booking is disabled | Confirmed after injection refusal |
| BUG-020 | Medium | Claim validation | A service-area claim its own tool confirmed is refused as unsupported | Confirmed on both tenants |

---

## Open defect narratives

Full reproductions for the defects that are still open. The resolved
BUG-001 through BUG-019 narratives were removed once each fix had a named
guard; the status table above records what guards them now.

## BUG-010 — Medium: turn and safe replay are not bounded by a useful end-to-end deadline

### Impact

The visitor can wait far beyond the configured model timeout, and admin safe replay fails after a long wait with only a generic message. Operators cannot distinguish provider unavailability, timeout, or replay reconstruction problems.

### Reproduction

1. Ask Clearview for a pricing answer that exercises retrieval/model generation.
2. Measure wall-clock time until the response.
3. In Trace Explorer, run safe replay for the slow turn.
4. Measure replay time and inspect the surfaced error/status.

### Observed evidence

- One Clearview pricing request took approximately `153 seconds`.
- `services/api/src/tenantchat/api/settings.py` defaults `llm_timeout_seconds` to `120`.
- Safe replay ran for a long time and then displayed: `The replay did not run. The model may be unavailable.`
- `services/api/src/tenantchat/api/replay.py` calls `model.complete(...)` directly in `_complete()` and does not itself apply an end-to-end timeout.

### Working theories to test

- The configured timeout applies per provider attempt rather than to the whole graph/turn.
- Retry/fallback behavior resets the timer.
- Replay relies entirely on the adapter timeout and loses the typed failure reason at the API/UI boundary.
- Retrieval, graph, or queue time sits outside the model timeout.

### Fix hints

- Define separate provider-attempt and end-to-end turn/replay deadlines.
- Carry typed timeout/unavailable/reconstruction errors through the trace API and admin UI.
- Record elapsed time and terminal reason even for failed replay.
- Keep safe replay tool-free and side-effect-free.

### Acceptance criteria

- Live turn and safe replay stop within documented bounds, including retries/fallbacks.
- Timeout and provider-unavailable failures are distinguishable in API responses, traces, metrics, and UI.
- A failed replay records no domain action and does not alter the original turn.

---

## BUG-012 — Medium: observability stack receives data but key APM/AI views are incomplete

### Impact

Raw traces and metrics exist, but several operator workflows are not actionable: generic APM panels have no backend data, Phoenix is dominated by low-level spans, MLflow does not show a current tracing experiment, and Pyroscope does not profile the application.

### Observed evidence

#### Tempo and Loki

- Handoff trace `75db100e8bcf35811481829ca4b4ebc8`: HTTP 200, 352 spans.
- Callback trace `d5282f2dcaaf8682cc521c2811a16359`: HTTP 200, 122 spans.
- A sample of 100 recent `chat-backend` Loki logs contained 18 entries matching the handoff trace.
- Searching Loki for the test email produced zero matches, which is a positive redaction result.

#### Prometheus/Grafana

- Observed outcomes: `answered=7`, `abstained=3`, `handed_off=1`.
- Observed tool calls: `check_service_area=5`, `get_availability=1`.
- No standard `http_server_*{service_name="chat-backend"}` series was found.
- Generic Lightweight APM allowed selecting `chat-backend`, but panels showed no data.
- Tenant Chat metrics intentionally use a closed label set without tenant ID in `packages/core/src/tenantchat/core/metrics.py`; per-tenant Grafana breakdown is therefore unavailable.

#### Phoenix

- Phoenix login worked.
- The default project held roughly 240,626 traces but only 3 sessions.
- Views were dominated by low-level database/health spans named `WITH`, `connect`, and similar operations.
- Span kind was often unknown; token and cost fields were zero.

#### MLflow and Pyroscope

- MLflow showed only an older `Default` experiment, with no clearly current Tenant Chat tracing project.
- Pyroscope showed `observability/alloy` and `observability/pyroscope`, not the application.

### Interpretation

Tempo ingestion itself works. The remaining issues may be a combination of semantic-convention/resource attributes, parent/session grouping, collector/export configuration, missing metrics instrumentation, and components that were never enabled. The missing tenant metric label may be an intentional privacy/cardinality tradeoff; treat it as a product decision, not an automatic bug.

Relevant code/config:

- `packages/orchestration/src/tenantchat/orchestration/otel.py` — custom chat model span recording
- `packages/core/src/tenantchat/core/metrics.py` — metric label policy
- OpenTelemetry collector/operator configuration under `k8s/`
- Grafana, Phoenix, MLflow, and Pyroscope deployment/provisioning configuration

### Acceptance criteria

- Document which backends are supported for the current release and what each is expected to show.
- Chat/model spans have useful names, service/resource attributes, parentage, status, and AI semantic attributes without leaking prompt/PII content.
- A backend request can be followed coherently from HTTP entry through orchestration/model/tool spans in Tempo and the supported AI trace UI.
- Generic APM either receives the expected HTTP metrics or is removed from the operator path.
- If MLflow/Pyroscope are in scope, the application emits current data to them; otherwise the UI/runbook clearly says they are not enabled.
- Any per-tenant metrics design uses a bounded, privacy-reviewed approach rather than adding arbitrary tenant labels.

---

## BUG-013 — Low: old widget bundle is incompatible with the new session API until reload

### Impact

A visitor with an already-open page during deployment can receive a 422 from the new backend until the page is reloaded.

### Observed evidence

- A browser tab opened before the deployment sent the prior request shape to `POST /api/chat/session`.
- The new API returned HTTP 422.
- Reloading fetched the current bundle and fixed the problem.
- A current request body containing `tenant_id` returned HTTP 201.

### Fix options

- Maintain a short compatibility window for the previous request schema; or
- Add asset versioning/cache-busting plus a client/server version handshake that prompts or performs a safe reload before a chat request.

### Acceptance criteria

- A page open across a deployment either keeps working or receives a clear automatic/manual refresh path instead of an unexplained validation error.
- Contract coverage exercises an old-client/new-server combination for one supported deployment window.

### The mirror case, introduced by BUG-023's fix

`chat-backend` and `web` are separate images rolled independently from
`k8s/app.yaml`, so a rolling update can briefly serve the **new** widget against
the **old** API. The new widget renders `contact_consent_statement` from
`GET /api/tenants` verbatim; an API that predates that field returns nothing for
it and the consent label renders empty — a privacy regression, not a 422.

A local fallback would fix the window and reintroduce exactly the second source
of consent copy BUG-023 exists to forbid, so the deployment order is the control
instead: **roll `chat-backend` before `web`**. Whatever version handshake closes
this defect must cover the new-client/old-server direction too.

---

## Verify before fixing

These observations were not proven enough to assign as independent defects. Confirm them before changing behavior.

### Admin projection may omit messages and tools

- One admin queue view reported `Messages 0` even though a transcript existed.
- The same area said `No tools called` while the associated trace included `check_service_area`.
- This may be explained by BUG-001's shadow session and BUG-003's stale tool provenance rather than a separate projection bug.

### Model/tool history may be mislabeled as current-turn activity

- The callback trace exposed a `check_service_area(zip=98103)` call from earlier context even though the callback message did not require it.
- Determine whether the tool actually reran or whether accumulated state was serialized into the current turn record.

### Tenant metrics

- Grafana cannot break the custom metrics down by tenant because the label set intentionally excludes tenant IDs.
- Do not add an unbounded tenant label without privacy and cardinality review. A bounded tenant class, exemplar-to-trace workflow, or admin/API aggregation may be safer.

---

## Verified security invariants

These checks passed on the credentialed chat/admin surfaces and must remain
passing after fixes:

- Apex citation fetched with the owning Apex visitor credential: HTTP 200.
- The same Apex citation fetched with a Clearview visitor credential: HTTP 404.
- Northline-branded chunk stored under Clearview: Clearview credential HTTP 200; Apex credential HTTP 404.
- Forging `tenant_id=clearview` in an Apex **credentialed chat-route** request
  body: HTTP 422. This now holds for every visitor route, because the two that
  took body identity were retired (BUG-021).
- Admin headers sent directly without the authenticated gateway/session: HTTP 401.
- Authenticated viewer without tenant membership: HTTP 403.
- Platform-admin request for a nonexistent tenant: HTTP 404.
- Test email/PII search in sampled Loki backend logs: zero matches.

Expected security behavior:

- Authorization must derive tenant/session identity from the signed
  server-issued credential or authenticated admin membership, never from a
  request-body tenant claim. No visitor route violates this: BUG-021 retired
  the two that did.
- Cross-tenant resources remain indistinguishable from missing resources where the API currently returns 404.
- Fixes must not expose raw prompt, PII, credentials, or full tenant identifiers in telemetry.

## Test artifacts and environment state

Exploratory testing intentionally changed demo data. Agents should account for these artifacts instead of treating them as organic customer records:

- Apex staff message: `QA operator test: verifying staff reply delivery.`
- Review `efa888f5-1f35-4d62-b475-dd843e7abde0` is `Awaiting fix` / `Amended`.
- Integrity findings were persisted for Apex and Clearview.
- Handoff `HO-BC3D91E616D34B928F1AF13320B3007E` was resolved; the queue was clean at test completion.
- Handoff `HO-6F43D410E8FE46F3BAEEAAD019FE8D6D` was accepted, received one QA staff reply, and was resolved; the queue was clean at test completion.
- Clearview booking `BK-48AB2F4EE2414B60AB2D1CD6C4F0D24E` was committed once using fake QA contact/address data.
- Several QA sessions were created by isolation probes.
- Demo chat data contains `qa-tester@example.invalid`, `qa-tester-clearview@example.invalid`, `exploratory-qa@example.invalid`, `booking-qa@example.invalid`, and `480 Test Avenue`.
- Browser exploration did not modify source. The dashboard deployment scripts and their focused regression test were changed separately to repair BUG-011.
- A temporary Kibana port-forward was stopped.
- Destructive knowledge actions and privacy-delete operations were intentionally not executed.

## Implementation order

`BACKLOG.md`'s **Current dispatch sequence** owns the order, because sequencing
is a gate decision and a second ordered list here would drift from it. This
document owns which defects are open and what each one is.

## Definition of done for each assigned bug

- Original reproduction fails before the fix and passes afterward.
- Root cause is documented with evidence, not only inferred from the symptom.
- Regression coverage includes tenant boundaries where relevant.
- No credentials or PII are added to fixtures/logs.
- Targeted tests and repository checks pass.
- Deployment or data migrations are idempotent and have rollback/verification instructions.
- The agent's handoff includes changed files, tests run, remaining risks, and exact verification steps.
