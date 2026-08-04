import { fireEvent, waitFor } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";

import { VisitorData, type VisitorStorage } from "src/widget/visitorData";
import {
  TENANTS,
  jsonResponse,
  requestBodies,
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
        provenance: { model_name: "scripted", graph_version: "v1", prompt_version: "v1" }
      });
    }
    if (url.includes("/api/chat/session")) {
      return jsonResponse({
        session: { session_id: SESSION_ID },
        messages: [STAFF_MESSAGE],
        pending: null
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
          provenance: { model_name: "scripted", graph_version: "v1", prompt_version: "v1" }
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
          pending: null
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
        return jsonResponse({ session: { session_id: SESSION_ID } });
      }
      if (url.endsWith("/api/chat")) {
        return jsonResponse({
          session_id: SESSION_ID,
          reply: "Hold on.",
          pending: null,
          committed: [],
          provenance: { model_name: "scripted", graph_version: "v1", prompt_version: "v1" }
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

describe("a stored session the API would reject", () => {
  test("a pre-cutover session id is discarded and a fresh one minted", async () => {
    // The prototype minted `web-<tenant>-<ts>-<rand>` ids; the API only
    // accepts UUIDs, so a returning visitor holding one must not be stuck
    // retrying a session that can never be found.
    window.sessionStorage.setItem("tenant-chat-session-id:apex", "web-apex-1720000000-ab12cd34");
    const minted = "11111111-2222-3333-4444-555555555555";
    const fetchMock = stubBackend((url, init) => {
      if (url.endsWith("/api/tenants")) return jsonResponse({ tenants: TENANTS });
      if (url.endsWith("/api/chat/session") && init?.method === "POST") {
        return jsonResponse({ session: { session_id: minted } });
      }
      if (url.endsWith("/api/chat")) {
        return jsonResponse({
          session_id: minted,
          reply: "Fresh start.",
          pending: null,
          committed: [],
          provenance: { model_name: "scripted", graph_version: "v1", prompt_version: "v1" }
        });
      }
      return null;
    });
    await renderDemo();

    submitChat("Anyone there?");

    await waitFor(() => expect(inWidget("#messages")?.textContent).toContain("Fresh start."));
    const [request] = requestBodies(fetchMock, "/api/chat") as { session_id: string }[];
    expect(request?.session_id).toBe(minted);
    expect(window.sessionStorage.getItem("tenant-chat-session-id:apex")).toBe(minted);
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
    expect(visitor.existingSessionId()).toBeNull();

    visitor.recordSession("session-issued-by-server");
    expect(visitor.existingSessionId()).toBe("session-issued-by-server");

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
