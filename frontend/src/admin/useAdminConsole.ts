import { useCallback, useEffect, useRef, useState } from "react";

import {
  AccessDeniedError,
  UnauthorizedError,
  redirectToLogin,
  type AdminApi
} from "src/admin/adminApi";
import type { SessionDetail, SessionSummary, TenantSummary } from "src/admin/types";

const REFRESH_INTERVAL_MS = 3000;

export interface AdminConsole {
  tenants: TenantSummary[];
  /** The tenant whose queue is open; null until the tenant list has arrived. */
  tenantId: string | null;
  sessions: SessionSummary[];
  selectedId: string | null;
  selected: SessionDetail | null;
  /** Unix seconds, matching every other timestamp here. Null until the first
   * successful poll. */
  lastUpdated: number | null;
  /** A transport failure worth telling the operator about, or null. */
  error: string | null;
  isLoading: boolean;
  selectTenant: (tenantId: string) => void;
  select: (sessionId: string) => void;
  sendStaffMessage: (content: string) => Promise<void>;
}

/**
 * The console's live view of one tenant's conversations.
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
  const [tenants, setTenants] = useState<TenantSummary[]>([]);
  const [tenantId, setTenantId] = useState<string | null>(null);
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selected, setSelected] = useState<SessionDetail | null>(null);
  const [lastUpdated, setLastUpdated] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isLoading, setLoading] = useState(true);

  // The poller reads the selection and the tenant through refs so that changing
  // either does not tear down and restart the interval. Every write happens in
  // an event handler or in the poll itself, never while rendering.
  const selectedIdRef = useRef<string | null>(null);
  const tenantIdRef = useRef<string | null>(null);

  // Interval ticks, tenant switches, manual selections, and post-send refreshes
  // all fetch the same two pieces of state, and a slower earlier response would
  // otherwise land last and win — showing one tenant's sessions under another
  // tenant's name. Every read claims a generation before its first
  // await and may only publish while it is still the newest.
  const generationRef = useRef(0);
  const claimGeneration = useCallback(() => {
    const generation = (generationRef.current += 1);
    return () => generation === generationRef.current;
  }, []);

  const refresh = useCallback(async () => {
    const openTenant = tenantIdRef.current;
    if (!openTenant) return;
    const isCurrent = claimGeneration();
    try {
      const rows = await api.sessions(openTenant);
      if (!isCurrent()) return;
      setSessions(rows);

      const current = selectedIdRef.current ?? rows[0]?.sessionId ?? null;
      if (current !== selectedIdRef.current) {
        selectedIdRef.current = current;
        setSelectedId(current);
      }
      const detail = current ? await api.session(current, openTenant) : null;
      if (!isCurrent()) return;
      setSelected(detail);
      setLastUpdated(Date.now() / 1000);
      setError(null);
    } catch (reason) {
      if (reason instanceof UnauthorizedError) {
        redirectToLogin();
        return;
      }
      if (!isCurrent()) return;
      setError("Could not reach the admin API. Retrying…");
    } finally {
      if (isCurrent()) setLoading(false);
    }
  }, [api, claimGeneration]);

  // The tenant list is stable enough to fetch once: it is the membership the
  // gateway resolved for this operator, which changes only when an
  // administrator grants or revokes access.
  useEffect(() => {
    let cancelled = false;
    void api
      .tenants()
      .then((rows) => {
        if (cancelled) return;
        setTenants(rows);
        const first = rows[0]?.tenantId ?? null;
        tenantIdRef.current = first;
        setTenantId(first);
        // The first poll above could not run without a tenant, so the one
        // that filled the queue is this one.
        if (first) void refresh();
      })
      .catch((reason: unknown) => {
        if (cancelled) return;
        if (reason instanceof UnauthorizedError) {
          redirectToLogin();
          return;
        }
        if (reason instanceof AccessDeniedError) {
          setError(
            "This account is signed in but has no TenantChat role. Ask a Keycloak administrator to assign an admin group."
          );
          setLoading(false);
          return;
        }
        setError("Could not reach the admin API. Retrying…");
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [api, refresh]);

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
    void refresh();
    start();
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [refresh]);

  const selectTenant = useCallback(
    (nextTenant: string) => {
      if (nextTenant === tenantIdRef.current) return;
      tenantIdRef.current = nextTenant;
      selectedIdRef.current = null;
      setTenantId(nextTenant);
      setSelectedId(null);
      setSelected(null);
      setLoading(true);
      void refresh();
    },
    [refresh]
  );

  const select = useCallback(
    (sessionId: string) => {
      const openTenant = tenantIdRef.current;
      if (!openTenant) return;
      selectedIdRef.current = sessionId;
      setSelectedId(sessionId);
      setSelected(null);
      const isCurrent = claimGeneration();
      void (async () => {
        try {
          const detail = await api.session(sessionId, openTenant);
          if (isCurrent()) setSelected(detail);
        } catch (reason) {
          if (reason instanceof UnauthorizedError) redirectToLogin();
        }
      })();
    },
    [api, claimGeneration]
  );

  const sendStaffMessage = useCallback(
    async (content: string) => {
      const sessionId = selectedIdRef.current;
      const openTenant = tenantIdRef.current;
      if (!sessionId || !openTenant) return;
      try {
        await api.sendStaffMessage(sessionId, openTenant, content);
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
    tenants,
    tenantId,
    sessions,
    selectedId,
    selected,
    lastUpdated,
    error,
    isLoading,
    selectTenant,
    select,
    sendStaffMessage
  };
}
