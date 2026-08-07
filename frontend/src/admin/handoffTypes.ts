/**
 * The FEAT-004 staff handoff queue's data contracts.
 *
 * The queue lists every open escalation ticket — the assistant's own reason
 * and summary plus the ownership state that decides who may work it. The
 * assigned principal is staff-facing accountability data; the visitor surface
 * never carries it.
 */

/** One open queue entry, as `GET /api/admin/handoffs` returns it. */
export interface HandoffSummary {
  handoffId: string;
  tenantId: string;
  sessionId: string;
  status: string;
  reason: string;
  summary: string;
  assignedPrincipalId: string | null;
  requestedAt: string;
  assignedAt: string | null;
  releasedAt: string | null;
  resolvedAt: string | null;
  resolvedByPrincipalId: string | null;
}

/** The queue statuses the console can render. */
export const HANDOFF_STATUSES = ["requested", "queued", "assigned"] as const;

export const HANDOFF_STATUS_LABELS: Record<string, string> = {
  requested: "Waiting for staff",
  queued: "Released to assistant",
  assigned: "Staff holds it"
};

export const HANDOFF_REASON_LABELS: Record<string, string> = {
  customer_request: "Customer asked for a person",
  outside_policy: "Outside policy",
  tool_failure: "Tool failure",
  unresolved: "Assistant could not finish"
};

export const HANDOFF_REASONS = Object.keys(HANDOFF_REASON_LABELS);

/** Whether the current operator may act on a row: only unassigned or their own. */
export function isOwnedBy(handoff: HandoffSummary, principal: string | null): boolean {
  return handoff.assignedPrincipalId === principal;
}

export function isTakeable(handoff: HandoffSummary): boolean {
  return handoff.status === "requested" || handoff.status === "queued";
}
