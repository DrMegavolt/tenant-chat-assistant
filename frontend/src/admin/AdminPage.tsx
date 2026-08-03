import { useMemo } from "react";

import { AdminApi } from "src/admin/adminApi";
import { SessionDetail } from "src/admin/components/SessionDetail";
import { SessionList } from "src/admin/components/SessionList";
import { StatBar } from "src/admin/components/StatBar";
import { relativeTime } from "src/admin/time";
import { useAdminConsole } from "src/admin/useAdminConsole";

/**
 * The operator console: every conversation, and the ability to answer one.
 *
 * Two panes and one selection. The queue on the left is the working surface —
 * searchable, filterable, and never rebuilt underneath a dispatcher who is
 * reading it — and the right pane is the whole of one conversation.
 */
export function AdminPage() {
  const api = useMemo(() => new AdminApi(), []);
  const console_ = useAdminConsole(api);

  return (
    <>
      <a className="skip-link" href="#queue">
        Skip to the chat queue
      </a>

      <div className="admin-shell">
        <header className="admin-header">
          <div>
            <p className="eyebrow">Live operations</p>
            <h1>Chat admin</h1>
          </div>
          <div className="admin-header-actions">
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
          {console_.error && (
            <p className="admin-alert" role="alert">
              {console_.error}
            </p>
          )}

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
        </main>
      </div>
    </>
  );
}
