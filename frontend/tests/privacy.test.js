import { fireEvent } from "@testing-library/dom";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { TENANTS, inWidget, jsonResponse, loadWidget, page, shadow } from "./support/page.js";

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

const BOOKING_CONFIRMED = {
  reply: "Your appointment is booked.",
  toolEvent: { name: "book_appointment", arguments: {}, result: { bookingId: "booking-1" } }
};

function backend(url) {
  if (url === "/api/tenants") return jsonResponse({ tenants: TENANTS });
  if (url === "/api/chat") return jsonResponse(AVAILABILITY_REPLY);
  if (url === "/api/book") return jsonResponse(BOOKING_CONFIRMED);
  throw new Error(`unexpected request: ${url}`);
}

async function openBookingForm() {
  const picker = document.querySelector("#tenantSelect");
  picker.value = "clearview";
  fireEvent.change(picker);
  inWidget("#chatInput").value = "Show HVAC availability";
  fireEvent.submit(inWidget("#composer"));
  await vi.waitFor(() => expect(inWidget(".booking-form-card")).not.toBeNull());
  return inWidget(".booking-form-card");
}

function fill(form) {
  form.querySelector("#booking-customerName").value = "Sam Lee";
  form.querySelector("#booking-address").value = "42 Cedar Road";
  form.querySelector("#booking-contact").value = "sam@example.test";
}

function bookingRequests() {
  return fetch.mock.calls.filter(([url]) => url.endsWith("/api/book"));
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

describe("consent before contact data leaves the browser", () => {
  test("the booking form states what is stored and by whom before anything is sent", async () => {
    await loadWidget(backend);
    const form = await openBookingForm();

    const consentLabel = form.querySelector("label[for='bookingConsent']");
    expect(consentLabel.textContent).toContain("Clearview Heating");
    expect(consentLabel.textContent).toContain("name, address, and contact details");
    expect(form.querySelector("#bookingConsent").checked).toBe(false);
  });

  test("submitting without consent sends nothing and moves focus to the checkbox", async () => {
    await loadWidget(backend);
    const form = await openBookingForm();
    fill(form);

    fireEvent.submit(form);

    expect(bookingRequests()).toHaveLength(0);
    expect(form.querySelector("#bookingError").textContent).toBe(
      "Please agree to the data notice before booking."
    );
    const checkbox = form.querySelector("#bookingConsent");
    expect(checkbox.getAttribute("aria-invalid")).toBe("true");
    expect(shadow().activeElement).toBe(checkbox);
  });

  test("granting consent records it locally and sends it with the booking", async () => {
    await loadWidget(backend);
    const form = await openBookingForm();
    fill(form);
    form.querySelector("#bookingConsent").checked = true;

    fireEvent.submit(form);
    await vi.waitFor(() => expect(bookingRequests()).toHaveLength(1));

    const sent = JSON.parse(bookingRequests()[0][1].body);
    expect(sent.consent.statement).toContain("Clearview Heating");
    expect(Date.parse(sent.consent.grantedAt)).not.toBeNaN();
    const stored = JSON.parse(window.sessionStorage.getItem("tenant-chat-consent:clearview"));
    expect(stored.statement).toBe(sent.consent.statement);
  });
});

describe("visitor data controls", () => {
  test("the privacy panel is collapsed until asked for and explains what is kept", async () => {
    await loadWidget(backend);

    const toggle = inWidget("#privacyToggle");
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(inWidget("#privacyPanel").hidden).toBe(true);

    fireEvent.click(toggle);

    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(inWidget("#privacyPanel").hidden).toBe(false);
    expect(inWidget("#privacyPanel").textContent).toContain("conversation id");
    expect(shadow().activeElement).toBe(inWidget("#clearVisitorData"));
  });

  test("deleting local data clears every stored key and announces the reset", async () => {
    await loadWidget(backend);
    const form = await openBookingForm();
    fill(form);
    form.querySelector("#bookingConsent").checked = true;
    fireEvent.submit(form);
    await vi.waitFor(() => expect(inWidget(".booking-form-card")).toBeNull());

    fireEvent.click(inWidget("#privacyToggle"));
    fireEvent.click(inWidget("#clearVisitorData"));

    for (const key of ["session-id", "consent", "contact"]) {
      expect(window.sessionStorage.getItem(`tenant-chat-${key}:clearview`)).toBeNull();
    }
    expect(inWidget("#assistantStatus").textContent).toContain("deleted from your browser");
    expect(inWidget("#messages").textContent).not.toContain("Your appointment is booked.");
  });

  test("a second booking in the same session does not ask for details already given", async () => {
    await loadWidget(backend);
    const first = await openBookingForm();
    fill(first);
    first.querySelector("#bookingConsent").checked = true;
    fireEvent.submit(first);
    await vi.waitFor(() => expect(inWidget(".booking-form-card")).toBeNull());

    inWidget("#chatInput").value = "Another HVAC visit please";
    fireEvent.submit(inWidget("#composer"));
    await vi.waitFor(() => expect(inWidget(".booking-form-card")).not.toBeNull());

    const second = inWidget(".booking-form-card");
    expect(second.querySelector("#booking-customerName").value).toBe("Sam Lee");
    expect(second.querySelector("#booking-address").value).toBe("42 Cedar Road");
    expect(second.querySelector("#booking-contact").value).toBe("sam@example.test");
    expect(second.querySelector("#bookingConsent").checked).toBe(false);
  });
});
