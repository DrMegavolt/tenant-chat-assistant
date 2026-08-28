import { useMemo } from "react";
import { createRoot, type Root } from "react-dom/client";

import { ErrorBoundary } from "src/shared/ErrorBoundary";
import { ChatApi, resolveApiBaseUrl } from "src/widget/api";
import { FatalError } from "src/widget/components/FatalError";
import { useTenants } from "src/widget/useTenants";
import { WidgetSurface } from "src/widget/WidgetSurface";
import { WIDGET_STYLESHEET } from "src/widget/styles";

/**
 * The widget as a customer site gets it: one mount element, no React.
 *
 * Everything configurable is a `data-` attribute on that element, because the
 * embedding page is plain HTML written by somebody who has never seen this
 * repository.
 *
 *   <div id="tenant-chat" data-company-id="clearview" data-open="true"></div>
 */
function EmbeddedWidget({ host, tenantId }: { host: HTMLElement; tenantId: string }) {
  const api = useMemo(() => new ChatApi(resolveApiBaseUrl(host)), [host]);
  const { tenants, error } = useTenants(api);
  const config = tenants?.[tenantId] ?? null;
  const unknownTenant =
    tenants !== null && config === null ? `No chat is configured for “${tenantId}”.` : null;

  // A crash anywhere in the widget must cost the widget, not the host page's
  // console silence: the fallback carries the stylesheet, since the subtree
  // that normally renders it is the one that failed.
  return (
    <ErrorBoundary
      fallback={
        <>
          <style>{WIDGET_STYLESHEET}</style>
          <FatalError message="The chat could not be displayed. Reload the page to try again." />
        </>
      }
    >
      <WidgetSurface
        api={api}
        tenantId={tenantId}
        config={config}
        error={error ?? unknownTenant}
        defaultOpen={host.dataset.open === "true"}
      />
    </ErrorBoundary>
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
  const companyId = host.dataset.companyId;
  if (!companyId) {
    // A default tenant here would silently put one company's bot on another
    // company's site. Refusing loudly is the only safe behaviour for a missing
    // embed attribute; the integrator sees the console error, the visitor sees
    // nothing at all.
    console.error(
      "tenant-chat: the mount element is missing data-company-id. No widget was started."
    );
    return { unmount: () => {} };
  }
  const shadow = host.shadowRoot ?? host.attachShadow({ mode: "open" });
  const root: Root = createRoot(shadow);
  root.render(<EmbeddedWidget host={host} tenantId={companyId} />);
  return { unmount: () => root.unmount() };
}
