import { fireEvent, waitFor } from "@testing-library/react";
import { describe, expect, test } from "vitest";

import { requestBodies, stubBackend, workingBackend } from "tests/support/backend";
import {
  inWidget,
  openBookingConfirmation,
  renderDemo,
  requireInWidget,
  shadow
} from "tests/support/widget";

describe("consent before contact data leaves the browser", () => {
  test("the booking confirmation states what is stored and by whom before approving", async () => {
    stubBackend(workingBackend());
    await renderDemo();
    const confirmation = await openBookingConfirmation();

    const consentLabel = confirmation.querySelector("label[for='bookingConfirmConsent']");
    expect(consentLabel?.textContent).toContain("Clearview Heating");
    expect(consentLabel?.textContent).toContain("name, address, and contact details");
    expect(confirmation.querySelector<HTMLInputElement>("#bookingConfirmConsent")?.checked).toBe(
      false
    );
  });

  test("approving without consent sends nothing and moves focus to the checkbox", async () => {
    const fetchMock = stubBackend(workingBackend());
    await renderDemo();
    const confirmation = await openBookingConfirmation();

    const approve = [...confirmation.querySelectorAll("button")].find((b) =>
      b.textContent?.includes("Confirm booking")
    )!;
    fireEvent.click(approve);

    expect(requestBodies(fetchMock, "/api/chat/confirmation")).toHaveLength(0);
    expect(confirmation.querySelector("#bookingConfirmError")?.textContent).toBe(
      "Please agree to the data notice before confirming."
    );
    const checkbox = confirmation.querySelector("#bookingConfirmConsent");
    expect(checkbox?.getAttribute("aria-invalid")).toBe("true");
    expect(shadow().activeElement).toBe(checkbox);
  });

  test("granting consent records it locally and commits the booking", async () => {
    const fetchMock = stubBackend(workingBackend());
    await renderDemo();
    const confirmation = await openBookingConfirmation();

    fireEvent.click(confirmation.querySelector("#bookingConfirmConsent")!);
    const approve = [...confirmation.querySelectorAll("button")].find((b) =>
      b.textContent?.includes("Confirm booking")
    )!;
    fireEvent.click(approve);

    await waitFor(() => expect(requestBodies(fetchMock, "/api/chat/confirmation")).toHaveLength(1));
    expect(requestBodies(fetchMock, "/api/chat/confirmation")[0]).toMatchObject({
      decision: "approved"
    });
    const stored = JSON.parse(
      window.sessionStorage.getItem("tenant-chat-consent:clearview") ?? "null"
    ) as { statement: string };
    expect(stored.statement).toContain("Clearview Heating");
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
    const confirmation = await openBookingConfirmation();
    fireEvent.click(confirmation.querySelector("#bookingConfirmConsent")!);
    const approve = [...confirmation.querySelectorAll("button")].find((b) =>
      b.textContent?.includes("Confirm booking")
    )!;
    fireEvent.click(approve);
    await waitFor(() => expect(inWidget(".booking-confirmation-card")).toBeNull());

    fireEvent.click(requireInWidget("#privacyToggle"));
    fireEvent.click(requireInWidget("#clearVisitorData"));

    for (const key of ["session-id", "consent", "contact"]) {
      expect(window.sessionStorage.getItem(`tenant-chat-${key}:clearview`)).toBeNull();
    }
    expect(inWidget("#assistantStatus")?.textContent).toContain("deleted from your browser");
    expect(inWidget("#messages")?.textContent).not.toContain("Your appointment is booked.");
  });

  test("a later booking in the same session reuses the recorded consent", async () => {
    stubBackend(workingBackend());
    await renderDemo();
    const first = await openBookingConfirmation();
    fireEvent.click(first.querySelector("#bookingConfirmConsent")!);
    const approve = [...first.querySelectorAll("button")].find((b) =>
      b.textContent?.includes("Confirm booking")
    )!;
    fireEvent.click(approve);
    await waitFor(() => expect(inWidget(".booking-confirmation-card")).toBeNull());

    // Consent persisted by the earlier approval.
    expect(
      JSON.parse(window.sessionStorage.getItem("tenant-chat-consent:clearview") ?? "null")
    ).not.toBeNull();
  });
});
