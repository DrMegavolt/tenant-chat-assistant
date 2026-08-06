import { fireEvent, waitFor } from "@testing-library/react";
import { describe, expect, test } from "vitest";

import {
  CREDENTIAL,
  SIMPLE_REPLY,
  jsonResponse,
  requestBodies,
  requestCredential,
  stubBackend,
  workingBackend
} from "tests/support/backend";
import { allInWidget, inWidget, renderDemo, shadow, submitChat } from "tests/support/widget";

describe("FEAT-008 visitor feedback", () => {
  test("a thumbs-down reveals the reason field and enqueues the review", async () => {
    const fetchMock = stubBackend((url, init) => {
      if (url.endsWith("/api/chat/feedback")) {
        const body = requestBodies(fetchMock, "/api/chat/feedback")[0] as Record<string, unknown>;
        return jsonResponse({
          turn_id: body.turn_id,
          rating: body.rating,
          reason: body.reason ?? null,
          created_at: "2026-08-06T12:00:00Z"
        });
      }
      if (url.endsWith("/api/chat")) return jsonResponse(SIMPLE_REPLY);
      return workingBackend()(url, init);
    });
    await renderDemo({ companyId: "clearview" });

    submitChat("What are your hours?");
    await waitFor(() =>
      expect(inWidget("#messages")?.textContent).toContain("I found one opening.")
    );

    const thumbsDown = allInWidget(".feedback-button").find(
      (button) => button.textContent === "Thumbs down"
    );
    expect(thumbsDown).toBeTruthy();
    fireEvent.click(thumbsDown!);

    const reason = inWidget<HTMLTextAreaElement>(".feedback-reason textarea");
    expect(reason).toBeTruthy();
    fireEvent.change(reason!, { target: { value: "The hours were wrong" } });
    fireEvent.click(shadow().querySelector<HTMLButtonElement>(".feedback-reason button")!);

    await waitFor(() =>
      expect(inWidget(".feedback-confirmed")?.textContent).toContain("the team will review")
    );

    const [body] = requestBodies(fetchMock, "/api/chat/feedback");
    expect(body).toEqual({
      turn_id: "turn-3",
      rating: "down",
      reason: "The hours were wrong"
    });
    expect(requestCredential(fetchMock.mock.calls.at(-1)?.[1])).toBe(CREDENTIAL);
  });

  test("a thumbs-up confirms without asking for a reason", async () => {
    const fetchMock = stubBackend((url, init) => {
      if (url.endsWith("/api/chat/feedback")) {
        const body = requestBodies(fetchMock, "/api/chat/feedback")[0] as Record<string, unknown>;
        return jsonResponse({
          turn_id: body.turn_id,
          rating: body.rating,
          reason: null,
          created_at: "2026-08-06T12:00:00Z"
        });
      }
      if (url.endsWith("/api/chat")) return jsonResponse(SIMPLE_REPLY);
      return workingBackend()(url, init);
    });
    await renderDemo({ companyId: "clearview" });

    submitChat("What are your hours?");
    await waitFor(() =>
      expect(inWidget("#messages")?.textContent).toContain("I found one opening.")
    );

    const thumbsUp = allInWidget(".feedback-button").find(
      (button) => button.textContent === "Thumbs up"
    );
    fireEvent.click(thumbsUp!);

    await waitFor(() =>
      expect(inWidget(".feedback-confirmed")?.textContent).toContain("glad this helped")
    );
    const [body] = requestBodies(fetchMock, "/api/chat/feedback");
    expect(body).toEqual({ turn_id: "turn-3", rating: "up", reason: undefined });
    expect(inWidget(".feedback-reason")).toBeNull();
  });

  test("no rating control is offered for a turn without a record id", async () => {
    stubBackend((url, init) => {
      if (url.endsWith("/api/chat")) {
        return jsonResponse({
          session_id: "session-1",
          turn_id: null,
          reply: "Noted.",
          pending: null,
          committed: [],
          provenance: { model_name: "scripted", graph_version: "v1", prompt_version: "v1" },
          credential: CREDENTIAL
        });
      }
      if (url.endsWith("/api/chat")) return jsonResponse(SIMPLE_REPLY);
      return workingBackend()(url, init);
    });
    await renderDemo({ companyId: "clearview" });

    submitChat("Hello");
    await waitFor(() => expect(inWidget("#messages")?.textContent).toContain("Noted."));
    expect(inWidget(".feedback-control")).toBeNull();
  });
});
