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
  inWidget,
  openBookingConfirmation,
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
      if (url.endsWith("/api/chat/session")) {
        return jsonResponse({ session: { session_id: "session-apex" }, messages: [] });
      }
      if (url.endsWith("/api/chat")) {
        return jsonResponse({
          session_id: "session-apex",
          reply: "Noted.",
          pending: null,
          committed: [],
          provenance: { model_name: "scripted", graph_version: "v1", prompt_version: "v1" }
        });
      }
      return null;
    });
    await renderDemo();

    submitChat("Hello from Apex");
    await waitFor(() => expect(inWidget("#messages")?.textContent).toContain("Noted."));
    const apexSession = window.sessionStorage.getItem("tenant-chat-session-id:apex");
    expect(apexSession).toBe("session-apex");

    selectTenant("Clearview Heating");

    expect(inWidget("#chatCompany")?.textContent).toBe("Clearview Assistant");
    expect(inWidget("#messages")?.textContent).toContain("help find appointment slots");
    expect(inWidget("#messages")?.textContent).not.toContain("Hello from Apex");

    submitChat("Hello from Clearview");
    await waitFor(() =>
      expect(window.sessionStorage.getItem("tenant-chat-session-id:clearview")).toBe("session-apex")
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
  test("posts one turn and renders the assistant reply", async () => {
    const fetchMock = stubBackend((url, init) => {
      if (url === "https://chat.example.test/api/tenants") {
        return jsonResponse({ tenants: TENANTS });
      }
      if (url === "https://chat.example.test/api/chat/session" && init?.method === "POST") {
        return jsonResponse({ session: { session_id: "session-clearview" }, messages: [] });
      }
      if (url === "https://chat.example.test/api/chat" && init?.method === "POST") {
        return jsonResponse({
          ...AVAILABILITY_REPLY,
          session_id: "session-clearview",
          reply: "I found one opening.",
          pending: null
        });
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
      tenant_id: string;
      session_id: string;
      message: string;
    }[];
    expect(request?.tenant_id).toBe("clearview");
    expect(request?.session_id).toBe("session-clearview");
    expect(request?.message).toBe("I need HVAC help");
  });

  test("renders a booking confirmation and commits it on approval", async () => {
    const fetchMock = stubBackend(workingBackend());
    await renderDemo();

    const confirmation = await openBookingConfirmation();
    expect(confirmation.textContent).toContain("Confirm your booking");
    expect(confirmation.textContent).toContain("HVAC");
    expect(confirmation.textContent).toContain("Tomorrow 09:00");

    const approve = [...confirmation.querySelectorAll("button")].find((b) =>
      b.textContent?.includes("Confirm booking")
    )!;
    const consent = confirmation.querySelector<HTMLInputElement>("#bookingConfirmConsent")!;
    expect(consent.checked).toBe(false);
    fireEvent.click(consent);
    fireEvent.click(approve);

    await waitFor(() => expect(inWidget(".booking-confirmation-card")).toBeNull());
    expect(requestBodies(fetchMock, "/api/chat/confirmation")[0]).toMatchObject({
      tenant_id: "clearview",
      session_id: "session-1",
      decision: "approved"
    });
    expect(inWidget("#messages")?.textContent).toContain("Your appointment is booked.");
  });

  test("a failed chat turn tells the visitor instead of leaving the composer stuck", async () => {
    stubBackend((url) => {
      if (url.endsWith("/api/tenants")) return jsonResponse({ tenants: TENANTS });
      if (url.endsWith("/api/chat/session")) {
        return jsonResponse({ session: { session_id: "session-1" }, messages: [] });
      }
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
