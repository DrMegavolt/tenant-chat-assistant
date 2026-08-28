import { useCallback, useEffect, useRef, useState } from "react";

import type { AdminApi } from "src/admin/adminApi";
import { ReviewDetail } from "src/admin/components/ReviewDetail";
import {
  REVIEW_SOURCE_LABELS,
  REVIEW_STATUSES,
  REVIEW_STATUS_LABELS,
  REVIEW_VERDICT_LABELS,
  type ReviewSummary
} from "src/admin/reviewTypes";
import { isUncertainStatus, relativeIsoTime } from "src/shared/display";
import { DIAGNOSIS_CAUSE_LABELS, OUTCOME_LABELS } from "src/admin/traceTypes";

export interface ReviewQueueProps {
  api: AdminApi;
  tenants: { tenantId: string; name: string }[];
  initialTenantId: string | null;
}

/**
 * The FEAT-008 review queue: one content-free entry per case, highest priority
 * first, with the detector's causes and the fix-closure status visible before
 * any content is read. Selecting an entry opens the audited review detail,
 * which embeds the linked turn in the FEAT-015 console.
 */
export function ReviewQueue({ api, tenants, initialTenantId }: ReviewQueueProps) {
  const [tenantId, setTenantId] = useState(initialTenantId ?? tenants[0]?.tenantId ?? "");
  const [status, setStatus] = useState("");
  const [reviews, setReviews] = useState<ReviewSummary[]>([]);
  const [selected, setSelected] = useState<ReviewSummary | null>(null);
  const [hasLoaded, setHasLoaded] = useState(false);
  const [isLoading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // The fetches read the open tenant and filter through refs, so a switch can
  // never leave a request running for a value the component no longer shows.
  const tenantIdRef = useRef(tenantId);
  const statusRef = useRef(status);

  // Refreshes, tenant switches, and detail opens all fetch overlapping state,
  // and a slower earlier response would otherwise land last and win — showing
  // one tenant's reviews under another's heading. Every read claims a
  // generation before its first await and may only publish while it is still
  // the newest, the same contract the chat queue's console poller uses.
  const generationRef = useRef(0);
  const claimGeneration = useCallback(() => {
    const generation = (generationRef.current += 1);
    return () => generation === generationRef.current;
  }, []);

  // Only ever publishes after an await, so the mount effect can start the
  // first read without touching state synchronously.
  const run = useCallback(async () => {
    const tenant = tenantIdRef.current;
    const filter = statusRef.current;
    const isCurrent = claimGeneration();
    try {
      const rows = await api.listReviews(tenant, filter || undefined);
      if (!isCurrent()) return;
      setReviews(rows);
      setHasLoaded(true);
    } catch {
      if (isCurrent()) setError("Could not reach the review queue. Retrying…");
    } finally {
      if (isCurrent()) setLoading(false);
    }
  }, [api, claimGeneration]);

  const beginRefresh = useCallback(() => {
    setLoading(true);
    setError(null);
    void run();
  }, [run]);

  useEffect(() => {
    void run();
  }, [run]);

  const open = async (review: ReviewSummary) => {
    // A detail read must not invalidate an in-flight list read — the list owns
    // the loading state, and discarding it would strand the spinner forever.
    // Reading (not claiming) the token still lets a newer list read or tenant
    // switch invalidate the detail, so a stale selection never returns.
    const generation = generationRef.current;
    setSelected(review);
    try {
      const detail = await api.reviewDetail(review.reviewId, tenantIdRef.current);
      if (generation === generationRef.current && detail) {
        setSelected({ ...review, ...detail.review });
      }
    } catch {
      if (generation === generationRef.current) setError("Could not open the review.");
    }
  };

  const switchingTenant = (next: string) => {
    if (next === tenantIdRef.current) return;
    tenantIdRef.current = next;
    statusRef.current = "";
    setTenantId(next);
    setStatus("");
    setReviews([]);
    setHasLoaded(false);
    setSelected(null);
    beginRefresh();
  };
  return (
    <section className="review-queue" aria-labelledby="reviewTitle">
      <div className="admin-panel-header">
        <h2 id="reviewTitle">Review queue</h2>
        <div className="review-toolbar">
          <label className="trace-filter">
            <span className="visually-hidden">Review status</span>
            <select
              value={status}
              aria-label="Review status"
              onChange={(event) => {
                statusRef.current = event.target.value;
                setStatus(event.target.value);
              }}
            >
              <option value="">All statuses</option>
              {REVIEW_STATUSES.map((entry) => (
                <option key={entry} value={entry}>
                  {REVIEW_STATUS_LABELS[entry]}
                </option>
              ))}
            </select>
          </label>
          {tenants.length > 1 && (
            <label className="tenant-picker">
              <span className="visually-hidden">Tenant</span>
              <select
                value={tenantId}
                onChange={(event) => switchingTenant(event.target.value)}
                aria-label="Review queue tenant"
              >
                {tenants.map((tenant) => (
                  <option key={tenant.tenantId} value={tenant.tenantId}>
                    {tenant.name}
                  </option>
                ))}
              </select>
            </label>
          )}
          <button
            type="button"
            className="ghost-button"
            disabled={isLoading}
            onClick={beginRefresh}
          >
            {isLoading ? "Refreshing…" : "Refresh"}
          </button>
        </div>
      </div>

      {error && (
        <p className="admin-alert" role="alert">
          {error}
        </p>
      )}

      {hasLoaded && reviews.length === 0 && (
        <p className="muted-copy">No reviews match these filters.</p>
      )}

      {reviews.length > 0 && (
        <div className="trace-results" aria-label="Review queue entries">
          {reviews.map((review) => (
            <button
              key={review.reviewId}
              type="button"
              className={`session-item${selected?.reviewId === review.reviewId ? " selected" : ""}`}
              aria-current={selected?.reviewId === review.reviewId}
              onClick={() => void open(review)}
            >
              <span className="session-row">
                <strong>
                  {REVIEW_SOURCE_LABELS[review.source] ?? review.source} ·{" "}
                  {REVIEW_STATUS_LABELS[review.status] ?? review.status} · priority{" "}
                  {review.priority}
                </strong>
                <span className="session-meta">{relativeIsoTime(review.createdAt)}</span>
              </span>
              <span className="session-preview">
                {review.diagnosisCauses.length
                  ? review.diagnosisCauses
                      .map((cause) => DIAGNOSIS_CAUSE_LABELS[cause] ?? cause)
                      .join(" · ")
                  : "No automatic diagnosis"}
                {review.diagnosisStatuses.some(isUncertainStatus) && (
                  <span className="uncertain-chip">uncertain</span>
                )}
                {review.verdict && (
                  <span className="muted-copy">
                    {" "}
                    · {REVIEW_VERDICT_LABELS[review.verdict] ?? review.verdict}
                  </span>
                )}
              </span>
              <span className="session-meta mono">
                {review.manifestHash.slice(0, 12)} ·{" "}
                {OUTCOME_LABELS[review.outcome] ?? review.outcome}
                {review.closingEvalRunId ? " · fixed" : ""}
              </span>
            </button>
          ))}
        </div>
      )}

      {selected && (
        <ReviewDetail
          key={selected.reviewId}
          api={api}
          tenantId={tenantId}
          summary={selected}
          onChanged={(updated) => {
            setSelected(updated);
            beginRefresh();
          }}
        />
      )}
    </section>
  );
}
