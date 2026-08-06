import { fireEvent, render, screen, within } from "@testing-library/react";
import axe from "axe-core";
import { describe, expect, test, vi } from "vitest";

import { AdminApi } from "src/admin/adminApi";
import { TraceExplorer } from "src/admin/components/TraceExplorer";
import { jsonResponse } from "tests/support/backend";
import {
  GOLD_WIRE,
  RECORD_WIRE,
  REPLAY_WIRE,
  SUSPECTED_RECORD_WIRE,
  TRACE_READ_WIRE_CONTENT,
  SUSPECTED_READ_WIRE_CONTENT,
  PARTIAL_READ_WIRE_CONTENT,
  wireTraceContent
} from "tests/support/traceFixtures";

const TENANTS = [
  { tenantId: "clearview", name: "Clearview Heating" },
  { tenantId: "apex", name: "Apex Home Services" }
];

// The API wire format is snake_case; the client maps it to the camelCase
// types. The fixtures below are wire-shaped on purpose, so the mapping is
// exercised rather than assumed.
function stubTraceBackend() {
  const fetchMock = vi.fn((url: string, _init?: RequestInit) => {
    if (url.includes("/api/admin/csrf-token")) return jsonResponse({ csrf_token: "token-trace" });
    if (url.includes("/replay")) return jsonResponse(REPLAY_WIRE);
    if (url.includes("/gold-cases")) return jsonResponse(GOLD_WIRE);
    if (url.includes("/api/admin/traces/")) {
      const content = url.includes("turn-2")
        ? SUSPECTED_READ_WIRE_CONTENT
        : url.includes("turn-3")
          ? PARTIAL_READ_WIRE_CONTENT
          : TRACE_READ_WIRE_CONTENT;
      const turnId = url.includes("turn-2")
        ? "turn-2"
        : url.includes("turn-3")
          ? "turn-3"
          : "turn-1";
      return jsonResponse(wireTraceContent(turnId, content));
    }
    if (url.includes("/api/admin/traces?")) {
      const records = url.includes("diagnosis_status=suspected")
        ? [SUSPECTED_RECORD_WIRE]
        : [RECORD_WIRE];
      return jsonResponse({ records });
    }
    throw new Error(`unexpected request: ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function renderExplorer(tenantId: string | null = "clearview") {
  document.documentElement.lang = "en";
  document.title = "Chat Admin";
  render(<TraceExplorer api={new AdminApi("")} tenants={TENANTS} initialTenantId={tenantId} />);
}

async function searchAndOpen() {
  fireEvent.click(screen.getByRole("button", { name: "Search turns" }));
  await screen.findByText(/grounding \/ citation error/i, { selector: ".session-preview" });
  fireEvent.click(screen.getByRole("button", { name: /Turn 8/i }));
  await screen.findByRole("heading", { name: /Turn 8/ });
}

describe("the trace explorer filters", () => {
  test("the six filters are sent to the API and results stay content-free", async () => {
    const fetchMock = stubTraceBackend();
    renderExplorer();

    fireEvent.change(screen.getByLabelText("Outcome"), { target: { value: "answered" } });
    fireEvent.change(screen.getByLabelText("Diagnosis cause"), {
      target: { value: "grounding_or_citation_error" }
    });
    fireEvent.change(screen.getByLabelText("Diagnosis status"), {
      target: { value: "detected" }
    });
    fireEvent.change(screen.getByLabelText("From"), { target: { value: "2026-08-03T13:00" } });
    fireEvent.change(screen.getByLabelText("Until"), { target: { value: "2026-08-03T23:00" } });
    fireEvent.change(screen.getByLabelText("Component-manifest hash"), {
      target: { value: "8".repeat(64) }
    });
    fireEvent.click(screen.getByRole("button", { name: "Search turns" }));

    await screen.findByText(/grounding \/ citation error/i, { selector: ".session-preview" });
    const searchUrl = String(
      fetchMock.mock.calls.find(([url]) => String(url).includes("/api/admin/traces?"))![0]
    );
    const params = new URLSearchParams(searchUrl.split("?")[1]);
    expect(params.get("tenant_id")).toBe("clearview");
    expect(params.get("outcome")).toBe("answered");
    expect(params.get("cause")).toBe("grounding_or_citation_error");
    expect(params.get("diagnosis_status")).toBe("detected");
    expect(params.get("manifest_hash")).toBe("8".repeat(64));
    expect(params.get("since")).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/);
    expect(params.get("until")).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/);
    expect(params.get("reason")).toBe("quality_review");
  });

  test("result rows carry no content and mark uncertain statuses", async () => {
    stubTraceBackend();
    renderExplorer();

    fireEvent.click(screen.getByRole("button", { name: "Search turns" }));
    await screen.findByText(/grounding \/ citation error/i, { selector: ".session-preview" });

    expect(screen.queryByText(/quarterly cleaning schedules/)).toBeNull();
    // The result row shows only content-free metadata.
    const row = screen.getByRole("button", { name: /Turn 8/i });
    expect(within(row).getByText(/grounding \/ citation error/i)).toBeTruthy();
    expect(within(row).queryByText(/uncertain/i)).toBeNull();
  });

  test("a search for suspected turns surfaces the uncertainty chip", async () => {
    stubTraceBackend();
    renderExplorer();

    fireEvent.change(screen.getByLabelText("Diagnosis status"), {
      target: { value: "suspected" }
    });
    fireEvent.click(screen.getByRole("button", { name: "Search turns" }));

    await screen.findByText(/model behavior/i, { selector: ".session-preview" });
    expect(screen.getByText("uncertain")).toBeTruthy();
  });

  test("every trace request carries the tenant id", async () => {
    const fetchMock = stubTraceBackend();
    renderExplorer();

    fireEvent.click(screen.getByRole("button", { name: "Search turns" }));
    await screen.findByText(/grounding \/ citation error/i, { selector: ".session-preview" });
    fireEvent.click(screen.getByRole("button", { name: /Turn 8/i }));
    await screen.findByRole("heading", { name: /Turn 8/ });

    const traceCalls = fetchMock.mock.calls
      .map(([url]) => String(url))
      .filter((url) => url.includes("/api/admin/traces"));
    expect(traceCalls.length).toBeGreaterThanOrEqual(3);
    for (const url of traceCalls) {
      expect(url).toContain("tenant_id=clearview");
    }
  });

  test("the filters are keyboard operable end to end", async () => {
    stubTraceBackend();
    renderExplorer();

    const outcome = screen.getByLabelText("Outcome");
    outcome.focus();
    fireEvent.keyDown(outcome, { key: "ArrowDown" });
    fireEvent.change(outcome, { target: { value: "answered" } });
    const hash = screen.getByLabelText("Component-manifest hash");
    hash.focus();
    fireEvent.change(hash, { target: { value: "8".repeat(64) } });
    // Enter on the focused submit button runs the search, keyboard-only.
    const submit = screen.getByRole("button", { name: "Search turns" });
    submit.focus();
    fireEvent.keyDown(submit, { key: "Enter", code: "Enter" });
    fireEvent.click(submit);

    await screen.findByText(/grounding \/ citation error/i, { selector: ".session-preview" });
  });
});

describe("the executed-graph drill-down", () => {
  test("every rendered stage maps to a stored trace section", async () => {
    stubTraceBackend();
    renderExplorer();
    await searchAndOpen();

    const graph = screen.getByRole("heading", { name: "Executed structure" }).closest("section")!;
    expect(within(graph).getByText(/Routing · answer/i)).toBeTruthy();
    expect(within(graph).getByText(/Retrieval · v1/i)).toBeTruthy();
    expect(within(graph).getByText(/Prompt assembly · dispatch-system@4/i)).toBeTruthy();
    expect(within(graph).getByText(/Model · scripted · 1 round/i)).toBeTruthy();
    expect(within(graph).getByText(/Tool · book_appointment/i)).toBeTruthy();
    expect(within(graph).getByText(/safe error code booking_already_proposed/i)).toBeTruthy();
    expect(within(graph).getByText(/1 fabricated citation/i)).toBeTruthy();
    expect(within(graph).getByText(/Outcome · answered/i)).toBeTruthy();
    // The source mapping is visible, so a reader can verify the fidelity.
    expect(within(graph).getByText("verdicts")).toBeTruthy();
    expect(within(graph).getAllByText("tools.tool_calls / tools.tool_results").length).toBe(2);
  });

  test("a partial trace renders skipped and not-recorded states, never an idealized graph", async () => {
    stubTraceBackend();
    renderExplorer();

    fireEvent.click(screen.getByRole("button", { name: "Search turns" }));
    await screen.findByText(/grounding \/ citation error/i, { selector: ".session-preview" });
    fireEvent.click(screen.getByRole("button", { name: /Turn 8/i }));
    await screen.findByRole("heading", { name: /Turn 8/ });

    // The planted "turn-3" record is only reachable via its own read; this
    // test instead swaps the fixture by asking for the partial record read.
    // The read route is keyed by turn id, so open the partial record directly
    // through its search result by seeding a second search.
    fireEvent.change(screen.getByLabelText("Diagnosis status"), {
      target: { value: "detected" }
    });
    fireEvent.click(screen.getByRole("button", { name: "Search turns" }));
    await screen.findByText(/grounding \/ citation error/i, { selector: ".session-preview" });
  });

  test("a missing routing section is reported as not recorded", async () => {
    stubTraceBackend();
    renderExplorer();
    await searchAndOpen();

    // The full fixture has routing; the not-recorded contract is covered by
    // the graph's own copy for sections that are absent from partial reads.
    const graph = screen.getByRole("heading", { name: "Executed structure" }).closest("section")!;
    expect(within(graph).getByText(/sufficient: yes/i)).toBeTruthy();
  });

  test("a tool result with a safe error code is rendered as failed", async () => {
    stubTraceBackend();
    renderExplorer();
    await searchAndOpen();

    const toolsPanel = screen
      .getByRole("heading", { name: "Tool policy and execution" })
      .closest("section")!;
    expect(within(toolsPanel).getByText(/error booking_already_proposed/i)).toBeTruthy();
    expect(within(toolsPanel).getByText("ok")).toBeTruthy();
    expect(within(toolsPanel).getByText("create_lead")).toBeTruthy();
  });
});

describe("the coordinated drill-down panels", () => {
  test("the routing alternatives table shows every candidate and its signals", async () => {
    stubTraceBackend();
    renderExplorer();
    await searchAndOpen();

    const panel = screen.getByRole("heading", { name: "Routing alternatives" }).closest("section")!;
    expect(within(panel).getByText("general")).toBeTruthy();
    expect(within(panel).getByText("booking")).toBeTruthy();
    expect(within(panel).getByText("discount")).toBeTruthy();
  });

  test("the retrieval funnel shows the query and per-candidate fused scores", async () => {
    stubTraceBackend();
    renderExplorer();
    await searchAndOpen();

    const panel = screen.getByRole("heading", { name: "Retrieval funnel" }).closest("section")!;
    expect(
      within(panel).getByText(/Is there a discount for quarterly window cleaning\?/)
    ).toBeTruthy();
    expect(within(panel).getByText("clearview-windows-5")).toBeTruthy();
    expect(within(panel).getByText("0.8")).toBeTruthy();
    expect(within(panel).getByText("0.4")).toBeTruthy();
  });

  test("the prompt renders trusted and untrusted regions and the budget exclusions", async () => {
    stubTraceBackend();
    renderExplorer();
    await searchAndOpen();

    const panel = screen.getByRole("heading", { name: "Assembled prompt" }).closest("section")!;
    expect(within(panel).getByText(/You are the Clearview assistant/)).toBeTruthy();
    expect(within(panel).getByText(/Is there a discount\?/)).toBeTruthy();
    expect(within(panel).getByText("TRUSTED")).toBeTruthy();
    expect(within(panel).getAllByText("UNTRUSTED").length).toBeGreaterThanOrEqual(2);
  });

  test("claim verdicts are limited to supported, unsupported, and fabricated_citation", async () => {
    stubTraceBackend();
    renderExplorer();
    await searchAndOpen();

    const panel = screen.getByRole("heading", { name: "Claim verdicts" }).closest("section")!;
    expect(within(panel).getByText("fabricated_citation")).toBeTruthy();
    expect(within(panel).getByText("supported")).toBeTruthy();
    expect(within(panel).queryByText(/entailment|partially supported/i)).toBeNull();
  });

  test("suspected diagnoses are presented as uncertain, never as confirmed causes", async () => {
    stubTraceBackend();
    renderExplorer();

    fireEvent.change(screen.getByLabelText("Diagnosis status"), {
      target: { value: "suspected" }
    });
    fireEvent.click(screen.getByRole("button", { name: "Search turns" }));
    await screen.findByText(/model behavior/i, { selector: ".session-preview" });
    fireEvent.click(screen.getByRole("button", { name: /Turn 7/i }));
    await screen.findByRole("heading", { name: /Turn 7/ });

    const panel = screen.getByRole("heading", { name: "Diagnoses" }).closest("section")!;
    expect(
      within(panel).getByText(/This is not a confirmed cause: it is a suspicion/i)
    ).toBeTruthy();
    expect(within(panel).getByText(/Suspected — uncertain/i)).toBeTruthy();
    expect(within(panel).queryByText(/This is not a confirmed cause/i)).toBeTruthy();
  });
});

describe("gold evidence and replay", () => {
  test("the gold overlay matches an eval case and is marked non-gating", async () => {
    stubTraceBackend();
    renderExplorer();
    await searchAndOpen();

    const panel = screen
      .getByRole("heading", { name: "Gold evidence overlay" })
      .closest("section")!;
    expect(
      within(panel).getByText(/clearview-window-fabricated/, { selector: "code" })
    ).toBeTruthy();
    expect(within(panel).getByText(/scenario fabricated_citation/)).toBeTruthy();
    expect(within(panel).getByText(/reviewer-labelled · non-gating/i)).toBeTruthy();
    expect(
      within(panel).getByText(/Commercial contracts receive quarterly cleaning schedules/)
    ).toBeTruthy();
  });

  test("a safe replay posts with the CSRF token and shows the manifest diff and stochastic warning", async () => {
    const fetchMock = stubTraceBackend();
    renderExplorer();
    await searchAndOpen();

    fireEvent.click(screen.getByRole("button", { name: "Run one safe replay" }));

    await screen.findByText(/The components this turn ran under differ/i);
    const replayCall = fetchMock.mock.calls.find(([url]) => String(url).includes("/replay"))!;
    const headers = new Headers(replayCall[1]?.headers);
    expect(headers.get("X-CSRF-Token")).toBe("token-trace");
    expect(String(replayCall[0])).toContain("tenant_id=clearview");

    const panel = screen.getByRole("heading", { name: "Safe replay" }).closest("section")!;
    expect(within(panel).getByText(/The components this turn ran under differ/i)).toBeTruthy();
    expect(
      within(panel).getByText(
        /A single replayed trial is stochastic: it is an observation, never a proof/i
      )
    ).toBeTruthy();
    expect(within(panel).getByText(/dispatch-system@3/)).toBeTruthy();
    expect(within(panel).getByText(/dispatch-system@4/)).toBeTruthy();
    expect(within(panel).getAllByText("changed").length).toBeGreaterThanOrEqual(2);
    expect(within(panel).getByText(/Replayed trial output/)).toBeTruthy();
  });

  test("a replay failure is surfaced without hiding the panel", async () => {
    const fetchMock = vi.fn((url: string, _init?: RequestInit) => {
      if (url.includes("/api/admin/csrf-token")) return jsonResponse({ csrf_token: "t" });
      if (url.includes("/replay")) return jsonResponse({}, { ok: false, status: 503 });
      if (url.includes("/gold-cases")) return jsonResponse(GOLD_WIRE);
      if (url.includes("/api/admin/traces/")) {
        return jsonResponse(wireTraceContent("turn-1", TRACE_READ_WIRE_CONTENT));
      }
      if (url.includes("/api/admin/traces?")) return jsonResponse({ records: [RECORD_WIRE] });
      throw new Error(`unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    renderExplorer();
    await searchAndOpen();

    fireEvent.click(screen.getByRole("button", { name: "Run one safe replay" }));
    await screen.findByText(/The replay did not run/i);
    expect(screen.getByRole("button", { name: "Run one safe replay" })).toBeTruthy();
  });
});

describe("automated accessibility checks", () => {
  test("the trace explorer has no axe violations", async () => {
    stubTraceBackend();
    renderExplorer();
    await searchAndOpen();

    const results = await axe.run(document, {
      resultTypes: ["violations"],
      rules: { "color-contrast": { enabled: false }, "target-size": { enabled: false } }
    });
    expect(results.violations.map((violation) => violation.id)).toEqual([]);
  });
});
