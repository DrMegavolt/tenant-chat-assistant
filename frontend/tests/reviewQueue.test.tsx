import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";

import { AdminApi } from "src/admin/adminApi";
import { ReviewQueue } from "src/admin/components/ReviewQueue";
import { jsonResponse } from "tests/support/backend";

const TENANTS = [
  { tenantId: "clearview", name: "Clearview Heating" },
  { tenantId: "apex", name: "Apex Home Services" }
];

const REVIEW_WIRE = {
  review_id: "review-1",
  turn_id: "turn-1",
  session_id: "session-1",
  recorded_at: "2026-08-06T12:00:00Z",
  outcome: "answered",
  source: "user_feedback",
  status: "open",
  priority: 32,
  recurrence: 1,
  manifest_hash: "a".repeat(64),
  committed_actions: false,
  novel_manifest: true,
  case_id: null,
  verdict: null,
  diagnosis_causes: ["provider_failure"],
  diagnosis_statuses: ["confirmed"],
  closing_eval_run_id: null,
  closing_eval_case_id: null,
  created_at: "2026-08-06T12:00:00Z",
  turn_index: 4
};

const DETAIL_WIRE = {
  review: REVIEW_WIRE,
  feedback: {
    turn_id: "turn-1",
    rating: "down",
    reason: "The price was wrong for my ZIP",
    created_at: "2026-08-06T12:00:00Z"
  },
  reviewer_subject: null,
  reviewed_at: null,
  verdict_note: null,
  corrected_answer: null,
  proposed_fix: null,
  closing_eval_passed_at: null,
  diagnoses: []
};

const TRACE_WIRE = {
  turn_id: "turn-1",
  tenant_id: "clearview",
  session_id: "session-1",
  trace_id: "trace-1",
  recorded_at: "2026-08-06T12:00:00Z",
  content: {
    schema_version: "1",
    turn_index: 4,
    routing: null,
    retrieval: { query: "What are your hours?", sufficient: true },
    prompt: null,
    model: { name: "scripted", usage: {} },
    output: { answer: "We are open daily from 7 AM to 7 PM.", raw: "raw", claims: [] },
    verdicts: { citations: [], citation_invalid: [], refused_tools: [], claims_invalid: [] },
    tools: { tool_calls: [], tool_results: [], committed: [] },
    outcome: { status: "answered", rounds: 1, failure: null },
    diagnoses: [
      {
        cause: "provider_failure",
        stage: "model",
        role: "primary",
        status: "confirmed",
        confidence: "high",
        evidence: [],
        detector_version: "diagnosis@1"
      }
    ]
  },
  projections: []
};

const REVIEWED_DETAIL_WIRE = {
  ...DETAIL_WIRE,
  review: {
    ...REVIEW_WIRE,
    status: "awaiting_fix",
    verdict: "confirmed",
    case_id: null
  },
  reviewer_subject: "operator-7",
  reviewed_at: "2026-08-06T13:00:00Z",
  diagnoses: [
    {
      diagnosis_id: "diag-1",
      review_id: "review-1",
      relationship: "confirms",
      automatic_index: 0,
      cause: "provider_failure",
      stage: "model",
      role: "primary",
      status: "confirmed",
      confidence: "high",
      evidence: [],
      note: null,
      created_at: "2026-08-06T13:00:00Z"
    }
  ]
};

function stubReviewBackend() {
  let reviewed = false;
  const fetchMock = vi.fn((url: string, init?: RequestInit) => {
    if (url.includes("/api/admin/csrf-token")) return jsonResponse({ csrf_token: "token-review" });
    if (url.includes("/api/admin/traces/gold-cases")) return jsonResponse({ cases: [] });
    if (url.includes("/api/admin/traces/")) return jsonResponse(TRACE_WIRE);
    if (url.includes("/api/admin/reviews/") && init?.method === "POST") {
      const apiPath = url.split("?")[0] ?? "";
      if (apiPath.endsWith("/promote")) {
        return jsonResponse({ review_id: "review-1", case_id: "review-1", status: "awaiting_fix" });
      }
      if (apiPath.endsWith("/review")) {
        reviewed = true;
        return jsonResponse({
          review_id: "review-1",
          status: "awaiting_fix",
          verdict: "confirmed"
        });
      }
      return jsonResponse({ review_id: "review-1", status: "in_review" });
    }
    if (url.includes("/api/admin/reviews/")) {
      return jsonResponse(reviewed ? REVIEWED_DETAIL_WIRE : DETAIL_WIRE);
    }
    if (url.includes("/api/admin/reviews?")) {
      return jsonResponse({ reviews: [REVIEW_WIRE] });
    }
    throw new Error(`unexpected request: ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function renderQueue(tenantId: string | null = "clearview") {
  document.documentElement.lang = "en";
  document.title = "Chat Admin";
  render(<ReviewQueue api={new AdminApi("")} tenants={TENANTS} initialTenantId={tenantId} />);
}

describe("the FEAT-008 review queue", () => {
  test("lists content-free entries and opens the review with its feedback reason", async () => {
    const fetchMock = stubReviewBackend();
    renderQueue();

    await screen.findByText(/priority 32/i);
    expect(screen.getByText(/visitor feedback/i)).toBeTruthy();
    // The list is content-free: the reason stays behind the detail surface.
    expect(screen.queryByText(/price was wrong/i)).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /priority 32/i }));
    await screen.findByRole("article", { name: /review-1/i });
    expect(await screen.findByText(/“The price was wrong for my ZIP”/)).toBeTruthy();

    const listUrl = String(
      fetchMock.mock.calls.map(([url]) => url).find((url) => url.includes("/api/admin/reviews?"))
    );
    expect(listUrl).toContain("tenant_id=clearview");
    expect(listUrl).toContain("reason=quality_review");
  });

  test("filters by status and carries the CSRF token on mutations", async () => {
    const fetchMock = stubReviewBackend();
    renderQueue();
    await screen.findByText(/priority 32/i);

    fireEvent.change(screen.getByLabelText("Review status"), { target: { value: "open" } });
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));

    await screen.findByText(/priority 32/i);
    const filtered = String(
      fetchMock.mock.calls.find(([url]) => url.includes("review_status=open"))?.[0]
    );
    expect(filtered).toContain("review_status=open");

    fireEvent.click(screen.getByRole("button", { name: /priority 32/i }));
    await screen.findByText(/“The price was wrong for my ZIP”/);
    fireEvent.click(screen.getByRole("button", { name: "Submit review" }));

    await screen.findByRole("heading", { name: /Review decision/i });
    const mutation = fetchMock.mock.calls.find(
      ([url, init]) => (url.split("?")[0] ?? "").endsWith("/review") && init?.method === "POST"
    );
    expect(mutation).toBeTruthy();
    const headers = mutation?.[1]?.headers as Record<string, string>;
    expect(headers["X-CSRF-Token"]).toBe("token-review");
  });

  test("the review form requires deciding every automatic diagnosis", async () => {
    stubReviewBackend();
    renderQueue();
    await screen.findByText(/priority 32/i);

    fireEvent.click(screen.getByRole("button", { name: /priority 32/i }));
    await screen.findByRole("article", { name: /review-1/i });
    await screen.findAllByText(/provider failure/i);

    // The decision for the automatic record is preselected "confirms"; the
    // form is submittable and the server accepts the coverage.
    const submit = screen.getByRole("button", { name: "Submit review" });
    expect((submit as HTMLButtonElement).disabled).toBe(false);

    // Amending reveals the replacement fields.
    fireEvent.change(screen.getByLabelText("Decision for provider_failure"), {
      target: { value: "amends" }
    });
    await screen.findByLabelText("Amended cause");
  });

  test("promotion is offered for a reviewed awaiting-fix case", async () => {
    let reviewed = false;
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      if (url.includes("/api/admin/csrf-token"))
        return jsonResponse({ csrf_token: "token-review" });
      if (url.includes("/api/admin/traces/gold-cases")) return jsonResponse({ cases: [] });
      if (url.includes("/api/admin/traces/")) return jsonResponse(TRACE_WIRE);
      if (url.includes("/api/admin/reviews/") && init?.method === "POST") {
        const apiPath = url.split("?")[0] ?? "";
        if (apiPath.endsWith("/promote")) {
          return jsonResponse({
            review_id: "review-1",
            case_id: "review-1",
            status: "awaiting_fix"
          });
        }
        return jsonResponse({ review_id: "review-1", status: "in_review" });
      }
      if (url.includes("/api/admin/reviews/")) {
        return jsonResponse(reviewed ? REVIEWED_DETAIL_WIRE : DETAIL_WIRE);
      }
      if (url.includes("/api/admin/reviews?")) return jsonResponse({ reviews: [REVIEW_WIRE] });
      throw new Error(`unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    renderQueue();
    await screen.findByText(/priority 32/i);

    // Submit the review first: the form moves the case to awaiting_fix.
    fireEvent.click(screen.getByRole("button", { name: /priority 32/i }));
    await screen.findByRole("article", { name: /review-1/i });
    fireEvent.click(screen.getByRole("button", { name: "Submit review" }));
    reviewed = true;

    await screen.findByRole("heading", { name: /Review decision/i });
    const promote = screen.getByRole("button", { name: "Promote to evaluation case" });
    fireEvent.click(promote);
    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some(
          ([url, init]) => (url.split("?")[0] ?? "").endsWith("/promote") && init?.method === "POST"
        )
      ).toBe(true);
    });
  });
});
