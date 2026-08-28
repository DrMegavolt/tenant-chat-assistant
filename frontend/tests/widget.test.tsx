import { fireEvent, waitFor } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";

import { mountWidget } from "src/widget/mount";
import {
  AVAILABILITY_REPLY,
  CONSENT_GRANTED,
  CREDENTIAL,
  LEAD_CAPTURED,
  LEAD_PENDING,
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
        return jsonResponse({
          session: { session_id: "session-apex" },
          messages: [],
          credential: CREDENTIAL
        });
      }
      if (url.endsWith("/api/chat")) {
        return jsonResponse({
          session_id: "session-apex",
          reply: "Noted.",
          pending: null,
          committed: [],
          provenance: { model_name: "scripted", graph_version: "v1", prompt_version: "v1" },
          credential: CREDENTIAL
        });
      }
      return null;
    });
    await renderDemo();

    submitChat("Hello from Apex");
    await waitFor(() => expect(inWidget("#messages")?.textContent).toContain("Noted."));
    const apexCredential = window.sessionStorage.getItem("tenant-chat-credential:apex");
    expect(apexCredential).toBe(CREDENTIAL);

    selectTenant("Clearview Heating");

    expect(inWidget("#chatCompany")?.textContent).toBe("Clearview Assistant");
    expect(inWidget("#messages")?.textContent).toContain("help find appointment slots");
    expect(inWidget("#messages")?.textContent).not.toContain("Hello from Apex");

    submitChat("Hello from Clearview");
    await waitFor(() =>
      expect(window.sessionStorage.getItem("tenant-chat-credential:clearview")).toBe(CREDENTIAL)
    );
    expect(window.sessionStorage.getItem("tenant-chat-credential:apex")).toBe(apexCredential);
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
    host.dataset.companyId = "apex";
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
        return jsonResponse({
          session: { session_id: "session-clearview" },
          messages: [],
          credential: CREDENTIAL
        });
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
      message: string;
    }[];
    expect(request?.message).toBe("I need HVAC help");
    expect(request).not.toHaveProperty("tenant_id");
    expect(request).not.toHaveProperty("session_id");
    const chatCall = fetchMock.mock.calls.find(([url]) => url.endsWith("/api/chat"));
    expect(requestCredential(chatCall?.[1])).toBe(CREDENTIAL);
  });

  test("renders a booking confirmation and commits it on approval", async () => {
    const fetchMock = stubBackend(workingBackend());
    await renderDemo();

    const confirmationCard = await openBookingConfirmation();
    expect(confirmationCard.textContent).toContain("Confirm your booking");
    expect(confirmationCard.textContent).toContain("HVAC");
    expect(confirmationCard.textContent).toContain("Tomorrow 09:00");
    // The card is the review step: the visitor must see the contact that will
    // be stored before consenting, not just the slot and address.
    expect(confirmationCard.textContent).toContain("(555) 222-1919");

    const approve = [...confirmationCard.querySelectorAll("button")].find((b) =>
      b.textContent?.includes("Confirm booking")
    )!;
    const consent = confirmationCard.querySelector<HTMLInputElement>("#bookingConfirmConsent")!;
    expect(consent.checked).toBe(false);
    fireEvent.click(consent);
    fireEvent.click(approve);

    await waitFor(() => expect(inWidget(".booking-confirmation-card")).toBeNull());
    const [confirmationBody] = requestBodies(fetchMock, "/api/chat/confirmation") as {
      decision: string;
    }[];
    expect(confirmationBody?.decision).toBe("approved");
    expect(confirmationBody).not.toHaveProperty("tenant_id");
    expect(confirmationBody).not.toHaveProperty("session_id");
    const confirmCall = fetchMock.mock.calls.find(([url]) =>
      url.endsWith("/api/chat/confirmation")
    );
    expect(requestCredential(confirmCall?.[1])).toBe(CREDENTIAL);
    expect(inWidget("#messages")?.textContent).toContain("Your appointment is booked.");
  });

  test("renders a lead confirmation and captures it only after consent", async () => {
    const fetchMock = stubBackend((url, init) => {
      if (url.endsWith("/api/tenants")) return jsonResponse({ tenants: TENANTS });
      if (url.endsWith("/api/chat/session") && init?.method === "POST") {
        return jsonResponse({
          session: { session_id: "session-apex" },
          messages: [],
          pending: null,
          credential: CREDENTIAL
        });
      }
      if (url.endsWith("/api/chat/session")) {
        return jsonResponse({
          session: { session_id: "session-apex" },
          messages: [],
          credential: CREDENTIAL
        });
      }
      if (url.endsWith("/api/chat/consent")) return jsonResponse(CONSENT_GRANTED);
      if (url.endsWith("/api/chat/confirmation")) return jsonResponse(LEAD_CAPTURED);
      if (url.endsWith("/api/chat") && init?.method === "POST") return jsonResponse(LEAD_PENDING);
      return null;
    });
    await renderDemo();

    // Apex is the lead-capturing tenant: a callback request pauses on consent.
    selectTenant("Apex Home Services");
    submitChat("I would like a callback about my furnace.");
    await waitFor(() => expect(inWidget(".booking-confirmation-card")).not.toBeNull());
    const confirmationCard = requireInWidget(".booking-confirmation-card");
    expect(confirmationCard.textContent).toContain("Confirm your callback request");
    expect(confirmationCard.textContent).toContain("dana@example.com");

    const approve = [...confirmationCard.querySelectorAll("button")].find((b) =>
      b.textContent?.includes("Confirm callback request")
    )!;
    const consent = confirmationCard.querySelector<HTMLInputElement>("#bookingConfirmConsent")!;
    fireEvent.click(consent);
    fireEvent.click(approve);

    await waitFor(() => expect(inWidget(".booking-confirmation-card")).toBeNull());
    expect(inWidget("#messages")?.textContent).toContain("The team will call you back.");
    const [confirmationBody] = requestBodies(fetchMock, "/api/chat/confirmation") as {
      decision: string;
    }[];
    expect(confirmationBody?.decision).toBe("approved");
  });

  test("a failed chat turn tells the visitor instead of leaving the composer stuck", async () => {
    stubBackend((url) => {
      if (url.endsWith("/api/tenants")) return jsonResponse({ tenants: TENANTS });
      if (url.endsWith("/api/chat/session")) {
        return jsonResponse({
          session: { session_id: "session-1" },
          messages: [],
          credential: CREDENTIAL
        });
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

describe("an embed without data-company-id", () => {
  test("no widget starts, and the integrator is told why in the console", async () => {
    // The default-tenant fallback would silently serve one company's bot on
    // another company's site — the worst possible way to fail.
    vi.useFakeTimers();
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
    stubBackend(workingBackend());
    const host = document.createElement("div");
    host.id = "tenant-chat";
    document.body.append(host);

    mountWidget(host);

    await tick();
    expect(host.shadowRoot?.querySelector("#chatWindow")).toBeUndefined();
    expect(host.shadowRoot?.querySelector("#openChat")).toBeUndefined();
    expect(consoleError).toHaveBeenCalledWith(expect.stringContaining("data-company-id"));
    consoleError.mockRestore();
  });
});
