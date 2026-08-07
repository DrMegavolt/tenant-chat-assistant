import { useMemo, useState } from "react";

import { AdminApi } from "src/admin/adminApi";
import { KnowledgeBase } from "src/admin/components/KnowledgeBase";
import { ReviewQueue } from "src/admin/components/ReviewQueue";
import { SessionDetail } from "src/admin/components/SessionDetail";
import { SessionList } from "src/admin/components/SessionList";
import { StatBar } from "src/admin/components/StatBar";
import { TraceExplorer } from "src/admin/components/TraceExplorer";
import { relativeTime } from "src/admin/time";
import { useAdminConsole } from "src/admin/useAdminConsole";

type AdminView = "queue" | "reviews" | "traces" | "knowledge";

const VIEW_LABELS: Record<AdminView, string> = {
  queue: "Chat queue",
  reviews: "Review queue",
  traces: "AI turn explorer",
  knowledge: "Knowledge base"
};

/**
 * The operator console: every conversation, the ability to answer one, the
 * FEAT-008 review queue, the FEAT-015 AI turn explorer over the inference
 * plane, and the FEAT-001 knowledge base.
 *
 * The queue and the explorer are diagnosis surfaces, not live queues — the
 * console keeps polling only while the chat queue tab is open.
 */
export function AdminPage() {
  const api = useMemo(() => new AdminApi(), []);
  const console_ = useAdminConsole(api);
  const [view, setView] = useState<AdminView>("queue");

  return (
    <>
      <a
        className="skip-link"
        href={
          view === "queue"
            ? "#queue"
            : view === "reviews"
              ? "#reviewTitle"
              : view === "knowledge"
                ? "#knowledgeTitle"
                : "#traceTitle"
        }
      >
        Skip to main content
      </a>

      <div className="admin-shell">
        <header className="admin-header">
          <div>
            <p className="eyebrow">Live operations</p>
            <h1>Chat admin</h1>
          </div>
          <div className="admin-header-actions">
            <nav className="admin-tabs" aria-label="Console views">
              {(Object.keys(VIEW_LABELS) as AdminView[]).map((tab) => (
                <button
                  key={tab}
                  type="button"
                  className={tab === view ? "admin-tab active" : "admin-tab"}
                  aria-current={tab === view ? "page" : undefined}
                  onClick={() => setView(tab)}
                >
                  {VIEW_LABELS[tab]}
                </button>
              ))}
            </nav>
            {console_.tenants.length > 1 && view === "queue" && (
              <label className="tenant-picker">
                <span className="visually-hidden">Tenant</span>
                <select
                  value={console_.tenantId ?? ""}
                  onChange={(event) => console_.selectTenant(event.target.value)}
                >
                  {console_.tenants.map((tenant) => (
                    <option key={tenant.tenantId} value={tenant.tenantId}>
                      {tenant.name}
                    </option>
                  ))}
                </select>
              </label>
            )}
            <span className="refresh-status" role="status">
              <span className="visually-hidden">Last refreshed </span>
              <span id="lastUpdated">{relativeTime(console_.lastUpdated ?? undefined)}</span>
            </span>
            <a className="ghost-button" href="/">
              Open website demo
            </a>
          </div>
        </header>

        <main className="admin-main">
          {console_.error && view === "queue" && (
            <p className="admin-alert" role="alert">
              {console_.error}
            </p>
          )}

          {view === "queue" && (
            <>
              <StatBar sessions={console_.sessions} />

              <div className="admin-layout">
                <aside className="admin-queue" id="queue" aria-labelledby="queueTitle">
                  <div className="admin-panel-header">
                    <h2 id="queueTitle">Live &amp; archived chats</h2>
                    <span className="muted-copy">{console_.sessions.length}</span>
                  </div>
                  <SessionList
                    sessions={console_.sessions}
                    selectedId={console_.selectedId}
                    onSelect={console_.select}
                  />
                </aside>

                <SessionDetail
                  session={console_.selected}
                  isLoading={console_.isLoading}
                  onSendStaffMessage={console_.sendStaffMessage}
                />
              </div>
            </>
          )}

          {view === "traces" && (
            <TraceExplorer
              api={api}
              tenants={console_.tenants.map((tenant) => ({
                tenantId: tenant.tenantId,
                name: tenant.name
              }))}
              initialTenantId={console_.tenantId}
            />
          )}

          {view === "reviews" && (
            <ReviewQueue
              api={api}
              tenants={console_.tenants.map((tenant) => ({
                tenantId: tenant.tenantId,
                name: tenant.name
              }))}
              initialTenantId={console_.tenantId}
            />
          )}

          {view === "knowledge" && (
            <KnowledgeBase
              api={api}
              tenants={console_.tenants.map((tenant) => ({
                tenantId: tenant.tenantId,
                name: tenant.name
              }))}
              initialTenantId={console_.tenantId}
            />
          )}
        </main>
      </div>
    </>
  );
}
