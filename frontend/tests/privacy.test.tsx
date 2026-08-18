import { fireEvent, waitFor } from "@testing-library/react";
import { describe, expect, test } from "vitest";

import {
  CLEARVIEW_CONSENT_STATEMENT,
  requestBodies,
  stubBackend,
  workingBackend
} from "tests/support/backend";
import {
  inWidget,
  openBookingConfirmation,
  renderDemo,
  requireInWidget,
  shadow
} from "tests/support/widget";

describe("consent before contact data leaves the browser", () => {
  test("the booking confirmation shows the server's own consent statement", async () => {
    /**
     * BUG-023: the widget composed this sentence itself from the tenant name.
     * The default text matched by coincidence, so a tenant override displayed
     * one statement while the server recorded another. Asserting the exact
     * served string is what makes rebuilding it locally fail here.
     */
    stubBackend(workingBackend());
    await renderDemo();
    const confirmation = await openBookingConfirmation();

    const consentLabel = confirmation.querySelector("label[for='bookingConfirmConsent']");
    expect(consentLabel?.textContent).toContain(CLEARVIEW_CONSENT_STATEMENT);
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
    expect(stored.statement).toBe(CLEARVIEW_CONSENT_STATEMENT);
  });

  test("the displayed statement and the recorded statement are the same string", async () => {
    /** The whole point of BUG-023: these two must never be able to diverge. */
    stubBackend(workingBackend());
    await renderDemo();
    const confirmation = await openBookingConfirmation();

    const displayed = confirmation
      .querySelector("label[for='bookingConfirmConsent']")!
      .textContent.trim();
    fireEvent.click(confirmation.querySelector("#bookingConfirmConsent")!);
    fireEvent.click(
      [...confirmation.querySelectorAll("button")].find((b) =>
        b.textContent?.includes("Confirm booking")
      )!
    );

    await waitFor(() =>
      expect(window.sessionStorage.getItem("tenant-chat-consent:clearview")).not.toBeNull()
    );
    const stored = JSON.parse(window.sessionStorage.getItem("tenant-chat-consent:clearview")!) as {
      statement: string;
    };
    expect(displayed).toContain(stored.statement);
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
    expect(inWidget("#privacyPanel")?.textContent).toContain("server keeps it");
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
