/** Chat backend transport. Every request the widget makes goes through here. */

import type {
  BookingFailure,
  BookingRequest,
  BookingResponse,
  ChatRequest,
  ChatResponse,
  SessionSnapshot,
  TenantDirectory
} from "src/widget/types";

/**
 * Resolve the backend origin an embed should call.
 *
 * Checked in order: an explicit `window.CHAT_API_BASE_URL`, a
 * `data-api-base-url` attribute on a script tag, then the same attribute on the
 * mount element. An embed served from `file:` falls back to the local prototype
 * server; anything else uses same-origin relative paths.
 *
 * The script tag is found by query rather than `document.currentScript`, which
 * is always null inside a module.
 */
export function resolveApiBaseUrl(mountElement: HTMLElement | null = null): string {
  const configured =
    window.CHAT_API_BASE_URL ??
    document.querySelector<HTMLScriptElement>("script[data-api-base-url]")?.dataset.apiBaseUrl ??
    mountElement?.dataset.apiBaseUrl ??
    "";
  if (configured.trim()) {
    return configured.trim().replace(/\/+$/, "");
  }
  return window.location.protocol === "file:" ? "http://127.0.0.1:8000" : "";
}

const JSON_HEADERS = { "Content-Type": "application/json" };

export class ChatApi {
  readonly baseUrl: string;

  constructor(baseUrl: string) {
    this.baseUrl = baseUrl;
  }

  url(path: string): string {
    return `${this.baseUrl}${path}`;
  }

  /** @throws {Error} when the backend cannot supply tenant configuration. */
  async tenants(): Promise<TenantDirectory> {
    const response = await fetch(this.url("/api/tenants"));
    if (!response.ok) {
      throw new Error("Unable to load tenant configuration from backend.");
    }
    const payload = (await response.json()) as { tenants: TenantDirectory };
    return payload.tenants;
  }

  /** @throws {Error} when the backend rejects the turn. */
  async chat(body: ChatRequest): Promise<ChatResponse> {
    const response = await fetch(this.url("/api/chat"), {
      method: "POST",
      headers: JSON_HEADERS,
      body: JSON.stringify(body)
    });
    if (!response.ok) {
      throw new Error(`Chat request failed with ${response.status}`);
    }
    return (await response.json()) as ChatResponse;
  }

  /** Resolves with `{ ok, payload }` so the caller can render field errors. */
  async book(
    body: BookingRequest
  ): Promise<{ ok: true; payload: BookingResponse } | { ok: false; payload: BookingFailure }> {
    const response = await fetch(this.url("/api/book"), {
      method: "POST",
      headers: JSON_HEADERS,
      body: JSON.stringify(body)
    });
    const payload = (await response.json()) as BookingResponse & BookingFailure;
    return response.ok ? { ok: true, payload } : { ok: false, payload };
  }

  /** Returns null rather than throwing; transcript polling is best effort. */
  async session(sessionId: string): Promise<SessionSnapshot | null> {
    try {
      const response = await fetch(
        this.url(`/api/chat/session?sessionId=${encodeURIComponent(sessionId)}`)
      );
      if (!response.ok) return null;
      const payload = (await response.json()) as { session?: SessionSnapshot };
      return payload.session ?? null;
    } catch {
      return null;
    }
  }
}
