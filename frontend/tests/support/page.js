import { expect, vi } from "vitest";

export const TENANTS = {
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

/**
 * Recreate the demo host page the widget mounts into.
 *
 * The document-level attributes and the placeholder heading copy mirror
 * `public/index.html`, so an axe run over this fixture reports on the widget
 * rather than on gaps in the fixture.
 */
export function page(companyId = "apex", apiBaseUrl = "") {
  document.documentElement.lang = "en";
  document.title = "Tenant Chat Assistant Prototype";
  document.body.innerHTML = `
    <main>
      <h1 id="siteName">Apex Home Services</h1>
      <label for="tenantSelect">Preview company</label>
      <select id="tenantSelect">
        <option value="apex">Apex</option>
        <option value="clearview">Clearview</option>
      </select>
      <h2 id="headline">Fast help for HVAC, electrical, and home repairs.</h2>
      <p id="description">This page stands in for a customer website.</p>
      <dl id="configSummary"></dl>
    </main>
    <div id="tenant-chat" data-company-id="${companyId}" data-api-base-url="${apiBaseUrl}"></div>
  `;
}

export function jsonResponse(body, { ok = true, status = 200 } = {}) {
  return Promise.resolve({
    ok,
    status,
    json: () => Promise.resolve(body)
  });
}

/** Load the entry point against a stubbed backend and wait for first paint. */
export async function loadWidget(fetchImplementation) {
  vi.stubGlobal("fetch", vi.fn(fetchImplementation));
  vi.spyOn(window, "setInterval").mockReturnValue(1);
  await import("../../public/app.js");
  await vi.waitFor(() => {
    expect(shadow()?.querySelector("#chatCompany")?.textContent).toBeTruthy();
  });
}

/** The widget's shadow root: the only surface a host page can observe. */
export function shadow() {
  return document.querySelector("#tenant-chat").shadowRoot;
}

export function inWidget(selector) {
  return shadow().querySelector(selector);
}

export function allInWidget(selector) {
  return [...shadow().querySelectorAll(selector)];
}
