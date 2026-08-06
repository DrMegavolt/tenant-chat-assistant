import { useEffect, useState } from "react";

import type { AdminApi } from "src/admin/adminApi";
import { TraceDetail } from "src/admin/components/TraceDetail";
import {
  DIAGNOSIS_RELATIONSHIP_LABELS,
  REVIEW_STATUS_LABELS,
  REVIEW_VERDICT_LABELS,
  type ReviewDetail as ReviewDetailData,
  type ReviewSummary
} from "src/admin/reviewTypes";
import { relativeTime } from "src/admin/time";
import {
  DIAGNOSIS_CAUSES,
  DIAGNOSIS_CAUSE_LABELS,
  type DiagnosisRecord,
  type GoldCase,
  type TraceSearchRecord
} from "src/admin/traceTypes";

const RELATIONSHIPS = ["confirms", "rejects", "amends", "adds"] as const;
type Relationship = (typeof RELATIONSHIPS)[number];

const CAUSE_OPTIONS = DIAGNOSIS_CAUSES.filter(
  (cause) => cause !== "query_rewrite_error" && cause !== "prompt_regression"
);

export interface ReviewDetailProps {
  api: AdminApi;
  tenantId: string;
  summary: ReviewSummary;
  onChanged: (updated: ReviewSummary) => void;
}

interface Decision {
  automaticIndex: number | null;
  relationship: Relationship;
  cause: string;
  stage: string;
  role: "primary" | "contributing";
  status: "detected" | "suspected" | "confirmed" | "inconclusive";
  confidence: "low" | "medium" | "high";
  note?: string;
}

/**
 * One queue entry under review: the linked turn in the FEAT-015 console, the
 * visitor's feedback reason, the detector's automatic diagnoses, and the
 * reviewer's overlay — confirm, reject, or amend each automatic record, add
 * new ones, write the corrected answer and the proposed fix, and promote the
 * anonymized case. The original trace is never edited here: the corrected
 * answer is a new record beside it.
 */
export function ReviewDetail({ api, tenantId, summary, onChanged }: ReviewDetailProps) {
  const [detail, setDetail] = useState<ReviewDetailData | null>(null);
  const [automatic, setAutomatic] = useState<DiagnosisRecord[]>([]);
  const [gold, setGold] = useState<GoldCase[]>([]);
  const [decisions, setDecisions] = useState<Decision[]>([]);
  const [verdict, setVerdict] = useState<"confirmed" | "rejected" | "amended">("confirmed");
  const [status, setStatus] = useState<"awaiting_fix" | "rejected">("awaiting_fix");
  const [note, setNote] = useState("");
  const [correctedAnswer, setCorrectedAnswer] = useState("");
  const [proposedFix, setProposedFix] = useState("");
  const [saving, setSaving] = useState(false);
  const [promoting, setPromoting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const [loaded, loadedGold, trace] = await Promise.all([
          api.reviewDetail(summary.reviewId, tenantId),
          api.goldCases(tenantId),
          api.trace(summary.turnId, tenantId)
        ]);
        if (cancelled) return;
        setGold(loadedGold);
        if (loaded) setDetail(loaded);
        setAutomatic(trace?.content.diagnoses ?? []);
        setDecisions(
          (trace?.content.diagnoses ?? []).map((diagnosis) => ({
            automaticIndex: null,
            relationship: "confirms",
            cause: diagnosis.cause,
            stage: diagnosis.stage,
            role: diagnosis.role === "contributing" ? "contributing" : "primary",
            status: diagnosis.status as Decision["status"],
            confidence: diagnosis.confidence as Decision["confidence"]
          }))
        );
      } catch {
        if (!cancelled) setError("Could not open the review.");
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
  }, [api, tenantId, summary.reviewId, summary.turnId]);

  const updateDecision = (index: number, patch: Partial<Decision>) => {
    setDecisions((current) =>
      current.map((decision, i) => (i === index ? { ...decision, ...patch } : decision))
    );
  };

  const addDecision = () => {
    setDecisions((current) => [
      ...current,
      {
        automaticIndex: null,
        relationship: "adds",
        cause: "retrieval_rank",
        stage: "retrieval",
        role: "primary",
        status: "confirmed",
        confidence: "medium"
      }
    ]);
  };

  const submit = async () => {
    setSaving(true);
    setError(null);
    try {
      await api.submitReview(summary.reviewId, tenantId, {
        verdict,
        status,
        ...(note.trim() ? { note: note.trim() } : {}),
        ...(correctedAnswer.trim() ? { correctedAnswer: correctedAnswer.trim() } : {}),
        ...(proposedFix.trim() ? { proposedFix: proposedFix.trim() } : {}),
        diagnoses: decisions.map((decision, index) => ({
          ...decision,
          automaticIndex: index < automatic.length ? index : null
        }))
      });
      const updated = await api.reviewDetail(summary.reviewId, tenantId);
      if (updated) {
        setDetail(updated);
        onChanged(updated.review);
      }
    } catch {
      setError("The review decision was not accepted. Check that every diagnosis is decided.");
    } finally {
      setSaving(false);
    }
  };

  const promote = async () => {
    setPromoting(true);
    setError(null);
    try {
      const caseId = await api.promoteReview(summary.reviewId, tenantId);
      const updated = await api.reviewDetail(summary.reviewId, tenantId);
      if (updated) {
        setDetail(updated);
        onChanged(updated.review);
      }
      if (!caseId) setError("The case was already promoted.");
    } catch {
      setError(
        "Promotion was refused. The case may carry contact data — anonymize the query first."
      );
    } finally {
      setPromoting(false);
    }
  };

  const review = detail?.review ?? summary;
  const traceRecord: TraceSearchRecord = {
    turnId: review.turnId,
    sessionId: review.sessionId ?? "",
    traceId: null,
    recordedAt: review.recordedAt ?? review.createdAt,
    outcome: review.outcome,
    componentManifestHash: review.manifestHash,
    diagnosisCauses: review.diagnosisCauses,
    diagnosisStatuses: review.diagnosisStatuses,
    turnIndex: review.turnIndex,
    traceSchemaVersion: "1"
  };

  const reviewRows = detail?.diagnoses ?? [];
  const disagreements = reviewRows.filter(
    (row) =>
      row.relationship === "rejects" || row.relationship === "amends" || row.relationship === "adds"
  );
  const canPromote = review.status === "awaiting_fix" && review.caseId === null && !promoting;
  const decisionsMissing = decisions.length < automatic.length;

  return (
    <article className="review-detail" aria-label={`Review ${summary.reviewId}`}>
      <div className="admin-panel-header">
        <h3 className="review-title">
          {REVIEW_STATUS_LABELS[review.status] ?? review.status} · priority {review.priority} ·{" "}
          {REVIEW_SOURCE_LABEL(review.source)}
        </h3>
        {review.closingEvalRunId ? (
          <span className="review-closure" role="status">
            Fixed by evaluation run {review.closingEvalRunId} (
            {relativeTime(new Date(detail?.closingEvalPassedAt ?? "").getTime() / 1000)})
          </span>
        ) : (
          review.status === "awaiting_fix" && (
            <span className="uncertain-chip">open — waiting for an evaluation run</span>
          )
        )}
      </div>

      {error && (
        <p className="admin-alert" role="alert">
          {error}
        </p>
      )}

      {detail?.feedback && (
        <section className="review-section" aria-labelledby="feedbackTitle">
          <h4 id="feedbackTitle">Visitor feedback</h4>
          <p className="muted-copy">
            {detail.feedback.rating === "up" ? "Thumbs up" : "Thumbs down"}
            {detail.feedback.reason ? ` — “${detail.feedback.reason}”` : ""}
          </p>
        </section>
      )}

      {review.caseId && (
        <section className="review-section" aria-labelledby="caseTitle">
          <h4 id="caseTitle">Promoted evaluation case</h4>
          <p className="mono">{review.caseId}</p>
        </section>
      )}

      <section className="review-section" aria-labelledby="diagnosisTitle">
        <h4 id="diagnosisTitle">Diagnosis review</h4>
        {automatic.length === 0 && (
          <p className="muted-copy">The detector recorded no diagnosis.</p>
        )}
        <ul className="diagnosis-list">
          {automatic.map((diagnosis, index) => (
            <li key={`auto-${index}`} className="diagnosis-row">
              <div className="diagnosis-copy">
                <strong>{DIAGNOSIS_CAUSE_LABELS[diagnosis.cause] ?? diagnosis.cause}</strong>{" "}
                <span className="muted-copy">
                  {diagnosis.status} · {diagnosis.confidence} confidence
                </span>
              </div>
              {decisions[index] && (
                <label className="trace-filter">
                  <span className="visually-hidden">Decision for {diagnosis.cause}</span>
                  <select
                    value={decisions[index].relationship}
                    onChange={(event) =>
                      updateDecision(index, {
                        relationship: event.target.value as Relationship
                      })
                    }
                  >
                    {RELATIONSHIPS.filter((rel) => rel !== "adds").map((rel) => (
                      <option key={rel} value={rel}>
                        {DIAGNOSIS_RELATIONSHIP_LABELS[rel]}
                      </option>
                    ))}
                  </select>
                </label>
              )}
              {decisions[index]?.relationship === "amends" && (
                <div className="amended-fields">
                  <label className="trace-filter">
                    <span className="trace-filter-label">Amended cause</span>
                    <select
                      value={decisions[index].cause}
                      onChange={(event) => updateDecision(index, { cause: event.target.value })}
                    >
                      {CAUSE_OPTIONS.map((cause) => (
                        <option key={cause} value={cause}>
                          {DIAGNOSIS_CAUSE_LABELS[cause]}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label className="trace-filter">
                    <span className="trace-filter-label">Amended status</span>
                    <select
                      value={decisions[index].status}
                      onChange={(event) =>
                        updateDecision(index, {
                          status: event.target.value as Decision["status"]
                        })
                      }
                    >
                      {(["confirmed", "suspected", "detected", "inconclusive"] as const).map(
                        (entry) => (
                          <option key={entry} value={entry}>
                            {entry}
                          </option>
                        )
                      )}
                    </select>
                  </label>
                  <label className="trace-filter">
                    <span className="trace-filter-label">Amended confidence</span>
                    <select
                      value={decisions[index].confidence}
                      onChange={(event) =>
                        updateDecision(index, {
                          confidence: event.target.value as Decision["confidence"]
                        })
                      }
                    >
                      {(["high", "medium", "low"] as const).map((entry) => (
                        <option key={entry} value={entry}>
                          {entry}
                        </option>
                      ))}
                    </select>
                  </label>
                </div>
              )}
            </li>
          ))}
        </ul>
        <button type="button" className="ghost-button" onClick={addDecision}>
          Add a diagnosis
        </button>
      </section>

      {reviewRows.length > 0 && (
        <section className="review-section" aria-labelledby="overlayTitle">
          <h4 id="overlayTitle">Reviewer diagnosis records</h4>
          {disagreements.length > 0 && (
            <p className="admin-alert" role="alert">
              {disagreements.length} record{disagreements.length === 1 ? "" : "s"} disagree with the
              detector — the automatic records are preserved beside them.
            </p>
          )}
          <ul className="diagnosis-list">
            {reviewRows.map((row) => (
              <li key={row.diagnosisId} className="diagnosis-row">
                <div className="diagnosis-copy">
                  <strong>{DIAGNOSIS_CAUSE_LABELS[row.cause] ?? row.cause}</strong>{" "}
                  <span className="muted-copy">
                    {DIAGNOSIS_RELATIONSHIP_LABELS[row.relationship] ?? row.relationship} ·{" "}
                    {row.status} · {row.confidence}
                  </span>
                </div>
                {row.note && <p className="muted-copy">{row.note}</p>}
              </li>
            ))}
          </ul>
        </section>
      )}

      {review.status === "open" || review.status === "in_review" ? (
        <form
          className="review-form"
          onSubmit={(event) => {
            event.preventDefault();
            void submit();
          }}
        >
          <div className="amended-fields">
            <label className="trace-filter">
              <span className="trace-filter-label">Verdict on the automatic diagnosis</span>
              <select
                value={verdict}
                onChange={(event) => setVerdict(event.target.value as typeof verdict)}
              >
                {(["confirmed", "rejected", "amended"] as const).map((entry) => (
                  <option key={entry} value={entry}>
                    {REVIEW_VERDICT_LABELS[entry]}
                  </option>
                ))}
              </select>
            </label>
            <label className="trace-filter">
              <span className="trace-filter-label">Destination</span>
              <select
                value={status}
                onChange={(event) => setStatus(event.target.value as typeof status)}
              >
                <option value="awaiting_fix">Awaiting fix (stays open)</option>
                <option value="rejected">Rejected — not a real problem</option>
              </select>
            </label>
          </div>
          <label className="trace-filter trace-filter-wide">
            <span className="trace-filter-label">Reviewer note</span>
            <textarea value={note} rows={2} onChange={(event) => setNote(event.target.value)} />
          </label>
          <label className="trace-filter trace-filter-wide">
            <span className="trace-filter-label">Corrected answer (stored beside the trace)</span>
            <textarea
              value={correctedAnswer}
              rows={2}
              onChange={(event) => setCorrectedAnswer(event.target.value)}
            />
          </label>
          <label className="trace-filter trace-filter-wide">
            <span className="trace-filter-label">Proposed fix</span>
            <textarea
              value={proposedFix}
              rows={2}
              onChange={(event) => setProposedFix(event.target.value)}
            />
          </label>
          <div className="review-actions">
            <button type="submit" className="primary-button" disabled={saving || decisionsMissing}>
              {saving ? "Saving…" : "Submit review"}
            </button>
            {decisionsMissing && (
              <span className="muted-copy">Decide every automatic diagnosis first.</span>
            )}
          </div>
        </form>
      ) : (
        <section className="review-section" aria-labelledby="decisionTitle">
          <h4 id="decisionTitle">Review decision</h4>
          {detail?.reviewedAt && (
            <p className="muted-copy">
              {REVIEW_VERDICT_LABELS[review.verdict ?? ""] ?? review.verdict} by{" "}
              {detail.reviewerSubject ?? "unknown"} ·{" "}
              {relativeTime(new Date(detail.reviewedAt).getTime() / 1000)}
            </p>
          )}
          {detail?.verdictNote && <p className="muted-copy">{detail.verdictNote}</p>}
          {detail?.correctedAnswer && (
            <p className="review-corrected">Corrected answer: {detail.correctedAnswer}</p>
          )}
          {detail?.proposedFix && <p className="muted-copy">Proposed fix: {detail.proposedFix}</p>}
          {canPromote && (
            <button type="button" className="primary-button" onClick={() => void promote()}>
              {promoting ? "Promoting…" : "Promote to evaluation case"}
            </button>
          )}
        </section>
      )}

      <TraceDetail api={api} tenantId={tenantId} record={traceRecord} gold={gold} />
    </article>
  );
}

function REVIEW_SOURCE_LABEL(source: string): string {
  return source === "automatic" ? "Automatic failure" : "Visitor feedback";
}
