import { vi } from "vitest";

import type { TenantDirectory } from "src/widget/types";

export const TENANTS: TenantDirectory = {
  apex: {
    name: "Apex Home Services",
    assistantName: "Apex Assistant",
    tagline: "Home-service help",
    site: { headline: "Apex headline", description: "Apex description" },
    address: "10 Main Street",
    phone: "555-111-2222",
    hours: "Always open",
    pricingPolicy: "never",
    bookingEnabled: false,
    leadCaptureEnabled: true,
    proactiveLeadCapture: false,
    services: ["HVAC", "Electrical"],
    quickActions: ["What do you repair?", "Talk to a person"]
  },
  clearview: {
    name: "Clearview Heating",
    assistantName: "Clearview Assistant",
    tagline: "Appointments and answers",
    site: { headline: "Clearview headline", description: "Clearview description" },
    address: "20 Broad Street",
    phone: "555-333-4444",
    hours: "Weekdays",
    pricingPolicy: "fixed",
    bookingEnabled: true,
    leadCaptureEnabled: true,
    proactiveLeadCapture: true,
    services: ["HVAC"],
    quickActions: ["Find an appointment"]
  }
};

export const AVAILABILITY_REPLY = {
  session_id: "session-1",
  reply: "",
  pending: {
    awaiting: "booking_confirmation",
    service: "HVAC",
    slot: "Tomorrow 09:00",
    customer_name: "Dana Ruiz",
    address: "12 Alder Court, Portland, OR 97205"
  },
  committed: [],
  provenance: {
    model_name: "scripted",
    graph_version: "dispatch@1",
    prompt_version: "dispatch-system@1"
  }
};

export const BOOKING_CONFIRMED = {
  session_id: "session-1",
  reply: "Your appointment is booked.",
  pending: null,
  committed: [{ action: "book_appointment", reference: "booking-1", replayed: false }],
  provenance: {
    model_name: "scripted",
    graph_version: "dispatch@1",
    prompt_version: "dispatch-system@1"
  }
};

export const SIMPLE_REPLY = {
  session_id: "session-1",
  reply: "I found one opening.",
  pending: null,
  committed: [],
  provenance: {
    model_name: "scripted",
    graph_version: "dispatch@1",
    prompt_version: "dispatch-system@1"
  }
};

export function jsonResponse(body: unknown, { ok = true, status = 200 } = {}) {
  return Promise.resolve({ ok, status, json: () => Promise.resolve(body) });
}

export type RouteHandler = (url: string, init?: RequestInit) => Promise<unknown> | undefined | null;

/**
 * Install a fetch stub for one test.
 *
 * Transcript polling is answered with an empty session unless the test says
 * otherwise, so a suite that is not about polling does not have to know the
 * widget polls at all.
 */
export function stubBackend(handler: RouteHandler) {
  const fetchMock = vi.fn((url: string, init?: RequestInit) => {
    const handled = handler(url, init);
    if (handled) return handled;
    if (url.includes("/api/chat/session")) {
      // `messages` sits top-level, mirroring `ChatSessionResponse`.
      return jsonResponse({ session: { session_id: "session-1" }, messages: [] });
    }
    throw new Error(`unexpected request: ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

/** A backend that answers tenants, chat, and session minting with fixtures. */
export function workingBackend(): RouteHandler {
  return (url) => {
    if (url.endsWith("/api/tenants")) return jsonResponse({ tenants: TENANTS });
    if (url.includes("/api/chat/session") && url.includes("?tenant_id=")) {
      return jsonResponse({ session: { session_id: "session-1" }, messages: [], pending: null });
    }
    if (url.endsWith("/api/chat/session"))
      return jsonResponse({ session: { session_id: "session-1" }, messages: [] });
    if (url.endsWith("/api/chat/confirmation")) return jsonResponse(BOOKING_CONFIRMED);
    if (url.endsWith("/api/chat")) return jsonResponse(AVAILABILITY_REPLY);
    return null;
  };
}

/** The JSON body of a request, or null when it carried none. */
export function requestBody(init: RequestInit | undefined): unknown {
  return typeof init?.body === "string" ? (JSON.parse(init.body) as unknown) : null;
}

/** The bodies posted to one endpoint, in order. */
export function requestBodies(fetchMock: ReturnType<typeof stubBackend>, path: string): unknown[] {
  return fetchMock.mock.calls
    .filter(([url]) => url.endsWith(path))
    .map(([, init]) => requestBody(init));
}
