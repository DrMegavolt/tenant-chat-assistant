import { useId, useMemo, useState } from "react";

import { OutcomeBadge } from "src/admin/components/StatBar";
import { relativeTime } from "src/admin/time";
import {
  OUTCOMES,
  OUTCOME_LABELS,
  outcomeOf,
  type Outcome,
  type SessionSummary
} from "src/admin/types";

export interface SessionListProps {
  sessions: SessionSummary[];
  selectedId: string | null;
  onSelect: (sessionId: string) => void;
}

function matches(session: SessionSummary, query: string): boolean {
  if (!query) return true;
  const haystack = [session.tenantName, session.sessionId, session.lastMessage?.content ?? ""]
    .join(" ")
    .toLowerCase();
  return haystack.includes(query.toLowerCase());
}

/**
 * The queue.
 *
 * Filtering and search are local state, so a poll that arrives mid-typing
 * changes the rows behind the filter without resetting the filter itself.
 */
export function SessionList({ sessions, selectedId, onSelect }: SessionListProps) {
  const [query, setQuery] = useState("");
  const [outcomeFilter, setOutcomeFilter] = useState<Outcome | "all">("all");
  const searchId = useId();

  const visible = useMemo(
    () =>
      sessions.filter(
        (session) =>
          matches(session, query) &&
          (outcomeFilter === "all" || outcomeOf(session) === outcomeFilter)
      ),
    [sessions, query, outcomeFilter]
  );

  // Only offer filters that would return something.
  const availableOutcomes = useMemo(() => {
    const present = new Set(sessions.map(outcomeOf));
    return OUTCOMES.filter((outcome) => present.has(outcome));
  }, [sessions]);

  return (
    <>
      <div className="queue-controls">
        <label className="visually-hidden" htmlFor={searchId}>
          Search chats
        </label>
        <input
          id={searchId}
          type="search"
          placeholder="Search company, id, or last message"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
        {availableOutcomes.length > 1 && (
          <div className="filter-chips" role="group" aria-label="Filter by outcome">
            <button
              type="button"
              aria-pressed={outcomeFilter === "all"}
              onClick={() => setOutcomeFilter("all")}
            >
              All
            </button>
            {availableOutcomes.map((outcome) => (
              <button
                key={outcome}
                type="button"
                aria-pressed={outcomeFilter === outcome}
                onClick={() => setOutcomeFilter(outcome)}
              >
                {OUTCOME_LABELS[outcome]}
              </button>
            ))}
          </div>
        )}
      </div>

      <div className="session-list" id="sessionList">
        {visible.length === 0 && (
          <p className="muted-copy">
            {sessions.length ? "No chat matches this filter." : "No chats saved yet."}
          </p>
        )}
        {visible.map((session) => {
          const outcome = outcomeOf(session);
          const isSelected = session.sessionId === selectedId;
          return (
            <button
              key={session.sessionId}
              type="button"
              className={`session-item outcome-${outcome}${isSelected ? " selected" : ""}`}
              aria-current={isSelected}
              onClick={() => onSelect(session.sessionId)}
            >
              <span className="session-row">
                <strong>{session.tenantName}</strong>
                <OutcomeBadge outcome={outcome} />
              </span>
              <span className="session-preview">
                {session.lastMessage?.content ?? "Open to read the transcript"}
              </span>
              <span className="session-meta">
                {[
                  session.messageCount === undefined ? null : `${session.messageCount} messages`,
                  session.leadCount === undefined ? null : `${session.leadCount} leads`,
                  relativeTime(session.updatedAt)
                ]
                  .filter(Boolean)
                  .join(" · ")}
              </span>
            </button>
          );
        })}
      </div>
    </>
  );
}
