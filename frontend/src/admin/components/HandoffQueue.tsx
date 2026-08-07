import { useCallback, useEffect, useState } from "react";

import { redirectToLogin, UnauthorizedError, type AdminApi } from "src/admin/adminApi";
import {
  HANDOFF_REASON_LABELS,
  HANDOFF_STATUS_LABELS,
  type HandoffSummary
} from "src/admin/handoffTypes";
import { relativeTime } from "src/admin/time";

const POLL_INTERVAL_MS = 3000;

export interface HandoffQueueProps {
  api: AdminApi;
  tenants: { tenantId: string; name: string }[];
  initialTenantId: string | null;
}

function relativeTimeOr(iso: string): string {
  const seconds = new Date(iso).getTime() / 1000;
  return relativeTime(Number.isFinite(seconds) ? seconds : undefined);
}

/**
 * The FEAT-004 staff handoff queue: every escalation ticket a conversation
 * left behind, and the ownership actions that decide who is working it.
 *
 * The tab polls while it is open, exactly like the chat queue — a new handoff
 * is a conversation that just left the assistant, and staff should not have to
 * refresh to see it. The ownership buttons are the front half of a transaction
 * the database decides: a lost accept is reported as a conflict to reload from,
 * never as a silent overwrite.
 */
export function HandoffQueue({ api, tenants, initialTenantId }: HandoffQueueProps) {
  const [tenantId, setTenantId] = useState(initialTenantId ?? tenants[0]?.tenantId ?? "");
  const [rows, setRows] = useState<HandoffSummary[]>([]);
  const [operatorSubject, setOperatorSubject] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    if (!tenantId) return;
    try {
      const { rows: loaded, operatorSubject: subject } = await api.handoffs(tenantId);
      setRows(loaded);
      setOperatorSubject(subject);
      setError(null);
    } catch (reason) {
      if (reason instanceof UnauthorizedError) {
        redirectToLogin();
        return;
      }
      setError("Could not reach the handoff queue. Retrying…");
    }
  }, [api, tenantId]);

  useEffect(() => {
    let cancelled = false;
    const poll = async () => {
      if (cancelled || document.hidden) return;
      await refresh();
    };
    void poll();
    const timer = window.setInterval(() => void poll(), POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [refresh]);

  const switchingTenant = (next: string) => {
    setTenantId(next);
    setRows([]);
    setSelectedId(null);
    setActionError(null);
    void refresh();
  };

  const mutate = async (handoff: HandoffSummary, action: "accept" | "release" | "resolve") => {
    if (busy) return;
    setBusy(true);
    setActionError(null);
    try {
      if (action === "accept") await api.acceptHandoff(handoff.handoffId, tenantId);
      if (action === "release") await api.releaseHandoff(handoff.handoffId, tenantId);
      if (action === "resolve") await api.resolveHandoff(handoff.handoffId, tenantId);
      await refresh();
    } catch (reason) {
      if (reason instanceof UnauthorizedError) {
        redirectToLogin();
        return;
      }
      setActionError(
        action === "accept"
          ? "Another staff member took this conversation first. Reloading the queue."
          : reason instanceof Error && reason.name === "HandoffOwnershipError"
            ? "Only the staff member who owns this conversation can take that action."
            : "That action could not be completed. The handoff may have changed."
      );
    } finally {
      setBusy(false);
    }
  };

  const selected = rows.find((row) => row.handoffId === selectedId) ?? rows[0] ?? null;
  const owns = selected?.assignedPrincipalId === operatorSubject;
  const isTakeable = selected && (selected.status === "requested" || selected.status === "queued");

  return (
    <section className="handoff-queue" aria-labelledby="handoffTitle">
      <div className="admin-panel-header">
        <h2 id="handoffTitle">Handoff queue</h2>
        <div className="review-toolbar">
          {tenants.length > 1 && (
            <label className="tenant-picker">
              <span className="visually-hidden">Tenant</span>
              <select
                value={tenantId}
                onChange={(event) => switchingTenant(event.target.value)}
                aria-label="Handoff queue tenant"
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
            disabled={busy}
            onClick={() => void refresh()}
          >
            {busy ? "Working…" : "Refresh"}
          </button>
        </div>
      </div>

      {error && (
        <p className="admin-alert" role="alert">
          {error}
        </p>
      )}

      {rows.length === 0 && !error && (
        <p className="muted-copy">No conversations are waiting for a person right now.</p>
      )}

      {rows.length > 0 && (
        <div className="handoff-layout">
          <aside className="admin-queue" aria-label="Handoff queue entries">
            {rows.map((row) => (
              <button
                key={row.handoffId}
                type="button"
                className={`session-item${selected?.handoffId === row.handoffId ? " selected" : ""}`}
                aria-current={selected?.handoffId === row.handoffId}
                onClick={() => setSelectedId(row.handoffId)}
              >
                <span className="session-row">
                  <strong>{HANDOFF_STATUS_LABELS[row.status] ?? row.status}</strong>
                  <span className="session-meta">{relativeTimeOr(row.requestedAt)}</span>
                </span>
                <span className="session-preview">{row.summary}</span>
                <span className="session-meta">
                  {HANDOFF_REASON_LABELS[row.reason] ?? row.reason}
                  {row.assignedPrincipalId
                    ? ` · held by ${row.assignedPrincipalId === operatorSubject ? "you" : "a colleague"}`
                    : " · unassigned"}
                </span>
              </button>
            ))}
          </aside>

          {selected && (
            <article className="admin-detail" aria-label="Selected handoff">
              <div className="session-detail-header">
                <div>
                  <p className="eyebrow">Escalation ticket</p>
                  <h2>{selected.handoffId}</h2>
                </div>
                <div className="detail-status">
                  <span className="live-pill">
                    <span aria-hidden="true" />
                    {HANDOFF_STATUS_LABELS[selected.status] ?? selected.status}
                  </span>
                </div>
              </div>

              <div className="admin-card">
                <h3>Why it escalated</h3>
                <p className="handoff-summary">{selected.summary}</p>
                <p className="muted-copy">
                  Reason: {HANDOFF_REASON_LABELS[selected.reason] ?? selected.reason} · Requested{" "}
                  {relativeTimeOr(selected.requestedAt)}
                </p>
              </div>

              <div className="admin-card">
                <h3>Ownership</h3>
                <p className="muted-copy">
                  {selected.assignedPrincipalId
                    ? owns
                      ? "You hold this conversation."
                      : "A colleague holds this conversation."
                    : "No one holds this conversation yet."}
                </p>
                <div className="handoff-actions">
                  {isTakeable && (
                    <button
                      type="button"
                      className="primary-button"
                      disabled={busy}
                      onClick={() => void mutate(selected, "accept")}
                    >
                      Accept conversation
                    </button>
                  )}
                  {owns && (
                    <button
                      type="button"
                      className="ghost-button"
                      disabled={busy}
                      onClick={() => void mutate(selected, "release")}
                    >
                      Release to assistant
                    </button>
                  )}
                  <button
                    type="button"
                    className="ghost-button"
                    disabled={busy}
                    onClick={() => void mutate(selected, "resolve")}
                  >
                    Mark resolved
                  </button>
                </div>
                {actionError && (
                  <p className="admin-alert" role="alert">
                    {actionError}
                  </p>
                )}
                <p className="muted-copy">
                  Open the Chat queue tab to read the transcript and reply. A conversation a
                  colleague holds only accepts replies from its owner.
                </p>
              </div>
            </article>
          )}
        </div>
      )}
    </section>
  );
}
