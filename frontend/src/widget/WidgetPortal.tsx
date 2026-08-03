import { useMemo, type ReactNode } from "react";
import { createPortal } from "react-dom";

/**
 * Render into a mount element's shadow root.
 *
 * The shadow root is why an embedding page can neither style the widget's
 * internals nor collide with its element ids, and why the widget's own styles
 * never escape. A React portal keeps the widget inside the host application's
 * tree — so state and context flow into it normally — while its DOM lands
 * behind the boundary.
 */
export function WidgetPortal({ host, children }: { host: HTMLElement; children: ReactNode }) {
  const shadow = useMemo(() => host.shadowRoot ?? host.attachShadow({ mode: "open" }), [host]);
  return createPortal(children, shadow);
}
