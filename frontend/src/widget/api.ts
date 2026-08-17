/** Chat backend transport. Every request the widget makes goes through here. */

import type {
  ChatRequest,
  ChatTurnResponse,
  ConfirmationRequest,
  ConsentGrantRequest,
  ConsentGrantResponse,
  FeedbackRequest,
  FeedbackResponse,
  OpenSessionRequest,
  PendingBooking,
  ServerMessage,
  ServerSession,
  SessionSnapshot,
  SourceView,
  TenantConfig,
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
 * The backend rejected the presented credential — missing, forged, expired, or
 * naming a conversation that no longer exists. The caller can recover by
 * opening a fresh session.
 */
export class CredentialRejectedError extends Error {
  readonly status: number;

  constructor(status: number) {
    super(`Visitor credential rejected with ${status}`);
    this.status = status;
  }
}

/**
 * The backend cannot show a source: absent, superseded, expired, revoked, or
 * another tenant's. Every reason is the same bounded failure to the widget,
 * so a citation cannot be used to probe what a tenant has.
 */
export class SourceUnavailableError extends Error {
  constructor() {
    super("This source is no longer available.");
  }
}

/** Wire shapes the FastAPI backend actually returns (snake_case). */
interface WireTurn {
  session_id: string;
  turn_id: string | null;
  reply: string;
  pending: {
    awaiting: string;
    service: string;
    slot: string;
    customer_name?: string;
    address?: string;
    contact?: string;
    summary?: string;
  } | null;
  committed: Array<{ action: string; reference: string; replayed: boolean }>;
  citations?: Array<{
    source_id: string;
    title: string;
    source_name: string;
    location: string;
    revision: number;
    effective_at: string;
  }>;
  provenance: { model_name: string; graph_version: string; prompt_version: string };
  credential: string;
}

interface WireSourceView {
  source_id: string;
  title: string;
  source_name: string;
  location: string;
  text: string;
  revision: number;
  effective_at: string;
}

export interface WireTenant {
  name: string;
  assistant_name: string;
  tagline: string;
  address: string;
  phone: string;
  hours: string;
  booking_enabled: boolean;
  lead_capture_enabled: boolean;
  proactive_lead_capture: boolean;
  services: string[];
  quick_actions: string[];
  site_headline: string;
  site_description: string;
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
        address: wire.pending.address ?? "",
        contact: wire.pending.contact ?? "",
        summary: wire.pending.summary ?? ""
      }
    : null;
  const provenance: TurnProvenance = {
    modelName: wire.provenance.model_name,
    graphVersion: wire.provenance.graph_version,
    promptVersion: wire.provenance.prompt_version
  };
  return {
    sessionId: wire.session_id,
    turnId: wire.turn_id ?? null,
    reply: wire.reply,
    pending,
    committed: wire.committed.map((c) => ({
      action: c.action,
      reference: c.reference,
      replayed: c.replayed
    })),
    // Absent on a reply from an older backend; the contract sends it on every
    // turn, and the widget must render nothing rather than assume it exists.
    citations: (wire.citations ?? []).map((c) => ({
      sourceId: c.source_id,
      title: c.title,
      sourceName: c.source_name,
      location: c.location,
      revision: c.revision,
      effectiveAt: c.effective_at
    })),
    provenance,
    credential: wire.credential
  };
}

function normalizeTenant(wire: WireTenant): TenantConfig {
  return {
    name: wire.name,
    assistantName: wire.assistant_name,
    tagline: wire.tagline,
    site: { headline: wire.site_headline, description: wire.site_description },
    address: wire.address,
    phone: wire.phone,
    hours: wire.hours,
    bookingEnabled: wire.booking_enabled,
    leadCaptureEnabled: wire.lead_capture_enabled,
    proactiveLeadCapture: wire.proactive_lead_capture,
    services: wire.services,
    quickActions: wire.quick_actions
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
    const payload = (await response.json()) as { tenants: Record<string, WireTenant> };
    return Object.fromEntries(
      Object.entries(payload.tenants).map(([id, tenant]) => [id, normalizeTenant(tenant)])
    );
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
      body: JSON.stringify({ tenant_id: body.tenantId })
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
      // 401 is a credential the server no longer accepts; 404 is a credential
      // whose conversation no longer resolves (deleted or moved). Both mean the
      // stored token is useless and the caller must open a fresh session.
      if (response.status === 401 || response.status === 404) {
        throw new CredentialRejectedError(response.status);
      }
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
            address: wire.pending.address ?? "",
            contact: wire.pending.contact ?? "",
            summary: wire.pending.summary ?? ""
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

  /**
   * Resolve a citation to the authorized view of its source (`RAG-005`).
   *
   * The widget never assumes a source is still answerable: the server rechecks
   * tenant, audience, and quarantine on every read and answers 404 for every
   * reason a chunk is not retrievable. Any non-OK response is the same bounded
   * `SourceUnavailableError`, so the failure cannot leak whether the source
   * exists.
   *
   * @throws {SourceUnavailableError} when the source is not answerable.
   */
  async source(credential: string, sourceId: string): Promise<SourceView> {
    const response = await fetch(this.url(`/api/chat/sources/${encodeURIComponent(sourceId)}`), {
      headers: { [VISITOR_CREDENTIAL_HEADER]: credential }
    });
    if (!response.ok) throw new SourceUnavailableError();
    const wire = (await response.json()) as WireSourceView;
    return {
      sourceId: wire.source_id,
      title: wire.title,
      sourceName: wire.source_name,
      location: wire.location,
      text: wire.text,
      revision: wire.revision,
      effectiveAt: wire.effective_at
    };
  }

  /**
   * Rate one turn record (`FEAT-008`). The turn must belong to the credential's
   * conversation; the server refuses anything else.
   *
   * @throws {Error} when the backend rejects the rating.
   */
  async feedback(credential: string, body: FeedbackRequest): Promise<FeedbackResponse> {
    const response = await fetch(this.url("/api/chat/feedback"), {
      method: "POST",
      headers: ChatApi.credentialHeaders(credential),
      body: JSON.stringify({ turn_id: body.turnId, rating: body.rating, reason: body.reason })
    });
    if (!response.ok) throw new Error(`Feedback failed with ${response.status}`);
    const payload = (await response.json()) as {
      turn_id: string;
      rating: "up" | "down";
      reason: string | null;
      created_at: string;
    };
    return {
      turnId: payload.turn_id,
      rating: payload.rating,
      reason: payload.reason,
      createdAt: payload.created_at
    };
  }
}
