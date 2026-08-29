import { fireEvent, waitFor } from "@testing-library/react";
import { describe, expect, test } from "vitest";

import {
  CITED_REPLY,
  CREDENTIAL,
  TENANTS,
  citedBackend,
  jsonResponse,
  requestCredential,
  stubBackend
} from "tests/support/backend";
import {
  allInWidget,
  inWidget,
  renderDemo,
  requireInWidget,
  shadow,
  submitChat
} from "tests/support/widget";

async function askCitedQuestion(): Promise<void> {
  submitChat("Do you do tune-ups?");
  await waitFor(() => expect(inWidget("#messages")?.textContent).toContain("maintenance plans"));
}

/** The fetch call that resolved a citation to its source, or null. */
function sourceCall(fetchMock: ReturnType<typeof stubBackend>) {
  return fetchMock.mock.calls.find(([url]) => url.includes("/api/chat/sources/"));
}

describe("citation display", () => {
  test("renders one numbered citation per source, in the turn's own order", async () => {
    stubBackend(citedBackend());
    await renderDemo();
    await askCitedQuestion();

    const buttons = allInWidget(".citation-button");
    expect(buttons).toHaveLength(2);
    expect(buttons.map((node) => node.getAttribute("aria-label"))).toEqual([
      "Source 1: HVAC Maintenance Guide",
      "Source 2: Annual Tune-up Policy"
    ]);
    const badges = buttons.map((node) => node.querySelector(".citation-badge")?.textContent);
    expect(badges).toEqual(["1", "2"]);
    expect(buttons[0]?.textContent).toContain("HVAC Maintenance Guide");
  });

  test("an abstaining turn shows no citation controls at all", async () => {
    stubBackend(citedBackend([]));
    await renderDemo();
    await askCitedQuestion();

    expect(inWidget(".citation-list")).toBeNull();
    expect(allInWidget(".citation-button")).toEqual([]);
  });

  test("a reply that omits the citations field still renders without one", async () => {
    stubBackend((url, init) => {
      if (url.endsWith("/api/tenants")) return jsonResponse({ tenants: TENANTS });
      if (url.endsWith("/api/chat/session")) {
        return jsonResponse({ session: { session_id: "session-1", messages: [] } });
      }
      if (url.endsWith("/api/chat") && init?.method === "POST") {
        return jsonResponse({
          session_id: CITED_REPLY.session_id,
          turn_id: CITED_REPLY.turn_id,
          reply: CITED_REPLY.reply,
          pending: null,
          committed: [],
          provenance: CITED_REPLY.provenance,
          credential: CITED_REPLY.credential
        });
      }
      return null;
    });
    await renderDemo();
    await askCitedQuestion();

    expect(inWidget(".citation-list")).toBeNull();
  });

  test("an unvalidated evidence tag disappears while the validated citation still chips", async () => {
    // N-06, live-observed: the model cited a label that was not in the
    // evidence context; the mismatch was recorded and the answer delivered
    // with the raw marker still in the text. The visitor sees the answer and
    // the one validated source chip — never the bracketed tag.
    stubBackend((url, init) => {
      if (url.endsWith("/api/tenants")) return jsonResponse({ tenants: TENANTS });
      if (url.endsWith("/api/chat/session")) {
        return jsonResponse({ session: { session_id: "session-1", messages: [] } });
      }
      if (url.endsWith("/api/chat") && init?.method === "POST") {
        return jsonResponse({
          ...CITED_REPLY,
          citations: [CITED_REPLY.citations[0]],
          reply: "Our Saturday hours are 9:00 AM - 2:00 PM [evidence: business facts]."
        });
      }
      return null;
    });
    await renderDemo();

    submitChat("What are your weekend hours?");
    await waitFor(() => expect(inWidget("#messages")?.textContent).toContain("Saturday hours"));

    const messages = inWidget("#messages")?.textContent ?? "";
    expect(messages).toContain("9:00 AM - 2:00 PM.");
    expect(messages).not.toContain("[evidence");
    expect(allInWidget(".citation-button")).toHaveLength(1);
  });
});

describe("source viewer", () => {
  test("opens the authorized view with title, publication, section, version, and excerpt", async () => {
    const fetchMock = stubBackend(citedBackend());
    await renderDemo();
    await askCitedQuestion();

    fireEvent.click(requireInWidget(".citation-button"));

    await waitFor(() =>
      expect(inWidget(".source-viewer-excerpt")?.textContent).toContain("filter check")
    );
    const viewer = requireInWidget(".source-viewer");
    expect(viewer.getAttribute("role")).toBe("dialog");
    expect(viewer.getAttribute("aria-modal")).toBe("true");
    expect(inWidget("#sourceViewerTitle")?.textContent).toBe("HVAC Maintenance Guide");
    expect(viewer.textContent).toContain("Clearview Policies");
    expect(viewer.textContent).toContain("Maintenance");
    expect(viewer.textContent).toContain("Revision 2");
    expect(viewer.textContent).toContain("effective 2026-07-01");
    expect(viewer.textContent).toContain(
      "Annual maintenance includes a tune-up, a filter check, and a safety inspection."
    );

    expect(requestCredential(sourceCall(fetchMock)?.[1])).toBe(CREDENTIAL);
  });

  test("a second citation opens its own source view", async () => {
    stubBackend(citedBackend());
    await renderDemo();
    await askCitedQuestion();

    fireEvent.click(allInWidget(".citation-button")[1]!);

    await waitFor(() =>
      expect(inWidget("#sourceViewerTitle")?.textContent).toBe("Annual Tune-up Policy")
    );
  });

  test("a revoked source degrades to a bounded message with no detail", async () => {
    stubBackend(
      citedBackend(
        undefined,
        jsonResponse(
          { detail: "source src-hvac-guide is not answerable" },
          {
            ok: false,
            status: 404
          }
        )
      )
    );
    await renderDemo();
    await askCitedQuestion();

    fireEvent.click(requireInWidget(".citation-button"));

    await waitFor(() =>
      expect(inWidget(".source-viewer-status")?.getAttribute("role")).toBe("alert")
    );
    const message = requireInWidget(".source-viewer-status").textContent ?? "";
    expect(message).toBe("This source is no longer available to you.");
    expect(message).not.toContain("not answerable");
    expect(message).not.toContain("404");
    expect(message).not.toContain("src-hvac-guide");
    expect(inWidget(".source-viewer-excerpt")).toBeNull();
  });

  test("a network failure degrades exactly like a revoked source", async () => {
    stubBackend(citedBackend(undefined, () => Promise.reject(new Error("connection refused"))));
    await renderDemo();
    await askCitedQuestion();

    fireEvent.click(requireInWidget(".citation-button"));

    await waitFor(() =>
      expect(inWidget(".source-viewer-status")?.textContent).toBe(
        "This source is no longer available to you."
      )
    );
    expect(inWidget(".source-viewer-status")?.textContent).not.toContain("connection refused");
  });

  test("the source fetch never mints a session; it reuses the conversation's credential", async () => {
    const fetchMock = stubBackend(citedBackend());
    await renderDemo();
    await askCitedQuestion();

    fireEvent.click(requireInWidget(".citation-button"));
    await waitFor(() => expect(inWidget(".source-viewer-excerpt")).not.toBeNull());

    const posts = fetchMock.mock.calls.filter(
      ([url, init]) => url.endsWith("/api/chat/session") && init?.method === "POST"
    );
    // The one session was opened when the visitor typed their first message;
    // viewing a source must not mint another.
    expect(posts).toHaveLength(1);
  });
});

describe("source viewer keyboard navigation", () => {
  test("opening the viewer moves focus inside it", async () => {
    stubBackend(citedBackend());
    await renderDemo();
    await askCitedQuestion();

    fireEvent.click(requireInWidget(".citation-button"));
    await waitFor(() => expect(inWidget(".source-viewer")).not.toBeNull());

    const root = inWidget(".source-viewer")!.getRootNode() as ShadowRoot;
    expect(root.activeElement).toBe(inWidget(".source-viewer"));
  });

  test("Tab cannot leave the viewer while it is open", async () => {
    stubBackend(citedBackend());
    await renderDemo();
    await askCitedQuestion();

    fireEvent.click(requireInWidget(".citation-button"));
    await waitFor(() => expect(inWidget("#sourceViewerClose")).not.toBeNull());

    const close = requireInWidget("#sourceViewerClose");
    close.focus();
    fireEvent.keyDown(close, { key: "Tab" });
    expect(shadow().activeElement).toBe(close);
    fireEvent.keyDown(close, { key: "Tab", shiftKey: true });
    expect(shadow().activeElement).toBe(close);

    // The transcript and composer are made inert while the modal is open.
    expect(inWidget(".panel-main")?.hasAttribute("inert")).toBe(true);
  });

  test("Escape closes the viewer and returns focus to the citation that opened it", async () => {
    stubBackend(citedBackend());
    await renderDemo();
    await askCitedQuestion();

    const trigger = requireInWidget(".citation-button");
    fireEvent.click(trigger);
    await waitFor(() => expect(inWidget(".source-viewer")).not.toBeNull());

    fireEvent.keyDown(requireInWidget(".source-viewer"), { key: "Escape" });

    await waitFor(() => expect(inWidget(".source-viewer")).toBeNull());
    expect(inWidget(".panel-main")?.hasAttribute("inert")).toBe(false);
    expect(shadow().activeElement).toBe(trigger);
    expect(inWidget("#chatWindow")?.hasAttribute("hidden")).toBe(false);
  });

  test("the close button returns focus to the citation and leaves the widget open", async () => {
    stubBackend(citedBackend());
    await renderDemo();
    await askCitedQuestion();

    const trigger = requireInWidget(".citation-button");
    fireEvent.click(trigger);
    await waitFor(() => expect(inWidget("#sourceViewerClose")).not.toBeNull());

    fireEvent.click(requireInWidget("#sourceViewerClose"));

    await waitFor(() => expect(inWidget(".source-viewer")).toBeNull());
    expect(shadow().activeElement).toBe(trigger);
    expect(inWidget("#chatWindow")?.hasAttribute("hidden")).toBe(false);
  });
});

describe("user-appropriate action states", () => {
  test("a committed booking renders a plain confirmation with its reference", async () => {
    stubBackend((url, init) => {
      if (url.endsWith("/api/tenants")) return jsonResponse({ tenants: TENANTS });
      if (url.endsWith("/api/chat/session")) {
        return jsonResponse({ session: { session_id: "session-1", messages: [] } });
      }
      if (url.endsWith("/api/chat") && init?.method === "POST") {
        return jsonResponse({
          ...CITED_REPLY,
          committed: [{ action: "book_appointment", reference: "booking-7", replayed: false }],
          citations: []
        });
      }
      return null;
    });
    await renderDemo();

    submitChat("Book it");
    await waitFor(() => expect(inWidget(".action-note")).not.toBeNull());

    const note = requireInWidget(".action-note");
    expect(note.textContent).toContain("Appointment booked");
    expect(note.textContent).toContain("Reference booking-7");
    expect(inWidget("#messages")?.textContent).not.toContain("Tool:");
    expect(inWidget("#messages")?.textContent).not.toContain("book_appointment(");
  });

  test("a replayed action is told as already confirmed rather than booked again", async () => {
    stubBackend((url, init) => {
      if (url.endsWith("/api/tenants")) return jsonResponse({ tenants: TENANTS });
      if (url.endsWith("/api/chat/session")) {
        return jsonResponse({ session: { session_id: "session-1", messages: [] } });
      }
      if (url.endsWith("/api/chat") && init?.method === "POST") {
        return jsonResponse({
          ...CITED_REPLY,
          committed: [{ action: "book_appointment", reference: "booking-7", replayed: true }],
          citations: []
        });
      }
      return null;
    });
    await renderDemo();

    submitChat("Book it");
    await waitFor(() =>
      expect(inWidget(".action-note")?.textContent).toContain("already confirmed")
    );
  });

  test("an unrecognized action degrades to a plain statement, never its tool name", async () => {
    stubBackend((url, init) => {
      if (url.endsWith("/api/tenants")) return jsonResponse({ tenants: TENANTS });
      if (url.endsWith("/api/chat/session")) {
        return jsonResponse({ session: { session_id: "session-1", messages: [] } });
      }
      if (url.endsWith("/api/chat") && init?.method === "POST") {
        return jsonResponse({
          ...CITED_REPLY,
          committed: [{ action: "internal_escalation_v2", reference: "ref-1", replayed: false }],
          citations: []
        });
      }
      return null;
    });
    await renderDemo();

    submitChat("Escalate");
    await waitFor(() => expect(inWidget(".action-note")).not.toBeNull());

    const note = requireInWidget(".action-note");
    expect(note.textContent).toContain("Action completed");
    expect(note.textContent).not.toContain("internal_escalation_v2");
  });
});
