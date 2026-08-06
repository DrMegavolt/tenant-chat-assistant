/**
 * The FEAT-015 trace explorer's data contracts.
 *
 * The wire format is snake_case (the PRIV-002 envelope); the API client maps
 * to these camelCase shapes. Content-bearing sections arrive only from the
 * single-read endpoint — the search surface stays content-free by contract.
 */

export interface TraceSearchFilters {
  since?: string;
  until?: string;
  outcome?: string;
  cause?: string;
  diagnosisStatus?: string;
  manifestHash?: string | undefined;
}

/** One content-free search result: a queryable index entry, not a record. */
export interface TraceSearchRecord {
  turnId: string;
  sessionId: string;
  traceId: string | null;
  recordedAt: string;
  outcome: string;
  componentManifestHash: string;
  diagnosisCauses: string[];
  diagnosisStatuses: string[];
  turnIndex: number;
  traceSchemaVersion: string;
}

export interface RoutingCandidate {
  intent?: string;
  score?: number;
  matchedSignals?: string[];
  [key: string]: unknown;
}

export interface RoutingSection {
  rule?: string;
  intent?: string;
  score?: number;
  threshold?: number;
  policyVersion?: string;
  candidates?: RoutingCandidate[];
  [key: string]: unknown;
}

export interface RetrievalCandidate {
  sourceId?: string;
  score?: number;
  generationId?: string | null;
  embeddingModel?: string;
  [key: string]: unknown;
}

export interface RetrievalSection {
  query?: string;
  sufficient?: boolean;
  retrieverVersion?: string;
  reranker?: string | null;
  minEvidenceScore?: number | null;
  embeddingModel?: string;
  generationId?: string | null;
  filters?: Record<string, unknown>;
  budget?: Record<string, unknown>;
  parameters?: Record<string, unknown>;
  candidates?: RetrievalCandidate[];
  evidence?: RetrievalCandidate[];
  [key: string]: unknown;
}

export interface PromptSegment {
  segmentId: string;
  region: string;
  text: string;
}

export interface PromptMessage {
  role: string;
  segments?: PromptSegment[];
  toolCalls?: unknown[];
  toolCallId?: string | null;
  content?: string;
}

export interface ExcludedItem {
  kind?: string;
  reference?: string;
  reason?: string;
  [key: string]: unknown;
}

export interface PromptSection {
  templateRef?: string;
  contentHash?: string;
  bindings?: Record<string, string>;
  excluded?: ExcludedItem[];
  messages?: PromptMessage[];
  [key: string]: unknown;
}

export interface VerdictsSection {
  citations?: { sourceId?: string; title?: string }[];
  citationInvalid?: string[];
  refusedTools?: string[];
  claimsInvalid?: { claim?: string; reason?: string }[];
  [key: string]: unknown;
}

export interface ToolCallEntry {
  callId?: string;
  name?: string;
  arguments?: Record<string, unknown>;
}

export interface ToolResultEntry {
  callId?: string;
  result?: string;
}

export interface ToolsSection {
  toolCalls?: ToolCallEntry[];
  toolResults?: ToolResultEntry[];
  committed?: {
    action?: string;
    reference?: string;
    replayed?: boolean;
    idempotencyKey?: string;
  }[];
}

export interface DiagnosisRecord {
  cause: string;
  stage: string;
  role: string;
  status: string;
  confidence: string;
  evidence: string[];
  detectorVersion?: string;
}

export interface OutcomeSection {
  status?: string | undefined;
  rounds?: number | undefined;
  failure?: string | null | undefined;
}

export interface TraceContent {
  schemaVersion?: string | undefined;
  turnIndex?: number | undefined;
  routing?: RoutingSection | null | undefined;
  retrieval?: RetrievalSection | null | undefined;
  prompt?: PromptSection | null | undefined;
  model?: { name?: string; usage?: Record<string, unknown> } | undefined;
  output?: { answer?: string; raw?: string; claims?: string[] } | undefined;
  verdicts?: VerdictsSection | undefined;
  tools?: ToolsSection | undefined;
  outcome?: OutcomeSection | undefined;
  componentManifest?: Record<string, unknown> | undefined;
  manifestHash?: string | undefined;
  diagnoses?: DiagnosisRecord[] | undefined;
  [key: string]: unknown;
}

export interface TraceRead {
  turnId: string;
  tenantId: string;
  sessionId: string;
  traceId: string | null;
  recordedAt: string;
  content: TraceContent;
  projections: { projectionId: string; turnRecordId: string; kind: string; createdAt: string }[];
}

export interface ComponentVersionSnapshot {
  name: string;
  stored: string;
  current: string;
  changed: boolean;
}

export interface ReplayResult {
  turnId: string;
  recordedAt: string;
  manifestHash: string;
  currentManifestHash: string | null;
  manifestChanged: boolean;
  stochastic: boolean;
  components: ComponentVersionSnapshot[];
  original: { contentHash: string; modelName: string; outputRaw: string };
  replayed: { contentHash: string; modelName: string; outputRaw: string };
}

export interface GoldChunk {
  sourceId: string;
  text: string;
}

export interface GoldCase {
  caseId: string;
  tenantId: string;
  scenario: string | null;
  query: string;
  goldChunks: GoldChunk[];
}

/** The Gate B diagnosis cause vocabulary, as the filter's select options. */
export const DIAGNOSIS_CAUSES = [
  "stale_source",
  "ingestion_or_index_error",
  "routing_error",
  "query_rewrite_error",
  "filter_exclusion",
  "retrieval_miss",
  "retrieval_rank",
  "context_truncation",
  "prompt_regression",
  "model_behavior",
  "grounding_or_citation_error",
  "tool_error",
  "application_error",
  "provider_failure"
] as const;

export const DIAGNOSIS_STATUSES = ["detected", "suspected", "confirmed", "inconclusive"] as const;

export const OUTCOMES = ["answered", "paused", "escalated", "abstained", "clarified"] as const;

export const OUTCOME_LABELS: Record<string, string> = {
  answered: "Answered",
  paused: "Paused",
  escalated: "Escalated",
  abstained: "Abstained",
  clarified: "Clarified",
  unknown: "Unknown"
};

export const DIAGNOSIS_STATUS_LABELS: Record<string, string> = {
  detected: "Detected",
  suspected: "Suspected — uncertain",
  confirmed: "Confirmed",
  inconclusive: "Inconclusive — uncertain"
};

export const DIAGNOSIS_CAUSE_LABELS: Record<string, string> = {
  stale_source: "Stale source",
  ingestion_or_index_error: "Ingestion / index error",
  routing_error: "Routing error",
  query_rewrite_error: "Query rewrite error",
  filter_exclusion: "Filter exclusion",
  retrieval_miss: "Retrieval miss",
  retrieval_rank: "Retrieval rank",
  context_truncation: "Context truncation",
  prompt_regression: "Prompt regression",
  model_behavior: "Model behavior",
  grounding_or_citation_error: "Grounding / citation error",
  tool_error: "Tool error",
  application_error: "Application error",
  provider_failure: "Provider failure"
};

/** Whether a diagnosis status must never be presented as a confirmed cause. */
export function isUncertainStatus(status: string | undefined): boolean {
  return status === "suspected" || status === "inconclusive";
}
