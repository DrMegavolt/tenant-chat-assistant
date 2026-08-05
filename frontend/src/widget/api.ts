/** Chat backend transport. Every request the widget makes goes through here. */

import type {
  ChatRequest,
  ChatTurnResponse,
  ConfirmationRequest,
  ConsentGrantRequest,
  ConsentGrantResponse,
  OpenSessionRequest,
  PendingBooking,
  ServerMessage,
  ServerSession,
  SessionSnapshot,
  TenantDirectory,
  TurnProvenance,
  WireMessageRole
} from "src/widget/types";

/**
 * Resolve the backend origin an embed should call.
 *
 * Checked in order: an explicit `window.CHAT_API_BASE_URL`, a
 * `data-api-base-url` attribute on a script tag, then the same attribute on the
 * mount element. An embed served from `file:` falls back to the local API
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
  return window.location.protocol === "file:" ? "http://127.0.0.1:8080" : "";
}

const JSON_HEADERS = { "Content-Type": "application/json" };

/** Wire shapes the FastAPI backend actually returns (snake_case). */
interface WireTurn {
  session_id: string;
  reply: string;
  pending: {
    awaiting: string;
    service: string;
    slot: string;
    customer_name?: string;
    address?: string;
  } | null;
  committed: Array<{ action: string; reference: string; replayed: boolean }>;
  provenance: { model_name: string; graph_version: string; prompt_version: string };
}

interface WireSession {
  session: { session_id: string };
  messages?: Array<{
    message_id: string;
    role: WireMessageRole;
    content: string;
    created_at: string;
  }>;
  pending?: WireTurn["pending"];
}

function normalizeTurn(wire: WireTurn): ChatTurnResponse {
  const pending: PendingBooking | null = wire.pending
    ? {
        awaiting: wire.pending.awaiting,
        service: wire.pending.service,
        slot: wire.pending.slot,
        customerName: wire.pending.customer_name ?? "",
        address: wire.pending.address ?? ""
      }
    : null;
  const provenance: TurnProvenance = {
    modelName: wire.provenance.model_name,
    graphVersion: wire.provenance.graph_version,
    promptVersion: wire.provenance.prompt_version
  };
  return {
    sessionId: wire.session_id,
    reply: wire.reply,
    pending,
    committed: wire.committed.map((c) => ({
      action: c.action,
      reference: c.reference,
      replayed: c.replayed
    })),
    provenance
  };
}

function normalizeMessages(wire: WireSession): ServerMessage[] {
  return (wire.messages ?? []).map((m) => ({
    messageId: m.message_id,
    role: m.role,
    content: m.content,
    createdAt: m.created_at
  }));
}

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

  /**
   * Open a conversation and return the server-issued session ID.
   *
   * The ID is minted by the backend, never chosen by the visitor, so an
   * unguessable value is what lets it name a conversation at all (`SEC-002`).
   *
   * @throws {Error} when the backend cannot open a session.
   */
  async openSession(body: OpenSessionRequest): Promise<ServerSession> {
    const response = await fetch(this.url("/api/chat/session"), {
      method: "POST",
      headers: JSON_HEADERS,
      body: JSON.stringify(body)
    });
    if (!response.ok) {
      throw new Error(`Unable to open a conversation (${response.status}).`);
    }
    const payload = (await response.json()) as WireSession;
    return { sessionId: payload.session.session_id };
  }

  /** @throws {Error} when the backend rejects the turn. */
  async chat(body: ChatRequest): Promise<ChatTurnResponse> {
    const response = await fetch(this.url("/api/chat"), {
      method: "POST",
      headers: JSON_HEADERS,
      body: JSON.stringify({
        tenant_id: body.tenantId,
        session_id: body.sessionId,
        message: body.message
      })
    });
    if (!response.ok) {
      throw new Error(`Chat request failed with ${response.status}`);
    }
    return normalizeTurn((await response.json()) as WireTurn);
  }

  /** Answer a booking the assistant proposed, approving or declining it. */
  async confirm(body: ConfirmationRequest): Promise<ChatTurnResponse> {
    const response = await fetch(this.url("/api/chat/confirmation"), {
      method: "POST",
      headers: JSON_HEADERS,
      body: JSON.stringify({
        tenant_id: body.tenantId,
        session_id: body.sessionId,
        decision: body.decision
      })
    });
    if (!response.ok) {
      throw new Error(`Confirmation failed with ${response.status}`);
    }
    return normalizeTurn((await response.json()) as WireTurn);
  }

  /**
   * Record a consent grant for one session.
   *
   * Contact-bearing actions are refused by the backend until this has run; the
   * widget calls it at the moment the visitor agrees, and the server derives the
   * statement it records from the tenant's own policy rather than accepting one.
   *
   * @throws {Error} when the backend rejects the grant.
   */
  async consent(body: ConsentGrantRequest): Promise<ConsentGrantResponse> {
    const response = await fetch(this.url("/api/chat/consent"), {
      method: "POST",
      headers: JSON_HEADERS,
      body: JSON.stringify({
        tenant_id: body.tenantId,
        session_id: body.sessionId,
        purposes: body.purposes
      })
    });
    if (!response.ok) {
      throw new Error(`Consent was not accepted (${response.status}).`);
    }
    return (await response.json()) as ConsentGrantResponse;
  }

  /** Returns null rather than throwing; transcript polling is best effort. */
  async session(sessionId: string, tenantId: string): Promise<SessionSnapshot | null> {
    try {
      const response = await fetch(
        this.url(
          `/api/chat/session/${encodeURIComponent(sessionId)}?tenant_id=${encodeURIComponent(tenantId)}`
        )
      );
      if (!response.ok) return null;
      const wire = (await response.json()) as WireSession;
      const pending: PendingBooking | null = wire.pending
        ? {
            awaiting: wire.pending.awaiting,
            service: wire.pending.service,
            slot: wire.pending.slot,
            customerName: wire.pending.customer_name ?? "",
            address: wire.pending.address ?? ""
          }
        : null;
      return {
        sessionId: wire.session.session_id,
        messages: normalizeMessages(wire),
        pending
      };
    } catch {
      return null;
    }
  }
}
