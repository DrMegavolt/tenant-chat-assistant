import { fireEvent } from "@testing-library/dom";
import axe from "axe-core";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { TENANTS, inWidget, jsonResponse, loadWidget, page, shadow } from "./support/page.js";

/**
 * jsdom has no layout engine, so rules that need geometry or painted pixels
 * cannot reach a verdict here. Color contrast is proven instead by
 * `contrast.test.js`, and target size by the minimum dimensions in the widget
 * stylesheet.
 */
const LAYOUT_DEPENDENT_RULES = ["color-contrast", "target-size", "scrollable-region-focusable"];

async function violations(context) {
  const results = await axe.run(context, {
    resultTypes: ["violations"],
    rules: Object.fromEntries(LAYOUT_DEPENDENT_RULES.map((rule) => [rule, { enabled: false }]))
  });
  return results.violations.map((violation) => `${violation.id}: ${violation.help}`);
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

function backend(url) {
  if (url === "/api/tenants") return jsonResponse({ tenants: TENANTS });
  if (url === "/api/chat") return jsonResponse(AVAILABILITY_REPLY);
  throw new Error(`unexpected request: ${url}`);
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

describe("automated accessibility checks", () => {
  test("the mounted widget and its host page have no axe violations", async () => {
    await loadWidget(backend);

    expect(await violations(document)).toEqual([]);
  });

  test("the booking form, including its consent control, has no axe violations", async () => {
    await loadWidget(backend);
    const picker = document.querySelector("#tenantSelect");
    picker.value = "clearview";
    fireEvent.change(picker);

    const input = inWidget("#chatInput");
    input.value = "Show HVAC availability";
    fireEvent.submit(inWidget("#composer"));
    await vi.waitFor(() => expect(inWidget(".booking-form-card")).not.toBeNull());

    expect(await violations(document)).toEqual([]);
  });

  test("the unavailable-backend state has no axe violations", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => jsonResponse({}, { ok: false, status: 503 }))
    );
    vi.spyOn(window, "setInterval").mockReturnValue(1);
    await import("../public/app.js");
    await vi.waitFor(() => expect(shadow().querySelector(".widget-error")).not.toBeNull());

    expect(await violations(document)).toEqual([]);
  });
});

describe("keyboard and screen-reader behavior", () => {
  test("closing returns focus to the launcher and reopening returns it to the composer", async () => {
    await loadWidget(backend);

    fireEvent.click(inWidget("#closeChat"));

    expect(inWidget("#chatWindow").hidden).toBe(true);
    expect(inWidget("#openChat").getAttribute("aria-expanded")).toBe("false");
    expect(shadow().activeElement).toBe(inWidget("#openChat"));

    fireEvent.click(inWidget("#openChat"));

    expect(inWidget("#openChat").getAttribute("aria-expanded")).toBe("true");
    expect(shadow().activeElement).toBe(inWidget("#chatInput"));
  });

  test("Escape closes the widget without the host page having to handle the key", async () => {
    await loadWidget(backend);

    fireEvent.keyDown(inWidget("#chatInput"), { key: "Escape" });

    expect(inWidget("#chatWindow").hidden).toBe(true);
    expect(shadow().activeElement).toBe(inWidget("#openChat"));
  });

  test("the transcript is a polite log and marks itself busy while a reply is pending", async () => {
    let resolveChat;
    await loadWidget((url) => {
      if (url === "/api/tenants") return jsonResponse({ tenants: TENANTS });
      if (url === "/api/chat") return new Promise((resolve) => (resolveChat = resolve));
      throw new Error(`unexpected request: ${url}`);
    });

    const log = inWidget("#messages");
    expect(log.getAttribute("role")).toBe("log");
    expect(log.getAttribute("aria-live")).toBe("polite");

    inWidget("#chatInput").value = "Hello";
    fireEvent.submit(inWidget("#composer"));

    await vi.waitFor(() => expect(log.getAttribute("aria-busy")).toBe("true"));
    expect(inWidget("#assistantStatus").textContent).toBe("Waiting for the assistant to reply.");

    resolveChat(jsonResponse({ reply: "Hi there.", toolEvents: [] }));

    await vi.waitFor(() => expect(log.getAttribute("aria-busy")).toBe("false"));
    expect(inWidget("#assistantStatus").textContent).toBe("");
  });

  test("every message names its speaker for a listener who cannot see alignment", async () => {
    await loadWidget((url) => {
      if (url === "/api/tenants") return jsonResponse({ tenants: TENANTS });
      if (url === "/api/chat") return jsonResponse({ reply: "Hi there.", toolEvents: [] });
      throw new Error(`unexpected request: ${url}`);
    });

    inWidget("#chatInput").value = "Hello";
    fireEvent.submit(inWidget("#composer"));
    await vi.waitFor(() => expect(inWidget("#messages").textContent).toContain("Hi there."));

    const spoken = [...inWidget("#messages").querySelectorAll(".message")].map(
      (bubble) => bubble.querySelector(".visually-hidden").textContent
    );
    expect(spoken).toEqual(["Assistant: ", "You: ", "Assistant: "]);
  });

  test("the composer input has a real label rather than only a placeholder", async () => {
    await loadWidget(backend);

    const label = shadow().querySelector("label[for='chatInput']");
    expect(label.textContent).toBe("Message");
    expect(inWidget("#chatInput").getAttribute("aria-describedby")).toBe("privacyNote");
  });
});
