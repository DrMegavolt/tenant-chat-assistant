"""The FEAT-016 audit read surface and the permissions view.

The audit store already records every privileged action with the principal,
tenant, request ID, and timestamp; this module is the read side of that story.
It owns two bounded projections:

- the audit trail: content-free rows — action, principal, tenant, request ID,
  trace ID, timestamp, the bounded resource reference, and the permission that
  authorized the action — filtered only by time range, action, and principal;
- the permissions view: which subjects currently hold each role or trace-read
  grant for a tenant, and who granted each, with the two controls kept apart
  because they authorize different surfaces.

Every successful read here is itself recorded through the same audit envelope,
so an operator cannot inspect a tenant's activity silently. The ``record``
call never re-enters this module, which is what makes an audit of an audit
terminate rather than recurse.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Final

from fastapi import Depends, Request

from tenantchat.api.dependencies import get_membership_store
from tenantchat.api.identity import (
    ROLES,
    AdminIdentity,
    authenticate,
    effective_role,
)
from tenantchat.api.schemas import (
    AdminMembershipRole,
    AdminTraceGrant,
)
from tenantchat.api.settings import Settings
from tenantchat.api.store import (
    AuditEvent,
    AuditStore,
    MembershipStore,
    TenantMembership,
    TraceAccessGrant,
    TraceAccessStore,
)
from tenantchat.core.errors import NotFoundError

# The role a membership row may carry that is allowed to read the console
# surfaces. `platform_admin` is handled by the directory ceiling.
TENANT_ADMIN_ROLE: Final = "tenant_admin"

# How many of a tenant's events the grantor correlation reads. Assignments are
# rare and each row is small; the bound keeps the console from ever pulling an
# unbounded history, and a membership older than this still shows its created
# timestamp without an issuer.
GRANTOR_HISTORY_LIMIT: Final = 1000

# The permission that authorized each audited action, as the console must
# answer "who could have done this, and who did". Unlisted actions (new lanes)
# fall back to their action name rather than failing the read.
_AUTHORIZING_PERMISSION: Final[dict[str, str]] = {
    "audit.read": "tenant_admin — tenant membership",
    "permissions.read": "tenant_admin — tenant membership",
    "staff_reply_sent": "support_agent — tenant membership",
    "membership_assigned": "platform_admin — directory role",
    "membership_revoked": "platform_admin — directory role",
    "trace_access.granted": "platform_admin — directory role",
    "trace_access.revoked": "platform_admin — directory role",
    "trace.read": "trace_viewer — PRIV-002 grant, or platform_admin",
    "trace.search": "trace_viewer — PRIV-002 grant, or platform_admin",
    "trace.replay": "trace_viewer — PRIV-002 grant, or platform_admin",
    "trace.gold_read": "trace_viewer — PRIV-002 grant, or platform_admin",
    "trace.read_refused": "no permission — the read was refused",
    "review.read": "trace_viewer — PRIV-002 grant, or platform_admin",
    "review.search": "trace_viewer — PRIV-002 grant, or platform_admin",
    "review.taken": "trace_viewer — PRIV-002 grant, or platform_admin",
    "review.decided": "trace_viewer — PRIV-002 grant, or platform_admin",
    "review.promoted": "trace_viewer — PRIV-002 grant, or platform_admin",
    "knowledge.source_created": "tenant_admin — tenant membership",
    "knowledge.source_enabled": "tenant_admin — tenant membership",
    "knowledge.quarantine": "tenant_admin — tenant membership",
    "knowledge.quarantine_review": "tenant_admin — tenant membership",
    "knowledge.document_deleted": "tenant_admin — tenant membership",
    "privacy.export": "tenant_admin — directory role",
    "privacy.deletion_requested": "tenant_admin — directory role",
    "privacy.erased": "privacy worker — service role",
    "privacy.retention_purged": "privacy worker — service role",
}


def authorizing_permission(action: str) -> str:
    """The permission that authorized *action*, for the audit console."""
    return _AUTHORIZING_PERMISSION.get(action, f"{action}")


async def tenant_admin_scoped(
    request: Request,
    memberships: Annotated[MembershipStore, Depends(get_membership_store)],
) -> AdminIdentity:
    """Admit a tenant administrator to the FEAT-016 surfaces.

    The console's refusal is a 404 rather than the shared 403: on this surface
    a tenant this operator cannot administer must be indistinguishable from one
    that never existed, so the console cannot be used to probe the registry.

    Raises:
        UnauthenticatedError: no usable operator identity.
        NotFoundError: no admin-level membership grants access to the tenant,
            so the tenant is treated as absent.
    """
    settings: Settings = request.app.state.settings
    identity = authenticate(request, settings)
    tenant_id = request.query_params.get("tenant_id", "")
    if identity.role == "platform_admin":
        return identity
    membership_role = await memberships.role_for(tenant_id, identity.subject)
    effective = effective_role(identity, membership_role)
    if effective is None or ROLES.index(effective) < ROLES.index(TENANT_ADMIN_ROLE):
        raise NotFoundError(detail="tenant absent or outside this operator's authority")
    return identity


async def permission_views(
    tenant_id: str,
    memberships: MembershipStore,
    grants: TraceAccessStore,
    audit: AuditStore,
) -> tuple[list[AdminMembershipRole], list[AdminTraceGrant]]:
    """The live authorization state of one tenant, grantors resolved.

    Roles are the current membership rows; who granted each is the most recent
    ``membership_assigned`` audit row naming that subject. Trace-read grants
    carry their grantor on the row itself. Both are read live, so a revocation
    is reflected without a redeploy.
    """
    membership_rows = await memberships.for_tenant(tenant_id)
    grant_rows = await grants.for_tenant(tenant_id)
    history = await audit.for_tenant(tenant_id, limit=GRANTOR_HISTORY_LIMIT)
    roles = [_membership_role(tenant_id, membership, history) for membership in membership_rows]
    return roles, [_trace_grant(grant) for grant in grant_rows]


def _membership_role(
    tenant_id: str,
    membership: TenantMembership,
    history: Sequence[AuditEvent],
) -> AdminMembershipRole:
    granted_by: str | None = None
    granted_at = membership.created_at
    for event in history:
        if (
            event.action == "membership_assigned"
            and event.tenant_id == tenant_id
            and event.details.get("subject") == membership.principal_subject
        ):
            granted_by = event.principal_id
            granted_at = event.occurred_at
            break
    return AdminMembershipRole(
        tenant_id=tenant_id,
        subject=membership.principal_subject,
        role=membership.role,
        granted_by=granted_by,
        granted_at=granted_at,
        updated_at=membership.updated_at,
    )


def _trace_grant(grant: TraceAccessGrant) -> AdminTraceGrant:
    return AdminTraceGrant(
        tenant_id=grant.tenant_id,
        subject=grant.principal_subject,
        granted_by=grant.granted_by,
        granted_at=grant.granted_at,
    )
