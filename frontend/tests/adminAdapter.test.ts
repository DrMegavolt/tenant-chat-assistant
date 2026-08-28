import { describe, expect, test, vi } from "vitest";

import { AdminApi } from "src/admin/adminApi";
import type { SessionDetail } from "src/admin/types";
import { jsonResponse } from "tests/support/backend";

// The chat adapter is the seam between the wire and the console's view models,
// and the component suites feed their fixtures straight to the components — so
// nothing else pins the snake_case→camelCase mapping. These tests read the
// real response shapes (AdminChatSessionResponse / AdminChatSessionSummary),
// whose new side-card and count fields are what the queue stats and the
// session-detail cards render (review R-31 / L-A01 / L-A09 / L-A10).

const SESSION_ID = "3f1d5f0e-8a2b-4c7d-9e11-6b0c2a4d5e88";

// Exactly the server's preview budget: the wire bounds last_message content to
// 200 chars, and the adapter must pass it through verbatim.
const BOUNDED_PREVIEW = "a".repeat(200);

const FULL_SUMMARY_WIRE = {
  session_id: SESSION_ID,
  tenant_id: "clearview",
  status: "active",
  outcome: "lead",
  started_at: "2026-08-27T09:00:00.000Z",
  last_activity_at: "2026-08-27T10:00:00.000Z",
  message_count: 21,
  lead_count: 1,
  last_message: {
    role: "visitor",
    content: BOUNDED_PREVIEW,
    created_at: "2026-08-27T09:59:00.000Z"
  }
};

const FULL_DETAIL_WIRE = {
  session: FULL_SUMMARY_WIRE,
  messages: [
    {
      message_id: "c56d169b-bfa8-43ad-a624-d122797c4f7e",
      role: "visitor",
      content: "My boiler is out.",
      created_at: "2026-08-27T09:59:00.000Z"
    }
  ],
  pending: {
    awaiting: "booking_confirmation",
    service: "HVAC",
    slot: "Tomorrow 09:00",
    customer_name: "Dana Ruiz",
    address: "12 Alder Court, Portland, OR 97205",
    contact: "dana@example.com",
    summary: "Furnace is making a grinding noise."
  },
  leads: [
    {
      lead_id: "lead-1",
      customer_name: "Dana Ruiz",
      contact: "dana@example.com",
      service: "HVAC",
      urgency: "high",
      summary: "Furnace is making a grinding noise."
    }
  ],
  bookings: [
    {
      booking_id: "bk-1",
      customer_name: "Dana Ruiz",
      contact: "dana@example.com",
      service: "HVAC",
      slot: "Tomorrow 09:00",
      address: "12 Alder Court, Portland, OR 97205"
    }
  ],
  tool_events: [
    { name: "get_availability", result: '{"slots":["Tomorrow 09:00"]}' },
    { name: "check_service_area", result: "served" }
  ]
};

function stubChats(detail: Record<string, unknown>, summary: unknown = FULL_SUMMARY_WIRE) {
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string) => {
      if (url.includes("/api/admin/chats?")) {
        return jsonResponse({ sessions: [summary] });
      }
      if (url.includes(`/api/admin/chats/${SESSION_ID}`)) {
        return jsonResponse(detail);
      }
      throw new Error(`unexpected request: ${url}`);
    })
  );
}

describe("the chat adapter's list side", () => {
  test("a full summary maps the counts and bounded preview the queue rows and stat strip read", async () => {
    stubChats(FULL_DETAIL_WIRE);
    const api = new AdminApi("");

    const [row] = await api.sessions("clearview");

    expect(row?.sessionId).toBe(SESSION_ID);
    expect(row?.tenantName).toBe("clearview");
    expect(row?.active).toBe(true);
    expect(row?.messageCount).toBe(21);
    expect(row?.leadCount).toBe(1);
    // The server bounds the preview to 200 chars; the adapter's job is a
    // verbatim passthrough, never a re-truncation or a mangling.
    expect(row?.lastMessage?.content).toBe(BOUNDED_PREVIEW);
    expect(row?.updatedAt).toBe(Date.parse("2026-08-27T10:00:00.000Z") / 1000);
    // The preview carries its author and instant beside the declared content.
    const lastMessage = row?.lastMessage as { role?: string; createdAt?: number } | undefined;
    expect(lastMessage?.role).toBe("visitor");
    expect(lastMessage?.createdAt).toBe(Date.parse("2026-08-27T09:59:00.000Z") / 1000);
  });
});

describe("the chat adapter's detail side", () => {
  test("a full record maps the leads, bookings, tool events, and pending card", async () => {
    stubChats(FULL_DETAIL_WIRE);
    const api = new AdminApi("");

    const detail = await api.session(SESSION_ID, "clearview");
    expect(detail).toBeTruthy();

    // The wire's count is authoritative: a one-message transcript must not
    // overwrite it (the old adapter derived this from messages.length).
    expect(detail?.messageCount).toBe(21);
    expect(detail?.leadCount).toBe(1);

    // lead_id / booking_id have no consumer: the side cards render people,
    // slots, and summaries, not identifiers.
    expect(detail?.leads).toEqual([
      {
        customerName: "Dana Ruiz",
        contact: "dana@example.com",
        service: "HVAC",
        urgency: "high",
        summary: "Furnace is making a grinding noise."
      }
    ]);
    expect(detail?.bookings).toEqual([
      {
        customerName: "Dana Ruiz",
        contact: "dana@example.com",
        service: "HVAC",
        slot: "Tomorrow 09:00",
        address: "12 Alder Court, Portland, OR 97205"
      }
    ]);

    // The tool payload the model was shown is stored as a JSON string and is
    // rendered as its parsed shape; a non-JSON payload stays the string it was.
    expect(detail?.toolEvents).toEqual([
      { name: "get_availability", result: { slots: ["Tomorrow 09:00"] } },
      { name: "check_service_area", result: "served" }
    ]);

    // `pending` rides beside the declared detail shape until the widgets
    // branch's types merge; the pending card there reads exactly this view.
    const pending = (detail as SessionDetail & { pending?: unknown }).pending;
    expect(pending).toEqual({
      awaiting: "booking_confirmation",
      service: "HVAC",
      slot: "Tomorrow 09:00",
      customerName: "Dana Ruiz",
      address: "12 Alder Court, Portland, OR 97205",
      contact: "dana@example.com",
      summary: "Furnace is making a grinding noise."
    });
  });
});

describe("the chat adapter with a bare payload", () => {
  test("a response without the new fields maps without throwing and invents nothing", async () => {
    // A summary of the pre-side-card shape: no counts, no preview, no lists.
    const bareSummary = {
      session_id: SESSION_ID,
      tenant_id: "clearview",
      status: "closed",
      outcome: "none",
      started_at: "2026-08-27T09:00:00.000Z",
      last_activity_at: "2026-08-27T10:00:00.000Z"
    };
    stubChats({ session: bareSummary, messages: [] }, bareSummary);
    const api = new AdminApi("");

    const [row] = await api.sessions("clearview");
    expect(row?.messageCount).toBeUndefined();
    expect(row?.leadCount).toBeUndefined();
    expect(row?.lastMessage).toBeUndefined();

    const detail = await api.session(SESSION_ID, "clearview");
    expect(detail).toBeTruthy();
    expect(detail?.messageCount).toBeUndefined();
    expect(detail?.leadCount).toBeUndefined();
    expect(detail?.lastMessage).toBeUndefined();
    expect(detail?.leads).toBeUndefined();
    expect(detail?.bookings).toBeUndefined();
    expect(detail?.toolEvents).toBeUndefined();
    expect("pending" in (detail as object)).toBe(false);
  });
});
