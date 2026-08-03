import { useCallback, useEffect, useRef, useState } from "react";

import { UnauthorizedError, redirectToLogin, type AdminApi } from "src/admin/adminApi";
import type { SessionDetail, SessionSummary } from "src/admin/types";

const REFRESH_INTERVAL_MS = 3000;

export interface AdminConsole {
  sessions: SessionSummary[];
  selectedId: string | null;
  selected: SessionDetail | null;
  /** Unix seconds, matching every other timestamp here. Null until the first
   * successful poll. */
  lastUpdated: number | null;
  /** A transport failure worth telling the operator about, or null. */
  error: string | null;
  isLoading: boolean;
  select: (sessionId: string) => void;
  sendStaffMessage: (content: string) => Promise<void>;
}

/**
 * The console's live view of every conversation.
 *
 * Polling replaces the two lists it owns rather than the page: the previous
 * implementation re-rendered the whole document every two seconds and then
 * restored scroll positions and caret offsets by hand to hide the damage. Here
 * the poll updates data, and anything the operator is in the middle of — a
 * half-typed reply, a scroll position — is component state that polling never
 * touches.
 *
 * A hidden tab does not poll. An operator with the console open on a second
 * monitor should not keep the admin API busy all afternoon.
 */
export function useAdminConsole(api: AdminApi): AdminConsole {
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selected, setSelected] = useState<SessionDetail | null>(null);
  const [lastUpdated, setLastUpdated] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isLoading, setLoading] = useState(true);

  // The poller reads the selection through a ref so that changing it does not
  // tear down and restart the interval. Every write happens in an event handler
  // or in the poll itself, never while rendering.
  const selectedIdRef = useRef<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const rows = await api.sessions();
      setSessions(rows);

      const current = selectedIdRef.current ?? rows[0]?.sessionId ?? null;
      if (current !== selectedIdRef.current) {
        selectedIdRef.current = current;
        setSelectedId(current);
      }
      setSelected(current ? await api.session(current) : null);
      setLastUpdated(Date.now() / 1000);
      setError(null);
    } catch (reason) {
      if (reason instanceof UnauthorizedError) {
        redirectToLogin();
        return;
      }
      setError("Could not reach the admin API. Retrying…");
    } finally {
      setLoading(false);
    }
  }, [api]);

  useEffect(() => {
    let timer = 0;
    const tick = () => {
      if (!document.hidden) void refresh();
    };
    const start = () => {
      window.clearInterval(timer);
      timer = window.setInterval(tick, REFRESH_INTERVAL_MS);
    };
    const onVisibilityChange = () => {
      if (!document.hidden) void refresh();
    };

    // The first poll is this subscription's initial value, and it arrives
    // asynchronously rather than during the effect.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void refresh();
    start();
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [refresh]);

  const select = useCallback(
    (sessionId: string) => {
      selectedIdRef.current = sessionId;
      setSelectedId(sessionId);
      setSelected(null);
      void (async () => {
        try {
          setSelected(await api.session(sessionId));
        } catch (reason) {
          if (reason instanceof UnauthorizedError) redirectToLogin();
        }
      })();
    },
    [api]
  );

  const sendStaffMessage = useCallback(
    async (content: string) => {
      const sessionId = selectedIdRef.current;
      if (!sessionId) return;
      try {
        await api.sendStaffMessage(sessionId, content);
        await refresh();
      } catch (reason) {
        if (reason instanceof UnauthorizedError) {
          redirectToLogin();
          return;
        }
        setError("The reply was not delivered. Try again.");
      }
    },
    [api, refresh]
  );

  return {
    sessions,
    selectedId,
    selected,
    lastUpdated,
    error,
    isLoading,
    select,
    sendStaffMessage
  };
}
