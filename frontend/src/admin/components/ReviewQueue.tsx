import { useEffect, useState } from "react";

import type { AdminApi } from "src/admin/adminApi";
import { ReviewDetail } from "src/admin/components/ReviewDetail";
import {
  REVIEW_SOURCE_LABELS,
  REVIEW_STATUSES,
  REVIEW_STATUS_LABELS,
  REVIEW_VERDICT_LABELS,
  type ReviewSummary
} from "src/admin/reviewTypes";
import { relativeTime } from "src/admin/time";
import { DIAGNOSIS_CAUSE_LABELS, isUncertainStatus, OUTCOME_LABELS } from "src/admin/traceTypes";

function relativeTimeOr(iso: string | null): string {
  if (!iso) return "";
  const seconds = new Date(iso).getTime() / 1000;
  return relativeTime(Number.isFinite(seconds) ? seconds : undefined);
}

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
  const [status, setStatus] = useState<string>("");
  const [reviews, setReviews] = useState<ReviewSummary[]>([]);
  const [selected, setSelected] = useState<ReviewSummary | null>(null);
  const [hasLoaded, setHasLoaded] = useState(false);
  const [isLoading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const run = async (overrides?: { tenantId?: string; status?: string }) => {
    const tenant = overrides?.tenantId ?? tenantId;
    const filter = overrides?.status ?? status;
    setLoading(true);
    setError(null);
    try {
      setReviews(await api.listReviews(tenant, filter || undefined));
      setHasLoaded(true);
    } catch {
      setError("Could not reach the review queue. Retrying…");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    let cancelled = false;
    api
      .listReviews(tenantId, undefined)
      .then((loaded) => {
        if (cancelled) return;
        setReviews(loaded);
        setHasLoaded(true);
      })
      .catch(() => {
        if (!cancelled) setError("Could not reach the review queue. Retrying…");
      });
    return () => {
      cancelled = true;
    };
  }, [api, tenantId]);

  const open = async (review: ReviewSummary) => {
    setSelected(review);
    try {
      const detail = await api.reviewDetail(review.reviewId, tenantId);
      if (detail) setSelected({ ...review, ...detail.review });
    } catch {
      setError("Could not open the review.");
    }
  };

  const switchingTenant = (next: string) => {
    setTenantId(next);
    setReviews([]);
    setHasLoaded(false);
    setSelected(null);
    void run({ tenantId: next });
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
              onChange={(event) => setStatus(event.target.value)}
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
            onClick={() => void run()}
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
                <span className="session-meta">{relativeTimeOr(review.createdAt)}</span>
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
            void run();
          }}
        />
      )}
    </section>
  );
}
