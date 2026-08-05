/** The shapes the widget exchanges with the chat backend. */

export interface TenantSiteCopy {
  headline: string;
  description: string;
}

/** The public view of a tenant policy, as `GET /api/tenants` returns it. */
export interface TenantConfig {
  name: string;
  assistantName: string;
  tagline: string;
  site: TenantSiteCopy;
  address: string;
  phone: string;
  hours: string;
  pricingPolicy: "never" | "fixed";
  bookingEnabled: boolean;
  leadCaptureEnabled: boolean;
  proactiveLeadCapture: boolean;
  services: string[];
  quickActions: string[];
}

export type TenantDirectory = Record<string, TenantConfig>;

export type MessageRole = "user" | "assistant";

/**
 * The wire role of one transcript message, as the API's `MessageRole` reports
 * it. Kept distinct from the widget's rendering roles so a staff reply can be
 * told from a model turn.
 */
export type WireMessageRole = "visitor" | "assistant" | "staff" | "system" | "tool";

/**
 * Who produced a message, which is not the same as its chat role: a staff reply
 * and a proactive nudge are both `assistant` turns to the model but are shown,
 * and announced, differently.
 */
export type MessageSource = "user" | "assistant" | "admin" | "proactive";

/**
 * A server-issued conversation identity (`POST /api/chat/session`). The
 * credential is the signed token that names the conversation on later
 * requests; the session id is for display and provenance only.
 */
export interface ServerSession {
  sessionId: string;
  credential: string;
}

/** The server's view of one transcript message (`TranscriptMessage`). */
export interface ServerMessage {
  messageId: string;
  role: WireMessageRole;
  content: string;
  createdAt: string;
}

export interface ToolEvent {
  name: string;
  arguments: Record<string, unknown>;
  result: Record<string, unknown>;
}

/** One entry in the visible transcript, in the order the visitor saw it. */
export type TranscriptEntry =
  | { kind: "message"; id: string; role: MessageRole; text: string; source: MessageSource }
  | { kind: "tool"; id: string; event: ToolEvent }
  | { kind: "booking"; id: string; pending: PendingBooking };

/**
 * A booking the assistant proposed that the customer still has to approve.
 * Mirrors the API's `PendingConfirmation` so the widget can render a review
 * before deciding.
 */
export interface PendingBooking {
  awaiting: string;
  service: string;
  slot: string;
  customerName: string;
  address: string;
}

/** One thing the conversation caused, as `CommittedActionSummary` reports it. */
export interface CommittedAction {
  action: string;
  reference: string;
  replayed: boolean;
}

/** Component versions an answer is attributable to (`TurnProvenance`). */
export interface TurnProvenance {
  modelName: string;
  graphVersion: string;
  promptVersion: string;
}

/** A request to open a conversation (`POST /api/chat/session`). */
export interface OpenSessionRequest {
  tenantId: string;
}

/**
 * A request to send one visitor turn (`POST /api/chat`). The conversation is
 * named by the `X-Visitor-Credential` header, never by body fields.
 */
export interface ChatRequest {
  message: string;
}

/** The purposes a visitor grants for one session (`POST /api/chat/consent`). */
export interface ConsentGrantRequest {
  /** The signed credential naming the session; the server reads no body identity. */
  credential: string;
  purposes: ConsentPurpose[];
}

/** What the session holds after a grant, echoed back by the server. */
export interface ConsentGrantResponse {
  purposes: ConsentPurpose[];
  statement: string;
  granted_at: string;
}

export type ConsentPurpose = "booking" | "follow_up";

/** A response to a proposed booking (`POST /api/chat/confirmation`). */
export interface ConfirmationRequest {
  decision: "approved" | "declined";
}

/** The response to one turn, pending and reply being alternatives. */
export interface ChatTurnResponse {
  sessionId: string;
  reply: string;
  pending: PendingBooking | null;
  committed: CommittedAction[];
  provenance: TurnProvenance;
  /** A freshly reissued token that names the same conversation (SEC-002). */
  credential: string;
}

export interface ConsentRecord {
  grantedAt: string;
  statement: string;
}

/** The contact details a visitor types into the booking form. */
export interface BookingContact {
  customerName: string;
  address: string;
  contact: string;
}

export interface SessionSnapshot {
  sessionId: string;
  credential: string;
  messages?: ServerMessage[];
  pending?: PendingBooking | null;
}
