import { fireEvent, waitFor } from "@testing-library/react";
import { describe, expect, test } from "vitest";

import { requestBodies, stubBackend, workingBackend } from "tests/support/backend";
import {
  fillBooking,
  inWidget,
  openBookingForm,
  renderDemo,
  requireInWidget,
  shadow,
  submitChat
} from "tests/support/widget";

describe("consent before contact data leaves the browser", () => {
  test("the booking form states what is stored and by whom before anything is sent", async () => {
    stubBackend(workingBackend());
    await renderDemo();
    const form = await openBookingForm();

    const consentLabel = form.querySelector("label[for='bookingConsent']");
    expect(consentLabel?.textContent).toContain("Clearview Heating");
    expect(consentLabel?.textContent).toContain("name, address, and contact details");
    expect(form.querySelector<HTMLInputElement>("#bookingConsent")?.checked).toBe(false);
  });

  test("submitting without consent sends nothing and moves focus to the checkbox", async () => {
    const fetchMock = stubBackend(workingBackend());
    await renderDemo();
    const form = await openBookingForm();
    fillBooking(form, { consent: false });

    fireEvent.submit(form);

    expect(requestBodies(fetchMock, "/api/book")).toHaveLength(0);
    expect(form.querySelector("#bookingError")?.textContent).toBe(
      "Please agree to the data notice before booking."
    );
    const checkbox = form.querySelector("#bookingConsent");
    expect(checkbox?.getAttribute("aria-invalid")).toBe("true");
    expect(shadow().activeElement).toBe(checkbox);
  });

  test("granting consent records it locally and sends it with the booking", async () => {
    const fetchMock = stubBackend(workingBackend());
    await renderDemo();
    const form = await openBookingForm();
    fillBooking(form);

    fireEvent.submit(form);
    await waitFor(() => expect(requestBodies(fetchMock, "/api/book")).toHaveLength(1));

    const [sent] = requestBodies(fetchMock, "/api/book") as {
      consent: { statement: string; grantedAt: string };
    }[];
    expect(sent?.consent.statement).toContain("Clearview Heating");
    expect(Date.parse(sent!.consent.grantedAt)).not.toBeNaN();
    const stored = JSON.parse(
      window.sessionStorage.getItem("tenant-chat-consent:clearview") ?? "null"
    ) as { statement: string };
    expect(stored.statement).toBe(sent?.consent.statement);
  });
});

describe("visitor data controls", () => {
  test("the privacy panel is collapsed until asked for and explains what is kept", async () => {
    stubBackend(workingBackend());
    await renderDemo();

    const toggle = requireInWidget("#privacyToggle");
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(requireInWidget("#privacyPanel").hidden).toBe(true);

    fireEvent.click(toggle);

    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(requireInWidget("#privacyPanel").hidden).toBe(false);
    expect(inWidget("#privacyPanel")?.textContent).toContain("conversation id");
    expect(shadow().activeElement).toBe(inWidget("#clearVisitorData"));
  });

  test("deleting local data clears every stored key and announces the reset", async () => {
    stubBackend(workingBackend());
    await renderDemo();
    const form = await openBookingForm();
    fillBooking(form);
    fireEvent.submit(form);
    await waitFor(() => expect(inWidget(".booking-form-card")).toBeNull());

    fireEvent.click(requireInWidget("#privacyToggle"));
    fireEvent.click(requireInWidget("#clearVisitorData"));

    for (const key of ["session-id", "consent", "contact"]) {
      expect(window.sessionStorage.getItem(`tenant-chat-${key}:clearview`)).toBeNull();
    }
    expect(inWidget("#assistantStatus")?.textContent).toContain("deleted from your browser");
    expect(inWidget("#messages")?.textContent).not.toContain("Your appointment is booked.");
  });

  test("a second booking in the same session does not ask for details already given", async () => {
    stubBackend(workingBackend());
    await renderDemo();
    const first = await openBookingForm();
    fillBooking(first);
    fireEvent.submit(first);
    await waitFor(() => expect(inWidget(".booking-form-card")).toBeNull());

    submitChat("Another HVAC visit please");
    await waitFor(() => expect(inWidget(".booking-form-card")).not.toBeNull());

    const second = requireInWidget(".booking-form-card");
    expect(second.querySelector<HTMLInputElement>("#booking-customerName")?.value).toBe("Sam Lee");
    expect(second.querySelector<HTMLInputElement>("#booking-address")?.value).toBe("42 Cedar Road");
    expect(second.querySelector<HTMLInputElement>("#booking-contact")?.value).toBe(
      "sam@example.test"
    );
    expect(second.querySelector<HTMLInputElement>("#bookingConsent")?.checked).toBe(false);
  });
});
