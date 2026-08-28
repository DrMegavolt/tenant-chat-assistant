import { useEffect, useState } from "react";

import type { AdminApi } from "src/admin/adminApi";
import {
  DIAGNOSIS_CAUSE_LABELS,
  DIAGNOSIS_STATUS_LABELS,
  isUncertainStatus,
  OUTCOME_LABELS,
  type DiagnosisRecord,
  type ExecutedGraphSection,
  type GoldCase,
  type PromptMessage,
  type PromptSegment,
  type ReplayResult,
  type ReplayTrialsResult,
  type RetrievalSection,
  type RoutingSection,
  type ToolsSection,
  type TraceRead,
  type TraceSearchRecord,
  type VerdictsSection
} from "src/admin/traceTypes";

export interface TraceDetailProps {
  api: AdminApi;
  tenantId: string;
  record: TraceSearchRecord;
  gold: GoldCase[];
  /** An audited read of this record already performed by the caller (an id
   * lookup), so the drill-in renders it without a second read (review R-19). */
  preloadedTrace?: TraceRead | undefined;
}

/**
 * One turn, drilled down: the executed structure from the stored trace, the
 * coordinated diagnostic panels, the gold overlay, and safe replay. Every
 * rendered element carries its stored trace field, so nothing here can be
 * mistaken for an idealized graph.
 */
export function TraceDetail({ api, tenantId, record, gold, preloadedTrace }: TraceDetailProps) {
  // The audited read can load, be absent (another tenant's or a removed
  // record), or fail — collapsing those into one falsy value is what let a
  // 404 render "Opening…" forever (review R-18).
  const [load, setLoad] = useState<
    | { status: "loading" }
    | { status: "loaded"; trace: TraceRead }
    | { status: "absent" }
    | { status: "error" }
  >(preloadedTrace ? { status: "loaded", trace: preloadedTrace } : { status: "loading" });
  const [replay, setReplay] = useState<ReplayResult | null>(null);
  const [replayTrials, setReplayTrials] = useState<ReplayTrialsResult | null>(null);
  const [replayError, setReplayError] = useState<string | null>(null);
  const [isReplaying, setReplaying] = useState(false);
  const [isReplayingTrials, setReplayingTrials] = useState(false);

  const [reload, setReload] = useState(0);
  useEffect(() => {
    // A preloaded record was read once for this click already; reading it
    // again would be a second audited row for the same operator action.
    if (preloadedTrace) return;
    let cancelled = false;
    void fetchTrace(api, tenantId, record.turnId).then(
      (loaded) => {
        if (cancelled) return;
        setLoad(loaded ? { status: "loaded", trace: loaded } : { status: "absent" });
      },
      () => {
        if (!cancelled) setLoad({ status: "error" });
      }
    );
    return () => {
      cancelled = true;
    };
  }, [api, tenantId, record.turnId, reload, preloadedTrace]);

  const retry = () => {
    if (preloadedTrace) return;
    setLoad({ status: "loading" });
    setReload((n) => n + 1);
  };

  if (load.status !== "loaded") {
    return (
      <p className="muted-copy" role="status">
        {load.status === "loading" && "Opening the audited turn record…"}
        {load.status === "absent" && "No turn record was found for this entry."}
        {load.status === "error" && "Reading the turn record failed."}{" "}
        {load.status !== "loading" && (
          <button type="button" className="ghost-button" onClick={retry}>
            Retry
          </button>
        )}
      </p>
    );
  }

  const trace = load.trace;
  const content = trace.content;
  const goldCase = gold.find((case_) => case_.query === content.retrieval?.query);
  const traceId = trace.traceId ?? record.traceId ?? record.turnId;

  const runReplay = async () => {
    setReplaying(true);
    setReplayError(null);
    try {
      setReplay(await api.replayTrace(record.turnId, tenantId));
    } catch (err) {
      setReplayError(
        err instanceof Error ? err.message : "The replay did not run. The model may be unavailable."
      );
    } finally {
      setReplaying(false);
    }
  };

  const runReplayTrials = async () => {
    setReplayingTrials(true);
    setReplayError(null);
    try {
      setReplayTrials(await api.replayTrials(record.turnId, tenantId, 3));
    } catch (err) {
      setReplayError(
        err instanceof Error
          ? err.message
          : "The replay trials did not run. The model may be unavailable."
      );
    } finally {
      setReplayingTrials(false);
    }
  };

  return (
    <article className="trace-detail" aria-label={`Turn ${record.turnIndex} detail`}>
      <header className="trace-detail-header">
        <h3>Turn {content.turnIndex ?? record.turnIndex}</h3>
        <span className={`outcome-badge outcome-${record.outcome}`}>
          {OUTCOME_LABELS[record.outcome] ?? record.outcome}
        </span>
        <span className="session-meta mono">{traceId}</span>
        <span className="session-meta">
          Manifest {content.manifestHash?.slice(0, 12) ?? "not recorded"} · schema{" "}
          {content.schemaVersion ?? "?"}
        </span>
      </header>

      <ExecutedGraph content={content} />

      {goldCase && <GoldOverlay goldCase={goldCase} actualQuery={content.retrieval?.query} />}

      <DiagnosisPanel diagnoses={content.diagnoses} />

      <RoutingPanel routing={content.routing} />

      <RetrievalFunnel retrieval={content.retrieval} />

      <PromptPanel prompt={content.prompt} />

      <VerdictsPanel verdicts={content.verdicts} output={content.output} />

      <ToolsPanel tools={content.tools} />

      <ReplayPanel
        replay={replay}
        replayTrials={replayTrials}
        isReplaying={isReplaying}
        isReplayingTrials={isReplayingTrials}
        error={replayError}
        onReplay={() => void runReplay()}
        onReplayTrials={() => void runReplayTrials()}
      />
    </article>
  );
}

// A module-level promise registry deduplicates the audited read across a
// StrictMode double-mount and repeated openings of the same turn: each
// inference-plane read is audited, so two of them for one click would be two
// audit rows for one operator action.
const traceFetchers = new Map<string, Promise<TraceRead | null>>();

function fetchTrace(api: AdminApi, tenantId: string, turnId: string): Promise<TraceRead | null> {
  const key = `${tenantId}:${turnId}`;
  const existing = traceFetchers.get(key);
  if (existing) return existing;
  const promise = api.trace(turnId, tenantId).finally(() => traceFetchers.delete(key));
  traceFetchers.set(key, promise);
  return promise;
}

interface GraphSource {
  label: string;
  storedField: string;
  detail: string;
  status: "ok" | "failed" | "uncertain" | "skipped";
}

function graphSources(content: TraceRead["content"]): GraphSource[] {
  const sources: GraphSource[] = [];
  const routing = content.routing;
  sources.push({
    label: routing ? `Routing · ${routing.rule ?? routing.outcome ?? "recorded"}` : "Routing",
    storedField: "routing",
    detail: routing ? routingDecisionDetail(routing) : "not recorded",
    status: routing ? "ok" : "skipped"
  });

  const retrieval = content.retrieval;
  const retrievalFailed = retrieval?.retrieverVersion === "unavailable";
  sources.push({
    label: retrieval ? `Retrieval · ${retrieval.retrieverVersion ?? "recorded"}` : "Retrieval",
    storedField: "retrieval",
    detail: retrieval
      ? `${retrieval.candidates?.length ?? 0} candidate(s), sufficient: ${retrieval.sufficient ? "yes" : "no"}`
      : "not recorded",
    status: retrievalFailed
      ? "failed"
      : retrieval
        ? retrieval.sufficient
          ? "ok"
          : "uncertain"
        : "skipped"
  });

  const prompt = content.prompt;
  const excluded = prompt?.excluded?.length ?? 0;
  sources.push({
    label: prompt ? `Prompt assembly · ${prompt.templateRef ?? "?"}` : "Prompt assembly",
    storedField: "prompt.template_ref / prompt.excluded",
    detail: prompt
      ? `${prompt.messages?.length ?? 0} message(s), ${excluded} excluded by budget`
      : "not recorded",
    status: excluded > 0 ? "uncertain" : prompt ? "ok" : "skipped"
  });

  const toolCalls = content.tools?.toolCalls ?? [];
  const modelCalled = Boolean(content.model?.name);
  // Provenance, shown beside the serving model: a cache-served answer must not
  // read as a fresh zero-token completion (R-37), and a fallback chain is the
  // outage story behind the answering call (R-38).
  const hops = content.model?.fallbackHops ?? [];
  const provenance = [
    ...(content.model?.cacheHit ? ["served from cache"] : []),
    ...hops.map((hop) => `${hop.modelName || "unknown model"} failed (${hop.reason})`)
  ];
  sources.push({
    label: modelCalled
      ? `Model · ${content.model?.name} · ${content.outcome?.rounds ?? 1} round(s)`
      : "Model",
    storedField: "model.name / outcome.rounds",
    detail: modelCalled
      ? [
          toolCalls.length ? `${toolCalls.length} tool call(s) attempted` : "one completion",
          ...provenance
        ].join(", ")
      : "no model call (recorded as abstained/clarified)",
    status: modelCalled ? (hops.length > 0 ? "uncertain" : "ok") : "skipped"
  });

  for (const call of toolCalls) {
    const result = content.tools?.toolResults?.find((item) => item.callId === call.callId);
    const errorCode = result ? toolErrorCode(result.result) : null;
    sources.push({
      label: `Tool · ${call.name ?? "?"}`,
      storedField: "tools.tool_calls / tools.tool_results",
      detail: errorCode
        ? `executed, safe error code ${errorCode}`
        : result
          ? "executed"
          : "no result recorded (turn interrupted)",
      status: errorCode ? "failed" : result ? "ok" : "uncertain"
    });
  }

  const verdicts = content.verdicts;
  const failedCitations = verdicts?.citationInvalid?.length ?? 0;
  const refused = verdicts?.refusedTools?.length ?? 0;
  sources.push({
    label: "Validation",
    storedField: "verdicts",
    detail: [
      `${failedCitations} fabricated citation(s)`,
      `${refused} tool(s) refused`,
      `${verdicts?.claimsInvalid?.length ?? 0} unsupported claim(s)`
    ].join(", "),
    status: failedCitations > 0 || refused > 0 ? "failed" : "ok"
  });

  const outcome = content.outcome;
  sources.push({
    label: `Outcome · ${outcome?.status ?? "?"}`,
    storedField: "outcome",
    detail: outcome?.failure ? `failure ${outcome.failure}` : "no failure recorded",
    status:
      outcome?.status === "answered"
        ? "ok"
        : outcome?.status === "abstained"
          ? "uncertain"
          : "failed"
  });
  return sources;
}

function toolErrorCode(result: string | undefined): string | null {
  if (!result) return null;
  try {
    const parsed = JSON.parse(result) as { error?: unknown };
    return typeof parsed.error === "string" ? parsed.error : null;
  } catch {
    return null;
  }
}

/** The score the router actually compared its confidence against, when the
 * outcome names which threshold that was. */
function comparedThreshold(routing: RoutingSection): number | null {
  if (routing.outcome === "direct") return routing.directThreshold ?? null;
  if (routing.outcome === "clarify") return routing.clarifyThreshold ?? null;
  return null;
}

function formatScore(score: number | null | undefined): string {
  return score === null || score === undefined ? "?" : String(score);
}

/** The stored decision in one line: the winning intent (or the explicit
 * no-choice decision), its confidence, and the threshold it was judged
 * against. A null chosen means different things per outcome — clarify asks the
 * visitor again, the bounded_clarify handoff escalates to a human — so the
 * wording follows the stored outcome instead of assuming one of them. */
function routingDecisionDetail(routing: RoutingSection): string {
  const confidence = formatScore(routing.confidence);
  const threshold = comparedThreshold(routing);
  if (routing.chosen) {
    const judged =
      threshold !== null
        ? ` at confidence ${confidence} against the ${routing.outcome ?? "direct"} threshold ${formatScore(threshold)}`
        : ` at confidence ${confidence}`;
    return `chose ${routing.chosen} (${routing.rule ?? "recorded"})${judged}`;
  }
  if (routing.outcome === "handoff") {
    return `no intent chosen — escalated to a human after a prior clarification (${routing.rule ?? "recorded"}) at confidence ${confidence}`;
  }
  if (routing.outcome === "clarify") {
    return `no intent chosen — asked for clarification (${routing.rule ?? "recorded"}) at confidence ${confidence} against the clarify threshold ${formatScore(
      routing.clarifyThreshold
    )}`;
  }
  return `no intent chosen (${routing.rule ?? "recorded"}) at confidence ${confidence}`;
}

const STATUS_LABELS: Record<GraphSource["status"], string> = {
  ok: "ok",
  failed: "failed",
  uncertain: "uncertain",
  skipped: "skipped"
};

/**
 * The executed-graph panel. A turn recorded after `OBS-006` carries a captured
 * section of real node/edge events; this renders that as a waterfall with
 * per-node duration and attempt count. A turn recorded before it — or a run
 * whose listener degraded — has no section, and the FEAT-015 derived waterfall
 * is shown instead, visibly marked derived so a viewer is never shown a
 * derived view as a captured one.
 */
function ExecutedGraph({ content }: { content: TraceRead["content"] }) {
  const executed = content.executedGraph;
  return executed && executed.nodes.length > 0 ? (
    <CapturedGraph section={executed} />
  ) : (
    <DerivedGraph content={content} />
  );
}

function CapturedGraph({ section }: { section: ExecutedGraphSection }) {
  const stoppedIndex = section.nodes.findIndex((node) => node.status === "error");
  const stopped = stoppedIndex >= 0 ? section.nodes[stoppedIndex] : null;
  const rendered = stopped ? section.nodes.slice(0, stoppedIndex + 1) : section.nodes;
  return (
    <section className="trace-panel" aria-labelledby="graphTitle">
      <div className="admin-panel-header">
        <h3 id="graphTitle">Executed graph</h3>
        <span className="uncertain-chip">captured execution</span>
        {section.runKind === "resume" && (
          <span className="uncertain-chip">resumed from checkpoint</span>
        )}
      </div>
      <p className="muted-copy">
        Every node and edge below is a captured execution event — the node names, the edge taken at
        each branch, per-node duration, and attempt count. No captured event carries prompt text,
        evidence, an answer, or an argument value.
      </p>
      <p className="muted-copy">
        {section.nodes.length} node(s) · {section.edges.length} edge(s) · run duration{" "}
        {section.durationMs !== null && section.durationMs !== undefined
          ? `${section.durationMs} ms`
          : "not recorded"}
      </p>
      <ol className="graph-stages">
        {rendered.map((node, index) => (
          <li
            key={`${node.name}-${node.attempt}-${index}`}
            className={`graph-stage ${
              node.status === "error" ? "failed" : node.interrupted ? "uncertain" : "ok"
            }`}
          >
            <span className="graph-stage-status">
              {node.status === "error" ? "failed" : node.interrupted ? "paused" : node.status}
            </span>
            <span className="graph-stage-body">
              <strong>
                {node.name}
                {node.replayed && <span className="uncertain-chip">replayed</span>}
              </strong>
              <span className="muted-copy">
                attempt {node.attempt} · edge {node.edge ?? "—"}
                {node.interrupted && " · paused at confirmation"}
              </span>
              <span className="muted-copy">
                {node.durationMs !== null && node.durationMs !== undefined
                  ? `${node.durationMs} ms`
                  : "entered, never exited"}
              </span>
            </span>
          </li>
        ))}
      </ol>
      {stopped && (
        <p className="muted-copy" role="note">
          The graph stopped at <code>{stopped.name}</code> — that node failed and nothing below it
          ran. This is the captured execution, not an idealized completion.
        </p>
      )}
    </section>
  );
}

function DerivedGraph({ content }: { content: TraceRead["content"] }) {
  const sources = graphSources(content);
  return (
    <section className="trace-panel" aria-labelledby="graphTitle">
      <div className="admin-panel-header">
        <h3 id="graphTitle">Executed structure</h3>
        <span className="uncertain-chip">derived from stored trace fields</span>
      </div>
      <p className="muted-copy">
        This turn predates executed-graph capture (or its listener failed), so this view derives its
        stages from the stored trace fields — named under each row — and is marked derived, not
        captured. Per-node durations are not recorded for such a turn; full LangGraph node/edge
        events are only available for turns recorded after the upgrade.
      </p>
      <ol className="graph-stages">
        {sources.map((source, index) => (
          <li key={`${source.label}-${index}`} className={`graph-stage ${source.status}`}>
            <span className="graph-stage-status">{STATUS_LABELS[source.status]}</span>
            <span className="graph-stage-body">
              <strong>{source.label}</strong>
              <span className="muted-copy">{source.detail}</span>
              <code className="graph-stored-field">{source.storedField}</code>
            </span>
          </li>
        ))}
      </ol>
    </section>
  );
}

function GoldOverlay({
  goldCase,
  actualQuery
}: {
  goldCase: GoldCase;
  actualQuery: string | undefined;
}) {
  return (
    <section className="trace-panel gold-panel" aria-labelledby="goldTitle">
      <div className="admin-panel-header">
        <h3 id="goldTitle">Gold evidence overlay</h3>
        <span className="muted-copy">reviewer-labelled · non-gating</span>
      </div>
      <p className="muted-copy">
        This turn's query matches eval case{" "}
        <code>
          {goldCase.caseId}
          {goldCase.scenario ? ` · scenario ${goldCase.scenario}` : ""}
        </code>
        . The gold chunks below are the reviewer-labelled passages the case is anchored to; they do
        not change how this turn was judged.
      </p>
      <ul className="gold-chunks">
        {goldCase.goldChunks.map((chunk) => (
          <li key={chunk.sourceId}>
            <code>{chunk.sourceId}</code>
            <p>{chunk.text}</p>
          </li>
        ))}
      </ul>
      {goldCase.goldChunks.length === 0 && (
        <p className="muted-copy">
          This case is anchored to no gold chunks (it expects an abstention); the absence is the
          expectation.
        </p>
      )}
      {actualQuery !== undefined && actualQuery !== goldCase.query && (
        <p className="muted-copy">
          The turn's stored query differs from the fixture's; matching is by exact query equality.
        </p>
      )}
    </section>
  );
}

function DiagnosisPanel({ diagnoses }: { diagnoses: DiagnosisRecord[] | undefined }) {
  return (
    <section className="trace-panel" aria-labelledby="diagnosisTitle">
      <div className="admin-panel-header">
        <h3 id="diagnosisTitle">Diagnoses</h3>
      </div>
      {!diagnoses || diagnoses.length === 0 ? (
        <p className="muted-copy">No diagnosis recorded for this turn.</p>
      ) : (
        <ul className="diagnosis-list">
          {diagnoses.map((diagnosis) => {
            const uncertain = isUncertainStatus(diagnosis.status);
            return (
              <li
                key={`${diagnosis.cause}-${diagnosis.stage}`}
                className={`diagnosis ${diagnosis.status}`}
              >
                <span className="session-row">
                  <strong>{DIAGNOSIS_CAUSE_LABELS[diagnosis.cause] ?? diagnosis.cause}</strong>
                  <span className={`diagnosis-status ${diagnosis.status}`}>
                    {DIAGNOSIS_STATUS_LABELS[diagnosis.status] ?? diagnosis.status}
                  </span>
                </span>
                <span className="muted-copy">
                  stage {diagnosis.stage} · {diagnosis.role} · confidence {diagnosis.confidence}
                </span>
                {uncertain && (
                  <p className="uncertain-note" role="note">
                    This is not a confirmed cause: it is a suspicion or an inconclusive finding
                    until replay or review adds evidence.
                  </p>
                )}
                {diagnosis.evidence.length > 0 && (
                  <ul className="evidence-refs">
                    {diagnosis.evidence.map((ref) => (
                      <li key={ref}>
                        <code>{ref}</code>
                      </li>
                    ))}
                  </ul>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

function RoutingPanel({ routing }: { routing: RoutingSection | null | undefined }) {
  return (
    <section className="trace-panel" aria-labelledby="routingTitle">
      <div className="admin-panel-header">
        <h3 id="routingTitle">Routing</h3>
        <span className="muted-copy">stored in routing</span>
      </div>
      {!routing ? (
        <p className="muted-copy">No routing decision recorded.</p>
      ) : (
        <>
          <p className="muted-copy">
            {routingDecisionDetail(routing)} · policy <code>{routing.policyVersion ?? "?"}</code>.
          </p>
          <table className="trace-table">
            <thead>
              <tr>
                <th scope="col">Decision</th>
                <th scope="col">Intent</th>
                <th scope="col">Score</th>
                <th scope="col">Matched signals</th>
              </tr>
            </thead>
            <tbody>
              {(routing.candidates ?? []).map((candidate, index) => {
                const chosen =
                  candidate.intent !== undefined && candidate.intent === routing.chosen;
                return (
                  <tr key={index} className={chosen ? "routing-winner" : ""}>
                    <td>{chosen ? "chosen" : "—"}</td>
                    <td>{candidate.intent ?? "?"}</td>
                    <td>{formatScore(candidate.score)}</td>
                    <td>{(candidate.matchedSignals ?? []).join(", ") || "—"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </>
      )}
    </section>
  );
}

function RetrievalFunnel({ retrieval }: { retrieval: RetrievalSection | null | undefined }) {
  const hasResolved = retrieval?.resolvedQuery && retrieval.resolvedQuery !== retrieval.query;
  const displayQuery = retrieval?.resolvedQuery ?? retrieval?.query;
  return (
    <section className="trace-panel" aria-labelledby="funnelTitle">
      <div className="admin-panel-header">
        <h3 id="funnelTitle">Retrieval funnel</h3>
        <span className="muted-copy">stored in retrieval</span>
      </div>
      {!retrieval ? (
        <p className="muted-copy">No retrieval run recorded.</p>
      ) : (
        <>
          {hasResolved && (
            <p className="muted-copy">
              Original: <code>{retrieval.originalMessage ?? retrieval.query ?? "—"}</code>
            </p>
          )}
          <p className="muted-copy">
            {hasResolved ? "Resolved: " : "Query: "}
            <code>{displayQuery || "—"}</code> · retriever{" "}
            <code>{retrieval.retrieverVersion ?? "?"}</code> · reranker{" "}
            <code>{retrieval.reranker ?? "none"}</code> · sufficient:{" "}
            {retrieval.sufficient ? "yes" : "no"}
          </p>
          <table className="trace-table">
            <thead>
              <tr>
                <th scope="col">Source</th>
                <th scope="col">Score</th>
                <th scope="col">Generation</th>
              </tr>
            </thead>
            <tbody>
              {(retrieval.candidates ?? []).map((candidate, index) => (
                <tr key={`${candidate.sourceId}-${index}`}>
                  <td>
                    <code>{candidate.sourceId ?? "?"}</code>
                  </td>
                  <td>{candidate.score ?? "?"}</td>
                  <td>{candidate.generationId ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="muted-copy">
            The trace stores each candidate's fused score; per-stage lexical, vector, and rerank
            scores are recorded when the pipeline supplies them.
          </p>
        </>
      )}
    </section>
  );
}

function PromptPanel({ prompt }: { prompt: TraceRead["content"]["prompt"] }) {
  return (
    <section className="trace-panel" aria-labelledby="promptTitle">
      <div className="admin-panel-header">
        <h3 id="promptTitle">Assembled prompt</h3>
        <span className="muted-copy">stored in prompt</span>
      </div>
      {!prompt ? (
        <p className="muted-copy">No assembled prompt recorded.</p>
      ) : (
        <>
          <p className="muted-copy">
            Template <code>{prompt.templateRef ?? "?"}</code> · content hash{" "}
            <code>{(prompt.contentHash ?? "").slice(0, 12)}</code>
          </p>
          <div className="prompt-messages">
            {(prompt.messages ?? []).map((message, index) => (
              <PromptMessageView key={index} message={message} />
            ))}
          </div>
          {(prompt.excluded ?? []).length > 0 && (
            <div className="prompt-excluded">
              <h4>Excluded from the context budget</h4>
              <ul>
                {(prompt.excluded ?? []).map((item, index) => (
                  <li key={index}>
                    <code>{item.reference ?? "?"}</code> · {item.kind ?? "?"} · {item.reason ?? "?"}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </>
      )}
    </section>
  );
}

function PromptMessageView({ message }: { message: PromptMessage }) {
  const segments = message.segments ?? [];
  return (
    <div className="prompt-message">
      <h4>{message.role}</h4>
      {segments.length === 0 && <p className="muted-copy">{message.content ?? ""}</p>}
      {segments.map((segment) => (
        <PromptSegmentView key={segment.segmentId} segment={segment} />
      ))}
    </div>
  );
}

function PromptSegmentView({ segment }: { segment: PromptSegment }) {
  const trusted = segment.region === "trusted";
  return (
    <p className={`prompt-segment ${trusted ? "trusted" : "untrusted"}`}>
      <span className="visually-hidden">
        {trusted ? "Trusted server-authored" : "Untrusted visitor or evidence"} segment
      </span>
      <span aria-hidden="true" className="segment-region">
        {trusted ? "TRUSTED" : "UNTRUSTED"}
      </span>
      {segment.text}
    </p>
  );
}

function VerdictsPanel({
  verdicts,
  output
}: {
  verdicts: VerdictsSection | undefined;
  output: TraceRead["content"]["output"];
}) {
  const claims = output?.claims ?? [];
  const verified = new Set((verdicts?.citations ?? []).map((citation) => citation.sourceId));
  const fabricated = new Set(verdicts?.citationInvalid ?? []);
  return (
    <section className="trace-panel" aria-labelledby="verdictsTitle">
      <div className="admin-panel-header">
        <h3 id="verdictsTitle">Claim verdicts</h3>
        <span className="muted-copy">
          deterministic: supported / unsupported / fabricated_citation
        </span>
      </div>
      {claims.length === 0 && (verdicts?.claimsInvalid?.length ?? 0) === 0 ? (
        <p className="muted-copy">No claims recorded for this turn.</p>
      ) : (
        <ul className="claim-list">
          {claims.map((claim) => (
            <li key={claim}>
              <span
                className={`claim-verdict ${fabricated.has(claim) ? "fabricated" : "supported"}`}
              >
                {fabricated.has(claim)
                  ? "fabricated_citation"
                  : verified.has(claim)
                    ? "supported"
                    : "unchecked"}
              </span>
              <code>{claim}</code>
            </li>
          ))}
          {(verdicts?.claimsInvalid ?? []).map((item, index) => (
            <li key={`invalid-${index}`}>
              <span className="claim-verdict unsupported">unsupported</span>
              <code>{item.value || "?"}</code>
              <span className="muted-copy">{item.kind || "unclassified claim"}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function ToolsPanel({ tools }: { tools: ToolsSection | undefined }) {
  return (
    <section className="trace-panel" aria-labelledby="toolsTitle">
      <div className="admin-panel-header">
        <h3 id="toolsTitle">Tool policy and execution</h3>
        <span className="muted-copy">stored in tools / verdicts</span>
      </div>
      {(tools?.toolCalls ?? []).length === 0 &&
      (tools?.committed ?? []).length === 0 &&
      (tools?.toolResults ?? []).length === 0 ? (
        <p className="muted-copy">No tool activity recorded.</p>
      ) : (
        <>
          <table className="trace-table">
            <thead>
              <tr>
                <th scope="col">Call</th>
                <th scope="col">Tool</th>
                <th scope="col">Result</th>
              </tr>
            </thead>
            <tbody>
              {(tools?.toolCalls ?? []).map((call) => {
                const result = (tools?.toolResults ?? []).find(
                  (item) => item.callId === call.callId
                );
                const errorCode = result ? toolErrorCode(result.result) : null;
                return (
                  <tr key={call.callId ?? call.name}>
                    <td>
                      <code>{call.callId ?? "?"}</code>
                    </td>
                    <td>{call.name ?? "?"}</td>
                    <td>
                      {errorCode ? (
                        <span className="tool-error">error {errorCode}</span>
                      ) : result ? (
                        <span className="tool-ok">ok</span>
                      ) : (
                        <span className="muted-copy">no result recorded</span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {(tools?.committed ?? []).length > 0 && (
            <div className="prompt-excluded">
              <h4>Committed effects (idempotency keys)</h4>
              <ul>
                {(tools?.committed ?? []).map((action, index) => (
                  <li key={index}>
                    {action.action ?? "?"} · {action.reference ?? "?"} · key{" "}
                    <code>{(action.idempotencyKey ?? "").slice(0, 12)}</code> · replayed:{" "}
                    {action.replayed ? "yes" : "no"}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </>
      )}
    </section>
  );
}

function ReplayPanel({
  replay,
  replayTrials,
  isReplaying,
  isReplayingTrials,
  error,
  onReplay,
  onReplayTrials
}: {
  replay: ReplayResult | null;
  replayTrials: ReplayTrialsResult | null;
  isReplaying: boolean;
  isReplayingTrials: boolean;
  error: string | null;
  onReplay: () => void;
  onReplayTrials: () => void;
}) {
  const replayData = replayTrials ?? replay;
  const hasResult = replayData !== null;
  const shownComponents = hasResult ? replayData.components : [];
  const shownOriginal = hasResult ? replayData.original : null;
  const shownChanged = hasResult ? replayData.manifestChanged : false;

  return (
    <section className="trace-panel" aria-labelledby="replayTitle">
      <div className="admin-panel-header">
        <h3 id="replayTitle">Safe replay</h3>
        <span className="muted-copy">audited · current model · no tools</span>
      </div>
      {!hasResult ? (
        <>
          <p className="muted-copy">
            Replays the stored prompt through the current model with no tools, so nothing
            domain-effectful can be touched. Each replay is an audited model call.
          </p>
          {error && (
            <p className="admin-alert" role="alert">
              {error}
            </p>
          )}
          <p>
            <button
              type="button"
              className="ghost-button"
              onClick={onReplay}
              disabled={isReplaying}
            >
              {isReplaying ? "Replaying…" : "Run one safe replay"}
            </button>{" "}
            <button
              type="button"
              className="ghost-button"
              onClick={onReplayTrials}
              disabled={isReplayingTrials}
            >
              {isReplayingTrials ? "Running 3 trials…" : "Run 3 trials"}
            </button>
          </p>
        </>
      ) : (
        <>
          {shownChanged ? (
            <p className="replay-warning" role="note">
              The components this turn ran under differ from what this deployment serves now. The
              replay ran under the current components, marked below.
            </p>
          ) : (
            <p className="replay-safe" role="note">
              This deployment serves the same components the turn ran under.
            </p>
          )}
          {replayTrials ? (
            <>
              <p className="replay-warning" role="note">
                {replayTrials.trials.length} bounded repeated trials — each trial holds{" "}
                {replayTrials.constant} constant and varies only {replayTrials.variable}. Multiple
                trials show variance but are still labelled stochastic: they are observations, never
                a proof.
              </p>
              <div className="replay-outputs">
                <div>
                  <h4>Original output</h4>
                  <pre>{shownOriginal?.outputRaw || "—"}</pre>
                </div>
                {replayTrials.trials.map((trial) => (
                  <div key={trial.trialIndex}>
                    <h4>
                      Trial {trial.trialIndex + 1} ({trial.modelName || "current model"})
                    </h4>
                    <pre>{trial.outputRaw || "—"}</pre>
                  </div>
                ))}
              </div>
            </>
          ) : replay ? (
            <>
              <p className="replay-warning" role="note">
                A single replayed trial is stochastic: it is an observation, never a proof. The
                original record is untouched.
              </p>
              <div className="replay-outputs">
                <div>
                  <h4>Original output</h4>
                  <pre>{replay.original.outputRaw || "—"}</pre>
                </div>
                <div>
                  <h4>Replayed output ({replay.replayed.modelName || "current model"})</h4>
                  <pre>{replay.replayed.outputRaw || "—"}</pre>
                </div>
              </div>
            </>
          ) : null}
          <table className="trace-table">
            <thead>
              <tr>
                <th scope="col">Component</th>
                <th scope="col">Stored (original)</th>
                <th scope="col">Current</th>
              </tr>
            </thead>
            <tbody>
              {shownComponents.map((component) => (
                <tr key={component.name} className={component.changed ? "manifest-changed" : ""}>
                  <td>{component.name}</td>
                  <td>
                    <code>{component.stored || "—"}</code>
                  </td>
                  <td>
                    <code>{component.current || "—"}</code>
                    {component.changed && <span className="uncertain-chip">changed</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </section>
  );
}
