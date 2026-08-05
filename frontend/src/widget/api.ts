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

/** The header that carries the server-issued visitor credential (SEC-002). */
export const VISITOR_CREDENTIAL_HEADER = "X-Visitor-Credential";

/**
 * The backend rejected the presented credential — missing, forged, or expired.
 * The caller can recover by opening a fresh session.
 */
export class CredentialRejectedError extends Error {
  readonly status: number;

  constructor(status: number) {
    super(`Visitor credential rejected with ${status}`);
    this.status = status;
  }
}

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
  credential: string;
}

interface WireSession {
  session: { session_id: string };
  credential: string;
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
    provenance,
    credential: wire.credential
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
   * Open a conversation and return the server-issued session and credential.
   *
   * The credential is minted by the backend, never chosen by the visitor, so
   * an unguessable signed value is what lets it name a conversation at all
   * (`SEC-002`).
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
    return { sessionId: payload.session.session_id, credential: payload.credential };
  }

  private static credentialHeaders(credential: string): HeadersInit {
    return { ...JSON_HEADERS, [VISITOR_CREDENTIAL_HEADER]: credential };
  }

  private static async unwrap(response: Response, label: string): Promise<unknown> {
    if (!response.ok) {
      if (response.status === 401) throw new CredentialRejectedError(response.status);
      throw new Error(`${label} failed with ${response.status}`);
    }
    return response.json();
  }

  /** @throws {Error} when the backend rejects the turn. */
  async chat(credential: string, body: ChatRequest): Promise<ChatTurnResponse> {
    const response = await fetch(this.url("/api/chat"), {
      method: "POST",
      headers: ChatApi.credentialHeaders(credential),
      body: JSON.stringify({ message: body.message })
    });
    return normalizeTurn((await ChatApi.unwrap(response, "Chat request")) as WireTurn);
  }

  /** Answer a booking the assistant proposed, approving or declining it. */
  async confirm(credential: string, body: ConfirmationRequest): Promise<ChatTurnResponse> {
    const response = await fetch(this.url("/api/chat/confirmation"), {
      method: "POST",
      headers: ChatApi.credentialHeaders(credential),
      body: JSON.stringify({ decision: body.decision })
    });
    return normalizeTurn((await ChatApi.unwrap(response, "Confirmation")) as WireTurn);
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
      headers: ChatApi.credentialHeaders(body.credential),
      body: JSON.stringify({ purposes: body.purposes })
    });
    if (!response.ok) {
      throw new Error(`Consent was not accepted (${response.status}).`);
    }
    return (await response.json()) as ConsentGrantResponse;
  }

  /** Returns null rather than throwing; transcript polling is best effort. */
  async session(credential: string): Promise<SessionSnapshot | null> {
    try {
      const response = await fetch(this.url("/api/chat/session"), {
        headers: { [VISITOR_CREDENTIAL_HEADER]: credential }
      });
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
        credential: wire.credential,
        messages: normalizeMessages(wire),
        pending
      };
    } catch {
      return null;
    }
  }
}
