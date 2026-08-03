import { useState } from "react";

import type { ChatApi } from "src/widget/api";
import { ChatWidget } from "src/widget/ChatWidget";
import { FatalError } from "src/widget/components/FatalError";
import { WIDGET_STYLESHEET } from "src/widget/styles";
import type { TenantConfig } from "src/widget/types";

export interface WidgetSurfaceProps {
  api: ChatApi;
  tenantId: string;
  /** Null until the backend answers, or when the tenant id is unknown. */
  config: TenantConfig | null;
  error: string | null;
  defaultOpen: boolean;
}

/**
 * Everything inside the shadow root: the stylesheet and one tenant's widget.
 *
 * The panel's open state lives here rather than in `ChatWidget` so switching
 * tenants — which replaces `ChatWidget` — does not close a panel the visitor
 * has open.
 */
export function WidgetSurface({ api, tenantId, config, error, defaultOpen }: WidgetSurfaceProps) {
  const [isOpen, setOpen] = useState(defaultOpen);

  return (
    <>
      <style>{WIDGET_STYLESHEET}</style>
      {error !== null && <FatalError message={error} />}
      {error === null && config !== null && (
        <ChatWidget
          key={tenantId}
          api={api}
          tenantId={tenantId}
          config={config}
          isOpen={isOpen}
          onOpen={() => setOpen(true)}
          onClose={() => setOpen(false)}
        />
      )}
    </>
  );
}
