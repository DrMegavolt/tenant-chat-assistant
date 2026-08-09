# Exploratory Testing Defect Dossier

> Agent handoff document. Reproduce each issue against the current deployment before changing code. Treat **Observed evidence** as fact and **Working theory** as a hypothesis that still needs proof.

## Test context

- Test date: **2026-08-09**
- Deployment: local MicroK8s, latest deployed application version
- Visitor app: `http://192.168.1.180`
- Admin console: `https://chat.192.168.1.170.nip.io/admin/`
- Tenants exercised: **Apex** (`apex`) and **Clearview** (`clearview`)
- Admin identity observed: `tenantchat-operator`
- The admin endpoint uses a demo TLS certificate. Bypass the certificate warning only for this local demo environment.
- Do not put passwords, bearer credentials, cookies, or Kubernetes Secret values in source, logs, screenshots, test fixtures, or commits. Obtain demo credentials through the normal local operator/Secret workflow.

The cluster was healthy during testing. Application pods had zero restarts and there were no current warning events. Elasticsearch and PostgreSQL showed five older restarts each, but no active failure was observed.

## How implementation agents should use this document

1. Reproduce the assigned defect on the current deployment and capture the new session, turn, and trace identifiers.
2. Prove or disprove the working theory before implementing a fix.
3. Preserve the tenant-isolation and authorization behavior listed in [Verified security invariants](#verified-security-invariants).
4. Prefer one focused change and one focused pull request per numbered defect.
5. Add a deterministic regression test at the lowest useful layer plus an integration or browser test when the defect crosses boundaries.
6. Run the targeted test suites and the repository's standard checks before handoff.
7. Do not make model prose the source of truth for committed actions. Success language should be derived from committed server-side effects.

## Priority summary

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

---

## BUG-001 — Critical: handoff binds to a shadow session

### Impact

An operator can accept a handoff while the visitor's real conversation continues invoking the AI. The admin queue opens a different session and cannot see the original transcript. This defeats the core human-handoff safety contract and can produce simultaneous AI and staff handling.

### Reproduction

1. Open a Clearview visitor conversation.
2. Ask for a human until a handoff is requested.
3. In the admin queue, accept the request.
4. From the visitor, send `Are you there?` and then `Testing while assigned.`
5. Observe that the AI still processes the visitor turns.
6. Compare the visitor session ID with the session ID on the handoff record and the session shown in the admin queue.

### Observed evidence

- Real Clearview visitor/trace session: `81ed60de-74b0-4dd8-a747-19c813c819c2`
- Handoff: `HO-BC3D91E616D34B928F1AF13320B3007E`
- Handoff session: `3e3b49cd-67ef-425e-903e-974bba130f2c`
- Trace: `75db100e8bcf35811481829ca4b4ebc8`
- Trace outcome: `escalated`
- Graph duration: approximately `61,890 ms`, with 9 nodes and 9 edges
- The post-acceptance visitor messages invoked the graph and returned abstentions.
- The pause check queried the real visitor session, found no active handoff for it, and therefore did not pause.
- The admin queue selected the shadow session and showed handoff status notices rather than the original conversation.
- The handoff was accepted, released, accepted again, and resolved during testing. The queue was clean at the end.

### Working theory

`_action_session()` still treats its third argument as a legacy client correlation label. Callers now pass the SEC-002 server-issued session UUID. The repository inserts a new `chat_sessions.id` and stores the real session UUID in `client_correlation_id`, creating a shadow row. Chat pause logic then looks up the handoff by the real row ID and misses it.

Relevant code:

- `services/api/src/tenantchat/api/persistence/repositories.py`
  - `_action_session(connection, tenant_id, client_correlation_id)`
  - Its docstring says this is temporary “until SEC-002 replaces it,” but SEC-002 credentials are already in use.
- `services/api/src/tenantchat/api/actions.py`
  - `RecordedHandoffService.request(...)` passes a session ID into the persistence layer.
- `packages/orchestration/src/tenantchat/orchestration/nodes.py`
  - `_handoff` and `escalate` pass `state["session_id"]`.
- `services/api/src/tenantchat/api/routers/chat.py`
  - `send_message` pauses by calling `handoffs.for_session(tenant_id, str(session_id))` with the authoritative credential session.

### Fix hints

- Make the session identity contract explicit. A server-issued UUID should resolve the existing row by `(tenant_id, chat_sessions.id)`, not be inserted or resolved through `client_correlation_id`.
- Keep any legacy write-only correlation behavior behind a separately named method/parameter if it is still required.
- Fail closed if a handoff action references a nonexistent session. Do not silently create a new conversation row.
- Backfill or clean shadow rows only after identifying whether any non-demo data depends on them.

### Acceptance criteria

- A newly requested handoff's `session_id` exactly equals the session named by the visitor credential.
- Requested or assigned handoffs pause AI execution for subsequent visitor messages.
- The admin queue shows the complete visitor conversation for the handed-off session.
- Accept, release, re-accept, resolve, and visitor polling all operate on one session row.
- Tenant-leading lookups remain mandatory.

### Required regression coverage

- Repository test: an existing authoritative session is reused and no shadow row is inserted.
- API/integration test: request a handoff, accept it, then POST another visitor message and assert that the model is not called.
- Admin projection test: handoff session, displayed transcript, and visitor credential all resolve to the same server session.

---

## BUG-002 — High: booking workflow is displaced by service-area routing

### Impact

A user who supplies the requested booking details can be routed away from booking merely because the message contains a ZIP code. The system repeats service-area checks, fails the turn, and escalates instead of advancing the booking.

### Reproduction

1. Start a new Clearview conversation.
2. Send `Book HVAC`.
3. After availability is shown, send:

   ```text
   Mon Aug 10 at 9:00 AM. Name: QA Tester. Address: 480 Test Avenue, Portland, OR 97205. Email: qa-tester-clearview@example.invalid.
   ```

4. Inspect the trace, route decision, tool calls, and committed actions.

### Observed evidence

- `get_availability` succeeded and returned 20 slots.
- The details message was routed to `service_area`, apparently because it contained ZIP `97205`.
- `check_service_area` was called four times.
- The last service-area call ended as `turn_abandoned`.
- `book_appointment` was never called.
- The turn escalated instead of proposing or confirming a booking.
- Trace: `75db100e8bcf35811481829ca4b4ebc8`
- Approximate model-call durations included 46.8 s, 1.3 s, 12.4 s, and 1.3 s.
- The only committed domain effect was the handoff.

### Working theory

The router scores the current message independently enough that a ZIP code can switch an active booking workflow to service-area intent. The current workflow is loaded, but a different chosen route suspends it. In this context the ZIP is a booking field, not a new user intent.

Relevant code:

- `packages/core/src/tenantchat/core/routing.py` — ZIP-based service-area scoring and booking scores
- `packages/orchestration/src/tenantchat/orchestration/nodes.py` — `route()` and workflow suspension behavior
- `packages/orchestration/src/tenantchat/orchestration/agents.py` — booking field extraction and requirements

### Fix hints

- Give an active workflow ownership of messages that supply its outstanding fields unless the user clearly changes or cancels intent.
- Treat ZIP/address tokens as entity values during an active booking, not as automatic evidence of a new service-area request.
- If serviceability must be checked inside booking, make it an explicit booking substep rather than changing the top-level workflow.
- Cap retries and return a deterministic, user-actionable error if a tool repeats without progress.

### Acceptance criteria

- After availability, the full-details message remains in the booking workflow.
- Name, address, ZIP, email, service, and selected slot are parsed into booking state.
- The system proposes a confirmation and does not commit before explicit approval.
- Explicit approval commits exactly one booking.
- An explicit intent change such as `Never mind, do you serve 97205?` can still switch workflows.

### Required regression coverage

- Router test for an active booking plus a message containing an address and ZIP.
- End-to-end booking test from availability through explicit confirmation and one committed appointment.
- Loop-protection test ensuring repeated identical tool calls cannot consume an unbounded turn.

---

## BUG-003 — High: false callback confirmation without a committed lead

### Impact

The assistant tells the visitor that a team member will call, but no lead is created. This is a silent loss of a customer request and a high-risk mismatch between user-visible language and durable state.

### Reproduction

1. In an Apex conversation with prior service context, send:

   ```text
   Please have someone call QA Tester at qa-tester@example.invalid about an electrical panel repair.
   ```

2. Inspect the answer, route decision, tool calls, and committed action list.

### Observed evidence

- Session: `9e930e8a-5e0a-46e3-af55-b32ecd8f5de0`
- Trace: `d5282f2dcaaf8682cc521c2811a16359`
- Turn: `2865248b-8b70-4c0b-8627-ec519adf02a6`
- Assistant response:

  > Thank you. Our team will contact QA Tester at qa-tester@example.invalid regarding your electrical panel repair…

- Outcome: `answered`
- Committed actions: `[]`
- No `create_lead` call occurred.
- Routing candidates tied at `booking=4` and `lead=4`; booking was selected.
- The trace exposed only a prior-context `check_service_area(zip=98103)` tool call.
- Tempo returned this exact trace with HTTP 200 and 122 spans.

### Working theory

Service and work nouns raise the booking score enough to tie an explicit callback phrase. Current workflow/context then selects booking. Separately, answer generation is allowed to promise an effect that the commit layer did not perform.

Relevant code:

- `packages/core/src/tenantchat/core/routing.py` — callback/lead and booking scoring
- `packages/orchestration/src/tenantchat/orchestration/agents.py` — lead agent contract
- `packages/orchestration/src/tenantchat/orchestration/nodes.py` — `create_lead`, action commit, and final answer assembly

### Fix hints

- Make an explicit callback/contact request win over generic service-category nouns.
- Require the needed lead fields, propose/confirm according to the product contract, and commit through `create_lead`.
- Generate success wording from the committed effect/result. If no effect committed, the assistant must ask for missing information or state that it could not submit the request.
- Keep current-turn tool calls separate from historical tool calls in trace/admin projections.

### Acceptance criteria

- The reproduction routes to lead capture.
- A successful callback promise is accompanied by exactly one committed lead.
- A failed or uncommitted lead can never produce “will contact,” “submitted,” or equivalent success language.
- Service nouns such as `electrical panel repair` do not override an explicit callback intent.

### Required regression coverage

- Routing test for `call me`/`have someone call` plus a service noun.
- Orchestration invariant test: success-language rendering requires a committed `create_lead` result.
- Trace test: current-turn tool calls are not populated by a previous turn's tool history.

---

## BUG-004 — High: irrelevant citation passes as support for a service-area claim

### Impact

The bot can make a business commitment about where service is available, cite unrelated material, and still mark the claim as supported. This creates false confidence and makes citation review misleading.

### Reproduction

1. Ask Apex: `Do you serve 98103?`
2. Open the answer citation.
3. Inspect retrieval candidates and the claim/citation verdict in the trace explorer.

### Observed evidence

- The answer said Apex serves `98103`.
- The displayed citation was **Apex financing options / Customer Guidance**, which does not establish service area.
- Turn: `5c60cf23-4aa6-4e19-a269-b3621c43c147`
- Component manifest prefix: `75ca0bdbeb95`
- Retrieval was marked insufficient.
- Top observed financing chunks scored approximately `0.09736` and `0.09664`.
- The gold case `apex-hvac-serves-98103` points to gold chunk `apex-hvac-1`, whose text does support the service-area fact.
- The verdict nevertheless recorded the claim as supported and the citation as valid.
- A thumbs-down was submitted.
- Review `efa888f5-1f35-4d62-b475-dd843e7abde0` was changed to `Awaiting fix` / `Amended`, with diagnosis `Retrieval rank` and an operator note/proposed fix.

### Working theory

Citation finalization proves only that the cited ID exists in admitted context; it does not prove semantic relevance. The deterministic sensitive-claim validator recognizes prices and words such as `coverage`, `permit`, and `insurance`, but not `serve`, `serves`, ZIP serviceability, or `service area`. An answer with no recognized sensitive claim can therefore pass automatically.

Relevant code:

- `packages/core/src/tenantchat/core/claims.py`
  - `_SENSITIVE_KEYWORDS`
  - `sensitive_claims()`
  - `validate_sensitive_claims()`
- Orchestration finalization/citation verification code that checks cited IDs against current context
- Retrieval/reranking logic and the gold-case fixtures for `apex-hvac-serves-98103`

### Fix hints

- Add service-area assertions to deterministic sensitive-claim coverage, including common forms such as `serve`, `serves`, `service area`, ZIP/postal-code availability, and geographic coverage.
- Consider sentence-to-passage entailment or high-overlap validation for every cited factual sentence, not only ID membership.
- Do not allow low-score, unrelated evidence to be converted into a definitive answer.
- Re-evaluate ranking/index content so the known service-area gold chunk is retrievable.

### Acceptance criteria

- The question returns either a properly supported answer citing the service-area source or a clear abstention.
- A financing passage cannot validate a ZIP serviceability claim.
- Trace/admin views distinguish `citation ID exists` from `citation semantically supports claim`.
- The gold service-area evaluation passes consistently.

### Required regression coverage

- Unit tests for positive and negative service-area sensitive claims.
- Finalization test with a valid-but-irrelevant citation ID.
- Retrieval/gold-case test for `apex-hvac-serves-98103`.

---

## BUG-005 — High: knowledge integrity check reports zero indexed chunks for retrievable content

### Impact

Operators are told that published knowledge is missing from the index even while production retrieval uses it. Persistent false findings make the integrity dashboard untrustworthy and can trigger unnecessary or destructive remediation.

### Reproduction

1. In Admin, run the knowledge integrity check for Apex and Clearview.
2. Record published version IDs and recorded/indexed counts.
3. In a live chat or trace replay, retrieve a chunk from each flagged version.
4. Fetch a cited chunk through the authorized citation endpoint.

### Observed evidence

- Apex financing: recorded `7`, indexed `0`; findings `Partially indexed` and `Chunk count mismatch`.
- Clearview financing: recorded `6`, indexed `0`.
- Clearview service policy: recorded `9`, indexed `0`.
- Northline policy stored under Clearview: recorded `9`, indexed `0`.
- Despite those zero counts, live chat and trace replay retrieved chunks from the flagged content.
- The authorized citation endpoint returned HTTP 200 for retrieved chunks.
- Running the check persisted the findings in the admin system.

### Working theories to test

Do not choose one without evidence:

- The count query uses a different Elasticsearch index or alias from retrieval.
- Older documents store or map `version_id` differently from the exact `term` query.
- API detector and retriever receive different deployed index-name configuration.
- The integrity route constructs an empty/fresh adapter while live retrieval uses another configured instance.

Relevant code:

- `services/api/src/tenantchat/api/index_integrity.py`
  - `IndexIntegrityDetector._published_findings()`
- `services/api/src/tenantchat/api/search.py`
  - `SearchIndex.active_chunk_count(tenant_id, version_id)`
  - Elasticsearch `_count` query over `tenant_id`, `active`, and `version_id`
- `services/api/src/tenantchat/api/routers/knowledge.py` — integrity route and finding persistence

### Fix hints

- Log or expose the resolved index/alias and normalized version ID for both retrieval and integrity count operations.
- Compare the exact Elasticsearch request and mapping against one known retrievable chunk.
- Add a real-adapter test; an in-memory search fake is unlikely to catch index, alias, or mapping drift.
- Do not delete or reindex content until the detector itself is proven correct.

### Acceptance criteria

- `active_chunk_count` matches the count of retrievable active chunks for every published version.
- Fully indexed content produces no partial-index or mismatch finding.
- Retrieval and integrity code use the same configured index/alias and tenant/version representation.
- Existing false findings can be re-evaluated and resolved without manual database edits.

### Required regression coverage

- Elasticsearch integration test that indexes a published version, retrieves it, and asserts the same per-version count.
- Configuration test asserting retrieval and integrity adapters resolve the same index name.
- Older-version fixture if the defect is a mapping/migration incompatibility.

---

## BUG-006 — High: Clearview knowledge contains Northline-branded policy

### Impact

Clearview answers can be grounded in another company's policy. Even though authorization still scopes the chunk to Clearview, this is a tenant-content integrity failure that can expose the wrong brand, coverage area, or commitments to visitors.

### Reproduction

1. Open Clearview knowledge sources in Admin.
2. Locate **Northline Service Policy**.
3. Inspect its files/chunks and a Clearview prompt or retrieval trace.
4. Attempt to fetch the same chunk with Clearview and Apex visitor credentials.

### Observed evidence

- A Clearview source titled **Northline Service Policy** includes both `clearview-service-policy.md` and `northline-service-policy.md`.
- Both are Published/Indexed.
- A Clearview assembled prompt contained a full Northline Heating & Air service-area passage alongside Clearview/financing passages.
- Example source ID: `3715f5ee-1567-524f-8c26-7941cd05b911:5bb13c2c-7976-4be7-bf1e-02ae9ae5bd04:000001`
- That chunk returned HTTP 200 with a Clearview credential and HTTP 404 with an Apex credential.

### Interpretation

This is **not evidence of a cross-tenant authorization bypass**. The Northline chunk has been assigned to Clearview, and credential scoping correctly prevents Apex from reading it. The defect is in demo seeding, source upload/upsert behavior, migration, or content governance.

### Fix hints

- Audit source creation/upsert history and demo content initialization for the source/version IDs involved.
- `scripts/seed_knowledge.py` currently appears focused on financing sources, so this may be stale data from an older demo/upload path rather than the current seed script.
- Add source-title/brand/document consistency checks before publication.
- Add a safe cleanup or migration for known contaminated demo versions.
- Consider stable source keys that cannot accidentally merge two brands during idempotent seeding.

### Acceptance criteria

- Clearview has no published or active Northline text.
- Reindexing or reseeding cannot merge documents from different brands into one source.
- Brand/source validation blocks or clearly warns on mismatched content before publication.
- Tenant authorization behavior remains unchanged: foreign credentials still receive 404.

### Required regression coverage

- Idempotent seed/upsert test using two tenants and similarly named source files.
- Publication validation test for an obvious brand mismatch.
- Post-migration retrieval test confirming contaminated chunks are inactive and absent from prompts.

---

## BUG-007 — Medium: privacy disclosure does not match free-text PII handling

### Impact

Visitors are told that name, address, and contact details are sent only after they fill in a form and agree. In reality, a visitor can type those details into the chat composer and they are sent and stored immediately. The disclosure also implies that closing the tab ends the conversation, while the server-side session persists.

### Reproduction

1. Open the widget's **Privacy and your data** panel.
2. Read the contact-data and tab/session statements.
3. Without using a structured form, type a message containing a name, address, and email.
4. Observe the network request and stored visitor message.
5. Close/reopen or switch away and inspect whether the server session still exists.

### Observed evidence

- UI copy in `frontend/src/widget/components/PrivacyDisclosure.tsx` says:
  - `Name, address, and contact details are sent only when you fill in a form and agree first.`
  - `Closing the tab ends the conversation id.`
- The free-text composer immediately submits the raw message.
- The API appends/stores the visitor message before orchestration.
- `frontend/src/widget/useConversation.ts` best-effort grants `follow_up` consent when a session is created, before a visitor performs a visible consent action.
- Booking has a later explicit confirmation step, but that does not stop contact information from already being transmitted in chat.

### Product decision required

Choose and document one contract:

- Detect/block/redact contact details in free text until explicit consent, then collect through an appropriate form; or
- Clearly disclose that anything typed into chat is transmitted and stored, and obtain any legally/product-required consent before collection.

The correct legal wording is a product/legal decision. The engineering invariant is that UI claims and actual data flow must agree.

### Fix hints

- Review `PrivacyDisclosure.tsx`, `useConversation.ts`, the composer submission path, consent endpoint semantics, and server message persistence ordering together.
- Distinguish deleting a browser credential from deleting or ending the server-side conversation.
- Do not use automatic `follow_up` consent as evidence of a user gesture unless that is explicitly the intended policy.

### Acceptance criteria

- Disclosure text accurately describes free-text message transmission, storage, browser credential lifetime, and server retention.
- Any required affirmative consent is captured before the corresponding PII is transmitted.
- The “delete from browser” control does not imply server deletion unless it actually performs it.
- Automated browser coverage proves the selected contract.

---

## BUG-008 — Medium: tenant switch hides transcript but preserves hidden context

### Impact

Switching tenants presents a fresh-looking chat while retaining the previous tenant's credential and server checkpoint. When the visitor returns, the model can continue using context the user cannot see. This is confusing and makes it difficult to understand or correct the current conversational state.

### Reproduction

1. Have a multi-turn conversation with Clearview.
2. Switch to Apex.
3. Switch back to Clearview.
4. Observe the widget transcript.
5. Send a context-dependent follow-up or inspect the server session snapshot.

### Observed evidence

- Returning to Clearview showed only the welcome message; prior visitor and assistant messages were absent.
- The same stored Clearview credential/server checkpoint was reused.
- The AI could therefore continue with hidden context.
- Staff polling later imports only staff replies, not the historical visitor/assistant transcript.
- `frontend/src/widget/WidgetSurface.tsx` renders `<ChatWidget key={tenantId}>`, forcing a remount on tenant change.
- `frontend/src/widget/ChatWidget.tsx` comments say the remount makes the switched-away conversation unreachable.
- `frontend/src/widget/useConversation.ts` initializes entries with the welcome message and filters session hydration to unseen `staff` messages.
- `frontend/src/widget/visitorData.ts` persists one credential per tenant in session storage.

### Fix options

Choose one coherent experience:

- On remount, hydrate a bounded, authorized transcript for the stored credential; or
- Explicitly terminate/forget the previous local credential and create a new server session, so the blank UI really is a blank conversation.

### Acceptance criteria

- A blank transcript never retains hidden model context.
- If the session is reused, the visitor sees its complete retained transcript in order, including visitor, assistant, and staff messages allowed by the API contract.
- If the session is reset, the old credential/checkpoint is not reused.
- Switching tenants never mixes credentials or messages across tenants.

### Required regression coverage

- Browser test: Clearview conversation → Apex → Clearview, asserting the selected hydration/reset behavior.
- Isolation assertion that tenant-specific credentials and transcripts never cross.
- Polling test that staff messages are deduplicated after hydration.

---

## BUG-009 — Medium: known business hours are refused because they are not retrieved

### Impact

The bot refuses a basic question whose answer is present in trusted tenant configuration. Visitors receive an unnecessary abstention even though the system prompt/business facts already contain the hours.

### Reproduction

1. Ask Apex: `What are your hours?`
2. Inspect the assembled prompt, retrieval result, citation policy, and final answer.

### Observed evidence

- The assistant abstained.
- Trusted business facts in the assembled prompt contained Apex's hours.
- The public tenant page also displayed the hours.
- Apex knowledge sources available during testing were financing-focused, so retrieval did not supply an hours citation.
- The final grounding/citation policy prevented the configured fact from being used as an answer.

### Working theory

There are two competing contracts: tenant configuration is presented as trusted business truth, while finalization requires retrieved evidence for the answer. The current demo does not index an official hours document that satisfies the latter.

### Fix options

- Treat server-owned tenant business facts as an admitted, attributable evidence class; or
- Seed/index an official business-profile document and retrieve it reliably for common business-fact questions.

### Acceptance criteria

- A tenant with configured hours answers the hours question accurately.
- The trace states whether support came from tenant configuration or indexed knowledge.
- A tenant without hours still abstains or requests clarification.
- The fix does not create a general path for uncited model claims.

---

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

## BUG-011 — Medium: five repository Grafana dashboards are missing from the deployment

### Impact

The release contains dashboard definitions and a provisioning workflow, but operators cannot see the intended Tenant Chat dashboards in Grafana after deployment.

### Observed evidence

The following repository dashboards were absent from deployed Grafana:

- Chat Turn Outcomes
- Retrieval & Routing Quality
- LLM Ops & Token Cost
- Exemplar → Trace → Explorer
- Safety & Governance

Relevant assets:

- `docs/runbooks/observability-dashboards.md`
- `k8s/grafana/provision.sh`
- `k8s/grafana/*.json`

The runbook requires a separate execution of `./k8s/grafana/provision.sh`; the latest application release did not leave these dashboards available.

### Fix hints

- Make provisioning an idempotent, verified release step or package the dashboards through the cluster's normal Grafana sidecar/ConfigMap mechanism.
- Fail the release verification if required dashboard UIDs are absent.
- Preserve operator edits only if that is an explicit supported workflow.

### Acceptance criteria

- All five dashboards appear after a normal clean deployment and upgrade.
- Re-running provisioning is safe and deterministic.
- Deployment smoke tests query Grafana for the expected dashboard UIDs.
- Panels use data sources that exist in the deployed environment.

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

---

## BUG-014 — Low: stale live `chat-backend` Service points at port 8000

### Impact

The cluster contains a misleading service that selects the live backend pods but cannot reach their current port. Future automation, monitoring, or operators may choose the stale service and see connection failures.

### Observed evidence

- The live `chat-backend` Service used service/target port `8000`.
- The current backend container listens on `8004`; in-pod port 8000 refused connections.
- The active `chat-admin` Service uses port/target `8004` and works.
- The current `k8s/app.yaml` defines `chat-admin` on 8004 and the backend deployment on 8004. It does **not** appear to define the stale `chat-backend` Service, so this is likely an orphan from an older release rather than a current manifest defect.

### Fix hints

- Confirm ownership and references before deletion.
- If nothing intentionally depends on it, remove the orphan through the deployment/migration workflow and add drift detection.
- If the name is still part of the supported contract, manage it in source and point it to 8004.

### Acceptance criteria

- Every Service selecting backend pods targets an actual named/container port.
- No unmanaged legacy Service remains after upgrade.
- Release tests detect orphaned or mismatched service ports.

---

## BUG-015 — Low: availability list is difficult to scan

### Impact

The 20 returned appointment slots appear as dense, run-on `*` text rather than a clearly separated list, making selection unnecessarily difficult.

### Reproduction

1. Start a Clearview booking.
2. Ask for HVAC availability.
3. Inspect the assistant response on desktop and narrow widget widths.

### Fix hints

- Normalize model/tool slot output into structured presentation data or robust Markdown list rendering.
- Limit the initial number of choices and offer pagination or date grouping if appropriate.

### Acceptance criteria

- Each slot is visually distinct and keyboard/screen-reader understandable.
- Layout remains readable at the widget's minimum supported width.

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

These checks passed and must remain passing after fixes:

- Apex citation fetched with the owning Apex visitor credential: HTTP 200.
- The same Apex citation fetched with a Clearview visitor credential: HTTP 404.
- Northline-branded chunk stored under Clearview: Clearview credential HTTP 200; Apex credential HTTP 404.
- Forging `tenant_id=clearview` in an Apex visitor request body: HTTP 422.
- Admin headers sent directly without the authenticated gateway/session: HTTP 401.
- Authenticated viewer without tenant membership: HTTP 403.
- Platform-admin request for a nonexistent tenant: HTTP 404.
- Test email/PII search in sampled Loki backend logs: zero matches.

Expected security behavior:

- Authorization derives tenant/session identity from the signed server-issued credential or authenticated admin membership, never from a request-body tenant claim.
- Cross-tenant resources remain indistinguishable from missing resources where the API currently returns 404.
- Fixes must not expose raw prompt, PII, credentials, or full tenant identifiers in telemetry.

## Test artifacts and environment state

Exploratory testing intentionally changed demo data. Agents should account for these artifacts instead of treating them as organic customer records:

- Apex staff message: `QA operator test: verifying staff reply delivery.`
- Review `efa888f5-1f35-4d62-b475-dd843e7abde0` is `Awaiting fix` / `Amended`.
- Integrity findings were persisted for Apex and Clearview.
- Handoff `HO-BC3D91E616D34B928F1AF13320B3007E` was resolved; the queue was clean at test completion.
- Several QA sessions were created by isolation probes.
- Demo chat data contains `qa-tester@example.invalid`, `qa-tester-clearview@example.invalid`, and `480 Test Avenue`.
- No source-code or cluster-configuration changes were made during exploratory testing.
- A temporary Kibana port-forward was stopped.
- Destructive knowledge actions and privacy-delete operations were intentionally not executed.

## Suggested implementation order

1. **BUG-001** — restore the handoff safety invariant first.
2. **BUG-003** — prevent false durable-action promises.
3. **BUG-002** — stabilize booking workflow ownership.
4. **BUG-004** and **BUG-009** — repair grounding and trusted-fact contracts together only if they share a clearly bounded finalization change.
5. **BUG-005** and **BUG-006** — verify index truth, then clean contaminated demo content.
6. **BUG-007** and **BUG-008** — align visitor UX with actual session/data behavior.
7. **BUG-010** through **BUG-015** — observability, deployment, and UX hardening.

## Definition of done for each assigned bug

- Original reproduction fails before the fix and passes afterward.
- Root cause is documented with evidence, not only inferred from the symptom.
- Regression coverage includes tenant boundaries where relevant.
- No credentials or PII are added to fixtures/logs.
- Targeted tests and repository checks pass.
- Deployment or data migrations are idempotent and have rollback/verification instructions.
- The agent's handoff includes changed files, tests run, remaining risks, and exact verification steps.
