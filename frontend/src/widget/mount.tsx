import { useMemo } from "react";
import { createRoot, type Root } from "react-dom/client";

import { ChatApi, resolveApiBaseUrl } from "src/widget/api";
import { useTenants } from "src/widget/useTenants";
import { WidgetSurface } from "src/widget/WidgetSurface";

const DEFAULT_TENANT = "apex";

/**
 * The widget as a customer site gets it: one mount element, no React.
 *
 * Everything configurable is a `data-` attribute on that element, because the
 * embedding page is plain HTML written by somebody who has never seen this
 * repository.
 *
 *   <div id="tenant-chat" data-company-id="clearview" data-open="true"></div>
 */
function EmbeddedWidget({ host }: { host: HTMLElement }) {
  const api = useMemo(() => new ChatApi(resolveApiBaseUrl(host)), [host]);
  const { tenants, error } = useTenants(api);
  const tenantId = host.dataset.companyId ?? DEFAULT_TENANT;
  const config = tenants?.[tenantId] ?? null;
  const unknownTenant =
    tenants !== null && config === null ? `No chat is configured for “${tenantId}”.` : null;

  return (
    <WidgetSurface
      api={api}
      tenantId={tenantId}
      config={config}
      error={error ?? unknownTenant}
      defaultOpen={host.dataset.open === "true"}
    />
  );
}

export interface MountedWidget {
  unmount: () => void;
}

/**
 * Mount the widget into an embedder's element and start it.
 *
 * Never throws and never rejects: a broken backend has to leave the embedding
 * page's console clean and the visitor with a stated reason, not an unhandled
 * rejection and an empty corner of the screen.
 */
export function mountWidget(host: HTMLElement): MountedWidget {
  const shadow = host.shadowRoot ?? host.attachShadow({ mode: "open" });
  const root: Root = createRoot(shadow);
  root.render(<EmbeddedWidget host={host} />);
  return { unmount: () => root.unmount() };
}
