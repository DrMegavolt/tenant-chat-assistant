import { vi } from "vitest";

import type { WireTenant } from "src/widget/api";

/**
 * The tenant directory exactly as `GET /api/tenants` serves it.
 *
 * Deliberately the snake_case *wire* shape, not the widget's domain type: a
 * fixture in the domain shape would bypass `normalizeTenant` and let the suite
 * pass against a response the backend never sends, which is how a blank page
 * once shipped green.
 */
/**
 * A tenant override, deliberately unlike the sentence the server composes from
 * a tenant name. `GET /api/tenants` publishes it and `POST /api/chat/consent`
 * records it, because both read `TenantPolicy.consent_statement()` — so one
 * constant here is the faithful fixture. A widget that rebuilds the copy from
 * the tenant name renders the default instead and fails.
 */
export const CLEARVIEW_CONSENT_STATEMENT =
  "Clearview Heating keeps the details you enter here to schedule your " +
  "visit and to contact you about it.";

export const TENANTS: Record<string, WireTenant> = {
  apex: {
    name: "Apex Home Services",
    assistant_name: "Apex Assistant",
    tagline: "Home-service help",
    site_headline: "Apex headline",
    site_description: "Apex description",
    address: "10 Main Street",
    phone: "555-111-2222",
    hours: "Always open",
    booking_enabled: false,
    lead_capture_enabled: true,
    proactive_lead_capture: false,
    contact_consent_statement:
      "I agree that Apex Home Services may store the name, address, and " +
      "contact details I enter here in order to arrange this appointment and " +
      "follow up about it.",
    services: ["HVAC", "Electrical"],
    quick_actions: ["What do you repair?", "Talk to a person"]
  },
  clearview: {
    name: "Clearview Heating",
    assistant_name: "Clearview Assistant",
    tagline: "Appointments and answers",
    site_headline: "Clearview headline",
    site_description: "Clearview description",
    address: "20 Broad Street",
    phone: "555-333-4444",
    hours: "Weekdays",
    booking_enabled: true,
    lead_capture_enabled: true,
    proactive_lead_capture: true,
    contact_consent_statement: CLEARVIEW_CONSENT_STATEMENT,
    services: ["HVAC"],
    quick_actions: ["Find an appointment"]
  }
};

/** A token the widget's shape check accepts as a server-issued credential. */
export const CREDENTIAL = "tc.v1.testpayload.testsignature";

export const AVAILABILITY_REPLY = {
  session_id: "session-1",
  turn_id: "turn-1",
  reply: "",
  pending: {
    awaiting: "booking_confirmation",
    service: "HVAC",
    slot: "Tomorrow 09:00",
    customer_name: "Dana Ruiz",
    address: "12 Alder Court, Portland, OR 97205",
    contact: "(555) 222-1919"
  },
  committed: [],
  provenance: {
    model_name: "scripted",
    graph_version: "dispatch@1",
    prompt_version: "dispatch-system@1"
  },
  credential: CREDENTIAL
};

export const BOOKING_CONFIRMED = {
  session_id: "session-1",
  turn_id: "turn-2",
  reply: "Your appointment is booked.",
  pending: null,
  committed: [{ action: "book_appointment", reference: "booking-1", replayed: false }],
  provenance: {
    model_name: "scripted",
    graph_version: "dispatch@1",
    prompt_version: "dispatch-system@1"
  },
  credential: CREDENTIAL
};

export const SIMPLE_REPLY = {
  session_id: "session-1",
  turn_id: "turn-3",
  reply: "I found one opening.",
  pending: null,
  committed: [],
  provenance: {
    model_name: "scripted",
    graph_version: "dispatch@1",
    prompt_version: "dispatch-system@1"
  },
  credential: CREDENTIAL
};

export const CONSENT_GRANTED = {
  purposes: ["booking", "follow_up"],
  statement: CLEARVIEW_CONSENT_STATEMENT,
  granted_at: "2026-08-04T00:00:00Z"
};

/** A turn that pauses on a lead awaiting the visitor's consent. */
export const LEAD_PENDING = {
  session_id: "session-1",
  turn_id: "turn-lead",
  reply: "",
  pending: {
    awaiting: "lead_confirmation",
    service: "HVAC",
    customer_name: "Dana Ruiz",
    contact: "dana@example.com",
    summary: "Furnace is making a grinding noise."
  },
  committed: [],
  provenance: {
    model_name: "scripted",
    graph_version: "dispatch@1",
    prompt_version: "dispatch-system@1"
  },
  credential: CREDENTIAL
};

export const LEAD_CAPTURED = {
  session_id: "session-1",
  turn_id: "turn-lead-done",
  reply: "The team will call you back.",
  pending: null,
  committed: [{ action: "create_lead", reference: "lead-1", replayed: false }],
  provenance: {
    model_name: "scripted",
    graph_version: "dispatch@1",
    prompt_version: "dispatch-system@1"
  },
  credential: CREDENTIAL
};

/** One curated citation, shaped exactly as `CitationSummary` publishes it. */
export const CITED_REPLY = {
  session_id: "session-1",
  turn_id: "turn-cited",
  reply: "Clearview’s maintenance plans cover an annual tune-up.",
  pending: null,
  committed: [],
  citations: [
    {
      source_id: "src-hvac-guide",
      title: "HVAC Maintenance Guide",
      source_name: "Clearview Policies",
      location: "Maintenance",
      revision: 2,
      effective_at: "2026-07-01T00:00:00Z"
    },
    {
      source_id: "src-tuning-policy",
      title: "Annual Tune-up Policy",
      source_name: "Clearview Policies",
      location: "Pricing",
      revision: 1,
      effective_at: "2026-06-15T00:00:00Z"
    }
  ],
  provenance: {
    model_name: "scripted",
    graph_version: "dispatch@1",
    prompt_version: "dispatch-system@1"
  },
  credential: CREDENTIAL
};

/** The authorized view `GET /api/chat/sources/{id}` returns for those citations. */
export const SOURCE_VIEW = {
  source_id: "src-hvac-guide",
  title: "HVAC Maintenance Guide",
  source_name: "Clearview Policies",
  location: "Maintenance",
  text: "Annual maintenance includes a tune-up, a filter check, and a safety inspection.",
  revision: 2,
  effective_at: "2026-07-01T00:00:00Z"
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
      return jsonResponse({
        session: { session_id: "session-1" },
        messages: [],
        credential: CREDENTIAL
      });
    }
    throw new Error(`unexpected request: ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

/** A backend that answers tenants, chat, and session minting with fixtures. */
export function workingBackend(): RouteHandler {
  return (url, init) => {
    if (url.endsWith("/api/tenants")) return jsonResponse({ tenants: TENANTS });
    if (url.endsWith("/api/chat/session") && init?.method === "POST") {
      return jsonResponse({
        session: { session_id: "session-1" },
        messages: [],
        pending: null,
        credential: CREDENTIAL
      });
    }
    if (url.endsWith("/api/chat/session")) {
      return jsonResponse({
        session: { session_id: "session-1" },
        messages: [],
        credential: CREDENTIAL
      });
    }
    if (url.endsWith("/api/chat/confirmation")) return jsonResponse(BOOKING_CONFIRMED);
    if (url.endsWith("/api/chat/consent")) return jsonResponse(CONSENT_GRANTED);
    if (url.endsWith("/api/chat")) return jsonResponse(AVAILABILITY_REPLY);
    return null;
  };
}

/**
 * A backend that answers every turn with one cited reply and serves the
 * authorized source view, so a citation suite does not have to restate the
 * surrounding endpoints. The source answer is injectable: a revoked or absent
 * source can return a 404, or a function can reject like a network failure.
 * A rejecting function rather than a bare promise keeps the rejection inside
 * the fetch, where the widget catches it.
 */
export function citedBackend(
  citations: unknown = CITED_REPLY.citations,
  sourceAnswer: Promise<unknown> | (() => Promise<unknown>) = jsonResponse(SOURCE_VIEW)
): RouteHandler {
  return (url, init) => {
    if (url.endsWith("/api/tenants")) return jsonResponse({ tenants: TENANTS });
    if (url.endsWith("/api/chat/session")) {
      return jsonResponse({
        session: { session_id: "session-1" },
        messages: [],
        credential: CREDENTIAL
      });
    }
    if (url.includes("/api/chat/sources/")) {
      return typeof sourceAnswer === "function" ? sourceAnswer() : sourceAnswer;
    }
    if (url.endsWith("/api/chat") && init?.method === "POST") {
      return jsonResponse({ ...CITED_REPLY, citations });
    }
    return null;
  };
}

/** The `X-Visitor-Credential` header a request carried, or null. */
export function requestCredential(init: RequestInit | undefined): string | null {
  const headers = init?.headers;
  if (!headers) return null;
  if (typeof Headers !== "undefined" && headers instanceof Headers) {
    return headers.get("X-Visitor-Credential");
  }
  const record = headers as Record<string, string>;
  return record["X-Visitor-Credential"] ?? null;
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
