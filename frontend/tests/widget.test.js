import { fireEvent } from "@testing-library/dom";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import {
  TENANTS,
  allInWidget,
  inWidget,
  jsonResponse,
  loadWidget,
  page,
  shadow
} from "./support/page.js";

function submitChat(text) {
  const input = inWidget("#chatInput");
  input.value = text;
  fireEvent.submit(inWidget("#composer"));
}

function selectTenant(tenantId) {
  const picker = document.querySelector("#tenantSelect");
  picker.value = tenantId;
  fireEvent.change(picker);
}

function fillBooking(form, { consent = true } = {}) {
  form.querySelector("#booking-customerName").value = "Sam Lee";
  form.querySelector("#booking-address").value = "42 Cedar Road";
  form.querySelector("#booking-contact").value = "sam@example.test";
  form.querySelector("#bookingConsent").checked = consent;
}

const AVAILABILITY_REPLY = {
  reply: "Choose a time.",
  toolEvents: [
    {
      name: "get_availability",
      arguments: { service: "hvac" },
      result: { service: "hvac", slots: ["Tomorrow 09:00"] }
    }
  ]
};

beforeEach(() => {
  vi.resetModules();
  page();
  window.sessionStorage.clear();
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("widget initialization", () => {
  test("renders backend-owned tenant branding, welcome copy, and quick actions", async () => {
    await loadWidget(() => jsonResponse({ tenants: TENANTS }));

    expect(document.querySelector("#siteName").textContent).toBe("Apex Home Services");
    expect(document.querySelector("#headline").textContent).toBe("Apex headline");
    expect(inWidget("#chatCompany").textContent).toBe("Apex Assistant");
    expect(inWidget("#messages").textContent).toContain("555-111-2222");
    expect(allInWidget("#quickActions button").map((node) => node.textContent)).toEqual([
      "What do you repair?",
      "Talk to a person"
    ]);
  });

  test("keeps its markup out of the host document so page styles cannot reach it", async () => {
    await loadWidget(() => jsonResponse({ tenants: TENANTS }));

    expect(document.querySelector("#tenant-chat").childElementCount).toBe(0);
    expect(document.querySelector("#chatWindow")).toBeNull();
    expect(document.querySelector("#chatInput")).toBeNull();
    expect(shadow().querySelector("style").textContent).toContain("all: initial");
  });

  test("switching tenants creates an isolated session and resets the visible conversation", async () => {
    await loadWidget((url) => {
      if (url === "/api/tenants") return jsonResponse({ tenants: TENANTS });
      if (url === "/api/chat") return jsonResponse({ reply: "Noted.", toolEvents: [] });
      throw new Error(`unexpected request: ${url}`);
    });

    submitChat("Hello from Apex");
    await vi.waitFor(() => expect(inWidget("#messages").textContent).toContain("Noted."));
    const apexSession = window.sessionStorage.getItem("tenant-chat-session-id:apex");
    expect(apexSession).toMatch(/^web-apex-/);

    selectTenant("clearview");

    expect(document.querySelector("#tenant-chat").dataset.companyId).toBe("clearview");
    expect(inWidget("#chatCompany").textContent).toBe("Clearview Assistant");
    expect(inWidget("#messages").textContent).toContain("help find appointment slots");
    expect(inWidget("#messages").textContent).not.toContain("Hello from Apex");

    submitChat("Hello from Clearview");
    await vi.waitFor(() =>
      expect(window.sessionStorage.getItem("tenant-chat-session-id:clearview")).toMatch(
        /^web-clearview-/
      )
    );
    expect(window.sessionStorage.getItem("tenant-chat-session-id:apex")).toBe(apexSession);
  });

  test("the standalone embed needs nothing from the host page but a mount element", async () => {
    document.body.innerHTML = `<div id="tenant-chat" data-company-id="clearview"></div>`;
    vi.stubGlobal(
      "fetch",
      vi.fn(() => jsonResponse({ tenants: TENANTS }))
    );
    vi.spyOn(window, "setInterval").mockReturnValue(1);

    await import("../public/embed.js");

    await vi.waitFor(() =>
      expect(inWidget("#chatCompany").textContent).toBe("Clearview Assistant")
    );
    expect(inWidget("#messages").textContent).toContain("help find appointment slots");
  });

  test("no browser storage is written until the visitor actually sends something", async () => {
    await loadWidget(() => jsonResponse({ tenants: TENANTS }));

    expect(window.sessionStorage.length).toBe(0);
  });

  test("shows a bounded backend error when tenant configuration cannot load", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => jsonResponse({}, { ok: false, status: 503 }))
    );
    vi.spyOn(window, "setInterval").mockReturnValue(1);

    await import("../public/app.js");
    await vi.waitFor(() => expect(shadow().querySelector(".widget-error")).not.toBeNull());

    const error = shadow().querySelector(".widget-error");
    expect(error.getAttribute("role")).toBe("alert");
    expect(error.textContent).toContain("Unable to load tenant configuration from backend.");
    expect(shadow().querySelector("#chatWindow")).toBeNull();
  });
});

describe("chat and booking contracts", () => {
  test("posts the visible conversation and renders tool results and the assistant reply", async () => {
    page("apex", "https://chat.example.test///");
    await loadWidget((url, options) => {
      if (url === "https://chat.example.test/api/tenants") {
        return jsonResponse({ tenants: TENANTS });
      }
      if (url === "https://chat.example.test/api/chat" && options.method === "POST") {
        return jsonResponse({ ...AVAILABILITY_REPLY, reply: "I found one opening." });
      }
      throw new Error(`unexpected request: ${url}`);
    });

    selectTenant("clearview");
    submitChat("I need HVAC help");

    await vi.waitFor(() => {
      expect(inWidget("#messages").textContent).toContain("I found one opening.");
    });
    const chatCall = fetch.mock.calls.find(([url]) => url.endsWith("/api/chat"));
    const request = JSON.parse(chatCall[1].body);
    expect(request.tenantId).toBe("clearview");
    expect(request.sessionId).toMatch(/^web-clearview-/);
    expect(request.messages.at(-1)).toEqual({
      role: "user",
      content: "I need HVAC help",
      source: "user"
    });
    expect(inWidget(".tool-call").textContent).toContain("get_availability");
    expect(inWidget(".booking-form-card")).not.toBeNull();
  });

  test("submits the structured booking form and replaces it with confirmation", async () => {
    await loadWidget((url, options) => {
      if (url === "/api/tenants") return jsonResponse({ tenants: TENANTS });
      if (url === "/api/chat") return jsonResponse(AVAILABILITY_REPLY);
      if (url === "/api/book" && options.method === "POST") {
        return jsonResponse({
          reply: "Your appointment is booked.",
          toolEvent: {
            name: "book_appointment",
            arguments: {},
            result: { bookingId: "booking-1" }
          }
        });
      }
      throw new Error(`unexpected request: ${url}`);
    });
    selectTenant("clearview");

    submitChat("Show HVAC availability");
    await vi.waitFor(() => expect(inWidget(".booking-form-card")).not.toBeNull());
    const form = inWidget(".booking-form-card");
    fillBooking(form);
    fireEvent.submit(form);

    await vi.waitFor(() => expect(inWidget(".booking-form-card")).toBeNull());
    const bookingCall = fetch.mock.calls.find(([url]) => url.endsWith("/api/book"));
    expect(JSON.parse(bookingCall[1].body)).toMatchObject({
      tenantId: "clearview",
      service: "hvac",
      slot: "Tomorrow 09:00",
      customerName: "Sam Lee",
      address: "42 Cedar Road",
      contact: "sam@example.test"
    });
    expect(inWidget("#messages").textContent).toContain("Your appointment is booked.");
  });

  test("keeps a failed booking editable and presents the backend validation message", async () => {
    await loadWidget((url) => {
      if (url === "/api/tenants") return jsonResponse({ tenants: TENANTS });
      if (url === "/api/chat") return jsonResponse(AVAILABILITY_REPLY);
      if (url === "/api/book") {
        return jsonResponse(
          { toolEvent: { result: { message: "Please provide a reachable contact." } } },
          { ok: false, status: 422 }
        );
      }
      throw new Error(`unexpected request: ${url}`);
    });
    selectTenant("clearview");

    submitChat("Show HVAC availability");
    await vi.waitFor(() => expect(inWidget(".booking-form-card")).not.toBeNull());
    const form = inWidget(".booking-form-card");
    fillBooking(form);
    form.querySelector("#booking-contact").value = "invalid";
    fireEvent.submit(form);

    await vi.waitFor(() => {
      expect(inWidget("#bookingError").textContent).toBe("Please provide a reachable contact.");
    });
    expect(form.querySelector("button[type='submit']").disabled).toBe(false);
    expect(inWidget(".booking-form-card")).toBe(form);
  });

  test("a failed chat turn tells the visitor instead of leaving the composer stuck", async () => {
    await loadWidget((url) => {
      if (url === "/api/tenants") return jsonResponse({ tenants: TENANTS });
      if (url === "/api/chat") return jsonResponse({}, { ok: false, status: 500 });
      throw new Error(`unexpected request: ${url}`);
    });

    submitChat("Are you there?");

    await vi.waitFor(() => {
      expect(inWidget("#messages").textContent).toContain("could not reach the chat service");
    });
    expect(inWidget("#chatInput").disabled).toBe(false);
    expect(inWidget("#messages").getAttribute("aria-busy")).toBe("false");
  });
});
