import { useCallback, useEffect, useId, useRef, useState } from "react";

import type { AdminApi } from "src/admin/adminApi";
import {
  AUDIT_ACTIONS,
  ROLE_LABELS,
  type AuditEventRow,
  type AuditFilters,
  type MembershipRole,
  type PermissionsView,
  type TraceGrant
} from "src/admin/accessTypes";
import { relativeIsoTime, toIso } from "src/shared/display";

const EMPTY_FILTERS: AuditFilters = {};

/**
 * Who could have performed an action right now, from the live permissions view.
 *
 * The console answers "who could have done this, and who did" on one screen:
 * the row names the permission that authorized the action, and the holders are
 * the current subjects holding that control. "platform admin (directory)" is a
 * role that spans tenants and has no membership row, so it is named literally.
 */
function holdersOf(permission: string, roles: MembershipRole[], grants: TraceGrant[]): string[] {
  if (permission.includes("trace_viewer")) {
    return [...grants.map((grant) => grant.subject), "platform admin (directory)"];
  }
  if (permission.includes("support_agent")) {
    return roles.filter((role) => role.role === "support_agent").map((role) => role.subject);
  }
  if (permission.includes("tenant_admin")) {
    return roles.filter((role) => role.role === "tenant_admin").map((role) => role.subject);
  }
  if (permission.includes("platform_admin")) {
    return ["platform admin (directory)"];
  }
  if (permission.includes("service role")) {
    return ["system service"];
  }
  return [];
}

export interface AccessConsoleProps {
  api: AdminApi;
  tenants: { tenantId: string; name: string }[];
  initialTenantId: string | null;
}

/**
 * The FEAT-016 access console: the audit trail and the permissions view.
 *
 * Two controls are shown apart because they authorize different surfaces: an
 * admin role (a tenant membership) and a trace-read grant (PRIV-002) are not
 * interchangeable, and a viewer must not read one as the other. Every trail
 * read and every permissions read is itself audited server-side.
 */
export function AccessConsole({ api, tenants, initialTenantId }: AccessConsoleProps) {
  const [tenantId, setTenantId] = useState(initialTenantId ?? tenants[0]?.tenantId ?? "");
  const [permissions, setPermissions] = useState<PermissionsView | null>(null);
  const [events, setEvents] = useState<AuditEventRow[]>([]);
  const [filters, setFilters] = useState<AuditFilters>(EMPTY_FILTERS);
  const [isLoading, setLoading] = useState(true);
  const [notFound, setNotFound] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // The reads name the tenant through a ref, so the mount effect never depends
  // on — and never re-runs with — tenant state.
  const tenantIdRef = useRef(tenantId);

  // Every read claims a generation before its first await and may only publish
  // while it is still the newest: a permissions/trail read issued before a
  // tenant switch or a second search would otherwise land last and show one
  // tenant's audit rows under another's heading.
  const generationRef = useRef(0);
  const claimGeneration = useCallback(() => {
    const generation = (generationRef.current += 1);
    return () => generation === generationRef.current;
  }, []);

  const refresh = useCallback(
    async (nextTenant: string, active: AuditFilters) => {
      const isCurrent = claimGeneration();
      try {
        const [view, rows] = await Promise.all([
          api.permissions(nextTenant),
          api.audit(nextTenant, active)
        ]);
        if (!isCurrent()) return;
        if (view === null || rows === null) {
          setNotFound(true);
          setPermissions(null);
          setEvents([]);
          return;
        }
        setNotFound(false);
        setError(null);
        setPermissions(view);
        setEvents(rows);
      } catch {
        if (isCurrent()) setError("Could not reach the access console. Retrying…");
      } finally {
        if (isCurrent()) setLoading(false);
      }
    },
    [api, claimGeneration]
  );

  const switchingTenant = (next: string) => {
    if (next === tenantIdRef.current) return;
    tenantIdRef.current = next;
    setTenantId(next);
    setFilters(EMPTY_FILTERS);
    setLoading(true);
    void refresh(next, EMPTY_FILTERS);
  };

  // The one effect-driven read is the mount read; the generation guard drops
  // whatever the previous tenant's read returns late. No deferral: refresh
  // only ever writes state after an await, and deferring would let a tenant
  // switch that happens first claim the older generation and be discarded by
  // this late read.
  useEffect(() => {
    void refresh(tenantIdRef.current, EMPTY_FILTERS);
  }, [refresh]);

  const wiredFilters: AuditFilters = {};
  if (filters.since) wiredFilters.since = toIso(filters.since);
  if (filters.until) wiredFilters.until = toIso(filters.until);
  if (filters.action) wiredFilters.action = filters.action;
  if (filters.principal) wiredFilters.principal = filters.principal;

  const runSearch = () => {
    setLoading(true);
    setError(null);
    setNotFound(false);
    void refresh(tenantId, wiredFilters);
  };

  return (
    <section className="access-console" aria-labelledby="accessTitle">
      <div className="admin-panel-header">
        <h2 id="accessTitle">Access &amp; audit</h2>
        {tenants.length > 1 && (
          <label className="tenant-picker">
            <span className="visually-hidden">Tenant</span>
            <select
              value={tenantId}
              onChange={(event) => switchingTenant(event.target.value)}
              aria-label="Access console tenant"
            >
              {tenants.map((tenant) => (
                <option key={tenant.tenantId} value={tenant.tenantId}>
                  {tenant.name}
                </option>
              ))}
            </select>
          </label>
        )}
      </div>

      {notFound && (
        <p className="admin-alert" role="alert">
          This tenant cannot be opened. If you expected it, you may not have admin access here.
        </p>
      )}

      {error && (
        <p className="admin-alert" role="alert">
          {error}
        </p>
      )}

      {!notFound && !error && permissions && (
        <div className="access-permissions">
          <p className="access-controls-note">
            Two different controls. An <strong>admin role</strong> is a tenant membership that lets
            an operator work this tenant's console. A <strong>trace-read grant</strong> (PRIV-002)
            is a separate, dedicated permission to read the inference plane. Holding one never
            confers the other.
          </p>
          <div className="access-controls-grid">
            <AccessTable
              title="Admin roles (tenant memberships)"
              columns={["Role", "Subject", "Granted by", "Granted"]}
              rows={permissions.roles.map((role) => [
                ROLE_LABELS[role.role] ?? role.role,
                role.subject,
                role.grantedBy ?? "—",
                relativeIsoTime(role.grantedAt)
              ])}
              empty="No roles assigned in this tenant."
            />
            <AccessTable
              title="Trace-read grants (PRIV-002)"
              columns={["Subject", "Granted by", "Granted", "Expiry"]}
              rows={permissions.grants.map((grant) => [
                grant.subject,
                grant.grantedBy,
                relativeIsoTime(grant.grantedAt),
                grant.expiresAt ? relativeIsoTime(grant.expiresAt) : "Never"
              ])}
              empty="No trace-read grants in this tenant."
            />
          </div>
        </div>
      )}

      {!notFound && (
        <>
          <AuditFilters
            filters={filters}
            onFiltersChange={setFilters}
            onSearch={runSearch}
            isLoading={isLoading}
          />

          {isLoading && <p className="muted-copy">Loading the audit trail…</p>}
          {!isLoading && events.length === 0 && !error && (
            <p className="muted-copy">No audit rows match.</p>
          )}
          {!isLoading && events.length > 0 && permissions && (
            <AuditTable events={events} permissions={permissions} />
          )}
        </>
      )}
    </section>
  );
}

interface AccessTableProps {
  title: string;
  columns: string[];
  rows: string[][];
  empty: string;
}

/** One permissions table, labelled distinctly from the other control. */
function AccessTable({ title, columns, rows, empty }: AccessTableProps) {
  if (rows.length === 0) {
    return (
      <section className="access-table" aria-label={title}>
        <h3>{title}</h3>
        <p className="muted-copy">{empty}</p>
      </section>
    );
  }
  return (
    <section className="access-table" aria-label={title}>
      <h3>{title}</h3>
      <table>
        <thead>
          <tr>
            {columns.map((column) => (
              <th key={column} scope="col">
                {column}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={index}>
              {row.map((cell, column) => (
                <td key={column}>{cell}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

interface AuditFiltersProps {
  filters: AuditFilters;
  onFiltersChange: (filters: AuditFilters) => void;
  onSearch: () => void;
  isLoading: boolean;
}

function AuditFilters({ filters, onFiltersChange, onSearch, isLoading }: AuditFiltersProps) {
  const formId = useId();
  const set = (field: keyof AuditFilters, value: string) =>
    onFiltersChange({ ...filters, [field]: value || undefined });

  return (
    <form
      className="audit-filters"
      aria-label="Audit trail filters"
      onSubmit={(event) => {
        event.preventDefault();
        onSearch();
      }}
    >
      <label className="audit-filter">
        <span className="audit-filter-label" id={`${formId}-since`}>
          From
        </span>
        <input
          type="datetime-local"
          aria-labelledby={`${formId}-since`}
          value={filters.since ?? ""}
          onChange={(event) => set("since", event.target.value)}
        />
      </label>
      <label className="audit-filter">
        <span className="audit-filter-label" id={`${formId}-until`}>
          Until
        </span>
        <input
          type="datetime-local"
          aria-labelledby={`${formId}-until`}
          value={filters.until ?? ""}
          onChange={(event) => set("until", event.target.value)}
        />
      </label>
      <label className="audit-filter">
        <span className="audit-filter-label" id={`${formId}-action`}>
          Action
        </span>
        <select
          aria-labelledby={`${formId}-action`}
          value={filters.action ?? ""}
          onChange={(event) => set("action", event.target.value)}
        >
          <option value="">Any</option>
          {AUDIT_ACTIONS.map((action) => (
            <option key={action} value={action}>
              {action}
            </option>
          ))}
        </select>
      </label>
      <label className="audit-filter">
        <span className="audit-filter-label" id={`${formId}-principal`}>
          Principal
        </span>
        <input
          type="text"
          aria-labelledby={`${formId}-principal`}
          value={filters.principal ?? ""}
          onChange={(event) => set("principal", event.target.value)}
        />
      </label>
      <button type="submit" className="ghost-button" disabled={isLoading}>
        {isLoading ? "Reading…" : "Read the trail"}
      </button>
    </form>
  );
}

interface AuditTableProps {
  events: AuditEventRow[];
  permissions: PermissionsView;
}

function AuditTable({ events, permissions }: AuditTableProps) {
  return (
    <div className="audit-table-wrap">
      <table className="audit-table" aria-label="Audit trail">
        <thead>
          <tr>
            <th scope="col">When</th>
            <th scope="col">Action</th>
            <th scope="col">Permission that authorized it</th>
            <th scope="col">Who did it</th>
            <th scope="col">Resource</th>
            <th scope="col">Request</th>
          </tr>
        </thead>
        <tbody>
          {events.map((event) => {
            const holders = holdersOf(event.permission, permissions.roles, permissions.grants);
            return (
              <tr key={`${event.requestId}-${event.occurredAt}-${event.action}`}>
                <td title={event.occurredAt}>{relativeIsoTime(event.occurredAt)}</td>
                <td>
                  <code>{event.action}</code>
                </td>
                <td className="audit-permission">
                  {event.permission}
                  {holders.length > 0 && (
                    <span className="audit-holders">could have: {holders.join(", ")}</span>
                  )}
                </td>
                <td>{event.principal ?? "system"}</td>
                <td>
                  {event.resourceId
                    ? `${event.resourceType} ${event.resourceId}`
                    : event.resourceType}
                  {event.traceId ? (
                    <span className="audit-trace">trace {event.traceId}</span>
                  ) : null}
                </td>
                <td className="mono">{event.requestId ?? "—"}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
