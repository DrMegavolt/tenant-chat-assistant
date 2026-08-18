import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import axe from "axe-core";
import { describe, expect, test, vi } from "vitest";

import { AdminPage } from "src/admin/AdminPage";
import { jsonResponse, requestBody } from "tests/support/backend";
import { tick } from "tests/support/timers";

// These fixtures are the admin API's own wire shape — snake_case fields, the
// store's `visitor` role, and the transcript as a sibling of `session` rather
// than a property of it. They previously mirrored the console's camelCase
// types instead, so the suite passed against a backend that does not exist and
// every field the console read came back undefined in production.
const APEX_SESSION_ID = "3f1d5f0e-8a2b-4c7d-9e11-6b0c2a4d5e88";
const CLEARVIEW_SESSION_ID = "9c4a71b2-0d3e-4f56-8a90-1b2c3d4e5f67";

const WIRE_SUMMARY = {
  session_id: APEX_SESSION_ID,
  tenant_id: "apex",
  status: "active",
  outcome: "lead",
  started_at: "2025-10-09T07:33:20.000Z",
  last_activity_at: "2025-10-09T07:33:20.000Z"
};

const WIRE_ARCHIVED = {
  ...WIRE_SUMMARY,
  session_id: CLEARVIEW_SESSION_ID,
  tenant_id: "clearview",
  status: "closed",
  outcome: "booked",
  last_activity_at: "2025-10-09T07:20:00.000Z"
};

const WIRE_MESSAGES = [
  {
    message_id: "c56d169b-bfa8-43ad-a624-d122797c4f7e",
    role: "visitor",
    content: "My boiler is out.",
    created_at: "2025-10-09T07:33:20.000Z"
  },
  {
    message_id: "2b7e1a90-5c34-4d81-9f22-8e0a1c3b4d55",
    role: "assistant",
    content: "I can help.",
    created_at: "2025-10-09T07:34:20.000Z"
  },
  {
    message_id: "3c8f2b01-6d45-4e92-a033-9f1b2d4c5e66",
    role: "system",
    content: "A member of the team has joined this conversation.",
    created_at: "2025-10-09T07:35:20.000Z"
  },
  {
    message_id: "4d903c12-7e56-4fa3-b144-0a2c3e5d6f77",
    role: "staff",
    content: "On my way to help.",
    created_at: "2025-10-09T07:36:20.000Z"
  }
];

/** A console backed by two tenants and two sessions, with the newest selected. */
function stubAdminBackend() {
  const fetchMock = vi.fn((url: string, _init?: RequestInit) => {
    if (url.endsWith("/api/admin/tenants")) {
      return jsonResponse({
        tenants: [
          { tenant_id: "apex", name: "Apex Home Services", role: "support_agent" },
          { tenant_id: "clearview", name: "Clearview Heating", role: "support_agent" }
        ]
      });
    }
    if (url.includes("/api/admin/chats?")) {
      return jsonResponse({ sessions: [WIRE_SUMMARY, WIRE_ARCHIVED] });
    }
    if (url.includes("/api/admin/chats/")) {
      const archived = url.includes(CLEARVIEW_SESSION_ID);
      return jsonResponse({
        session: archived ? WIRE_ARCHIVED : WIRE_SUMMARY,
        messages: archived ? [] : WIRE_MESSAGES
      });
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
  // The list endpoint carries summaries only, so a row is identified by its
  // tenant rather than by a message preview it was never sent.
  await screen.findByText("Apex Home Services", { selector: ".session-row strong" });
}

describe("the chat queue", () => {
  test("selects the newest chat and shows its transcript", async () => {
    stubAdminBackend();
    await renderConsole();

    expect(await screen.findByRole("heading", { name: APEX_SESSION_ID })).toBeTruthy();
    const transcript = screen.getByRole("log", { name: "Transcript" });
    expect(transcript.textContent).toContain("My boiler is out.");
    expect(transcript.textContent).toContain("I can help.");
    // `visitor` on the wire is the customer side of the conversation.
    expect(transcript.textContent).toContain("Visitor");
  });

  test("handoff lifecycle notices are attributed to the system, not the visitor", async () => {
    // BUG-017: a system notice collapsed to the customer side of the
    // conversation, so operators read "a member of the team joined" as visitor
    // speech. Each author type keeps its own label.
    stubAdminBackend();
    await renderConsole();

    const transcript = screen.getByRole("log", { name: "Transcript" });
    const messages = within(transcript).getAllByRole("article");
    const labelled = (label: string) =>
      messages
        .filter((message) => message.textContent?.includes(label))
        .map((message) => message.textContent);
    expect(labelled("My boiler is out.")).toEqual([expect.stringContaining("Visitor")]);
    expect(labelled("On my way to help.")).toEqual([expect.stringContaining("Staff")]);
    expect(labelled("A member of the team has joined this conversation.")).toEqual([
      expect.stringContaining("System")
    ]);
  });

  test("filtering by text narrows the queue without touching the polled data", async () => {
    stubAdminBackend();
    await renderConsole();

    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "clearview" } });

    const queue = within(document.querySelector("#sessionList")!);
    expect(queue.queryByText("Apex Home Services")).toBeNull();
    expect(queue.getByText("Clearview Heating")).toBeTruthy();
    // The open conversation is unaffected: filtering is a view of the queue.
    expect(screen.getByRole("heading", { name: APEX_SESSION_ID })).toBeTruthy();
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
        url.includes(`/api/admin/chats/${APEX_SESSION_ID}`)
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

describe("overlapping reads never publish a superseded response", () => {
  /**
   * BUG-025: the console read the tenant and selection, awaited, then published
   * whatever came back. A slow earlier response landing last replaced the newer
   * tenant's queue or transcript, so an operator could read one conversation
   * under another's heading — and act on it.
   *
   * Each response is released by hand, so both orders are exercised
   * deterministically rather than by racing real timers.
   */
  /** Let every already-resolved promise settle, without touching timers. */
  async function flush() {
    await act(async () => {
      await Promise.resolve();
    });
  }

  function deferredBackend() {
    const pending: Array<{ url: string; release: (body: unknown) => void }> = [];
    const fetchMock = vi.fn((url: string) => {
      if (url.endsWith("/api/admin/tenants")) {
        return jsonResponse({
          tenants: [
            { tenant_id: "apex", name: "Apex Home Services", role: "support_agent" },
            { tenant_id: "clearview", name: "Clearview Heating", role: "support_agent" }
          ]
        });
      }
      if (url.endsWith("/api/admin/csrf-token")) return jsonResponse({ csrf_token: "token-1" });
      return new Promise((resolve) => {
        pending.push({ url, release: (body) => resolve(jsonResponse(body)) });
      });
    });
    vi.stubGlobal("fetch", fetchMock);
    return {
      pending,
      /** Release the oldest still-unanswered request whose URL matches.
       *
       * Waits for it, because the console issues its second read only after the
       * first resolves — a synchronous lookup would miss it.
       */
      async release(fragment: string, body: unknown) {
        await waitFor(() =>
          expect(pending.some((entry) => entry.url.includes(fragment))).toBe(true)
        );
        const index = pending.findIndex((entry) => entry.url.includes(fragment));
        pending.splice(index, 1)[0]!.release(body);
      }
    };
  }

  async function switchToClearview() {
    const backend = deferredBackend();
    render(<AdminPage />);
    await waitFor(() => expect(backend.pending.length).toBeGreaterThan(0));

    fireEvent.change(await screen.findByLabelText("Tenant"), {
      target: { value: "clearview" }
    });
    await waitFor(() =>
      expect(backend.pending.some((entry) => entry.url.includes("tenant_id=clearview"))).toBe(true)
    );
    return backend;
  }

  test("a stale tenant's queue is dropped when it answers after the new one", async () => {
    const backend = await switchToClearview();

    await backend.release("tenant_id=clearview", { sessions: [WIRE_ARCHIVED] });
    await backend.release(`/api/admin/chats/${CLEARVIEW_SESSION_ID}`, {
      session: WIRE_ARCHIVED,
      messages: []
    });
    await waitFor(() =>
      expect(document.querySelector("#sessionList")?.textContent).toContain("Clearview Heating")
    );

    // The superseded apex queue answers last, and must change nothing.
    await backend.release("tenant_id=apex", { sessions: [WIRE_SUMMARY] });
    await flush();

    const queue = document.querySelector("#sessionList")?.textContent ?? "";
    expect(queue).toContain("Clearview Heating");
    expect(queue).not.toContain("Apex Home Services");
  });

  test("a stale transcript is dropped when a newer selection answers first", async () => {
    // The selection race rather than the tenant race: the operator clicks a
    // second conversation while the first transcript is still in flight, and
    // the slower first response must not overwrite what they are now reading.
    const pending: Array<{ url: string; release: (body: unknown) => void }> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => {
        if (url.endsWith("/api/admin/tenants")) {
          return jsonResponse({
            tenants: [
              { tenant_id: "apex", name: "Apex Home Services", role: "support_agent" },
              { tenant_id: "clearview", name: "Clearview Heating", role: "support_agent" }
            ]
          });
        }
        if (url.endsWith("/api/admin/csrf-token")) return jsonResponse({ csrf_token: "token-1" });
        if (url.includes("/api/admin/chats?")) {
          return jsonResponse({ sessions: [WIRE_SUMMARY, WIRE_ARCHIVED] });
        }
        return new Promise((resolve) => {
          pending.push({ url, release: (body) => resolve(jsonResponse(body)) });
        });
      })
    );
    const release = async (fragment: string, body: unknown) => {
      await waitFor(() => expect(pending.some((entry) => entry.url.includes(fragment))).toBe(true));
      pending
        .splice(
          pending.findIndex((entry) => entry.url.includes(fragment)),
          1
        )[0]!
        .release(body);
    };

    render(<AdminPage />);
    // The poll auto-selects the first row, so its transcript is already in
    // flight when the operator clicks the second.
    const rows = await screen.findAllByText(/Home Services|Heating/, {
      selector: ".session-row strong"
    });
    fireEvent.click(rows[1]!.closest("button")!);

    await release(`/api/admin/chats/${CLEARVIEW_SESSION_ID}`, {
      session: WIRE_ARCHIVED,
      messages: []
    });
    await waitFor(() =>
      expect(screen.queryByRole("heading", { name: CLEARVIEW_SESSION_ID })).toBeTruthy()
    );

    // The first click's transcript lands last and names the other conversation.
    await release(`/api/admin/chats/${APEX_SESSION_ID}`, {
      session: WIRE_SUMMARY,
      messages: WIRE_MESSAGES
    });
    await flush();

    expect(screen.queryByRole("heading", { name: APEX_SESSION_ID })).toBeNull();
    expect(screen.getByRole("log", { name: "Transcript" }).textContent ?? "").not.toContain(
      "My boiler is out."
    );
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

  test("a signed-in account without a role is not sent through an OIDC loop", async () => {
    const fetchMock = vi.fn(() => jsonResponse({}, { ok: false, status: 403 }));
    vi.stubGlobal("fetch", fetchMock);
    const assign = vi.fn();
    vi.spyOn(window, "location", "get").mockReturnValue({
      protocol: "https:",
      set href(value: string) {
        assign(value);
      }
    } as unknown as Location);

    render(<AdminPage />);

    expect(await screen.findByRole("alert")).toHaveProperty(
      "textContent",
      "This account is signed in but has no TenantChat role. Ask a Keycloak administrator to assign an admin group."
    );
    expect(assign).not.toHaveBeenCalled();
    expect(fetchMock).toHaveBeenCalledTimes(1);
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
