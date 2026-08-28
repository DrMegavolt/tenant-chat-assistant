/** The shapes the operator console exchanges with the admin API. */

/** One row of `GET /api/admin/tenants`: a tenant this operator may open. */
export interface TenantSummary {
  tenantId: string;
  name: string;
  /** The role this operator holds inside the tenant. */
  role: string;
}

export const OUTCOMES = [
  "active",
  "lead",
  "booked",
  "handoff",
  "completed",
  "abandoned",
  "empty"
] as const;

export type Outcome = (typeof OUTCOMES)[number];

export interface AdminMessage {
  id: string;
  role: "user" | "assistant" | "system";
  source?: "admin" | "user" | "assistant" | "proactive" | "system";
  content: string;
  /** Unix seconds. */
  createdAt: number;
}

export interface Lead {
  customerName: string;
  contact: string;
  service: string;
  urgency: string;
  summary: string;
}

export interface Booking {
  customerName: string;
  contact: string;
  service: string;
  slot: string;
  address: string;
}

export interface AdminToolEvent {
  name: string;
  result: unknown;
}

/**
 * A question the assistant is waiting on the visitor to answer, as the session
 * detail's `pending` field reports it. A booking confirmation carries the slot
 * and address; a lead confirmation carries the contact and summary instead.
 */
export interface PendingConfirmation {
  awaiting: string;
  service: string;
  slot: string;
  customerName: string;
  address: string;
  contact: string;
  summary: string;
}

/**
 * One row of `GET /api/admin/chats`.
 *
 * The counts and the preview are optional because the list endpoint returns
 * summaries only — it deliberately carries no transcript, so a row knows how
 * recently it moved but not what was said. They are populated once a caller
 * has the detail; a row renders without them rather than inventing a zero.
 */
export interface SessionSummary {
  sessionId: string;
  tenantName: string;
  active: boolean;
  status: string;
  outcome?: Outcome;
  messageCount?: number;
  leadCount?: number;
  lastMessage?: { content: string };
  /** Unix seconds. */
  updatedAt: number;
}

export interface SessionDetail extends SessionSummary {
  messages?: AdminMessage[];
  leads?: Lead[];
  bookings?: Booking[];
  toolEvents?: AdminToolEvent[];
  /** Present only while the conversation is paused on a visitor decision. */
  pending?: PendingConfirmation | null;
}

export const OUTCOME_LABELS: Record<Outcome, string> = {
  active: "Active",
  abandoned: "Abandoned",
  lead: "Lead",
  booked: "Booked",
  handoff: "Handoff",
  completed: "Completed",
  empty: "Empty"
};

export function outcomeOf(session: SessionSummary): Outcome {
  return session.outcome ?? "active";
}
