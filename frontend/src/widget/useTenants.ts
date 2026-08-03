import { useEffect, useState } from "react";

import type { ChatApi } from "src/widget/api";
import type { TenantDirectory } from "src/widget/types";

export interface TenantsState {
  tenants: TenantDirectory | null;
  /** A visitor-safe reason the widget cannot start, or null while loading. */
  error: string | null;
}

/**
 * Load the tenant directory the backend owns.
 *
 * Branding, copy, and the actions a tenant permits all come from the server;
 * nothing here is baked into the bundle, so a policy change does not need a
 * redeploy of the widget.
 */
export function useTenants(api: ChatApi): TenantsState {
  const [state, setState] = useState<TenantsState>({ tenants: null, error: null });

  useEffect(() => {
    let cancelled = false;
    api.tenants().then(
      (tenants) => {
        if (!cancelled) setState({ tenants, error: null });
      },
      (reason: unknown) => {
        if (cancelled) return;
        const error = reason instanceof Error ? reason.message : "The chat service is unreachable.";
        setState({ tenants: null, error });
      }
    );
    return () => {
      cancelled = true;
    };
  }, [api]);

  return state;
}
