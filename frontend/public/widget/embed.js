import { ChatApi, resolveApiBaseUrl } from "./api.js";
import { ChatWidget } from "./widget.js";

/**
 * Mount the widget into an embedder's element and start it.
 *
 * Resolves once the widget is on screen, either configured or showing its
 * failure state; it never rejects, because a broken backend must not leave an
 * unhandled rejection in the embedding page's console.
 *
 * @returns {Promise<{widget: ChatWidget, tenants: object|null}>} `tenants` is
 *   null when the backend could not be reached.
 */
export async function mountWidget(mount, { tenantId = null } = {}) {
  const api = new ChatApi(resolveApiBaseUrl(mount));
  const widget = new ChatWidget({ mount, api });
  const selected = tenantId || mount.dataset.companyId || "apex";
  mount.dataset.companyId = selected;

  try {
    const tenants = await api.tenants();
    widget.setTenant(selected, tenants[selected]);
    widget.startPolling();
    return { widget, tenants };
  } catch (error) {
    widget.showFatalError(error.message);
    return { widget, tenants: null };
  }
}
