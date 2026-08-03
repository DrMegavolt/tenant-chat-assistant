/** The shapes the operator console exchanges with the admin API. */

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
  role: "user" | "assistant";
  source?: "admin" | "user" | "assistant" | "proactive";
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

/** One row of `GET /api/admin/chats`. */
export interface SessionSummary {
  sessionId: string;
  tenantName: string;
  active: boolean;
  status: string;
  outcome?: Outcome;
  messageCount: number;
  leadCount: number;
  lastMessage?: { content: string };
  /** Unix seconds. */
  updatedAt: number;
}

export interface SessionDetail extends SessionSummary {
  messages?: AdminMessage[];
  leads?: Lead[];
  bookings?: Booking[];
  toolEvents?: AdminToolEvent[];
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
