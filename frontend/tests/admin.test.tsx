import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import axe from "axe-core";
import { describe, expect, test, vi } from "vitest";

import { AdminPage } from "src/admin/AdminPage";
import type { SessionDetail, SessionSummary } from "src/admin/types";
import { jsonResponse, requestBody } from "tests/support/backend";
import { tick } from "tests/support/timers";

const SUMMARY: SessionSummary = {
  sessionId: "web-apex-1",
  tenantName: "Apex Home Services",
  active: true,
  status: "live",
  outcome: "lead",
  messageCount: 4,
  leadCount: 1,
  lastMessage: { content: "My boiler is out." },
  updatedAt: 1_760_000_000
};

const ARCHIVED: SessionSummary = {
  ...SUMMARY,
  sessionId: "web-clearview-9",
  tenantName: "Clearview Heating",
  active: false,
  status: "archived",
  outcome: "booked",
  lastMessage: { content: "Thanks, see you Tuesday." }
};

const DETAIL: SessionDetail = {
  ...SUMMARY,
  messages: [
    { id: "m1", role: "user", content: "My boiler is out.", createdAt: 1_760_000_000 },
    { id: "m2", role: "assistant", content: "I can help.", createdAt: 1_760_000_060 }
  ],
  leads: [
    {
      customerName: "Sam Lee",
      contact: "sam@example.test",
      service: "HVAC",
      urgency: "today",
      summary: "No heat."
    }
  ],
  bookings: [],
  toolEvents: [{ name: "create_lead", result: { leadId: "lead-1" } }]
};

/** A console backed by two tenants and two sessions, with the newest selected. */
function stubAdminBackend() {
  const fetchMock = vi.fn((url: string, _init?: RequestInit) => {
    if (url.endsWith("/api/admin/tenants")) {
      return jsonResponse({
        tenants: [
          { tenantId: "apex", name: "Apex Home Services", role: "support_agent" },
          { tenantId: "clearview", name: "Clearview Heating", role: "support_agent" }
        ]
      });
    }
    if (url.includes("/api/admin/chats?")) {
      return jsonResponse({ sessions: [SUMMARY, ARCHIVED] });
    }
    if (url.includes("/api/admin/chats/")) {
      const session = url.includes(ARCHIVED.sessionId) ? { ...ARCHIVED, messages: [] } : DETAIL;
      return jsonResponse({ session });
    }
    if (url.endsWith("/api/admin/csrf-token")) return jsonResponse({ csrf_token: "token-1" });
    throw new Error(`unexpected request: ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

async function renderConsole() {
  // Mirror the document-level attributes `admin.html` sets, so an axe run over
  // this fixture reports on the console rather than on gaps in the fixture.
  document.documentElement.lang = "en";
  document.title = "Chat Admin";
  render(<AdminPage />);
  await screen.findByText("My boiler is out.", { selector: ".session-preview" });
}

describe("the chat queue", () => {
  test("selects the newest chat and shows its transcript, leads, and tool calls", async () => {
    stubAdminBackend();
    await renderConsole();

    expect(await screen.findByRole("heading", { name: SUMMARY.sessionId })).toBeTruthy();
    expect(screen.getByRole("log", { name: "Transcript" }).textContent).toContain("I can help.");
    expect(screen.getByText("Sam Lee")).toBeTruthy();
    expect(screen.getByText(/leadId/)).toBeTruthy();
  });

  test("filtering by text narrows the queue without touching the polled data", async () => {
    stubAdminBackend();
    await renderConsole();

    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "clearview" } });

    const queue = within(document.querySelector("#sessionList")!);
    expect(queue.queryByText("Apex Home Services")).toBeNull();
    expect(queue.getByText("Clearview Heating")).toBeTruthy();
    // The open conversation is unaffected: filtering is a view of the queue.
    expect(screen.getByRole("heading", { name: SUMMARY.sessionId })).toBeTruthy();
  });

  test("a poll that lands mid-reply does not take the half-typed message away", async () => {
    // The previous console re-rendered the whole page on every poll and then
    // restored the caret by hand. Keeping the draft in component state is what
    // makes that unnecessary; this is the behaviour that has to survive.
    vi.useFakeTimers();
    stubAdminBackend();
    render(<AdminPage />);
    await tick();

    fireEvent.change(screen.getByLabelText("Staff message"), { target: { value: "On my way" } });

    await tick(3000);
    await tick(3000);

    expect(screen.getByLabelText<HTMLInputElement>("Staff message").value).toBe("On my way");
  });

  test("a hidden tab stops polling the admin API", async () => {
    vi.useFakeTimers();
    const fetchMock = stubAdminBackend();
    render(<AdminPage />);
    await tick();
    const callsWhileVisible = fetchMock.mock.calls.length;

    vi.spyOn(document, "hidden", "get").mockReturnValue(true);
    await tick(9000);

    expect(fetchMock.mock.calls.length).toBe(callsWhileVisible);
  });

  test("sending a staff message posts it with the CSRF token and clears the field", async () => {
    const fetchMock = stubAdminBackend();
    await renderConsole();

    fireEvent.change(screen.getByLabelText("Staff message"), { target: { value: "On my way" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() => {
      const posted = fetchMock.mock.calls.find(([url]) => url.endsWith("/messages"));
      expect(posted).toBeTruthy();
    });
    const posted = fetchMock.mock.calls.find(([url]) => url.endsWith("/messages"))!;
    const init = posted[1] as RequestInit;
    expect((init.headers as Record<string, string>)["X-CSRF-Token"]).toBe("token-1");
    expect(requestBody(init)).toEqual({ tenant_id: "apex", content: "On my way" });
    expect(screen.getByLabelText<HTMLInputElement>("Staff message").value).toBe("");
  });

  test("every tenant-scoped request carries the open tenant", async () => {
    const fetchMock = stubAdminBackend();
    await renderConsole();

    await waitFor(() => {
      const chatList = fetchMock.mock.calls.find(([url]) => url.includes("/api/admin/chats?"));
      expect(chatList?.[0]).toContain("tenant_id=apex");
    });
    await waitFor(() => {
      const detail = fetchMock.mock.calls.find(([url]) =>
        url.includes("/api/admin/chats/web-apex-1")
      );
      expect(detail?.[0]).toContain("tenant_id=apex");
    });
  });

  test("switching tenants opens the other tenant's queue", async () => {
    const fetchMock = stubAdminBackend();
    await renderConsole();

    fireEvent.change(screen.getByLabelText("Tenant"), { target: { value: "clearview" } });

    await waitFor(() => {
      const switched = fetchMock.mock.calls.find(([url]) => url.includes("tenant_id=clearview"));
      expect(switched).toBeTruthy();
    });
  });
});

describe("the console when the admin API is unreachable", () => {
  test("says so instead of showing an empty queue as though there were no chats", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.reject(new Error("offline")))
    );

    render(<AdminPage />);

    expect(await screen.findByRole("alert")).toHaveProperty(
      "textContent",
      "Could not reach the admin API. Retrying…"
    );
  });

  test("an expired session is sent back through the gateway rather than rendered blank", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => jsonResponse({}, { ok: false, status: 401 }))
    );
    const assign = vi.fn();
    vi.spyOn(window, "location", "get").mockReturnValue({
      protocol: "https:",
      set href(value: string) {
        assign(value);
      }
    } as unknown as Location);

    render(<AdminPage />);

    await waitFor(() => expect(assign).toHaveBeenCalledWith("/admin/"));
  });
});

describe("automated accessibility checks", () => {
  test("the console has no axe violations", async () => {
    stubAdminBackend();
    await renderConsole();

    const results = await axe.run(document, {
      resultTypes: ["violations"],
      rules: { "color-contrast": { enabled: false }, "target-size": { enabled: false } }
    });
    expect(results.violations.map((violation) => violation.id)).toEqual([]);
  });
});
