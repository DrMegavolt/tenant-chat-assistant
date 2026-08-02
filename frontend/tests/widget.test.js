import { fireEvent } from "@testing-library/dom";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

const TENANTS = {
  apex: {
    name: "Apex Home Services",
    assistantName: "Apex Assistant",
    tagline: "Home-service help",
    site: {
      headline: "Apex headline",
      description: "Apex description"
    },
    address: "10 Main Street",
    phone: "555-111-2222",
    hours: "Always open",
    pricingPolicy: "never",
    bookingEnabled: false,
    leadCaptureEnabled: true,
    proactiveLeadCapture: false,
    services: ["HVAC", "Electrical"],
    quickActions: ["What do you repair?", "Talk to a person"]
  },
  clearview: {
    name: "Clearview Heating",
    assistantName: "Clearview Assistant",
    tagline: "Appointments and answers",
    site: {
      headline: "Clearview headline",
      description: "Clearview description"
    },
    address: "20 Broad Street",
    phone: "555-333-4444",
    hours: "Weekdays",
    pricingPolicy: "fixed",
    bookingEnabled: true,
    leadCaptureEnabled: true,
    proactiveLeadCapture: true,
    services: ["HVAC"],
    quickActions: ["Find an appointment"]
  }
};

function page(companyId = "apex", apiBaseUrl = "") {
  document.body.innerHTML = `
    <h1 id="siteName"></h1>
    <h2 id="headline"></h2>
    <p id="description"></p>
    <select id="tenantSelect">
      <option value="apex">Apex</option>
      <option value="clearview">Clearview</option>
    </select>
    <dl id="configSummary"></dl>
    <div id="tenant-chat" data-company-id="${companyId}" data-api-base-url="${apiBaseUrl}"></div>
  `;
}

function jsonResponse(body, { ok = true, status = 200 } = {}) {
  return Promise.resolve({
    ok,
    status,
    json: () => Promise.resolve(body)
  });
}

async function loadWidget(fetchImplementation) {
  vi.stubGlobal("fetch", vi.fn(fetchImplementation));
  vi.spyOn(window, "setInterval").mockReturnValue(1);
  await import("../../app.js");
  await vi.waitFor(() => {
    expect(document.querySelector("#chatCompany")?.textContent).not.toBe("");
  });
}

function submitChat(text) {
  const input = document.querySelector("#chatInput");
  input.value = text;
  fireEvent.submit(document.querySelector("#composer"));
}

function selectTenant(tenantId) {
  const picker = document.querySelector("#tenantSelect");
  picker.value = tenantId;
  fireEvent.change(picker);
}

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
    expect(document.querySelector("#chatCompany").textContent).toBe("Apex Assistant");
    expect(document.querySelector("#messages").textContent).toContain("555-111-2222");
    expect(
      [...document.querySelectorAll("#quickActions button")].map((node) => node.textContent)
    ).toEqual(["What do you repair?", "Talk to a person"]);
  });

  test("switching tenants creates an isolated session and resets the visible conversation", async () => {
    await loadWidget(() => jsonResponse({ tenants: TENANTS }));
    const apexSession = window.sessionStorage.getItem("tenant-chat-session-id:apex");

    selectTenant("clearview");

    expect(document.querySelector("#tenant-chat").dataset.companyId).toBe("clearview");
    expect(document.querySelector("#chatCompany").textContent).toBe("Clearview Assistant");
    expect(document.querySelector("#messages").textContent).toContain(
      "help find appointment slots"
    );
    expect(window.sessionStorage.getItem("tenant-chat-session-id:clearview")).toMatch(
      /^web-clearview-/
    );
    expect(window.sessionStorage.getItem("tenant-chat-session-id:apex")).toBe(apexSession);
  });

  test("shows a bounded backend error when tenant configuration cannot load", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => jsonResponse({}, { ok: false, status: 503 }))
    );
    vi.spyOn(window, "setInterval").mockReturnValue(1);

    await import("../../app.js");
    await vi.waitFor(() => expect(document.querySelector(".backend-error")).not.toBeNull());

    expect(document.querySelector("#tenant-chat").textContent).toBe("");
    expect(document.querySelector(".backend-error").textContent).toBe(
      "Backend unavailable: Unable to load tenant configuration from backend."
    );
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
        return jsonResponse({
          reply: "I found one opening.",
          toolEvents: [
            {
              name: "get_availability",
              arguments: { service: "hvac" },
              result: { service: "hvac", slots: ["Tomorrow 09:00"] }
            }
          ]
        });
      }
      throw new Error(`unexpected request: ${url}`);
    });

    selectTenant("clearview");

    submitChat("I need HVAC help");

    await vi.waitFor(() => {
      expect(document.querySelector("#messages").textContent).toContain("I found one opening.");
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
    expect(document.querySelector(".tool-call").textContent).toContain("get_availability");
    expect(document.querySelector(".booking-form-card")).not.toBeNull();
  });

  test("submits the structured booking form and replaces it with confirmation", async () => {
    await loadWidget((url, options) => {
      if (url === "/api/tenants") return jsonResponse({ tenants: TENANTS });
      if (url === "/api/chat") {
        return jsonResponse({
          reply: "Choose a time.",
          toolEvents: [
            {
              name: "get_availability",
              arguments: { service: "hvac" },
              result: { service: "hvac", slots: ["Tomorrow 09:00"] }
            }
          ]
        });
      }
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
    await vi.waitFor(() => expect(document.querySelector(".booking-form-card")).not.toBeNull());
    const form = document.querySelector(".booking-form-card");
    form.elements.customerName.value = "Sam Lee";
    form.elements.address.value = "42 Cedar Road";
    form.elements.contact.value = "sam@example.test";
    fireEvent.submit(form);

    await vi.waitFor(() => expect(document.querySelector(".booking-form-card")).toBeNull());
    const bookingCall = fetch.mock.calls.find(([url]) => url.endsWith("/api/book"));
    expect(JSON.parse(bookingCall[1].body)).toMatchObject({
      tenantId: "clearview",
      service: "hvac",
      slot: "Tomorrow 09:00",
      customerName: "Sam Lee",
      address: "42 Cedar Road",
      contact: "sam@example.test"
    });
    expect(document.querySelector("#messages").textContent).toContain(
      "Your appointment is booked."
    );
  });

  test("keeps a failed booking editable and presents the backend validation message", async () => {
    await loadWidget((url) => {
      if (url === "/api/tenants") return jsonResponse({ tenants: TENANTS });
      if (url === "/api/chat") {
        return jsonResponse({
          reply: "Choose a time.",
          toolEvents: [
            {
              name: "get_availability",
              arguments: { service: "hvac" },
              result: { service: "hvac", slots: ["Tomorrow 09:00"] }
            }
          ]
        });
      }
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
    await vi.waitFor(() => expect(document.querySelector(".booking-form-card")).not.toBeNull());
    const form = document.querySelector(".booking-form-card");
    form.elements.customerName.value = "Sam Lee";
    form.elements.address.value = "42 Cedar Road";
    form.elements.contact.value = "invalid";
    fireEvent.submit(form);

    await vi.waitFor(() => {
      expect(document.querySelector(".booking-error")?.textContent).toBe(
        "Please provide a reachable contact."
      );
    });
    expect(form.querySelector("button").disabled).toBe(false);
    expect(document.querySelector(".booking-form-card")).toBe(form);
  });
});
