import { fireEvent, waitFor } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";

import { VisitorData, type VisitorStorage } from "src/widget/visitorData";
import {
  CREDENTIAL,
  TENANTS,
  jsonResponse,
  requestBodies,
  requestCredential,
  stubBackend,
  workingBackend
} from "tests/support/backend";
import { tick } from "tests/support/timers";
import {
  allInWidget,
  inWidget,
  renderDemo,
  requireInWidget,
  submitChat
} from "tests/support/widget";

const SESSION_ID = "11111111-2222-3333-4444-555555555555";

const STAFF_MESSAGE = {
  message_id: "msg-1",
  role: "staff",
  content: "A dispatcher is on the way.",
  created_at: "2026-01-01T00:00:00Z"
};

/**
 * Answer the tenant directory and one chat turn, then hand every poll the same
 * staff message. Repeating it is the point: the widget has to recognise a
 * message it has already shown.
 */
function backendWithStaffReply() {
  return stubBackend((url) => {
    if (url.endsWith("/api/tenants")) return jsonResponse({ tenants: TENANTS });
    if (url.endsWith("/api/chat")) {
      return jsonResponse({
        session_id: SESSION_ID,
        reply: "Hold on.",
        pending: null,
        committed: [],
        provenance: { model_name: "scripted", graph_version: "v1", prompt_version: "v1" },
        credential: CREDENTIAL
      });
    }
    if (url.includes("/api/chat/session")) {
      return jsonResponse({
        session: { session_id: SESSION_ID },
        messages: [STAFF_MESSAGE],
        pending: null,
        credential: CREDENTIAL
      });
    }
    return null;
  });
}

/** Render with the clock under test control and send one message. */
async function startConversation(companyId = "apex") {
  vi.useFakeTimers();
  await renderDemo({ companyId, awaitReady: false });
  submitChat("Anyone there?");
  await tick();
}

describe("staff replies reaching an open widget", () => {
  test("a staff message appears once and is attributed to staff", async () => {
    backendWithStaffReply();
    await startConversation();

    await tick(3000);
    await tick(3000);

    const staffBubbles = allInWidget(".message.admin");
    expect(staffBubbles).toHaveLength(1);
    expect(staffBubbles[0]?.querySelector(".visually-hidden")?.textContent).toBe("Staff: ");
    expect(staffBubbles[0]?.textContent).toContain("A dispatcher is on the way.");
  });

  test("a staff reply that arrives while the panel is closed is counted on the launcher", async () => {
    backendWithStaffReply();
    await startConversation();

    fireEvent.click(requireInWidget("#closeChat"));
    await tick(3000);

    expect(inWidget(".launcher-badge")?.textContent).toBe("1");
    expect(inWidget("#openChat")?.textContent).toContain("1 new reply");

    fireEvent.click(requireInWidget("#openChat"));
    expect(inWidget(".launcher-badge")).toBeNull();
  });

  test("the model's own reply is not re-rendered as a staff message by the poll", async () => {
    // The session response echoes the whole transcript, including the turn the
    // widget itself caused. Only `staff` role messages are arrivals; a model
    // reply already rendered once must not be duplicated with an admin label.
    stubBackend((url) => {
      if (url.endsWith("/api/tenants")) return jsonResponse({ tenants: TENANTS });
      if (url.endsWith("/api/chat")) {
        return jsonResponse({
          session_id: SESSION_ID,
          reply: "Hold on.",
          pending: null,
          committed: [],
          provenance: { model_name: "scripted", graph_version: "v1", prompt_version: "v1" },
          credential: CREDENTIAL
        });
      }
      if (url.includes("/api/chat/session")) {
        return jsonResponse({
          session: { session_id: SESSION_ID },
          messages: [
            {
              message_id: "msg-model-1",
              role: "assistant",
              content: "Hold on.",
              created_at: "2026-01-01T00:00:00Z"
            }
          ],
          pending: null,
          credential: CREDENTIAL
        });
      }
      return null;
    });
    await startConversation();

    await tick(3000);
    await tick(3000);

    expect(allInWidget(".message.admin")).toHaveLength(0);
    expect(
      allInWidget(".message.assistant").filter((bubble) => bubble.textContent?.includes("Hold on."))
    ).toHaveLength(1);
  });

  test("a failed poll leaves the transcript untouched", async () => {
    stubBackend((url, init) => {
      if (url.endsWith("/api/tenants")) return jsonResponse({ tenants: TENANTS });
      if (url.endsWith("/api/chat/session") && init?.method === "POST") {
        return jsonResponse({
          session: { session_id: SESSION_ID },
          credential: CREDENTIAL
        });
      }
      if (url.endsWith("/api/chat")) {
        return jsonResponse({
          session_id: SESSION_ID,
          reply: "Hold on.",
          pending: null,
          committed: [],
          provenance: { model_name: "scripted", graph_version: "v1", prompt_version: "v1" },
          credential: CREDENTIAL
        });
      }
      return Promise.reject(new Error("offline"));
    });
    await startConversation();
    const before = inWidget("#messages")?.textContent;

    await tick(3000);

    expect(inWidget("#messages")?.textContent).toBe(before);
  });
});

describe("the widget while it waits", () => {
  test("a tenant that allows it offers a callback after the visitor goes quiet", async () => {
    stubBackend(workingBackend());
    await startConversation("clearview");

    expect(inWidget(".message.proactive")).toBeNull();

    await tick(12_000);

    expect(inWidget(".message.proactive")?.textContent).toContain("have the team follow up");
  });

  test("a visitor who already left contact details is not nudged for them", async () => {
    stubBackend(workingBackend());
    vi.useFakeTimers();
    await renderDemo({ companyId: "clearview", awaitReady: false });

    submitChat("Call me on 555-222-1919");
    await tick();
    await tick(12_000);

    expect(inWidget(".message.proactive")).toBeNull();
  });
});

describe("a stored credential the API would reject", () => {
  test("a pre-cutover session id is discarded and a fresh credential minted", async () => {
    // The prototype minted `web-<tenant>-<ts>-<rand>` ids; the API only
    // accepts signed credentials, so a returning visitor holding one must not
    // be stuck retrying a session that can never be found.
    window.sessionStorage.setItem("tenant-chat-credential:apex", "web-apex-1720000000-ab12cd34");
    const fetchMock = stubBackend((url, init) => {
      if (url.endsWith("/api/tenants")) return jsonResponse({ tenants: TENANTS });
      if (url.endsWith("/api/chat/session") && init?.method === "POST") {
        return jsonResponse({
          session: { session_id: SESSION_ID },
          credential: CREDENTIAL
        });
      }
      if (url.endsWith("/api/chat")) {
        return jsonResponse({
          session_id: SESSION_ID,
          reply: "Fresh start.",
          pending: null,
          committed: [],
          provenance: { model_name: "scripted", graph_version: "v1", prompt_version: "v1" },
          credential: CREDENTIAL
        });
      }
      return null;
    });
    await renderDemo();

    submitChat("Anyone there?");

    await waitFor(() => expect(inWidget("#messages")?.textContent).toContain("Fresh start."));
    const [request] = requestBodies(fetchMock, "/api/chat") as { message: string }[];
    expect(request?.message).toBe("Anyone there?");
    expect(request).not.toHaveProperty("tenant_id");
    expect(request).not.toHaveProperty("session_id");
    const chatCall = fetchMock.mock.calls.find(([url]) => url.endsWith("/api/chat"));
    expect(requestCredential(chatCall?.[1])).toBe(CREDENTIAL);
    expect(window.sessionStorage.getItem("tenant-chat-credential:apex")).toBe(CREDENTIAL);
  });

  test("a credential the API rejects is discarded and the message delivered once again", async () => {
    // Expired or revoked credentials are a normal state, not an error: the
    // stored token must be replaced and the visitor's message sent on the new
    // conversation rather than dropped. The reset status announces it.
    window.sessionStorage.setItem("tenant-chat-credential:apex", "tc.v1.stale.stale");
    const fetchMock = stubBackend((url, init) => {
      if (url.endsWith("/api/tenants")) return jsonResponse({ tenants: TENANTS });
      if (url.endsWith("/api/chat/session") && init?.method === "POST") {
        return jsonResponse({
          session: { session_id: SESSION_ID },
          credential: CREDENTIAL
        });
      }
      if (url.endsWith("/api/chat")) {
        const rejected =
          fetchMock.mock.calls.filter(([chatUrl]) => chatUrl.endsWith("/api/chat")).length === 1;
        if (rejected) {
          return jsonResponse({ code: "visitor_credential_expired" }, { ok: false, status: 401 });
        }
        return jsonResponse({
          session_id: SESSION_ID,
          reply: "Delivered on a fresh session.",
          pending: null,
          committed: [],
          provenance: { model_name: "scripted", graph_version: "v1", prompt_version: "v1" },
          credential: CREDENTIAL
        });
      }
      return null;
    });
    await renderDemo();
    expect(inWidget("#assistantStatus")?.textContent).toBe("");

    submitChat("Still here?");

    await waitFor(() =>
      expect(inWidget("#messages")?.textContent).toContain("Delivered on a fresh session.")
    );
    expect(inWidget("#assistantStatus")?.textContent).toContain("new one has started");
    expect(window.sessionStorage.getItem("tenant-chat-credential:apex")).toBe(CREDENTIAL);
    const chatBodies = requestBodies(fetchMock, "/api/chat") as { message: string }[];
    expect(chatBodies).toHaveLength(2);
    expect(chatBodies.every((body) => body?.message === "Still here?")).toBe(true);
    const chatCalls = fetchMock.mock.calls.filter(([url]) => url.endsWith("/api/chat"));
    expect(requestCredential(chatCalls[1]?.[1])).toBe(CREDENTIAL);
  });

  test("a credential whose conversation is gone is discarded and the message delivered once again", async () => {
    // A 404 means the server no longer has the session the stored credential
    // names: a stored conversation erased server-side, or a
    // switch back to a tenant whose session was discarded. The widget must
    // recover the same way it recovers from an expired token instead of
    // leaving the visitor stuck on a generic failure.
    window.sessionStorage.setItem("tenant-chat-credential:apex", "tc.v1.stale.stale");
    const fetchMock = stubBackend((url, init) => {
      if (url.endsWith("/api/tenants")) return jsonResponse({ tenants: TENANTS });
      if (url.endsWith("/api/chat/session") && init?.method === "POST") {
        return jsonResponse({
          session: { session_id: SESSION_ID },
          credential: CREDENTIAL
        });
      }
      if (url.endsWith("/api/chat")) {
        const rejected =
          fetchMock.mock.calls.filter(([chatUrl]) => chatUrl.endsWith("/api/chat")).length === 1;
        if (rejected) {
          return jsonResponse({ code: "not_found" }, { ok: false, status: 404 });
        }
        return jsonResponse({
          session_id: SESSION_ID,
          reply: "Delivered on a fresh session.",
          pending: null,
          committed: [],
          provenance: { model_name: "scripted", graph_version: "v1", prompt_version: "v1" },
          credential: CREDENTIAL
        });
      }
      return null;
    });
    await renderDemo();

    submitChat("Still here?");

    await waitFor(() =>
      expect(inWidget("#messages")?.textContent).toContain("Delivered on a fresh session.")
    );
    expect(inWidget("#assistantStatus")?.textContent).toContain("new one has started");
    expect(window.sessionStorage.getItem("tenant-chat-credential:apex")).toBe(CREDENTIAL);
    const chatBodies = requestBodies(fetchMock, "/api/chat") as { message: string }[];
    expect(chatBodies).toHaveLength(2);
    expect(chatBodies.every((body) => body?.message === "Still here?")).toBe(true);
  });
});

describe("browsers that refuse storage", () => {
  test("the conversation still works when sessionStorage throws", () => {
    const blocked: VisitorStorage = {
      getItem: () => {
        throw new DOMException("blocked", "SecurityError");
      },
      setItem: () => {
        throw new DOMException("blocked", "SecurityError");
      },
      removeItem: () => {
        throw new DOMException("blocked", "SecurityError");
      }
    };
    vi.spyOn(window, "sessionStorage", "get").mockReturnValue(blocked as Storage);

    const visitor = new VisitorData("apex");
    expect(visitor.existingCredential()).toBeNull();

    visitor.recordCredential("tc.v1.payload.sig");
    expect(visitor.existingCredential()).toBe("tc.v1.payload.sig");

    visitor.recordConsent("statement");
    expect(visitor.hasConsent()).toBe(true);

    visitor.clear();
    expect(visitor.hasConsent()).toBe(false);
  });

  test("corrupted stored records are treated as absent rather than thrown", () => {
    window.sessionStorage.setItem("tenant-chat-consent:apex", "{not json");

    const visitor = new VisitorData("apex");

    expect(visitor.consent()).toBeNull();
  });
});

/**
 * The hydration fixtures below are the session snapshot exactly as
 * `GET /api/chat/session` serves it — transcript rows with their enrichment
 * when the backend publishes it, `pending` while a decision is awaited.
 */
const HYDRATED_MESSAGES = [
  {
    message_id: "hm-1",
    role: "visitor",
    content: "My furnace is dead.",
    created_at: "2026-08-26T08:59:00Z"
  },
  {
    message_id: "hm-2",
    role: "assistant",
    content: "A technician will call you back.",
    created_at: "2026-08-26T09:00:00Z"
  }
];

/**
 * Answer the tenant directory, defer the first session reads by hand, and
 * count every session read so the tests can pin how many were started.
 */
function backendWithDeferredSession(transcript: unknown[]) {
  const deferred: Array<(body: unknown) => void> = [];
  let sessionReads = 0;
  let answered = false;
  stubBackend((url, init) => {
    if (url.endsWith("/api/tenants")) return jsonResponse({ tenants: TENANTS });
    if (url.includes("/api/chat/session") && init?.method !== "POST") {
      sessionReads += 1;
      if (answered) {
        return jsonResponse({
          session: { session_id: SESSION_ID },
          messages: transcript,
          pending: null,
          credential: CREDENTIAL
        });
      }
      return new Promise((resolve) => {
        deferred.push((body) => resolve(jsonResponse(body)));
      });
    }
    if (url.endsWith("/api/chat")) {
      return jsonResponse({
        session_id: SESSION_ID,
        turn_id: null,
        reply: "Hold on.",
        pending: null,
        committed: [],
        provenance: { model_name: "scripted", graph_version: "v1", prompt_version: "v1" },
        credential: CREDENTIAL
      });
    }
    return null;
  });
  return {
    get sessionReads() {
      return sessionReads;
    },
    releaseSnapshot() {
      answered = true;
      deferred.splice(0).forEach((release) =>
        release({
          session: { session_id: SESSION_ID },
          messages: transcript,
          pending: null,
          credential: CREDENTIAL
        })
      );
    }
  };
}

describe("resuming a conversation after a reload", () => {
  test("a message typed while the snapshot is still loading survives hydration", async () => {
    // Hydration replaces nothing wholesale: the snapshot fetch is asynchronous
    // and the visitor does not stop typing just because it is in flight. The
    // one-shot hydrate used to delete exactly this message.
    vi.useFakeTimers();
    window.sessionStorage.setItem("tenant-chat-credential:apex", CREDENTIAL);
    const backend = backendWithDeferredSession(HYDRATED_MESSAGES);
    await renderDemo({ awaitReady: false });
    expect(backend.sessionReads).toBe(1);

    submitChat("And my heat pump clicks loudly.");
    await tick();
    expect(inWidget("#messages")?.textContent).toContain("clicks loudly");

    backend.releaseSnapshot();
    await tick();

    const transcript = inWidget("#messages")?.textContent ?? "";
    expect(transcript).toContain("My furnace is dead.");
    expect(transcript).toContain("A technician will call you back.");
    // The visitor's own mid-hydration message is still there.
    expect(transcript).toContain("clicks loudly");
  });

  test("a resumed answer carries the citations, feedback control, and action notes the wire publishes", async () => {
    // The resume GETs were always 200; the mapper threw the enrichment away.
    // Whatever the transcript rows publish must render exactly as the live
    // turn that first showed it did.
    window.sessionStorage.setItem("tenant-chat-credential:clearview", CREDENTIAL);
    stubBackend((url) => {
      if (url.endsWith("/api/tenants")) return jsonResponse({ tenants: TENANTS });
      if (url.includes("/api/chat/session")) {
        return jsonResponse({
          session: { session_id: SESSION_ID },
          messages: [
            {
              message_id: "hm-1",
              role: "visitor",
              content: "Do you service heat pumps?",
              created_at: "2026-08-26T08:59:00Z"
            },
            {
              message_id: "hm-2",
              role: "assistant",
              content: "Yes — annual tune-ups are included in the maintenance plan.",
              created_at: "2026-08-26T09:00:00Z",
              turn_id: "turn-9",
              citations: [
                {
                  source_id: "src-1",
                  title: "HVAC Maintenance Guide",
                  source_name: "Clearview Policies",
                  location: "Maintenance",
                  revision: 2,
                  effective_at: "2026-07-01T00:00:00Z"
                }
              ],
              committed: [{ action: "create_lead", reference: "lead-9", replayed: false }]
            }
          ],
          pending: null,
          credential: CREDENTIAL
        });
      }
      return null;
    });
    await renderDemo({ companyId: "clearview" });

    await waitFor(() => expect(inWidget("#messages")?.textContent).toContain("annual tune-ups"));
    expect(allInWidget(".citation-list")).toHaveLength(1);
    expect(allInWidget(".action-note")).toHaveLength(1);
    // The feedback control renders because the row carried its turn id; it is
    // the same control a live answer gets.
    expect(allInWidget(".feedback-control")).toHaveLength(1);
  });
});

describe("the transcript poll under stress", () => {
  test("a poll still in flight is not started again by the next tick", async () => {
    // Two overlapping polls would both read the same transcript and both
    // register the same unseen staff message, doubling it on screen.
    vi.useFakeTimers();
    window.sessionStorage.setItem("tenant-chat-credential:apex", CREDENTIAL);
    const backend = backendWithDeferredSession([]);
    await renderDemo({ awaitReady: false });

    await tick(2500);
    expect(backend.sessionReads).toBe(2);
    await tick(2500);
    await tick(2500);
    // The first poll never returned, so the later ticks started nothing.
    expect(backend.sessionReads).toBe(2);
  });

  test("a hidden tab does not poll, and returning to it polls at once", async () => {
    vi.useFakeTimers();
    window.sessionStorage.setItem("tenant-chat-credential:apex", CREDENTIAL);
    const backend = backendWithDeferredSession([]);
    await renderDemo({ awaitReady: false });
    backend.releaseSnapshot();
    await tick();
    expect(backend.sessionReads).toBe(1);

    const hiddenSpy = vi.spyOn(document, "hidden", "get").mockReturnValue(true);
    await tick(7500);
    expect(backend.sessionReads).toBe(1);

    hiddenSpy.mockRestore();
    document.dispatchEvent(new Event("visibilitychange"));
    await tick();

    expect(backend.sessionReads).toBe(2);
  });

  test("the same staff message is never appended twice across polls", async () => {
    vi.useFakeTimers();
    window.sessionStorage.setItem("tenant-chat-credential:apex", CREDENTIAL);
    const backend = backendWithDeferredSession([
      {
        message_id: "hm-staff",
        role: "staff",
        content: "A dispatcher is on the way.",
        created_at: "2026-08-26T09:01:00Z"
      }
    ]);
    await renderDemo({ awaitReady: false });

    backend.releaseSnapshot();
    await tick();
    await tick(2500);
    await tick(2500);

    expect(allInWidget(".message.admin")).toHaveLength(1);
  });
});

describe("a credential that stops being valid mid-visit", () => {
  test("a poll answered 401 stops the loop and the widget says the session ended", async () => {
    // Every poll that retries a dead credential is another rejection the
    // backend has to log; after the first one there must be no further calls,
    // and the visitor must see the honest end state instead of silence.
    vi.useFakeTimers();
    window.sessionStorage.setItem("tenant-chat-credential:apex", CREDENTIAL);
    let sessionReads = 0;
    stubBackend((url, init) => {
      if (url.endsWith("/api/tenants")) return jsonResponse({ tenants: TENANTS });
      if (url.includes("/api/chat/session") && init?.method !== "POST") {
        sessionReads += 1;
        if (sessionReads === 1) {
          return jsonResponse({
            session: { session_id: SESSION_ID },
            messages: [],
            pending: null,
            credential: CREDENTIAL
          });
        }
        return jsonResponse({ code: "visitor_credential_expired" }, { ok: false, status: 401 });
      }
      return null;
    });
    await renderDemo({ awaitReady: false });
    await tick();
    expect(sessionReads).toBe(1);

    await tick(2500);
    expect(sessionReads).toBe(2);
    expect(inWidget("#assistantStatus")?.textContent).toContain("has ended");

    await tick(2500);
    await tick(5000);
    expect(sessionReads).toBe(2);
    expect(window.sessionStorage.getItem("tenant-chat-credential:apex")).toBeNull();
  });

  test("a stored credential rejected on resume ends the session before the first poll", async () => {
    // Hydration discovers the dead token; the poll must never present the
    // same rejected credential on its own.
    vi.useFakeTimers();
    window.sessionStorage.setItem("tenant-chat-credential:apex", CREDENTIAL);
    let sessionReads = 0;
    stubBackend((url, init) => {
      if (url.endsWith("/api/tenants")) return jsonResponse({ tenants: TENANTS });
      if (url.includes("/api/chat/session") && init?.method !== "POST") {
        sessionReads += 1;
        return jsonResponse({ code: "invalid_visitor_credential" }, { ok: false, status: 401 });
      }
      return null;
    });
    await renderDemo({ awaitReady: false });
    await tick();
    await tick(2500);
    await tick(2500);

    expect(sessionReads).toBe(1);
    expect(inWidget("#assistantStatus")?.textContent).toContain("has ended");
  });

  test("sending after the session ended starts a fresh conversation and polling resumes", async () => {
    vi.useFakeTimers();
    window.sessionStorage.setItem("tenant-chat-credential:apex", CREDENTIAL);
    let sessionReads = 0;
    let minted = false;
    stubBackend((url, init) => {
      if (url.endsWith("/api/tenants")) return jsonResponse({ tenants: TENANTS });
      if (url.endsWith("/api/chat/session") && init?.method === "POST") {
        minted = true;
        return jsonResponse({ session: { session_id: SESSION_ID }, credential: CREDENTIAL });
      }
      if (url.includes("/api/chat/session")) {
        sessionReads += 1;
        if (sessionReads === 1 || minted) {
          return jsonResponse({
            session: { session_id: SESSION_ID },
            messages: [],
            pending: null,
            credential: CREDENTIAL
          });
        }
        return jsonResponse({ code: "visitor_credential_expired" }, { ok: false, status: 401 });
      }
      if (url.endsWith("/api/chat")) {
        return jsonResponse({
          session_id: SESSION_ID,
          reply: "Fresh start.",
          pending: null,
          committed: [],
          provenance: { model_name: "scripted", graph_version: "v1", prompt_version: "v1" },
          credential: CREDENTIAL
        });
      }
      return null;
    });
    await renderDemo({ awaitReady: false });
    await tick();
    await tick(2500);
    expect(inWidget("#assistantStatus")?.textContent).toContain("has ended");
    expect(sessionReads).toBe(2);

    submitChat("Still here?");
    await tick();

    expect(inWidget("#messages")?.textContent).toContain("Fresh start.");
    expect(inWidget("#assistantStatus")?.textContent).not.toContain("has ended");
    expect(window.sessionStorage.getItem("tenant-chat-credential:apex")).toBe(CREDENTIAL);

    await tick(2500);
    expect(sessionReads).toBe(3);
  });
});

describe("what a failed send says", () => {
  test("a message the backend rejects as too long is told apart from an outage", async () => {
    // A 422 in a hundredth of a second used to borrow the network-failure
    // wording and sent visitors hunting for an outage that was not there.
    stubBackend((url, init) => {
      if (url.endsWith("/api/tenants")) return jsonResponse({ tenants: TENANTS });
      if (url.endsWith("/api/chat/session") && init?.method === "POST") {
        return jsonResponse({ session: { session_id: SESSION_ID }, credential: CREDENTIAL });
      }
      if (url.endsWith("/api/chat")) {
        return jsonResponse({ detail: "message too long" }, { ok: false, status: 422 });
      }
      return null;
    });
    await renderDemo();

    submitChat("x".repeat(5000));

    await waitFor(() => expect(inWidget("#messages")?.textContent).toContain("too long to send"));
    expect(inWidget("#messages")?.textContent).toContain("4000 characters");
    expect(inWidget("#messages")?.textContent).not.toContain("could not reach the chat service");
  });

  test("a chat turn that never comes back times out and gives the composer back", async () => {
    // No timeouts anywhere in the transport meant a hung POST disabled the
    // composer for the rest of the visit.
    vi.useFakeTimers();
    stubBackend((url, init) => {
      if (url.endsWith("/api/tenants")) return jsonResponse({ tenants: TENANTS });
      if (url.endsWith("/api/chat/session") && init?.method === "POST") {
        return jsonResponse({ session: { session_id: SESSION_ID }, credential: CREDENTIAL });
      }
      if (url.endsWith("/api/chat")) {
        // Real fetch honours the abort signal; a hung connection never
        // settles on its own.
        return new Promise((_, reject) => {
          init?.signal?.addEventListener("abort", () =>
            reject(new DOMException("The operation was aborted.", "AbortError"))
          );
        });
      }
      return null;
    });
    await renderDemo({ awaitReady: false });

    submitChat("Anyone there?");
    await tick();
    expect(requireInWidget<HTMLInputElement>("#chatInput").disabled).toBe(true);

    await tick(45_000);
    await tick();

    expect(requireInWidget<HTMLInputElement>("#chatInput").disabled).toBe(false);
    expect(inWidget("#messages")?.textContent).toContain("could not reach the chat service");
  });
});
