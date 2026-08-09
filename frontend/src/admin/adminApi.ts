/**
 * Admin backend transport.
 *
 * Every response can be a 401: the gateway redirects unauthenticated *page*
 * requests to the login flow but answers API requests with a plain 401, so the
 * console has to recognise a lost session itself and send the browser back
 * through the gateway rather than render an empty console.
 */

import { OUTCOMES } from "src/admin/types";
import type {
  AdminMessage,
  Outcome,
  SessionDetail,
  SessionSummary,
  TenantSummary
} from "src/admin/types";
import type { ReviewDetail, ReviewDiagnosis, ReviewSummary } from "src/admin/reviewTypes";
import type {
  GoldCase,
  ReplayResult,
  ReplayTrialsResult,
  ReplayRetrievalResult,
  ReplayTemplateResult,
  TraceContent,
  TraceRead,
  TraceSearchFilters,
  TraceSearchRecord
} from "src/admin/traceTypes";
import type {
  AuditEventRow,
  AuditFilters,
  MembershipRole,
  PermissionsView,
  TraceGrant
} from "src/admin/accessTypes";
import type {
  KnowledgeDocument,
  KnowledgeFinding,
  KnowledgePreview,
  KnowledgeSource,
  KnowledgeVersion
} from "src/admin/knowledgeTypes";
import type { HandoffSummary } from "src/admin/handoffTypes";

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
  private tenantNames: Map<string, string> = new Map();

  constructor(baseUrl: string = resolveAdminApiBaseUrl()) {
    this.baseUrl = baseUrl;
  }

  private async request(path: string, init?: RequestInit): Promise<Response> {
    const response = await fetch(`${this.baseUrl}${path}`, init);
    if (response.status === 401) throw new UnauthorizedError();
    return response;
  }

  private async apiError(response: Response, fallback: string): Promise<Error> {
    try {
      const body = (await response.json()) as Record<string, unknown>;
      const code = typeof body.code === "string" ? body.code : null;
      const detail = typeof body.detail === "string" ? body.detail : "";
      if (code && detail) return new Error(`${detail} (code: ${code})`);
      if (code) return new Error(`${fallback} (code: ${code})`);
    } catch {
      // response body is not JSON or unreadable
    }
    return new Error(`${fallback} with ${response.status}`);
  }

  /** @throws {UnauthorizedError} when the admin session has expired. */
  async tenants(): Promise<TenantSummary[]> {
    const response = await this.request("/api/admin/tenants");
    if (!response.ok) throw new Error(`Tenant list failed with ${response.status}`);
    const payload = (await response.json()) as { tenants?: unknown[] };
    const rows = (payload.tenants ?? []).map((wire) =>
      tenantSummaryFromWire(wire as Record<string, unknown>)
    );
    // Conversation rows name their tenant by id; the console shows the display
    // name. This is the only response that carries both.
    this.tenantNames = new Map(rows.map((row) => [row.tenantId, row.name]));
    return rows;
  }

  /** @throws {UnauthorizedError} when the admin session has expired. */
  async sessions(tenantId: string): Promise<SessionSummary[]> {
    const response = await this.request(
      `/api/admin/chats?tenant_id=${encodeURIComponent(tenantId)}`
    );
    if (!response.ok) throw new Error(`Chat list failed with ${response.status}`);
    const payload = (await response.json()) as { sessions?: unknown[] };
    return (payload.sessions ?? []).map((wire) =>
      sessionSummaryFromWire(wire as Record<string, unknown>, this.tenantNames)
    );
  }

  /** Returns null when the session has since been removed. */
  async session(sessionId: string, tenantId: string): Promise<SessionDetail | null> {
    const response = await this.request(
      `/api/admin/chats/${encodeURIComponent(sessionId)}?tenant_id=${encodeURIComponent(tenantId)}`
    );
    if (!response.ok) return null;
    const payload = (await response.json()) as {
      session?: Record<string, unknown>;
      messages?: unknown[];
    };
    if (!payload.session) return null;
    const summary = sessionSummaryFromWire(payload.session, this.tenantNames);
    const messages = (payload.messages ?? []).map((wire) =>
      adminMessageFromWire(wire as Record<string, unknown>)
    );
    const last = messages[messages.length - 1];
    return {
      ...summary,
      messageCount: messages.length,
      ...(last ? { lastMessage: { content: last.content } } : {}),
      messages
    };
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
    if (filters.generationId) params.set("generation_id", filters.generationId);
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
    if (!response.ok) throw await this.apiError(response, "Replay failed");
    return replayFromWire((await response.json()) as Record<string, unknown>);
  }

  /**
   * Bounded repeated trials: N replays of the stored prompt through the
   * current model, reported as an aggregate with an explicit stochastic label.
   *
   * @throws {UnauthorizedError} when the admin session has expired.
   */
  async replayTrials(
    turnId: string,
    tenantId: string,
    trials: number = 3
  ): Promise<ReplayTrialsResult> {
    const response = await this.request(
      `/api/admin/traces/${encodeURIComponent(turnId)}/replay/trials?tenant_id=${encodeURIComponent(tenantId)}&reason=quality_review`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": await this.csrf()
        },
        body: JSON.stringify({ trials })
      }
    );
    if (!response.ok) throw await this.apiError(response, "Replay trials failed");
    return replayTrialsFromWire((await response.json()) as Record<string, unknown>);
  }

  /**
   * Immutable-index retrieval replay with optional gold-evidence substitution.
   *
   * @throws {UnauthorizedError} when the admin session has expired.
   */
  async replayRetrieval(
    turnId: string,
    tenantId: string,
    goldEvidence?: { sourceId: string; text: string }[]
  ): Promise<ReplayRetrievalResult> {
    const response = await this.request(
      `/api/admin/traces/${encodeURIComponent(turnId)}/replay/retrieval?tenant_id=${encodeURIComponent(tenantId)}&reason=quality_review`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": await this.csrf()
        },
        body: JSON.stringify({ gold_evidence: goldEvidence ?? null })
      }
    );
    if (!response.ok) throw await this.apiError(response, "Replay retrieval failed");
    return replayRetrievalFromWire((await response.json()) as Record<string, unknown>);
  }

  /**
   * Template-version-pinned replay: model and evidence constant, template pinned.
   *
   * @throws {UnauthorizedError} when the admin session has expired.
   */
  async replayTemplate(
    turnId: string,
    tenantId: string,
    templateVersion?: number
  ): Promise<ReplayTemplateResult> {
    const response = await this.request(
      `/api/admin/traces/${encodeURIComponent(turnId)}/replay/template?tenant_id=${encodeURIComponent(tenantId)}&reason=quality_review`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": await this.csrf()
        },
        body: JSON.stringify({ template_version: templateVersion ?? null })
      }
    );
    if (!response.ok) throw await this.apiError(response, "Replay template failed");
    return replayTemplateFromWire((await response.json()) as Record<string, unknown>);
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

  /** The FEAT-001 knowledge tree: sources, documents, and versions. */
  async knowledge(tenantId: string): Promise<KnowledgeSource[]> {
    const response = await this.request(
      `/api/admin/knowledge?tenant_id=${encodeURIComponent(tenantId)}&limit=200`
    );
    if (!response.ok) throw new Error(`Knowledge list failed with ${response.status}`);
    const payload = (await response.json()) as { sources?: unknown[] };
    return (payload.sources ?? []).map((wire) => knowledgeSourceFromWire(obj(wire)));
  }

  /** Create a source under the caller's tenant (idempotent per name). */
  async createKnowledgeSource(
    tenantId: string,
    body: { domain: string; kind: string; displayName: string }
  ): Promise<KnowledgeSource> {
    const response = await this.request(`/api/admin/knowledge/sources`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": await this.csrf() },
      body: JSON.stringify({
        tenant_id: tenantId,
        domain: body.domain,
        kind: body.kind,
        display_name: body.displayName
      })
    });
    if (!response.ok) throw new Error(`Creating the source failed with ${response.status}`);
    return knowledgeSourceFromWire(obj(await response.json()));
  }

  /** Withdraw or restore every document under a source at once. */
  async setSourceEnabled(
    sourceId: string,
    tenantId: string,
    enabled: boolean
  ): Promise<KnowledgeSource> {
    const response = await this.request(
      `/api/admin/knowledge/sources/${encodeURIComponent(sourceId)}/enabled`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": await this.csrf() },
        body: JSON.stringify({ tenant_id: tenantId, enabled })
      }
    );
    if (!response.ok) throw new Error(`Updating the source failed with ${response.status}`);
    return knowledgeSourceFromWire(obj(await response.json()));
  }

  /** Stage a new document revision under one source. */
  async uploadKnowledge(
    tenantId: string,
    sourceId: string,
    file: File,
    body: { externalKey: string; title: string }
  ): Promise<{ versionId: string; documentId: string; revision: number }> {
    const form = new FormData();
    form.set("tenant_id", tenantId);
    form.set("source_id", sourceId);
    form.set("external_key", body.externalKey);
    form.set("title", body.title);
    form.set("file", file, file.name);
    const response = await this.request("/api/admin/knowledge/uploads", {
      method: "POST",
      headers: { "X-CSRF-Token": await this.csrf() },
      body: form
    });
    if (!response.ok) throw new Error(`Uploading the document failed with ${response.status}`);
    const payload = (await response.json()) as {
      version_id?: string;
      document_id?: string;
      revision?: number;
    };
    return {
      versionId: payload.version_id ?? "",
      documentId: payload.document_id ?? "",
      revision: payload.revision ?? 0
    };
  }

  /** Parse one version's stored bytes into a bounded preview. */
  async previewVersion(versionId: string, tenantId: string): Promise<KnowledgePreview | null> {
    const response = await this.request(
      `/api/admin/knowledge/versions/${encodeURIComponent(versionId)}/preview?tenant_id=${encodeURIComponent(tenantId)}`
    );
    if (!response.ok) return null;
    const payload = (await response.json()) as Record<string, unknown>;
    return {
      versionId: str(payload.version_id),
      documentId: str(payload.document_id),
      title: str(payload.title),
      mediaType: str(payload.media_type),
      parserVersion: str(payload.parser_version),
      chunkCount: typeof payload.chunk_count === "number" ? payload.chunk_count : 0,
      blocks: list(payload.blocks).map((block) => ({
        location: str(block.location),
        text: str(block.text)
      }))
    };
  }

  /** Mark a draft reviewed and publishable. */
  async approveVersion(versionId: string, tenantId: string): Promise<KnowledgeVersion> {
    return this.versionMutation(versionId, tenantId, "approve");
  }

  /** Make one approved version current and enqueue its ingestion job. */
  async publishVersion(
    versionId: string,
    tenantId: string
  ): Promise<{ version: KnowledgeVersion; jobId: string | null }> {
    const mutation = await this.versionMutationResponse(versionId, tenantId, "publish");
    return { version: mutation.version, jobId: strOrNull(mutation.job?.job_id) };
  }

  /** Re-run a version's ingestion job. */
  async reindexVersion(versionId: string, tenantId: string): Promise<KnowledgeVersion> {
    return this.versionMutation(versionId, tenantId, "reindex");
  }

  /** End the current version's effective window now. */
  async expireVersion(versionId: string, tenantId: string): Promise<KnowledgeVersion> {
    return this.versionMutation(versionId, tenantId, "expire");
  }

  /** Withdraw a document and every revision of it (a tombstone). */
  async deleteDocument(documentId: string, tenantId: string): Promise<void> {
    const response = await this.request(
      `/api/admin/knowledge/documents/${encodeURIComponent(documentId)}`,
      {
        method: "DELETE",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": await this.csrf() },
        body: JSON.stringify({ tenant_id: tenantId })
      }
    );
    if (!response.ok) throw new Error(`Deleting the document failed with ${response.status}`);
  }

  /** The tenant's open index-integrity findings, linked to source versions. */
  async knowledgeFindings(tenantId: string): Promise<KnowledgeFinding[]> {
    const response = await this.request(
      `/api/admin/knowledge/index-findings?tenant_id=${encodeURIComponent(tenantId)}&limit=200`
    );
    if (!response.ok) throw new Error(`Findings list failed with ${response.status}`);
    const payload = (await response.json()) as { findings?: unknown[] };
    return (payload.findings ?? []).map((wire) => knowledgeFindingFromWire(obj(wire)));
  }

  /** Run the detector and persist the tenant's current findings. */
  async runIntegrityCheck(tenantId: string): Promise<KnowledgeFinding[]> {
    const response = await this.request(
      `/api/admin/knowledge/index-integrity-check?tenant_id=${encodeURIComponent(tenantId)}`,
      { method: "POST", headers: { "X-CSRF-Token": await this.csrf() } }
    );
    if (!response.ok) throw new Error(`The integrity check failed with ${response.status}`);
    const payload = (await response.json()) as { findings?: unknown[] };
    return (payload.findings ?? []).map((wire) => knowledgeFindingFromWire(obj(wire)));
  }

  /**
   * The FEAT-016 audit trail: content-free rows for one tenant, newest first.
   * Every read is itself audited server-side. Returns null on a 404 — a tenant
   * this operator cannot administer is the same as one that does not exist.
   *
   * @throws {UnauthorizedError} when the admin session has expired.
   */
  async audit(tenantId: string, filters: AuditFilters): Promise<AuditEventRow[] | null> {
    const params = new URLSearchParams({ tenant_id: tenantId, limit: "200" });
    if (filters.since) params.set("since", filters.since);
    if (filters.until) params.set("until", filters.until);
    if (filters.action) params.set("action", filters.action);
    if (filters.principal) params.set("principal", filters.principal);
    const response = await this.request(`/api/admin/audit?${params}`);
    if (response.status === 404) return null;
    if (!response.ok) throw new Error(`Audit read failed with ${response.status}`);
    const payload = (await response.json()) as { events?: unknown[] };
    return (payload.events ?? []).map((wire) => auditEventFromWire(obj(wire)));
  }

  /**
   * The FEAT-016 permissions view: the tenant's live roles and trace-read
   * grants, as separate controls with grantors resolved. Returns null on a 404
   * like the audit read.
   *
   * @throws {UnauthorizedError} when the admin session has expired.
   */
  async permissions(tenantId: string): Promise<PermissionsView | null> {
    const response = await this.request(
      `/api/admin/permissions?tenant_id=${encodeURIComponent(tenantId)}`
    );
    if (response.status === 404) return null;
    if (!response.ok) throw new Error(`Permissions read failed with ${response.status}`);
    const payload = (await response.json()) as { roles?: unknown[]; grants?: unknown[] };
    return {
      roles: (payload.roles ?? []).map((wire) => membershipRoleFromWire(obj(wire))),
      grants: (payload.grants ?? []).map((wire) => traceGrantFromWire(obj(wire)))
    };
  }

  /** The FEAT-004 staff queue: open escalation tickets, oldest first. */
  async handoffs(tenantId: string): Promise<{ rows: HandoffSummary[]; operatorSubject: string }> {
    const response = await this.request(
      `/api/admin/handoffs?tenant_id=${encodeURIComponent(tenantId)}&limit=200`
    );
    if (!response.ok) throw new Error(`Handoff queue failed with ${response.status}`);
    const payload = (await response.json()) as {
      handoffs?: unknown[];
      operator_subject?: string;
    };
    return {
      rows: (payload.handoffs ?? []).map((wire) => handoffSummaryFromWire(obj(wire))),
      operatorSubject: payload.operator_subject ?? ""
    };
  }

  /** Take ownership of an unowned handoff; the race is decided in the database. */
  async acceptHandoff(handoffId: string, tenantId: string): Promise<HandoffSummary> {
    return this.handoffMutation(handoffId, tenantId, "accept");
  }

  /** Release an assigned handoff back to the queue and resume the assistant. */
  async releaseHandoff(handoffId: string, tenantId: string): Promise<HandoffSummary> {
    return this.handoffMutation(handoffId, tenantId, "release");
  }

  /** Close an open handoff and mark the conversation resolved. */
  async resolveHandoff(handoffId: string, tenantId: string): Promise<HandoffSummary> {
    return this.handoffMutation(handoffId, tenantId, "resolve");
  }

  private async handoffMutation(
    handoffId: string,
    tenantId: string,
    action: "accept" | "release" | "resolve"
  ): Promise<HandoffSummary> {
    const response = await this.request(
      `/api/admin/handoffs/${encodeURIComponent(handoffId)}/${action}?tenant_id=${encodeURIComponent(tenantId)}`,
      { method: "POST", headers: { "X-CSRF-Token": await this.csrf() } }
    );
    if (!response.ok) {
      const problem = (await response.json().catch(() => null)) as { code?: unknown } | null;
      if (problem && problem.code === "handoff_ownership_refused") {
        const error = new Error("Only the staff member who owns this conversation can do that.");
        error.name = "HandoffOwnershipError";
        throw error;
      }
      throw new Error(`The ${action} failed with ${response.status}`);
    }
    const payload = (await response.json()) as { handoff?: unknown };
    return handoffSummaryFromWire(obj(payload.handoff));
  }

  private async versionMutation(
    versionId: string,
    tenantId: string,
    action: "approve" | "reindex" | "expire"
  ): Promise<KnowledgeVersion> {
    const mutation = await this.versionMutationResponse(versionId, tenantId, action);
    return mutation.version;
  }

  private async versionMutationResponse(
    versionId: string,
    tenantId: string,
    action: "approve" | "publish" | "reindex" | "expire"
  ): Promise<{ version: KnowledgeVersion; job: { job_id?: string } | null }> {
    const response = await this.request(
      `/api/admin/knowledge/versions/${encodeURIComponent(versionId)}/${action}`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": await this.csrf() },
        body: JSON.stringify({ tenant_id: tenantId })
      }
    );
    if (!response.ok) throw new Error(`${action} failed with ${response.status}`);
    const payload = (await response.json()) as {
      version?: unknown;
      job?: { job_id?: string } | null;
    };
    return {
      version: knowledgeVersionFromWire(obj(payload.version)),
      job: payload.job ?? null
    };
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

/** Unix seconds from an ISO-8601 instant, which is what the time helpers take. */
function epochSeconds(value: unknown): number {
  const parsed = typeof value === "string" ? Date.parse(value) : NaN;
  return Number.isFinite(parsed) ? parsed / 1000 : 0;
}

function tenantSummaryFromWire(wire: Record<string, unknown>): TenantSummary {
  return {
    tenantId: str(wire.tenant_id),
    name: str(wire.name),
    role: str(wire.role)
  };
}

/**
 * The store's role vocabulary is wider than the transcript's: `visitor` and
 * `staff` are both rendered as the customer-facing side of the conversation,
 * and `source` is what tells a staff reply apart from a visitor's own words.
 */
function adminMessageFromWire(wire: Record<string, unknown>): AdminMessage {
  const role = str(wire.role);
  return {
    id: str(wire.message_id),
    role: role === "assistant" ? "assistant" : "user",
    ...(role === "staff" ? { source: "admin" as const } : {}),
    content: str(wire.content),
    createdAt: epochSeconds(wire.created_at)
  };
}

function sessionSummaryFromWire(
  wire: Record<string, unknown>,
  tenantNames: Map<string, string>
): SessionSummary {
  const tenantId = str(wire.tenant_id);
  const status = str(wire.status);
  const outcome = str(wire.outcome);
  return {
    sessionId: str(wire.session_id),
    // Falls back to the id so a tenant added since the console loaded its
    // membership list still names a row rather than rendering blank.
    tenantName: tenantNames.get(tenantId) ?? tenantId,
    active: status === "active",
    status,
    // `none` is the store's resting value and is not one of the console's
    // outcomes; leaving the key off lets `outcomeOf` apply its own default.
    ...(OUTCOMES.includes(outcome as Outcome) ? { outcome: outcome as Outcome } : {}),
    updatedAt: epochSeconds(wire.last_activity_at)
  };
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
    traceSchemaVersion: str(wire.trace_schema_version),
    sourceGenerationIds: Array.isArray(wire.source_generation_ids)
      ? wire.source_generation_ids.map(str)
      : []
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
          originalMessage: str(retrieval.original_message),
          resolvedQuery: strOrNull(retrieval.resolved_query),
          plan: obj(retrieval.plan),
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
    },
    elapsedSeconds: typeof wire.elapsed_seconds === "number" ? wire.elapsed_seconds : 0
  };
}

function replayTrialsFromWire(wire: Record<string, unknown>): ReplayTrialsResult {
  const original = obj(wire.original);
  return {
    turnId: str(wire.turn_id),
    recordedAt: str(wire.recorded_at),
    manifestHash: str(wire.manifest_hash),
    currentManifestHash: strOrNull(wire.current_manifest_hash),
    manifestChanged: wire.manifest_changed === true,
    stochastic: wire.stochastic === true,
    components: Array.isArray(wire.components)
      ? (wire.components as ReplayTrialsResult["components"])
      : [],
    original: {
      contentHash: str(original.content_hash),
      modelName: str(original.model_name),
      outputRaw: str(original.output_raw)
    },
    trials: list(wire.trials).map((trial) => ({
      trialIndex: typeof trial.trial_index === "number" ? trial.trial_index : 0,
      contentHash: str(trial.content_hash),
      modelName: str(trial.model_name),
      outputRaw: str(trial.output_raw)
    })),
    trialCount: typeof wire.trial_count === "number" ? wire.trial_count : 0,
    constant: str(wire.constant),
    variable: str(wire.variable),
    elapsedSeconds: typeof wire.elapsed_seconds === "number" ? wire.elapsed_seconds : 0
  };
}

function replayRetrievalFromWire(wire: Record<string, unknown>): ReplayRetrievalResult {
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
      ? (wire.components as ReplayRetrievalResult["components"])
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
    },
    generationAvailable: wire.generation_available === true,
    generationId: strOrNull(wire.generation_id),
    goldEvidenceCount: typeof wire.gold_evidence_count === "number" ? wire.gold_evidence_count : 0,
    constant: str(wire.constant),
    variable: str(wire.variable),
    elapsedSeconds: typeof wire.elapsed_seconds === "number" ? wire.elapsed_seconds : 0
  };
}

function replayTemplateFromWire(wire: Record<string, unknown>): ReplayTemplateResult {
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
      ? (wire.components as ReplayTemplateResult["components"])
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
    },
    templateRef: str(wire.template_ref),
    templateMatchesCurrent: wire.template_matches_current === true,
    constant: str(wire.constant),
    variable: str(wire.variable),
    elapsedSeconds: typeof wire.elapsed_seconds === "number" ? wire.elapsed_seconds : 0
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

function knowledgeSourceFromWire(wire: Record<string, unknown>): KnowledgeSource {
  return {
    sourceId: str(wire.source_id),
    tenantId: str(wire.tenant_id),
    domain: str(wire.domain),
    kind: str(wire.kind),
    displayName: str(wire.display_name),
    enabled: wire.enabled === true,
    documents: list(wire.documents).map((item) => knowledgeDocumentFromWire(item))
  };
}

function knowledgeDocumentFromWire(wire: Record<string, unknown>): KnowledgeDocument {
  return {
    documentId: str(wire.document_id),
    sourceId: str(wire.source_id),
    externalKey: str(wire.external_key),
    title: str(wire.title),
    deleted: wire.deleted === true,
    versions: list(wire.versions).map((item) => knowledgeVersionFromWire(item))
  };
}

function knowledgeVersionFromWire(wire: Record<string, unknown>): KnowledgeVersion {
  return {
    versionId: str(wire.version_id),
    revision: typeof wire.revision === "number" ? wire.revision : 0,
    state: str(wire.state),
    indexingState: str(wire.indexing_state),
    safetyState: str(wire.safety_state),
    visibility: str(wire.visibility),
    checksum: str(wire.checksum),
    byteSize: typeof wire.byte_size === "number" ? wire.byte_size : 0,
    mediaType: str(wire.media_type),
    approvedAt: strOrNull(wire.approved_at),
    publishedAt: strOrNull(wire.published_at),
    supersededAt: strOrNull(wire.superseded_at),
    indexedAt: strOrNull(wire.indexed_at),
    effectiveAt: strOrNull(wire.effective_at),
    expiresAt: strOrNull(wire.expires_at),
    indexErrorCode: strOrNull(wire.index_error_code),
    generationStatus: strOrNull(wire.generation_status),
    chunkCount: typeof wire.chunk_count === "number" ? wire.chunk_count : 0,
    embeddingModel: strOrNull(wire.embedding_model)
  };
}

function knowledgeFindingFromWire(wire: Record<string, unknown>): KnowledgeFinding {
  return {
    code: str(wire.code),
    tenantId: str(wire.tenant_id),
    documentId: str(wire.document_id),
    versionId: str(wire.version_id),
    generationId: strOrNull(wire.generation_id),
    detectedAt: str(wire.detected_at),
    detail: obj(wire.detail),
    sourceName: strOrNull(wire.source_name),
    documentTitle: strOrNull(wire.document_title),
    revision: typeof wire.revision === "number" ? wire.revision : null
  };
}

function handoffSummaryFromWire(wire: Record<string, unknown>): HandoffSummary {
  return {
    handoffId: str(wire.handoff_id),
    tenantId: str(wire.tenant_id),
    sessionId: str(wire.session_id),
    status: str(wire.status),
    reason: str(wire.reason),
    summary: str(wire.summary),
    assignedPrincipalId: strOrNull(wire.assigned_principal_id),
    requestedAt: str(wire.requested_at),
    assignedAt: strOrNull(wire.assigned_at),
    releasedAt: strOrNull(wire.released_at),
    resolvedAt: strOrNull(wire.resolved_at),
    resolvedByPrincipalId: strOrNull(wire.resolved_by_principal_id)
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

/** The FEAT-016 audit projection: bounded fields only, never the details dict. */
function auditEventFromWire(wire: Record<string, unknown>): AuditEventRow {
  return {
    action: str(wire.action),
    actorType: str(wire.actor_type),
    principal: strOrNull(wire.principal),
    tenantId: str(wire.tenant_id),
    requestId: strOrNull(wire.request_id),
    traceId: strOrNull(wire.trace_id),
    resourceType: str(wire.resource_type),
    resourceId: strOrNull(wire.resource_id),
    occurredAt: str(wire.occurred_at),
    permission: str(wire.permission)
  };
}

function membershipRoleFromWire(wire: Record<string, unknown>): MembershipRole {
  return {
    tenantId: str(wire.tenant_id),
    subject: str(wire.subject),
    role: str(wire.role),
    grantedBy: strOrNull(wire.granted_by),
    grantedAt: str(wire.granted_at),
    updatedAt: str(wire.updated_at)
  };
}

function traceGrantFromWire(wire: Record<string, unknown>): TraceGrant {
  return {
    tenantId: str(wire.tenant_id),
    subject: str(wire.subject),
    grantedBy: str(wire.granted_by),
    grantedAt: str(wire.granted_at),
    expiresAt: strOrNull(wire.expires_at)
  };
}
