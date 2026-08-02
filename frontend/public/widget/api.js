/** Chat backend transport. Every request the widget makes goes through here. */

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
export function resolveApiBaseUrl(mountElement = null) {
  const configured =
    window.CHAT_API_BASE_URL ||
    document.querySelector("script[data-api-base-url]")?.dataset.apiBaseUrl ||
    mountElement?.dataset.apiBaseUrl ||
    "";
  if (configured.trim()) {
    return configured.trim().replace(/\/+$/, "");
  }
  return window.location.protocol === "file:" ? "http://127.0.0.1:8000" : "";
}

export class ChatApi {
  constructor(baseUrl) {
    this.baseUrl = baseUrl;
  }

  url(path) {
    return `${this.baseUrl}${path}`;
  }

  /** @throws {Error} when the backend cannot supply tenant configuration. */
  async tenants() {
    const response = await fetch(this.url("/api/tenants"));
    if (!response.ok) {
      throw new Error("Unable to load tenant configuration from backend.");
    }
    const payload = await response.json();
    return payload.tenants;
  }

  /** @throws {Error} when the backend rejects the turn. */
  async chat(body) {
    const response = await fetch(this.url("/api/chat"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
    if (!response.ok) {
      throw new Error(`Chat request failed with ${response.status}`);
    }
    return response.json();
  }

  /** Resolves with `{ ok, payload }` so the caller can render field errors. */
  async book(body) {
    const response = await fetch(this.url("/api/book"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
    return { ok: response.ok, payload: await response.json() };
  }

  /** Returns null rather than throwing; transcript polling is best effort. */
  async session(sessionId) {
    try {
      const response = await fetch(
        this.url(`/api/chat/session?sessionId=${encodeURIComponent(sessionId)}`)
      );
      if (!response.ok) return null;
      const payload = await response.json();
      return payload.session ?? null;
    } catch (error) {
      return null;
    }
  }
}
