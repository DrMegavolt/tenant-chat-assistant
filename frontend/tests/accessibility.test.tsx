import { fireEvent, waitFor } from "@testing-library/react";
import axe from "axe-core";
import { describe, expect, test } from "vitest";

import { mountWidget } from "src/widget/mount";
import { TENANTS, jsonResponse, stubBackend, workingBackend } from "tests/support/backend";
import {
  allInWidget,
  inWidget,
  openBookingForm,
  renderDemo,
  requireInWidget,
  shadow,
  submitChat
} from "tests/support/widget";

/**
 * jsdom has no layout engine, so rules that need geometry or painted pixels
 * cannot reach a verdict here. Colour contrast is proven instead by
 * `contrast.test.ts`, and target size by the minimum dimensions in the widget
 * stylesheet.
 */
const LAYOUT_DEPENDENT_RULES = ["color-contrast", "target-size", "scrollable-region-focusable"];

async function violations(context: axe.ElementContext): Promise<string[]> {
  const results = await axe.run(context, {
    resultTypes: ["violations"],
    rules: Object.fromEntries(LAYOUT_DEPENDENT_RULES.map((rule) => [rule, { enabled: false }]))
  });
  return results.violations.map((violation) => `${violation.id}: ${violation.help}`);
}

describe("automated accessibility checks", () => {
  test("the mounted widget and its host page have no axe violations", async () => {
    stubBackend(workingBackend());
    await renderDemo();

    expect(await violations(document)).toEqual([]);
  });

  test("the booking form, including its consent control, has no axe violations", async () => {
    stubBackend(workingBackend());
    await renderDemo();
    await openBookingForm();

    expect(await violations(document)).toEqual([]);
  });

  test("the unavailable-backend state has no axe violations", async () => {
    stubBackend(() => jsonResponse({}, { ok: false, status: 503 }));
    const host = document.createElement("div");
    host.id = "tenant-chat";
    document.body.append(host);
    mountWidget(host);
    await waitFor(() => expect(inWidget(".widget-error")).not.toBeNull());

    expect(await violations(document)).toEqual([]);
  });
});

describe("keyboard and screen-reader behavior", () => {
  test("closing returns focus to the launcher and reopening returns it to the composer", async () => {
    stubBackend(workingBackend());
    await renderDemo();

    fireEvent.click(requireInWidget("#closeChat"));

    expect(requireInWidget("#chatWindow").hidden).toBe(true);
    expect(requireInWidget("#openChat").getAttribute("aria-expanded")).toBe("false");
    expect(shadow().activeElement).toBe(inWidget("#openChat"));

    fireEvent.click(requireInWidget("#openChat"));

    expect(requireInWidget("#openChat").getAttribute("aria-expanded")).toBe("true");
    expect(shadow().activeElement).toBe(inWidget("#chatInput"));
  });

  test("Escape closes the widget without the host page having to handle the key", async () => {
    stubBackend(workingBackend());
    await renderDemo();

    fireEvent.keyDown(requireInWidget("#chatInput"), { key: "Escape" });

    expect(requireInWidget("#chatWindow").hidden).toBe(true);
    expect(shadow().activeElement).toBe(inWidget("#openChat"));
  });

  test("the transcript is a polite log and marks itself busy while a reply is pending", async () => {
    let resolveChat: (value: unknown) => void = () => undefined;
    stubBackend((url) => {
      if (url.endsWith("/api/tenants")) return jsonResponse({ tenants: TENANTS });
      if (url.endsWith("/api/chat")) return new Promise((resolve) => (resolveChat = resolve));
      return null;
    });
    await renderDemo();

    const log = requireInWidget("#messages");
    expect(log.getAttribute("role")).toBe("log");
    expect(log.getAttribute("aria-live")).toBe("polite");

    submitChat("Hello");

    await waitFor(() => expect(log.getAttribute("aria-busy")).toBe("true"));
    expect(inWidget("#assistantStatus")?.textContent).toBe("Waiting for the assistant to reply.");

    resolveChat(await jsonResponse({ reply: "Hi there.", toolEvents: [] }));

    await waitFor(() => expect(log.getAttribute("aria-busy")).toBe("false"));
    expect(inWidget("#assistantStatus")?.textContent).toBe("");
  });

  test("every message names its speaker for a listener who cannot see alignment", async () => {
    stubBackend((url) => {
      if (url.endsWith("/api/tenants")) return jsonResponse({ tenants: TENANTS });
      if (url.endsWith("/api/chat")) return jsonResponse({ reply: "Hi there.", toolEvents: [] });
      return null;
    });
    await renderDemo();

    submitChat("Hello");
    await waitFor(() => expect(inWidget("#messages")?.textContent).toContain("Hi there."));

    const spoken = allInWidget(".message").map(
      (bubble) => bubble.querySelector(".visually-hidden")?.textContent
    );
    expect(spoken).toEqual(["Assistant: ", "You: ", "Assistant: "]);
  });

  test("the composer input has a real label rather than only a placeholder", async () => {
    stubBackend(workingBackend());
    await renderDemo();

    expect(inWidget("label[for='chatInput']")?.textContent).toBe("Message");
    expect(inWidget("#chatInput")?.getAttribute("aria-describedby")).toBe("privacyNote");
  });

  test("an unopened panel does not take focus away from the embedding page", async () => {
    stubBackend(workingBackend());
    const other = document.createElement("input");
    document.body.append(other);
    other.focus();

    await renderDemo({ open: false });

    expect(document.activeElement).toBe(other);
  });
});
