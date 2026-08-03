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
 * Who produced a message, which is not the same as its chat role: a staff reply
 * and a proactive nudge are both `assistant` turns to the model but are shown,
 * and announced, differently.
 */
export type MessageSource = "user" | "assistant" | "admin" | "proactive";

export interface ToolEvent {
  name: string;
  arguments: Record<string, unknown>;
  result: Record<string, unknown>;
}

/** One entry in the visible transcript, in the order the visitor saw it. */
export type TranscriptEntry =
  | { kind: "message"; id: string; role: MessageRole; text: string; source: MessageSource }
  | { kind: "tool"; id: string; event: ToolEvent }
  | { kind: "booking"; id: string; service: string; slots: string[] };

export interface ChatTurn {
  role: MessageRole;
  content: string;
  source: MessageSource;
}

export interface ChatRequest {
  tenantId: string;
  sessionId: string;
  messages: ChatTurn[];
}

export interface ChatResponse {
  reply: string;
  toolEvents?: ToolEvent[];
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

export interface BookingRequest extends BookingContact {
  tenantId: string;
  sessionId: string;
  service: string;
  slot: string;
  consent: ConsentRecord;
}

export interface BookingResponse {
  reply: string;
  toolEvent: ToolEvent;
}

/** The failure body `POST /api/book` returns, which drives the field error. */
export interface BookingFailure {
  toolEvent?: { result?: { message?: string; error?: string; missingFields?: string[] } };
}

export interface ServerMessage {
  id: string;
  role: MessageRole;
  content: string;
  source?: MessageSource;
}

export interface SessionSnapshot {
  messages?: ServerMessage[];
}
