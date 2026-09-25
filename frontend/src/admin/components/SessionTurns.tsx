import { useEffect, useState } from "react";

import { redirectToLogin, UnauthorizedError, type AdminApi } from "src/admin/adminApi";
import { OUTCOME_LABELS, type TraceSearchPage, type TraceSearchRecord } from "src/admin/traceTypes";

export interface SessionTurnsProps {
  api: AdminApi;
  tenantId: string;
  sessionId: string;
  onOpen: (record: TraceSearchRecord) => void;
}

/**
 * The content-free bridge from a transcript to its governed turn records.
 * Loading it is an audited trace-plane search, so it runs once per selected
 * chat rather than joining the queue's three-second polling loop.
 */
export function SessionTurns({ api, tenantId, sessionId, onOpen }: SessionTurnsProps) {
  type LoadState =
    | { key: string; status: "loading" }
    | { key: string; status: "loaded"; page: TraceSearchPage }
    | { key: string; status: "denied" }
    | { key: string; status: "failed" };
  const requestKey = `${tenantId}:${sessionId}`;
  const [load, setLoad] = useState<LoadState>({ key: requestKey, status: "loading" });

  useEffect(() => {
    let current = true;
    void api
      .tracesForSession(sessionId, tenantId)
      .then((result) => {
        if (!current) return;
        if (result === null) setLoad({ key: requestKey, status: "denied" });
        else setLoad({ key: requestKey, status: "loaded", page: result });
      })
      .catch((reason: unknown) => {
        if (reason instanceof UnauthorizedError) {
          redirectToLogin();
          return;
        }
        if (current) setLoad({ key: requestKey, status: "failed" });
      });
    return () => {
      current = false;
    };
  }, [api, requestKey, sessionId, tenantId]);

  const current: LoadState =
    load.key === requestKey ? load : { key: requestKey, status: "loading" };
  const page = current.status === "loaded" ? current.page : null;

  return (
    <section className="admin-card" aria-labelledby="sessionTurnsTitle">
      <h2 id="sessionTurnsTitle">AI turns</h2>
      {current.status === "denied" && (
        <p className="muted-copy">A trace-read grant is required to view turn identifiers.</p>
      )}
      {current.status === "failed" && (
        <p className="admin-alert">Could not load this chat’s AI turns.</p>
      )}
      {current.status === "loading" && <p className="muted-copy">Loading AI turns…</p>}
      {page?.records.length === 0 && (
        <p className="muted-copy">No retained AI turn records for this chat.</p>
      )}
      {page && page.total > page.records.length && (
        <p className="muted-copy">
          Showing {page.records.length} of {page.total} retained turns.
        </p>
      )}
      {page?.records
        .slice()
        .reverse()
        .map((record) => (
          <article className="session-turn" key={record.turnId}>
            <div className="session-row">
              <strong>Turn {record.turnIndex}</strong>
              <span className="outcome-badge">
                {OUTCOME_LABELS[record.outcome] ?? record.outcome}
              </span>
            </div>
            <dl className="identifier-list">
              <div>
                <dt>Turn ID</dt>
                <dd>{record.turnId}</dd>
              </div>
              <div>
                <dt>Trace ID</dt>
                <dd>{record.traceId || "Not recorded"}</dd>
              </div>
            </dl>
            <button type="button" className="ghost-button" onClick={() => onOpen(record)}>
              Open in AI turn explorer
            </button>
          </article>
        ))}
    </section>
  );
}
