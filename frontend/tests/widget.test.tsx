import { fireEvent, waitFor } from "@testing-library/react";
import { describe, expect, test } from "vitest";

import { mountWidget } from "src/widget/mount";
import {
  AVAILABILITY_REPLY,
  TENANTS,
  jsonResponse,
  requestBodies,
  stubBackend,
  workingBackend
} from "tests/support/backend";
import {
  allInWidget,
  fillBooking,
  inWidget,
  openBookingForm,
  renderDemo,
  requireInWidget,
  selectTenant,
  shadow,
  submitChat
} from "tests/support/widget";

describe("widget initialization", () => {
  test("renders backend-owned tenant branding, welcome copy, and quick actions", async () => {
    stubBackend(workingBackend());
    await renderDemo();

    expect(document.querySelector("#siteName")?.textContent).toBe("Apex Home Services");
    expect(document.querySelector("#headline")?.textContent).toBe("Apex headline");
    expect(inWidget("#chatCompany")?.textContent).toBe("Apex Assistant");
    expect(inWidget("#messages")?.textContent).toContain("555-111-2222");
    expect(allInWidget("#quickActions button").map((node) => node.textContent)).toEqual([
      "What do you repair?",
      "Talk to a person"
    ]);
  });

  test("keeps its markup out of the host document so page styles cannot reach it", async () => {
    stubBackend(workingBackend());
    const host = await renderDemo();

    expect(host.childElementCount).toBe(0);
    expect(document.querySelector("#chatWindow")).toBeNull();
    expect(document.querySelector("#chatInput")).toBeNull();
    expect(shadow().querySelector("style")?.textContent).toContain("all: initial");
  });

  test("switching tenants creates an isolated session and resets the visible conversation", async () => {
    stubBackend((url) => {
      if (url.endsWith("/api/tenants")) return jsonResponse({ tenants: TENANTS });
      if (url.endsWith("/api/chat")) return jsonResponse({ reply: "Noted.", toolEvents: [] });
      return null;
    });
    await renderDemo();

    submitChat("Hello from Apex");
    await waitFor(() => expect(inWidget("#messages")?.textContent).toContain("Noted."));
    const apexSession = window.sessionStorage.getItem("tenant-chat-session-id:apex");
    expect(apexSession).toMatch(/^web-apex-/);

    selectTenant("Clearview Heating");

    expect(inWidget("#chatCompany")?.textContent).toBe("Clearview Assistant");
    expect(inWidget("#messages")?.textContent).toContain("help find appointment slots");
    expect(inWidget("#messages")?.textContent).not.toContain("Hello from Apex");

    submitChat("Hello from Clearview");
    await waitFor(() =>
      expect(window.sessionStorage.getItem("tenant-chat-session-id:clearview")).toMatch(
        /^web-clearview-/
      )
    );
    expect(window.sessionStorage.getItem("tenant-chat-session-id:apex")).toBe(apexSession);
  });

  test("the standalone embed needs nothing from the host page but a mount element", async () => {
    stubBackend(workingBackend());
    const host = document.createElement("div");
    host.id = "tenant-chat";
    host.dataset.companyId = "clearview";
    document.body.append(host);

    mountWidget(host);

    // An embed starts collapsed: a widget that opens itself on somebody else's
    // site covers their content before the visitor has asked for anything.
    await waitFor(() => expect(inWidget("#openChat")).not.toBeNull());
    expect(requireInWidget("#chatWindow").hidden).toBe(true);

    fireEvent.click(requireInWidget("#openChat"));

    expect(inWidget("#chatCompany")?.textContent).toBe("Clearview Assistant");
    expect(inWidget("#messages")?.textContent).toContain("help find appointment slots");
  });

  test("no browser storage is written until the visitor actually sends something", async () => {
    stubBackend(workingBackend());
    await renderDemo();

    expect(window.sessionStorage.length).toBe(0);
  });

  test("polling for staff replies does not issue a conversation id on its own", async () => {
    // A visitor who opened the page and typed nothing has started no
    // conversation, and background polling must not leave an identifier behind.
    const fetchMock = stubBackend(workingBackend());
    await renderDemo();

    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(fetchMock.mock.calls.filter(([url]) => url.includes("/api/chat/session"))).toEqual([]);
    expect(window.sessionStorage.length).toBe(0);
  });

  test("shows a bounded backend error when tenant configuration cannot load", async () => {
    stubBackend(() => jsonResponse({}, { ok: false, status: 503 }));

    const host = document.createElement("div");
    host.id = "tenant-chat";
    document.body.append(host);
    mountWidget(host);

    await waitFor(() => expect(inWidget(".widget-error")).not.toBeNull());
    const error = requireInWidget(".widget-error");
    expect(error.getAttribute("role")).toBe("alert");
    expect(error.textContent).toContain("Unable to load tenant configuration from backend.");
    expect(inWidget("#chatWindow")).toBeNull();
  });
});

describe("chat and booking contracts", () => {
  test("posts the visible conversation and renders tool results and the assistant reply", async () => {
    const fetchMock = stubBackend((url, init) => {
      if (url === "https://chat.example.test/api/tenants") {
        return jsonResponse({ tenants: TENANTS });
      }
      if (url === "https://chat.example.test/api/chat" && init?.method === "POST") {
        return jsonResponse({ ...AVAILABILITY_REPLY, reply: "I found one opening." });
      }
      return null;
    });
    await renderDemo({ apiBaseUrl: "https://chat.example.test///" });

    selectTenant("Clearview Heating");
    submitChat("I need HVAC help");

    await waitFor(() => {
      expect(inWidget("#messages")?.textContent).toContain("I found one opening.");
    });
    const [request] = requestBodies(fetchMock, "/api/chat") as {
      tenantId: string;
      sessionId: string;
      messages: unknown[];
    }[];
    expect(request?.tenantId).toBe("clearview");
    expect(request?.sessionId).toMatch(/^web-clearview-/);
    expect(request?.messages.at(-1)).toEqual({
      role: "user",
      content: "I need HVAC help",
      source: "user"
    });
    expect(inWidget(".tool-call")?.textContent).toContain("get_availability");
    expect(inWidget(".booking-form-card")).not.toBeNull();
  });

  test("submits the structured booking form and replaces it with confirmation", async () => {
    const fetchMock = stubBackend(workingBackend());
    await renderDemo();

    const form = await openBookingForm();
    fillBooking(form);
    fireEvent.submit(form);

    await waitFor(() => expect(inWidget(".booking-form-card")).toBeNull());
    expect(requestBodies(fetchMock, "/api/book")[0]).toMatchObject({
      tenantId: "clearview",
      service: "hvac",
      slot: "Tomorrow 09:00",
      customerName: "Sam Lee",
      address: "42 Cedar Road",
      contact: "sam@example.test"
    });
    expect(inWidget("#messages")?.textContent).toContain("Your appointment is booked.");
  });

  test("keeps a failed booking editable and presents the backend validation message", async () => {
    stubBackend((url) => {
      if (url.endsWith("/api/tenants")) return jsonResponse({ tenants: TENANTS });
      if (url.endsWith("/api/chat")) return jsonResponse(AVAILABILITY_REPLY);
      if (url.endsWith("/api/book")) {
        return jsonResponse(
          { toolEvent: { result: { message: "Please provide a reachable contact." } } },
          { ok: false, status: 422 }
        );
      }
      return null;
    });
    await renderDemo();

    const form = await openBookingForm();
    fillBooking(form);
    fireEvent.change(form.querySelector("#booking-contact")!, { target: { value: "invalid" } });
    fireEvent.submit(form);

    await waitFor(() => {
      expect(inWidget("#bookingError")?.textContent).toBe("Please provide a reachable contact.");
    });
    expect(form.querySelector<HTMLButtonElement>("button[type='submit']")?.disabled).toBe(false);
    expect(inWidget(".booking-form-card")).toBe(form);
    expect(form.querySelector<HTMLInputElement>("#booking-customerName")?.value).toBe("Sam Lee");
  });

  test("a failed chat turn tells the visitor instead of leaving the composer stuck", async () => {
    stubBackend((url) => {
      if (url.endsWith("/api/tenants")) return jsonResponse({ tenants: TENANTS });
      if (url.endsWith("/api/chat")) return jsonResponse({}, { ok: false, status: 500 });
      return null;
    });
    await renderDemo();

    submitChat("Are you there?");

    await waitFor(() => {
      expect(inWidget("#messages")?.textContent).toContain("could not reach the chat service");
    });
    expect(requireInWidget<HTMLInputElement>("#chatInput").disabled).toBe(false);
    expect(inWidget("#messages")?.getAttribute("aria-busy")).toBe("false");
  });
});
