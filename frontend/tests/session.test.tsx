import { fireEvent } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";

import { VisitorData, type VisitorStorage } from "src/widget/visitorData";
import { TENANTS, jsonResponse, stubBackend, workingBackend } from "tests/support/backend";
import { tick } from "tests/support/timers";
import {
  allInWidget,
  inWidget,
  renderDemo,
  requireInWidget,
  submitChat
} from "tests/support/widget";

const STAFF_MESSAGE = {
  id: "msg-1",
  source: "admin",
  role: "assistant",
  content: "A dispatcher is on the way."
};

/**
 * Answer the tenant directory and one chat turn, then hand every poll the same
 * staff message. Repeating it is the point: the widget has to recognise a
 * message it has already shown.
 */
function backendWithStaffReply() {
  return stubBackend((url) => {
    if (url.endsWith("/api/tenants")) return jsonResponse({ tenants: TENANTS });
    if (url.endsWith("/api/chat")) return jsonResponse({ reply: "Hold on.", toolEvents: [] });
    if (url.includes("/api/chat/session")) {
      return jsonResponse({ session: { messages: [STAFF_MESSAGE] } });
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

  test("a failed poll leaves the transcript untouched", async () => {
    stubBackend((url) => {
      if (url.endsWith("/api/tenants")) return jsonResponse({ tenants: TENANTS });
      if (url.endsWith("/api/chat")) return jsonResponse({ reply: "Hold on.", toolEvents: [] });
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
    const sessionId = visitor.sessionId();

    expect(sessionId).toMatch(/^web-apex-/);
    expect(visitor.sessionId()).toBe(sessionId);

    visitor.recordConsent("statement");
    expect(visitor.hasConsent()).toBe(true);

    visitor.clear();
    expect(visitor.hasConsent()).toBe(false);
  });

  test("corrupted stored records are treated as absent rather than thrown", () => {
    window.sessionStorage.setItem("tenant-chat-consent:apex", "{not json");
    window.sessionStorage.setItem("tenant-chat-contact:apex", "{not json");

    const visitor = new VisitorData("apex");

    expect(visitor.consent()).toBeNull();
    expect(visitor.contact()).toBeNull();
  });
});
