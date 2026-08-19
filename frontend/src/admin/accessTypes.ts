/**
 * The FEAT-016 access console's data contracts.
 *
 * The audit trail is content-free by contract: every row carries identifiers,
 * enums, and a timestamp, plus the permission that authorized the action. The
 * permissions view carries the tenant's live roles and trace-read grants as
 * separate lists, because they are separate controls.
 */

/** One row of `GET /api/admin/audit`: a content-free accountability record. */
export interface AuditEventRow {
  action: string;
  actorType: string;
  principal: string | null;
  tenantId: string;
  requestId: string | null;
  traceId: string | null;
  resourceType: string;
  resourceId: string | null;
  occurredAt: string;
  /** The permission that authorized this action, resolved server-side. */
  permission: string;
}

/** The filters an operator may query the trail by — never free text. */
export interface AuditFilters {
  since?: string;
  until?: string;
  action?: string;
  principal?: string;
}

/** One live role assignment (`tenant_memberships`), grantor resolved. */
export interface MembershipRole {
  tenantId: string;
  subject: string;
  role: string;
  grantedBy: string | null;
  grantedAt: string;
  updatedAt: string;
}

/** One live PRIV-002 trace-read grant. Never an admin role. */
export interface TraceGrant {
  tenantId: string;
  subject: string;
  grantedBy: string;
  grantedAt: string;
  expiresAt: string | null;
}

/** The tenant's live authorization state, rendered as two distinct controls. */
export interface PermissionsView {
  roles: MembershipRole[];
  grants: TraceGrant[];
}

export const ROLE_LABELS: Record<string, string> = {
  viewer: "Viewer",
  support_agent: "Support agent",
  tenant_admin: "Tenant admin",
  platform_admin: "Platform admin"
};

/**
 * Every action the trail may be filtered by.
 *
 * Must equal `AUDIT_ACTIONS` in `services/api/src/tenantchat/api/store.py`;
 * `tests/test_audit_taxonomy.py` fails when the two drift, because an action
 * that reaches the table but not this filter is invisible to an operator
 * working an incident.
 */
export const AUDIT_ACTIONS = [
  "audit.read",
  "permissions.read",
  "staff_reply_sent",
  "membership_assigned",
  "membership_revoked",
  "handoff.accepted",
  "handoff.released",
  "handoff.resolved",
  "trace_access.granted",
  "trace_access.revoked",
  "trace.read",
  "trace.search",
  "trace.replay",
  "trace.replay_trials",
  "trace.replay_retrieval",
  "trace.replay_template",
  "trace.gold_read",
  "trace.read_refused",
  "review.taken",
  "review.decided",
  "review.promoted",
  "review.read",
  "review.search",
  "knowledge.source_created",
  "knowledge.source_enabled",
  "knowledge.document_deleted",
  "knowledge.quarantine",
  "knowledge.quarantine_review",
  "knowledge.version_approved",
  "knowledge.version_published",
  "knowledge.version_reindexed",
  "knowledge.version_expired",
  "privacy.export",
  "privacy.deletion_requested",
  "privacy.erased",
  "privacy.retention_purged"
] as const;
