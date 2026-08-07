/**
 * Admin backend transport.
 *
 * Every response can be a 401: the gateway redirects unauthenticated *page*
 * requests to the login flow but answers API requests with a plain 401, so the
 * console has to recognise a lost session itself and send the browser back
 * through the gateway rather than render an empty console.
 */

import type { SessionDetail, SessionSummary, TenantSummary } from "src/admin/types";
import type { ReviewDetail, ReviewDiagnosis, ReviewSummary } from "src/admin/reviewTypes";
import type {
  GoldCase,
  ReplayResult,
  TraceContent,
  TraceRead,
  TraceSearchFilters,
  TraceSearchRecord
} from "src/admin/traceTypes";

export class UnauthorizedError extends Error {
  constructor() {
    super("The admin session has expired.");
    this.name = "UnauthorizedError";
  }
}

function resolveAdminApiBaseUrl(): string {
  const configured =
    window.CHAT_API_BASE_URL ??
    document.querySelector<HTMLScriptElement>("script[data-api-base-url]")?.dataset.apiBaseUrl ??
    document.body.dataset.apiBaseUrl ??
    "";
  if (configured.trim()) {
    return configured.trim().replace(/\/+$/, "");
  }
  return window.location.protocol === "file:" ? "http://127.0.0.1:8000" : "";
}

export class AdminApi {
  readonly baseUrl: string;
  private csrfToken: string | null = null;

  constructor(baseUrl: string = resolveAdminApiBaseUrl()) {
    this.baseUrl = baseUrl;
  }

  private async request(path: string, init?: RequestInit): Promise<Response> {
    const response = await fetch(`${this.baseUrl}${path}`, init);
    if (response.status === 401) throw new UnauthorizedError();
    return response;
  }

  /** @throws {UnauthorizedError} when the admin session has expired. */
  async tenants(): Promise<TenantSummary[]> {
    const response = await this.request("/api/admin/tenants");
    if (!response.ok) throw new Error(`Tenant list failed with ${response.status}`);
    const payload = (await response.json()) as { tenants?: TenantSummary[] };
    return payload.tenants ?? [];
  }

  /** @throws {UnauthorizedError} when the admin session has expired. */
  async sessions(tenantId: string): Promise<SessionSummary[]> {
    const response = await this.request(
      `/api/admin/chats?tenant_id=${encodeURIComponent(tenantId)}`
    );
    if (!response.ok) throw new Error(`Chat list failed with ${response.status}`);
    const payload = (await response.json()) as { sessions?: SessionSummary[] };
    return payload.sessions ?? [];
  }

  /** Returns null when the session has since been removed. */
  async session(sessionId: string, tenantId: string): Promise<SessionDetail | null> {
    const response = await this.request(
      `/api/admin/chats/${encodeURIComponent(sessionId)}?tenant_id=${encodeURIComponent(tenantId)}`
    );
    if (!response.ok) return null;
    const payload = (await response.json()) as { session?: SessionDetail };
    return payload.session ?? null;
  }

  async sendStaffMessage(sessionId: string, tenantId: string, content: string): Promise<void> {
    const response = await this.request(
      `/api/admin/chats/${encodeURIComponent(sessionId)}/messages`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": await this.csrf() },
        body: JSON.stringify({ tenant_id: tenantId, content })
      }
    );
    if (!response.ok) throw new Error(`Sending the staff message failed with ${response.status}`);
  }

  /**
   * The FEAT-015 attribution surface: content-free results only. The record
   * itself arrives through `trace`, whose read is audited per record.
   *
   * @throws {UnauthorizedError} when the admin session has expired.
   */
  async searchTraces(tenantId: string, filters: TraceSearchFilters): Promise<TraceSearchRecord[]> {
    const params = new URLSearchParams({
      tenant_id: tenantId,
      reason: "quality_review",
      limit: "100"
    });
    if (filters.since) params.set("since", filters.since);
    if (filters.until) params.set("until", filters.until);
    if (filters.outcome) params.set("outcome", filters.outcome);
    if (filters.cause) params.set("cause", filters.cause);
    if (filters.diagnosisStatus) params.set("diagnosis_status", filters.diagnosisStatus);
    if (filters.manifestHash) params.set("manifest_hash", filters.manifestHash);
    const response = await this.request(`/api/admin/traces?${params}`);
    if (!response.ok) throw new Error(`Trace search failed with ${response.status}`);
    const payload = (await response.json()) as { records?: unknown[] };
    return (payload.records ?? []).map((wire) =>
      searchRecordFromWire(wire as Record<string, unknown>)
    );
  }

  /**
   * One full content-bearing turn record. Every call is RBAC-gated and
   * audited server-side with this read's reason.
   *
   * @throws {UnauthorizedError} when the admin session has expired.
   */
  async trace(turnId: string, tenantId: string): Promise<TraceRead | null> {
    const response = await this.request(
      `/api/admin/traces/${encodeURIComponent(turnId)}?tenant_id=${encodeURIComponent(tenantId)}&reason=quality_review`
    );
    if (!response.ok) return null;
    return traceReadFromWire((await response.json()) as Record<string, unknown>);
  }

  /**
   * One safe replay: the stored prompt through the current model, no tools.
   * Launches an audited model call, so it carries the CSRF token like every
   * admin mutation.
   *
   * @throws {UnauthorizedError} when the admin session has expired.
   */
  async replayTrace(turnId: string, tenantId: string): Promise<ReplayResult> {
    const response = await this.request(
      `/api/admin/traces/${encodeURIComponent(turnId)}/replay?tenant_id=${encodeURIComponent(tenantId)}&reason=quality_review`,
      { method: "POST", headers: { "X-CSRF-Token": await this.csrf() } }
    );
    if (!response.ok) throw new Error(`Replay failed with ${response.status}`);
    return replayFromWire((await response.json()) as Record<string, unknown>);
  }

  /** The reviewer-labelled gold cases for one tenant (trace-read gated). */
  async goldCases(tenantId: string): Promise<GoldCase[]> {
    const response = await this.request(
      `/api/admin/traces/gold-cases?tenant_id=${encodeURIComponent(tenantId)}&reason=quality_review`
    );
    if (!response.ok) throw new Error(`Gold cases failed with ${response.status}`);
    const payload = (await response.json()) as { cases?: unknown[] };
    return (payload.cases ?? []).map((wire) => goldCaseFromWire(obj(wire)));
  }

  /**
   * The FEAT-008 queue: content-free entries only, highest priority first.
   *
   * @throws {UnauthorizedError} when the admin session has expired.
   */
  async listReviews(tenantId: string, status?: string): Promise<ReviewSummary[]> {
    const params = new URLSearchParams({
      tenant_id: tenantId,
      reason: "quality_review",
      limit: "200"
    });
    if (status) params.set("review_status", status);
    const response = await this.request(`/api/admin/reviews?${params}`);
    if (!response.ok) throw new Error(`Review list failed with ${response.status}`);
    const payload = (await response.json()) as { reviews?: unknown[] };
    return (payload.reviews ?? []).map((wire) => reviewSummaryFromWire(obj(wire)));
  }

  /** One content-bearing review entry (trace-read gated and audited). */
  async reviewDetail(reviewId: string, tenantId: string): Promise<ReviewDetail | null> {
    const response = await this.request(
      `/api/admin/reviews/${encodeURIComponent(reviewId)}?tenant_id=${encodeURIComponent(tenantId)}&reason=quality_review`
    );
    if (!response.ok) return null;
    return reviewDetailFromWire((await response.json()) as Record<string, unknown>);
  }

  /** Mark an open case as in review by the current operator. */
  async takeReview(reviewId: string, tenantId: string): Promise<void> {
    const response = await this.request(
      `/api/admin/reviews/${encodeURIComponent(reviewId)}/take?tenant_id=${encodeURIComponent(tenantId)}`,
      { method: "POST", headers: { "X-CSRF-Token": await this.csrf() } }
    );
    if (!response.ok) throw new Error(`Taking the review failed with ${response.status}`);
  }

  /** Record the reviewer's decision, correction, and fix. */
  async submitReview(
    reviewId: string,
    tenantId: string,
    body: {
      verdict: "confirmed" | "rejected" | "amended";
      status: "awaiting_fix" | "rejected";
      note?: string;
      correctedAnswer?: string;
      proposedFix?: string;
      diagnoses: {
        automaticIndex: number | null;
        relationship: "confirms" | "rejects" | "amends" | "adds";
        cause: string;
        stage: string;
        role: "primary" | "contributing";
        status: "detected" | "suspected" | "confirmed" | "inconclusive";
        confidence: "low" | "medium" | "high";
        note?: string;
      }[];
    }
  ): Promise<void> {
    const response = await this.request(
      `/api/admin/reviews/${encodeURIComponent(reviewId)}/review?tenant_id=${encodeURIComponent(tenantId)}`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": await this.csrf() },
        body: JSON.stringify({
          tenant_id: tenantId,
          verdict: body.verdict,
          status: body.status,
          note: body.note,
          corrected_answer: body.correctedAnswer,
          proposed_fix: body.proposedFix,
          diagnoses: body.diagnoses.map((diagnosis) => ({
            automatic_index: diagnosis.automaticIndex,
            relationship: diagnosis.relationship,
            cause: diagnosis.cause,
            stage: diagnosis.stage,
            role: diagnosis.role,
            status: diagnosis.status,
            confidence: diagnosis.confidence,
            note: diagnosis.note
          }))
        })
      }
    );
    if (!response.ok) throw new Error(`Submitting the review failed with ${response.status}`);
  }

  /** Promote a reviewed, anonymized case into the evaluation dataset. */
  async promoteReview(reviewId: string, tenantId: string): Promise<string> {
    const response = await this.request(
      `/api/admin/reviews/${encodeURIComponent(reviewId)}/promote?tenant_id=${encodeURIComponent(tenantId)}`,
      { method: "POST", headers: { "X-CSRF-Token": await this.csrf() } }
    );
    if (!response.ok) throw new Error(`Promoting the review failed with ${response.status}`);
    const payload = (await response.json()) as { case_id?: string };
    return payload.case_id ?? "";
  }

  private async csrf(): Promise<string> {
    if (this.csrfToken) return this.csrfToken;
    const response = await this.request("/api/admin/csrf-token");
    if (!response.ok) return "";
    const payload = (await response.json()) as { csrf_token?: string };
    this.csrfToken = payload.csrf_token ?? "";
    return this.csrfToken;
  }
}

function str(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function strOrNull(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function searchRecordFromWire(wire: Record<string, unknown>): TraceSearchRecord {
  return {
    turnId: str(wire.turn_id),
    sessionId: str(wire.session_id),
    traceId: strOrNull(wire.trace_id),
    recordedAt: str(wire.recorded_at),
    outcome: str(wire.outcome),
    componentManifestHash: str(wire.component_manifest_hash),
    diagnosisCauses: Array.isArray(wire.diagnosis_causes) ? wire.diagnosis_causes.map(str) : [],
    diagnosisStatuses: Array.isArray(wire.diagnosis_statuses)
      ? wire.diagnosis_statuses.map(str)
      : [],
    turnIndex: typeof wire.turn_index === "number" ? wire.turn_index : 0,
    traceSchemaVersion: str(wire.trace_schema_version)
  };
}

function traceReadFromWire(wire: Record<string, unknown>): TraceRead {
  return {
    turnId: str(wire.turn_id),
    tenantId: str(wire.tenant_id),
    sessionId: str(wire.session_id),
    traceId: strOrNull(wire.trace_id),
    recordedAt: str(wire.recorded_at),
    content: traceContentFromWire(obj(wire.content)),
    projections: Array.isArray(wire.projections)
      ? (wire.projections as TraceRead["projections"])
      : []
  };
}

/** The deep snake→camel mapping of the OBS-004 content object. The wire
 * format is the store's; the UI contract is camelCase, so the client owns
 * the mapping and the panels read typed fields only. */
function traceContentFromWire(wire: Record<string, unknown>): TraceContent {
  const prompt = obj(wire.prompt);
  const retrieval = obj(wire.retrieval);
  const routing = obj(wire.routing);
  const tools = obj(wire.tools);
  const verdicts = obj(wire.verdicts);
  const output = obj(wire.output);
  const outcome = obj(wire.outcome);
  const model = obj(wire.model);
  return {
    schemaVersion: str(wire.schema_version),
    turnIndex: typeof wire.turn_index === "number" ? wire.turn_index : undefined,
    manifestHash: str(wire.manifest_hash),
    routing: routing
      ? {
          ...routing,
          rule: str(routing.rule),
          intent: str(routing.intent),
          policyVersion: str(routing.policy_version),
          candidates: list(routing.candidates).map((candidate) => ({
            ...candidate,
            intent: str(candidate.intent),
            matchedSignals: listStr(candidate.matched_signals)
          }))
        }
      : null,
    retrieval: retrieval
      ? {
          query: str(retrieval.query),
          sufficient: retrieval.sufficient === true,
          retrieverVersion: str(retrieval.retriever_version),
          reranker: strOrNull(retrieval.reranker),
          minEvidenceScore:
            typeof retrieval.min_evidence_score === "number" ? retrieval.min_evidence_score : null,
          embeddingModel: str(retrieval.embedding_model),
          generationId: strOrNull(retrieval.generation_id),
          filters: obj(retrieval.filters),
          budget: obj(retrieval.budget),
          parameters: obj(retrieval.parameters),
          candidates: list(retrieval.candidates).map((candidate) => ({
            ...candidate,
            sourceId: str(candidate.source_id),
            generationId: strOrNull(candidate.generation_id),
            embeddingModel: str(candidate.embedding_model)
          })),
          evidence: list(retrieval.evidence).map((item) => ({
            ...item,
            sourceId: str(item.source_id),
            generationId: strOrNull(item.generation_id)
          }))
        }
      : null,
    prompt: prompt
      ? {
          templateRef: str(prompt.template_ref),
          contentHash: str(prompt.content_hash),
          bindings: obj(prompt.bindings) as Record<string, string>,
          excluded: list(prompt.excluded).map((item) => ({
            ...item,
            kind: str(item.kind),
            reference: str(item.reference),
            reason: str(item.reason)
          })),
          messages: list(prompt.messages).map((message) => ({
            role: str(message.role),
            content: str(message.content),
            toolCallId: strOrNull(message.tool_call_id),
            segments: list(message.segments).map((segment) => ({
              segmentId: str(segment[0]),
              region: str(segment[1]),
              text: str(segment[2])
            }))
          }))
        }
      : null,
    model: model ? { name: str(model.name), usage: obj(model.usage) } : undefined,
    output: output
      ? {
          answer: str(output.answer),
          raw: str(output.raw),
          claims: listStr(output.claims)
        }
      : undefined,
    verdicts: verdicts
      ? {
          citations: list(verdicts.citations).map((citation) => ({
            sourceId: str(citation.source_id),
            title: str(citation.title)
          })),
          citationInvalid: listStr(verdicts.citation_invalid),
          refusedTools: listStr(verdicts.refused_tools),
          claimsInvalid: list(verdicts.claims_invalid).map((item) => ({
            claim: str(item.claim),
            reason: str(item.reason)
          }))
        }
      : undefined,
    tools: tools
      ? {
          toolCalls: list(tools.tool_calls).map((call) => ({
            callId: str(call.call_id),
            name: str(call.name),
            arguments: obj(call.arguments)
          })),
          toolResults: list(tools.tool_results).map((result) => ({
            callId: str(result.call_id),
            result: str(result.result)
          })),
          committed: list(tools.committed).map((action) => ({
            action: str(action.action),
            reference: str(action.reference),
            replayed: action.replayed === true,
            idempotencyKey: str(action.idempotency_key)
          }))
        }
      : undefined,
    outcome: outcome
      ? {
          status: str(outcome.status),
          rounds: typeof outcome.rounds === "number" ? outcome.rounds : undefined,
          ...(strOrNull(outcome.failure) === null ? {} : { failure: strOrNull(outcome.failure) })
        }
      : undefined,
    componentManifest: obj(wire.component_manifest),
    executedGraph: executedGraphFromWire(wire.executed_graph),
    diagnoses: list(wire.diagnoses).map((diagnosis) => ({
      cause: str(diagnosis.cause),
      stage: str(diagnosis.stage),
      role: str(diagnosis.role),
      status: str(diagnosis.status),
      confidence: str(diagnosis.confidence),
      evidence: listStr(diagnosis.evidence),
      detectorVersion: str(diagnosis.detector_version)
    }))
  };
}

/** The `OBS-006` executed-graph section: node events are content-free, so the
 * UI contract maps the wire shape directly. */
function executedGraphFromWire(wire: unknown): TraceContent["executedGraph"] {
  const section = obj(wire);
  if (!Array.isArray(section.nodes)) return undefined;
  return {
    runKind: section.run_kind === "resume" ? "resume" : "send",
    startedAt: strOrNull(section.started_at),
    endedAt: strOrNull(section.ended_at),
    durationMs: typeof section.duration_ms === "number" ? section.duration_ms : null,
    nodes: section.nodes.map((raw) => {
      const node = obj(raw);
      return {
        name: str(node.name),
        attempt: typeof node.attempt === "number" ? node.attempt : 1,
        edge: strOrNull(node.edge),
        status: node.status === "error" ? "error" : "ok",
        interrupted: node.interrupted === true,
        replayed: node.replayed === true,
        startedAt: strOrNull(node.started_at),
        endedAt: strOrNull(node.ended_at),
        durationMs: typeof node.duration_ms === "number" ? node.duration_ms : null
      };
    }),
    edges: Array.isArray(section.edges)
      ? section.edges.map((raw) => {
          const edge = obj(raw);
          return {
            source: str(edge.source),
            target: str(edge.target),
            label: strOrNull(edge.label)
          };
        })
      : []
  };
}

function obj(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function listStr(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string")
    : [];
}

function list(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value)
    ? value.filter(
        (item): item is Record<string, unknown> => typeof item === "object" && item !== null
      )
    : [];
}

function replayFromWire(wire: Record<string, unknown>): ReplayResult {
  const original = obj(wire.original);
  const replayed = obj(wire.replayed);
  return {
    turnId: str(wire.turn_id),
    recordedAt: str(wire.recorded_at),
    manifestHash: str(wire.manifest_hash),
    currentManifestHash: strOrNull(wire.current_manifest_hash),
    manifestChanged: wire.manifest_changed === true,
    stochastic: wire.stochastic === true,
    components: Array.isArray(wire.components)
      ? (wire.components as ReplayResult["components"])
      : [],
    original: {
      contentHash: str(original.content_hash),
      modelName: str(original.model_name),
      outputRaw: str(original.output_raw)
    },
    replayed: {
      contentHash: str(replayed.content_hash),
      modelName: str(replayed.model_name),
      outputRaw: str(replayed.output_raw)
    }
  };
}

function goldCaseFromWire(wire: Record<string, unknown>): GoldCase {
  return {
    caseId: str(wire.case_id),
    tenantId: str(wire.tenant_id),
    scenario: strOrNull(wire.scenario),
    query: str(wire.query),
    goldChunks: list(wire.gold_chunks).map((chunk) => ({
      sourceId: str(chunk.source_id),
      text: str(chunk.text)
    }))
  };
}

function reviewSummaryFromWire(wire: Record<string, unknown>): ReviewSummary {
  return {
    reviewId: str(wire.review_id),
    turnId: str(wire.turn_id),
    sessionId: strOrNull(wire.session_id),
    recordedAt: strOrNull(wire.recorded_at),
    outcome: str(wire.outcome),
    source: str(wire.source),
    status: str(wire.status),
    priority: typeof wire.priority === "number" ? wire.priority : 0,
    recurrence: typeof wire.recurrence === "number" ? wire.recurrence : 1,
    manifestHash: str(wire.manifest_hash),
    committedActions: wire.committed_actions === true,
    novelManifest: wire.novel_manifest === true,
    caseId: strOrNull(wire.case_id),
    verdict: strOrNull(wire.verdict),
    diagnosisCauses: Array.isArray(wire.diagnosis_causes) ? wire.diagnosis_causes.map(str) : [],
    diagnosisStatuses: Array.isArray(wire.diagnosis_statuses)
      ? wire.diagnosis_statuses.map(str)
      : [],
    closingEvalRunId: strOrNull(wire.closing_eval_run_id),
    closingEvalCaseId: strOrNull(wire.closing_eval_case_id),
    createdAt: str(wire.created_at),
    turnIndex: typeof wire.turn_index === "number" ? wire.turn_index : 0
  };
}

function reviewDiagnosisFromWire(wire: Record<string, unknown>): ReviewDiagnosis {
  return {
    diagnosisId: str(wire.diagnosis_id),
    reviewId: str(wire.review_id),
    relationship: str(wire.relationship),
    automaticIndex: typeof wire.automatic_index === "number" ? wire.automatic_index : null,
    cause: str(wire.cause),
    stage: str(wire.stage),
    role: str(wire.role),
    status: str(wire.status),
    confidence: str(wire.confidence),
    evidence: Array.isArray(wire.evidence) ? wire.evidence.map(str) : [],
    note: strOrNull(wire.note),
    createdAt: str(wire.created_at)
  };
}

function reviewDetailFromWire(wire: Record<string, unknown>): ReviewDetail {
  const review = obj(wire.review);
  const feedback = obj(wire.feedback);
  return {
    review: reviewSummaryFromWire(review),
    feedback:
      feedback && Object.keys(feedback).length > 0
        ? {
            turnId: str(feedback.turn_id),
            rating: str(feedback.rating),
            reason: strOrNull(feedback.reason),
            createdAt: str(feedback.created_at)
          }
        : null,
    reviewerSubject: strOrNull(wire.reviewer_subject),
    reviewedAt: strOrNull(wire.reviewed_at),
    verdictNote: strOrNull(wire.verdict_note),
    correctedAnswer: strOrNull(wire.corrected_answer),
    proposedFix: strOrNull(wire.proposed_fix),
    closingEvalPassedAt: strOrNull(wire.closing_eval_passed_at),
    diagnoses: Array.isArray(wire.diagnoses)
      ? wire.diagnoses.map((row) => reviewDiagnosisFromWire(obj(row)))
      : []
  };
}

/**
 * Send the browser back through the gateway, which starts the OIDC flow.
 *
 * Development has no auth proxy, so a `file:` page stops instead of looping.
 */
export function redirectToLogin(): void {
  if (window.location.protocol === "file:") return;
  window.location.href = "/admin/";
}
