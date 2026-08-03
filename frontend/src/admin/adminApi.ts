/**
 * Admin backend transport.
 *
 * Every response can be a 401: the gateway redirects unauthenticated *page*
 * requests to the login flow but answers API requests with a plain 401, so the
 * console has to recognise a lost session itself and send the browser back
 * through the gateway rather than render an empty console.
 */

import type { SessionDetail, SessionSummary } from "src/admin/types";

export class UnauthorizedError extends Error {
  constructor() {
    super("The admin session has expired.");
    this.name = "UnauthorizedError";
  }
}

function resolveAdminApiBaseUrl(): string {
  const configured =
    window.CHAT_API_BASE_URL ??
    document.querySelector<HTMLScriptElement>("script[data-api-base-url]")?.dataset.apiBaseUrl ??
    document.body.dataset.apiBaseUrl ??
    "";
  if (configured.trim()) {
    return configured.trim().replace(/\/+$/, "");
  }
  return window.location.protocol === "file:" ? "http://127.0.0.1:8000" : "";
}

export class AdminApi {
  readonly baseUrl: string;
  private csrfToken: string | null = null;

  constructor(baseUrl: string = resolveAdminApiBaseUrl()) {
    this.baseUrl = baseUrl;
  }

  private async request(path: string, init?: RequestInit): Promise<Response> {
    const response = await fetch(`${this.baseUrl}${path}`, init);
    if (response.status === 401) throw new UnauthorizedError();
    return response;
  }

  /** @throws {UnauthorizedError} when the admin session has expired. */
  async sessions(): Promise<SessionSummary[]> {
    const response = await this.request("/api/admin/chats");
    if (!response.ok) throw new Error(`Chat list failed with ${response.status}`);
    const payload = (await response.json()) as { sessions?: SessionSummary[] };
    return payload.sessions ?? [];
  }

  /** Returns null when the session has since been removed. */
  async session(sessionId: string): Promise<SessionDetail | null> {
    const response = await this.request(`/api/admin/chats/${encodeURIComponent(sessionId)}`);
    if (!response.ok) return null;
    const payload = (await response.json()) as { session?: SessionDetail };
    return payload.session ?? null;
  }

  async sendStaffMessage(sessionId: string, content: string): Promise<void> {
    const response = await this.request(
      `/api/admin/chats/${encodeURIComponent(sessionId)}/messages`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": await this.csrf() },
        body: JSON.stringify({ content })
      }
    );
    if (!response.ok) throw new Error(`Sending the staff message failed with ${response.status}`);
  }

  private async csrf(): Promise<string> {
    if (this.csrfToken) return this.csrfToken;
    const response = await this.request("/api/admin/csrf-token");
    if (!response.ok) return "";
    const payload = (await response.json()) as { csrfToken?: string };
    this.csrfToken = payload.csrfToken ?? "";
    return this.csrfToken;
  }
}

/**
 * Send the browser back through the gateway, which starts the OIDC flow.
 *
 * Development has no auth proxy, so a `file:` page stops instead of looping.
 */
export function redirectToLogin(): void {
  if (window.location.protocol === "file:") return;
  window.location.href = "/admin/";
}
