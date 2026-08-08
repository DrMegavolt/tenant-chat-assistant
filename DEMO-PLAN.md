# Demo plan — making the Gate B showcase real

Working document, not repo doctrine. Delete it when the lanes are consumed.
Where this and `BACKLOG.md` disagree on a task's *scope*, the backlog wins; this
document only sequences the work and records what was verified on `2026-08-07`.

## The goal these lanes serve

Three claims the demonstration has to survive being poked at:

1. **You can see the trace of the graph that actually ran** — not a
   reconstruction, not a plausible narrative.
2. **The system detects model errors** — and can separate a prompt regression
   from model nondeterminism from a retrieval miss.
3. **You can locate harness issues** — an application, tool, or provider failure
   lands at the executed node, visible in the explorer and in Grafana.

Gate B built the machinery for all three. The gap is that nothing exercises it:
the ten showcase cases are planted by hand, the live cluster has no governed
knowledge, and the operational plane sees nothing from the chat path.

## Verified starting state (2026-08-07, `c56d95d` + working tree)

Each of these was checked against the repository, not assumed.

| Fact | Evidence |
|---|---|
| The ten Gate B cases are planted, never executed | `BACKLOG.md:2842` — the docstring was corrected to say "the records are planted by hand and the test proves the explorer surface rather than the graph" |
| The seed Job feeds only the legacy financing index | `k8s/seed-ingestion-job.yaml` posts `{"domain":"financing"}` to `ingestion-service:8002/ingest` for both tenants; nothing traverses upload→approve→publish→ingest |
| Replay is single-trial, no tools, no retrieval | `services/api/src/tenantchat/api/replay.py:13` — "this service deliberately does not pretend to offer them" |
| The API has no ServiceMonitor | `k8s/app.yaml:1045` carries a stale comment claiming `/metrics` would 404; ServiceMonitors exist for embedding, ingestion, financing only |
| `APP_ENV` is unset on the chat path | Set on `embedding-service`, `ingestion-service`, `financing-agent`; absent from `chat-backend` (`k8s/app.yaml:837`) and `job-worker` (`:973`) |
| `correlation_headers()` has zero call sites | Definition at `services/api/src/tenantchat/api/correlation.py:145`, plus tests and docs. Nothing calls it |
| The trace discards the resolved query | `nodes.py:259` `_with_plan` writes `query` + `plan` into `evidence_meta`; `trace.py:566` `_retrieval_section` overwrites `query` with `_latest_user_message(history)` |
| `close_passing_reviews` has no production caller | `evals/gate.py:47`, referenced only from `test_eval_closure.py` |
| Trace schema is at `"2"`; stores default to `"1"` | `trace.py:77`, `store.py:1014` |
| 24 `tenantchat_*` metrics exist; no Grafana dashboards are in the repo | metric names under `packages/core`/`services/api`; `grep -rl grafana k8s/` finds only `observability-exposure.yaml` |

## Pre-flight — do this before dispatching any lane

**P0. Land the retrieval fixes sitting in the working tree.** Two changes, both
load-bearing, both uncommitted:

- `services/api/src/tenantchat/api/app.py` composes `RetrievalEvidenceSource` in
  `create_app`. Without it the default composition hands the runtime
  `evidence=None`: every turn answers from the prompt alone, with no retrieval,
  no citations, and no abstention.
- `services/api/src/tenantchat/api/search.py` fixes `_request(..., use_index=True)`
  being handed an already-resolved URL, which produced
  `http://search:9200/<idx>/http://search:9200/<idx>/_search`, a 400, and
  `retriever_version: "unavailable"` on every grounded answer — indistinguishable
  from a genuine no-match.

Run `make check`, then commit. **L1 and L9 produce nothing observable until these
land.** Note *why* the gate stayed green through this: every test injects its own
`evidence_source`, and the new code guards on `evidence_source is None`, so no
test ever exercised the default composition. Closing that hole is L9's whole
point.

**P1. Prune the worktrees.** `git worktree list` shows ~28 registrations, most
marked `prunable`, plus `.claude/worktrees/festive-dubinsky-86f377` holding an
uncommitted duplicate of work already merged in `c56d95d`. Six parallel lanes
starting from that state is how the `1c39c5d` merge fixup happens again.

```bash
git worktree prune -v
```

**P2. (optional, small) Close QA-006's remaining scope** — the check that a
backlog index checkbox and its detail `Status` cannot disagree. The whole plan
rests on "Gate B is complete" being true, and `BACKLOG.md:2848` records that
`RAG-010`/`RAG-011` had already drifted once.

## Batching

```
Pre-flight  P0 retrieval fixes · P1 worktree prune · P2 drift check
   │
   ├─ Batch 1 ── six lanes, disjoint files, dispatch together
   │    L1 SEED       governed knowledge for both tenants          M
   │    L2 TRACE-Q    stop discarding the resolved query           S
   │    L3 K8S-PLANE  API ServiceMonitor, APP_ENV, NetworkPolicy   S
   │    L4 CORR       call correlation_headers() from the clients  S
   │    L5 METRICS    router confidence, truncation, token cost    M
   │    L9a HARNESS-A real turns for cases 1, 8, 9, 10             L
   │
   ├─ Batch 2 ── after L3 (L6) / after L2 (L7, L8)
   │    L6 DASH       Grafana dashboards as code                   M
   │    L7 REPLAY     bounded trials, index replay, pinned replay  L
   │    L8 OTEL       content-free GenAI spans from the chat path  L
   │
   └─ Batch 3 ── after L1 + L2 + L7
        L9b HARNESS-B cases 2–7, both tenants, live cluster mode   L
        L10 DEMO-DOC  the walkthrough runbook                      S
```

**Why the harness is split.** L9 is the point of the exercise. Held to the end as
a single XL it is the thing most likely to get cut, and the P0 bug above is
exactly what its absence costs. Cases 1, 8, 9, and 10 depend on none of the other
lanes — they can run real turns on day one, which also gives every other lane a
live acceptance target instead of a promise. Note that cases 2–5 need
*precondition planters*, not replay; only the replay assertions on cases 6–7
actually need L7.

**Why L6 moved.** Almost every panel can be built against the 24 metrics that
already exist, so L6 is not blocked on L5. It is blocked on **L3**: with no
ServiceMonitor on `chat-backend`, Prometheus scrapes nothing from the chat path
and every panel that matters renders empty.

**If something has to be cut, cut L8, not L7.** The executed-graph capture in the
turn record already carries "the trace of the graph that was called"; L8 makes
the operational plane match the inference plane, which is polish. L7 ①③ are the
only thing that makes claim 2 demonstrable.

## Rules that apply to every lane

- `make check` is the gate. Never invoke `pip` or a bare `python` — everything
  goes through `uv run --frozen`. Dependency changes mean editing
  `pyproject.toml` and running `make lock`.
- Migration head is `0018`. **No lane below needs a migration.** If you think
  yours does, stop and say so rather than opening a chain everyone else has to
  rebase onto.
- The `packages/core` import ban, the domain-services rule, the checkpoint rule,
  the transport-free domain, the public/internal type split, and the PII rule are
  enforced by tests. Read the invariants in `CLAUDE.md` before touching
  `packages/core` or `packages/orchestration`.
- **ADR-0010's two-plane split is the one most likely to trip you.** Prompts,
  retrieved evidence, and model outputs are *content*: they belong to the
  inference trace plane only, never to logs, metrics, metric labels, or exported
  spans. L5 and L8 are both one careless label away from violating it.
- Tests read as specifications. Name them for the behavior guaranteed; give
  non-obvious cases a docstring naming the failure being prevented.
- Comments explain *why*. Do not narrate the change — git owns history.

## Collision domains

Derived from the merge fixup in `1c39c5d`. Lanes in the same domain must not run
concurrently.

| Domain | Files | Lanes |
|---|---|---|
| Orchestration trace | `packages/orchestration/.../trace.py`, `nodes.py` | L2, then L7/L8 |
| Replay + traces router | `api/replay.py`, `api/routers/traces.py` | L7 |
| Admin trace components | `frontend/src/admin/components/Trace*.tsx`, `traceTypes.ts` | L2 (funnel), L7 (replay panel) |
| Kubernetes manifests | `k8s/app.yaml`, `network-policies.yaml` | L3 only; L6 owns a new `k8s/grafana/` |
| Metrics vocabulary | `packages/core/.../metrics.py`, `api/metrics.py` | L5 |
| Chat routes | `api/routers/chat.py` | L9 |

---

# Lane briefs

Each brief is written to be handed to an agent cold. Sizes are relative:
S ≈ a focused sitting, M ≈ a day, L ≈ multi-day.

---

## L1 — SEED · governed knowledge for both demo tenants · M · Batch 1

**Depends on:** pre-flight P0.

**Problem.** `k8s/seed-ingestion-job.yaml` posts `{"tenantId":…,"domain":"financing"}`
to the legacy `ingestion-service`, which populates the financing side-agent's own
Elasticsearch index. The governed answer path reads a different store and drops
hits that carry no knowledge record, so the live cluster cannot show a grounded
answer with citations at all. Nothing in the repository seeds a document through
the real lifecycle.

**Deliverable.** A seed that drives the *governed* pipeline —
upload → approve → publish → ingest → indexed generation — for both `apex` and
`clearview`, using the same API surface an operator uses. A script plus a `make`
target plus a Job manifest that replaces the legacy one. Source documents should
be the ones the demo narrates: `docs/apex/` and `docs/clearview/` already hold
tenant material; check whether it is sufficient before authoring new content.

**Acceptance.**
- A fresh cluster reaches a state where a chat turn against each tenant returns a
  grounded answer with at least one authorized citation.
- The seed is idempotent — running it twice does not duplicate documents or
  strand a half-published generation.
- The legacy financing seed either still runs (the financing side-agent is part
  of the demo) or its removal is deliberate and stated.
- `make deployment-security` and `make image-contracts` stay green.

**Owns.** `scripts/`, `k8s/seed-*.yaml`, `Makefile`, `docs/runbooks/`.

**Watch for.** Ingestion is asynchronous and durable (`REL-003`); the seed must
wait for the generation to be indexed rather than assuming completion, or L9b's
live mode will race it.

---

## L2 — TRACE-Q · stop discarding the resolved query · S · Batch 1

**Problem.** `_with_plan` at
`packages/orchestration/src/tenantchat/orchestration/nodes.py:259` already writes
the resolved standalone query and the full `RetrievalPlan` into `evidence_meta` —
its docstring says `query` duplicates `plan.query` precisely so a reader does not
need to know the plan shape. Then `_retrieval_section` at `trace.py:566`
overwrites it:

```python
return {
    "query": _latest_user_message(history),   # discards meta["query"]
    ...
}
```

The consequence is that the retrieval funnel shows the customer's original
message, not the query the retriever actually ran. A multi-turn mis-retrieval —
the `query_rewrite_error` diagnosis cause, which already exists in the taxonomy
at `trace.py:97` — is invisible in the record. **The data is present and being
thrown away; this is a small change with outsized demo value.**

**Deliverable.** The retrieval section carries both the original message and the
resolved query plus plan. Bump `TRACE_SCHEMA_VERSION` to `"3"`, additively. The
`RetrievalFunnel` shows original vs resolved when they differ, and says nothing
extra when they do not.

**Acceptance.**
- A multi-turn conversation whose second message is a pronoun reference produces
  a record in which the resolved query is visible and different from the message.
- **Version tolerance is part of this lane, not someone else's problem.** The
  explorer renders `"1"`, `"2"`, and `"3"` records side by side without error —
  the demo will have all three, and the captured-vs-derived graph badge already
  branches on version.
- No migration. `trace_schema_version` is already a `varchar(16)` column with a
  non-empty check (`0014_inference_trace.py`).
- Content stays in the inference plane: the resolved query is content, so it must
  not reach logs, metrics, or exported spans.

**Owns.** `packages/orchestration/.../trace.py`, `frontend/src/admin/traceTypes.ts`,
`frontend/src/admin/components/` (funnel), plus their tests.

---

## L3 — K8S-PLANE · make the chat path scrapeable · S · Batch 1

**Problem.** `k8s/app.yaml:1045` says a ServiceMonitor "would silently scrape
404s, so the API has none until `OBS-002` wires real instrumentation."
`OBS-002` is done and `/metrics` exists — the comment is stale and it is the
reason Prometheus sees nothing from the chat path. Separately, `APP_ENV` is set
on `embedding-service`, `ingestion-service`, and `financing-agent` but not on
`chat-backend` or `job-worker`, so the two services that matter most run without
the production environment marker (`k8s/README.md:23` describes the behavior this
gates).

**Deliverable.** ServiceMonitors for `chat-backend` and `job-worker`; `APP_ENV`
on both; whatever NetworkPolicy halves the Prometheus scrape needs; the stale
comment deleted.

**Acceptance.**
- Prometheus lists both new targets as `up` on the local cluster.
- `make deployment-security` and `tests/test_network_boundaries.py` stay green.
- `make network-policy-smoke` still proves the allowed and denied flows.

**Owns.** `k8s/app.yaml`, `k8s/network-policies.yaml`, `k8s/observability-*.yaml`.

**Note.** This lane unblocks L6. Land it early in the batch.

---

## L4 — CORR · propagate correlation to internal calls · S · Batch 1

**Problem.** `correlation_headers()` at
`services/api/src/tenantchat/api/correlation.py:145` is fully implemented and
tested, documented as the internal-service propagation contract in
`BACKLOG.md:1260` and `docs/runbooks/trace-walkthrough.md:63` — and called from
nowhere. Every internal hop (Elasticsearch, the embedding service, the financing
agent, the job worker) starts a fresh correlation context, so a request cannot be
followed across services.

**Deliverable.** Outbound internal calls carry the correlation headers. The job
worker propagates the context it inherits from the enqueuing request. Update
`docs/runbooks/trace-walkthrough.md` where it describes propagation as if it
already worked.

**Acceptance.**
- A single chat turn's request id appears on every internal hop it caused.
- `services/api/tests/test_correlation.py` and `test_worker_correlation.py` cover
  propagation, not just header construction.
- Headers go to internal services only. Never attach them to a third-party
  provider call.

**Owns.** `services/api/src/tenantchat/api/search.py`, `job_worker.py`,
`docs/runbooks/trace-walkthrough.md`.

**Coordinate.** Pre-flight P0 also touches `search.py`. Land P0 first.

---

## L5 — METRICS · the "quality by class, not volume" story · M · Batch 1

**Problem.** 24 `tenantchat_*` metrics exist, but three the demo narrative wants
are missing: router confidence distribution, context-truncation rate, and a token
cost estimate. The router already computes confidence —
`nodes.py:181` puts `decision.confidence` into the trace — and
`ROUTING_DECISIONS` (`packages/core/.../metrics.py:73`) records the decision
without it.

**Deliverable.** Router-confidence buckets, context-truncation rate, and a
token-cost estimate derived from `tenantchat_llm_tokens_total`. Extend
`docs/runbooks/metrics-walkthrough.md` with the manifest-correlation queries —
"show me turn outcomes for this prompt template version" is the query that makes
the component manifest worth having.

**Acceptance.**
- Each new metric has a test asserting its labels.
- **Cardinality is bounded and deliberate.** Confidence is bucketed, not emitted
  raw. No label carries a tenant-supplied string.
- **ADR-0010: no metric label carries content.** Not the query, not a document
  id that identifies content, not a model output. This is the lane most likely to
  violate the two-plane split — treat any label whose value comes from a customer
  message or a retrieved chunk as disqualified.

**Owns.** `packages/core/.../metrics.py`, `services/api/.../metrics.py`, the route
node, `docs/runbooks/metrics-walkthrough.md`, tests.

---

## L9a — HARNESS-A · real turns for the independent cases · L · Batch 1

**Depends on:** pre-flight P0.

**Problem.** `services/api/tests/test_trace_explorer.py` writes turn records
directly into the store. Its own docstring, corrected in `26e86a0`, says the test
proves the explorer surface rather than the graph. The records default to
`trace_schema_version "1"` while real turns write `"2"`, carry no captured graph,
no model, and no detector result. **A demo of "why did it say that" resting on
fabricated records is the single thing most likely to be caught.**

The P0 bug is the proof: retrieval was disabled in the default composition and
the suite stayed green, because every test injects its own `evidence_source`.

**Deliverable.** A driver that runs **real `POST /api/chat` turns through the
real graph** with a scripted provider, plus per-case precondition planters, for
the four cases that depend on no other lane:

- **Case 1** — a correct grounded answer with an authorized citation.
- **Case 8** — a fabricated citation rejected mechanically.
- **Case 9** — an application, tool, or provider failure located at the executed
  node.
- **Case 10** — an indirect prompt-injection document quarantined without
  changing policy or invoking a tool.

Structure it so L9b adds cases 2–7 by adding planters, not by rewriting the
driver.

**Acceptance.** For each case, assert against the record the system actually
produced:
- `trace_schema_version` is current and the graph section is **captured**, not
  derived.
- The expected detector fires with the expected `DiagnosisCause`, or the case
  explicitly expects `inconclusive`.
- The record is findable through all six explorer filters.
- Tenant isolation holds — the record is invisible to the other tenant.
- Runs in CI against disposable infrastructure, no LLM required.

**Owns.** A new harness module and `make` target, `services/api/tests/`.
Coordinate with L2 on `trace.py` — take the schema bump as a dependency rather
than editing it.

**Watch for.** `ScriptedModel` (`services/api/tests/conftest.py:70`) replays a
fixed list and then repeats the last response — fine for deterministic cases,
useless for case 7's bounded trials. That is L7's problem, not yours.

---

## L6 — DASH · Grafana dashboards as code · M · Batch 2, after L3

**Problem.** Everything the demo can show operationally lives inside the admin
console. Grafana, Tempo, and Prometheus show nothing from the main chat path, so
"production-quality observability" is a claim the operator's own tooling does not
corroborate. No dashboards exist in the repository.

**Deliverable.** Provisioned dashboards as code, in a new `k8s/grafana/`, against
the existing kube-prometheus-stack: turn outcomes, diagnosis causes, node
latency, LLM calls and tokens, retrieval funnel, business actions. Plus the
drill-through document: **exemplar → trace id → the FEAT-015 explorer**, which is
the beat that ties the operational plane to the inference plane.

**Acceptance.**
- Dashboards provision from the repository, not from Grafana's UI — a hand-built
  dashboard is not a deliverable.
- Every panel resolves against a metric that exists. If it needs one of L5's
  three, take the metric name as a dependency and say so.
- The exemplar drill-through works end to end on the cluster. Verify after merge;
  this is the one lane whose acceptance is not fully provable in CI.

**Owns.** new `k8s/grafana/`, `docs/`.

---

## L7 — REPLAY · make "detects model errors" demonstrable · L · Batch 2, after L2

**Problem.** `replay.py:13` states the limits plainly: stored prompt → current
model, no tools, single trial, and it "deliberately does not pretend to offer"
repeated trials, immutable-index retrieval replay, or gold-evidence substitution.
Gate B cases 4–7 cannot show their work, and the acceptance script demands a
replay result per case.

**Three sequenced milestones. Land them in order; each is independently useful.**

**① Bounded repeated trials.** N trials with prompt and evidence held constant,
reported as an aggregate with an explicit stochastic label. This is what makes
**case 7** — a model-behavior difference — demonstrable rather than assertable.
Bound the trial count; an unbounded replay loop against a live model is a footgun.

**② Immutable-index retrieval replay + gold-evidence substitution.** Replay
retrieval against the pinned index generation and compare with the retained
generation. **When the generation is gone, the service must refuse the
reproducibility claim rather than silently replaying against current data** —
a replay that quietly changes its evidence is worse than no replay. Serves
**cases 2–5**.

**③ Template-version-pinned replay.** Model and evidence constant, prompt
template pinned to the stored version, isolating a prompt regression from
everything else. Serves **case 6**.

**Acceptance.**
- Replay still touches no domain effect — no booking, lead, or handoff can be
  caused by a replay, under any milestone. This is the property the whole feature
  rests on; test it explicitly per milestone.
- Every replay response states what it holds constant and what it does not, and
  a single trial is still labelled an observation rather than a proof.
- Manifest comparison stays content-free.
- Each milestone is audited to actor, turn, and reason like the existing paths.

**Owns.** `services/api/src/tenantchat/api/replay.py`,
`services/api/src/tenantchat/api/routers/traces.py`, the replay panel under
`frontend/src/admin/components/`, tests.

---

## L8 — OTEL · content-free spans from the chat path · L · Batch 2, after L2

**Problem.** Tempo sees the side services only. The chat backend and job worker
emit no spans, so a turn cannot be followed in the tracing UI even though its
inference record is complete.

**Deliverable.** GenAI-convention operational spans from the API and
orchestration runtime, plus instrumentation annotations for `chat-backend` and
`job-worker`. The collector's redaction allowlist already exists
(`tests/security/test_trace_plane.py` guards it).

**Acceptance.**
- **Spans are content-free, per ADR-0010.** No prompt, no retrieved evidence, no
  model output, on any span attribute or event. The trace store is the one
  deliberate home for that content, and it is not this plane. Assume a reviewer
  will grep the exported attributes for customer text.
- A turn is followable across services in Tempo — which needs L4's propagation to
  be genuinely useful.
- `tests/security/test_trace_plane.py` stays green and is extended to cover the
  new emitters.

**Owns.** orchestration runtime span emission, API telemetry setup, k8s
annotations.

**This is the cut candidate** if the schedule tightens. The executed-graph
capture already carries claim 1.

---

## L9b — HARNESS-B · the remaining cases and live mode · L · Batch 3

**Depends on:** L1, L2, L7, L9a.

**Deliverable.** Extend L9a's driver with precondition planters for the six
remaining cases, across **both** tenants:

- **Case 2** — a stale source that must not silently shape a current answer.
- **Case 3** — a published document whose index generation is missing or
  incomplete.
- **Case 4** — a relevant chunk retrieved but ranked below the selection cutoff.
- **Case 5** — selected evidence dropped by the context budget.
- **Case 6** — a prompt regression isolated with model and evidence constant.
  *(asserts L7 ③)*
- **Case 7** — a model-behavior difference through bounded repeated trials.
  *(asserts L7 ①)*

Plus a second run mode: **live**, against the cluster's LM-Studio endpoint. That
mode is not a test — **it is the demo seed**, and it is what puts real records in
front of the explorer on the day.

**Acceptance.**
- Every case asserts the same properties L9a established, plus its replay result
  where the case defines one.
- Cases 2–5 assert on the *record*; only 6–7 assert on replay output.
- Live mode is idempotent and re-runnable — the demo will be run more than once.
- Scripted mode stays CI-suitable: hermetic, no LLM.
- Case 4's ranking precondition and case 5's budget precondition must be set up
  through real configuration, not by hand-editing a record. A planted
  precondition is fine; a planted *outcome* defeats the exercise.

---

## L10 — DEMO-DOC · the walkthrough runbook · S · Batch 3, after L9b + L6

**Deliverable.** A per-case narrative — click this, it proves this — covering all
ten cases, the `FEAT-004` handoff journey, and the Grafana → exemplar → explorer
beat. Records the measured results rather than the intended ones.

**Acceptance.** Someone who did not build the system can drive the demo from the
document alone. Every claim in it points at a case that actually ran.

**Owns.** `docs/runbooks/`.

---

## Deliberately excluded

The backlog's own notes agree these add no evidence to the three claims:
`FEAT-002`/`003`/`005` (vendor plumbing), `FEAT-006`/`007`/`009`/`012`/`014`, and
all of Gate C (`DEP-002`..`006`, `QA-005`). `OBS-003` is partially absorbed by L3
and L6 — justified because the demonstration does need the dashboard, which is
the backlog's stated exception. `FEAT-013`'s remainder is a manual screen-reader
pass, orthogonal to this work.

**Deferred, worth doing after the demo:** `FEAT-010` streaming (after L9 — both
touch the chat routes) and the flywheel wiring (a production caller for
`close_passing_reviews`, which today exists only in `evals/gate.py:47` and its
tests, plus a first real judge with held-out agreement against the local model).
