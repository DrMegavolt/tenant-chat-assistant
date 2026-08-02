import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { TENANTS, inWidget, jsonResponse, page } from "./support/page.js";
import { VisitorData } from "../public/widget/privacy.js";

beforeEach(() => {
  vi.resetModules();
  page();
  window.sessionStorage.clear();
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("staff replies reaching an open widget", () => {
  test("a staff message appears once and is attributed to staff", async () => {
    const staffMessage = {
      id: "msg-1",
      source: "admin",
      role: "assistant",
      content: "A dispatcher is on the way."
    };
    let intervalCallback;
    vi.stubGlobal(
      "fetch",
      vi.fn((url) => {
        if (url === "/api/tenants") return jsonResponse({ tenants: TENANTS });
        if (url.startsWith("/api/chat/session")) {
          return jsonResponse({ session: { messages: [staffMessage] } });
        }
        throw new Error(`unexpected request: ${url}`);
      })
    );
    vi.spyOn(window, "setInterval").mockImplementation((callback) => {
      intervalCallback = callback;
      return 1;
    });

    await import("../public/app.js");
    await vi.waitFor(() => expect(inWidget("#chatCompany").textContent).toBeTruthy());

    await intervalCallback();
    await intervalCallback();

    const staffBubbles = [...inWidget("#messages").querySelectorAll(".message.admin")];
    expect(staffBubbles).toHaveLength(1);
    expect(staffBubbles[0].textContent).toBe("Staff: A dispatcher is on the way.");
  });

  test("a failed poll leaves the transcript untouched", async () => {
    let intervalCallback;
    vi.stubGlobal(
      "fetch",
      vi.fn((url) => {
        if (url === "/api/tenants") return jsonResponse({ tenants: TENANTS });
        return Promise.reject(new Error("offline"));
      })
    );
    vi.spyOn(window, "setInterval").mockImplementation((callback) => {
      intervalCallback = callback;
      return 1;
    });

    await import("../public/app.js");
    await vi.waitFor(() => expect(inWidget("#chatCompany").textContent).toBeTruthy());
    const before = inWidget("#messages").textContent;

    await intervalCallback();

    expect(inWidget("#messages").textContent).toBe(before);
  });
});

describe("browsers that refuse storage", () => {
  test("the conversation still works when sessionStorage throws", () => {
    const blocked = {
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
    vi.spyOn(window, "sessionStorage", "get").mockReturnValue(blocked);

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
