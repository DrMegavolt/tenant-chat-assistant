/**
 * The FEAT-008 review queue's data contracts.
 *
 * The list surface is content-free by contract: a queue entry carries the
 * content-free priority inputs and the turn's derived columns, and nothing
 * the visitor or reviewer wrote. Content arrives only through the audited
 * detail surface.
 */

/** One content-free queue entry, as the list surface shows it. */
export interface ReviewSummary {
  reviewId: string;
  turnId: string;
  sessionId: string | null;
  recordedAt: string | null;
  outcome: string;
  source: string;
  status: string;
  priority: number;
  recurrence: number;
  manifestHash: string;
  committedActions: boolean;
  novelManifest: boolean;
  caseId: string | null;
  verdict: string | null;
  diagnosisCauses: string[];
  diagnosisStatuses: string[];
  closingEvalRunId: string | null;
  closingEvalCaseId: string | null;
  createdAt: string;
  turnIndex: number;
}

/** One reviewer-authored diagnosis row, with its relationship explicit. */
export interface ReviewDiagnosis {
  diagnosisId: string;
  reviewId: string;
  relationship: string;
  automaticIndex: number | null;
  cause: string;
  stage: string;
  role: string;
  status: string;
  confidence: string;
  evidence: string[];
  note: string | null;
  createdAt: string;
}

/** The content-bearing detail surface: review + feedback + overlay. */
export interface ReviewDetail {
  review: ReviewSummary;
  feedback: { turnId: string; rating: string; reason: string | null; createdAt: string } | null;
  reviewerSubject: string | null;
  reviewedAt: string | null;
  verdictNote: string | null;
  correctedAnswer: string | null;
  proposedFix: string | null;
  closingEvalPassedAt: string | null;
  diagnoses: ReviewDiagnosis[];
}

/** The queue statuses, as the filter's options. */
export const REVIEW_STATUSES = [
  "open",
  "in_review",
  "awaiting_fix",
  "rejected",
  "resolved"
] as const;

export const REVIEW_STATUS_LABELS: Record<string, string> = {
  open: "Open",
  in_review: "In review",
  awaiting_fix: "Awaiting fix",
  rejected: "Rejected",
  resolved: "Resolved"
};

export const REVIEW_SOURCE_LABELS: Record<string, string> = {
  user_feedback: "Visitor feedback",
  automatic: "Automatic failure"
};

export const REVIEW_VERDICT_LABELS: Record<string, string> = {
  confirmed: "Confirmed",
  rejected: "Rejected",
  amended: "Amended"
};

export const DIAGNOSIS_RELATIONSHIP_LABELS: Record<string, string> = {
  confirms: "Confirms detector",
  rejects: "Rejects detector",
  amends: "Amends detector",
  adds: "Adds new diagnosis"
};
