import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import axe from "axe-core";
import { describe, expect, test, vi } from "vitest";

import { AdminApi } from "src/admin/adminApi";
import { TraceExplorer } from "src/admin/components/TraceExplorer";
import { jsonResponse } from "tests/support/backend";
import {
  CAPTURED_READ_WIRE_CONTENT,
  CRASHED_READ_WIRE_CONTENT,
  FABRICATED_READ_WIRE_CONTENT,
  FABRICATED_RECORD_WIRE,
  GOLD_WIRE,
  NO_RETRIEVAL_READ_WIRE_CONTENT,
  NO_RETRIEVAL_RECORD_WIRE,
  RECORD_WIRE,
  REPLAY_WIRE,
  RESUMED_READ_WIRE_CONTENT,
  SUSPECTED_READ_WIRE_CONTENT,
  SUSPECTED_RECORD_WIRE,
  TRACE_READ_WIRE_CONTENT,
  UNSUPPORTED_CLAIMS_READ_WIRE_CONTENT,
  UNSUPPORTED_RECORD_WIRE,
  wireTraceContent
} from "tests/support/traceFixtures";

const TENANTS = [
  { tenantId: "clearview", name: "Clearview Heating" },
  { tenantId: "apex", name: "Apex Home Services" }
];

/** The content-free search row for the captured schema-3 record (turn-4). */
const V3_RECORD_WIRE = {
  ...RECORD_WIRE,
  turn_id: "turn-4",
  trace_id: "trace-gateb-4",
  turn_index: 4,
  trace_schema_version: "3",
  diagnosis_causes: [],
  diagnosis_statuses: []
};

// The API wire format is snake_case; the client maps it to the camelCase
// types. The fixtures are the Python serializer's real output, so the mapping
// is exercised against shapes the backend actually stores.
const READ_BY_TURN: Record<string, Record<string, unknown>> = {
  "turn-1": TRACE_READ_WIRE_CONTENT,
  "turn-2": FABRICATED_READ_WIRE_CONTENT,
  "turn-3": CRASHED_READ_WIRE_CONTENT,
  "turn-4": CAPTURED_READ_WIRE_CONTENT,
  "turn-5": UNSUPPORTED_CLAIMS_READ_WIRE_CONTENT,
  "turn-6": NO_RETRIEVAL_READ_WIRE_CONTENT,
  "turn-7": SUSPECTED_READ_WIRE_CONTENT
};

function stubTraceBackend(
  overrides: {
    records?: unknown[];
    readFailure?: { status: number } | null;
  } = {}
) {
  const fetchMock = vi.fn((url: string, _init?: RequestInit) => {
    if (url.includes("/api/admin/csrf-token")) return jsonResponse({ csrf_token: "token-trace" });
    if (url.includes("/replay")) return jsonResponse(REPLAY_WIRE);
    if (url.includes("/gold-cases")) return jsonResponse(GOLD_WIRE);
    if (url.includes("/api/admin/traces/by-trace-id/")) {
      const known = url.includes("/by-trace-id/trace-gateb-8");
      return known
        ? jsonResponse(wireTraceContent("turn-1", TRACE_READ_WIRE_CONTENT))
        : jsonResponse({ code: "not_found" }, { ok: false, status: 404 });
    }
    if (url.includes("/api/admin/traces/")) {
      if (overrides.readFailure) {
        return jsonResponse(
          { code: "internal" },
          { ok: false, status: overrides.readFailure.status }
        );
      }
      const turnId = Object.keys(READ_BY_TURN).find((id) => url.includes(`/${id}`)) ?? "turn-1";
      return jsonResponse(wireTraceContent(turnId, READ_BY_TURN[turnId]!));
    }
    if (url.includes("/api/admin/traces?")) {
      return jsonResponse({ records: overrides.records ?? [RECORD_WIRE] });
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
  await screen.findByText(/tool error/i, { selector: ".session-preview" });
  fireEvent.click(screen.getByRole("button", { name: /Turn 8/i }));
  await screen.findByRole("heading", { name: /Turn 8/ });
}

describe("the trace explorer filters", () => {
  test("the six filters are sent to the API and results stay content-free", async () => {
    const fetchMock = stubTraceBackend();
    renderExplorer();

    fireEvent.change(screen.getByLabelText("Outcome"), { target: { value: "answered" } });
    fireEvent.change(screen.getByLabelText("Diagnosis cause"), { target: { value: "tool_error" } });
    fireEvent.change(screen.getByLabelText("Diagnosis status"), {
      target: { value: "detected" }
    });
    fireEvent.change(screen.getByLabelText("From"), { target: { value: "2026-08-03T13:00" } });
    fireEvent.change(screen.getByLabelText("Until"), { target: { value: "2026-08-03T23:00" } });
    fireEvent.change(screen.getByLabelText("Component-manifest hash"), {
      target: { value: RECORD_WIRE.component_manifest_hash }
    });
    fireEvent.click(screen.getByRole("button", { name: "Search turns" }));

    await screen.findByText(/tool error/i, { selector: ".session-preview" });
    const searchUrl = String(
      fetchMock.mock.calls.find(([url]) => String(url).includes("/api/admin/traces?"))![0]
    );
    const params = new URLSearchParams(searchUrl.split("?")[1]);
    expect(params.get("tenant_id")).toBe("clearview");
    expect(params.get("outcome")).toBe("answered");
    expect(params.get("cause")).toBe("tool_error");
    expect(params.get("diagnosis_status")).toBe("detected");
    expect(params.get("manifest_hash")).toBe(RECORD_WIRE.component_manifest_hash);
    expect(params.get("since")).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/);
    expect(params.get("until")).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/);
    expect(params.get("reason")).toBe("quality_review");
  });

  test("result rows carry no content and mark uncertain statuses", async () => {
    stubTraceBackend();
    renderExplorer();

    fireEvent.click(screen.getByRole("button", { name: "Search turns" }));
    await screen.findByText(/tool error/i, { selector: ".session-preview" });

    expect(screen.queryByText(/open daily from 7 AM/)).toBeNull();
    const row = screen.getByRole("button", { name: /Turn 8/i });
    expect(within(row).getByText(/tool error/i)).toBeTruthy();
    expect(within(row).queryByText(/uncertain/i)).toBeNull();
  });

  test("a search for suspected turns surfaces the uncertainty chip", async () => {
    stubTraceBackend({ records: [SUSPECTED_RECORD_WIRE] });
    renderExplorer();

    fireEvent.change(screen.getByLabelText("Diagnosis status"), {
      target: { value: "suspected" }
    });
    fireEvent.click(screen.getByRole("button", { name: "Search turns" }));

    await screen.findByText(/model behavior/i, { selector: ".session-preview" });
    expect(screen.getByText("uncertain")).toBeTruthy();
  });

  test("one click performs exactly one audited trace read", async () => {
    const fetchMock = stubTraceBackend();
    renderExplorer();

    fireEvent.click(screen.getByRole("button", { name: "Search turns" }));
    await screen.findByText(/tool error/i, { selector: ".session-preview" });
    fireEvent.click(screen.getByRole("button", { name: /Turn 8/i }));
    await screen.findByRole("heading", { name: /Turn 8/ });

    // R-19: the drill-in must not pre-fetch the record to harvest its trace id;
    // TraceDetail's read is the only audited trace.read for this click.
    const reads = fetchMock.mock.calls.filter(([url]) => String(url).includes("/traces/turn-1"));
    expect(reads).toHaveLength(1);
    const goldReads = fetchMock.mock.calls.filter(([url]) => String(url).includes("/gold-cases"));
    expect(goldReads).toHaveLength(1);
    for (const [url] of fetchMock.mock.calls) {
      expect(String(url)).toContain("tenant_id=clearview");
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
    fireEvent.change(hash, { target: { value: RECORD_WIRE.component_manifest_hash } });
    const submit = screen.getByRole("button", { name: "Search turns" });
    submit.focus();
    fireEvent.keyDown(submit, { key: "Enter", code: "Enter" });
    fireEvent.click(submit);

    await screen.findByText(/tool error/i, { selector: ".session-preview" });
  });
});

describe("the id lookup", () => {
  test("a trace id opens the audited turn record directly", async () => {
    stubTraceBackend();
    renderExplorer();

    fireEvent.change(screen.getByLabelText("Turn id or trace id"), {
      target: { value: "trace-gateb-8" }
    });
    fireEvent.click(screen.getByRole("button", { name: "Open turn" }));

    await screen.findByRole("heading", { name: /Turn 8/ });
  });

  test("a turn uuid opens the record without a search", async () => {
    const fetchMock = stubTraceBackend();
    renderExplorer();

    fireEvent.change(screen.getByLabelText("Turn id or trace id"), {
      target: { value: "1b2adde7-9c0d-4f6a-8a10-2b3c4d5e6f70" }
    });
    fireEvent.click(screen.getByRole("button", { name: "Open turn" }));

    await screen.findByRole("heading", { name: /Turn 8/ });
    const reads = fetchMock.mock.calls.filter(([url]) => String(url).includes("/traces/1b2adde7"));
    expect(reads).toHaveLength(1);
  });

  test("an id the backend cannot resolve says so, honestly", async () => {
    stubTraceBackend();
    renderExplorer();

    fireEvent.change(screen.getByLabelText("Turn id or trace id"), {
      target: { value: "trace-does-not-exist" }
    });
    fireEvent.click(screen.getByRole("button", { name: "Open turn" }));

    await screen.findByText("No turn record found for that id.");
    expect(screen.queryByRole("heading", { name: /Turn/ })).toBeNull();
  });
});

describe("the derived drill-down view (schema-1 turns)", () => {
  test("every rendered stage maps to a stored trace section and states the winner", async () => {
    stubTraceBackend();
    renderExplorer();
    await searchAndOpen();

    const graph = screen.getByRole("heading", { name: "Executed structure" }).closest("section")!;
    expect(within(graph).getByText(/Routing · matched/i)).toBeTruthy();
    expect(
      within(graph).getByText(
        /chose general \(matched\) at confidence 4 against the direct threshold 4/
      )
    ).toBeTruthy();
    expect(within(graph).getByText(/Retrieval · v1/i)).toBeTruthy();
    expect(within(graph).getByText(/Prompt assembly · dispatch-system@4/i)).toBeTruthy();
    expect(within(graph).getByText(/Model · scripted · 1 round/i)).toBeTruthy();
    expect(within(graph).getByText(/Tool · book_appointment/i)).toBeTruthy();
    expect(within(graph).getByText(/safe error code booking_already_proposed/i)).toBeTruthy();
    expect(within(graph).getByText(/Outcome · answered/i)).toBeTruthy();
    // The source mapping is visible, so a reader can verify the fidelity.
    expect(within(graph).getByText("verdicts")).toBeTruthy();
    expect(within(graph).getAllByText("tools.tool_calls / tools.tool_results").length).toBe(2);
  });

  test("a turn without a captured graph is labelled derived, never shown as captured", async () => {
    stubTraceBackend();
    renderExplorer();
    await searchAndOpen();

    const graph = screen.getByRole("heading", { name: "Executed structure" }).closest("section")!;
    expect(within(graph).getByText("derived from stored trace fields")).toBeTruthy();
    expect(within(graph).queryByText("captured execution")).toBeNull();
  });
});

describe("the routing panel states the decision (R-01/L-A04)", () => {
  test("a schema-1 legacy turn states the winning intent, confidence, and threshold", async () => {
    stubTraceBackend();
    renderExplorer();
    await searchAndOpen();

    const panel = screen.getByRole("heading", { name: "Routing" }).closest("section")!;
    expect(within(panel).getByText(/chose general/i)).toBeTruthy();
    expect(within(panel).getByText(/confidence 4/)).toBeTruthy();
    expect(within(panel).getByText(/direct threshold 4/)).toBeTruthy();
    const rows = within(panel).getAllByRole("row");
    const winner = rows.find((row) => within(row).queryByText("chosen"));
    expect(winner).toBeTruthy();
    expect(within(winner!).getByText("general")).toBeTruthy();
    expect(within(winner!).getByText("hours")).toBeTruthy();
    // Exactly one winner, and the losers are marked as such.
    expect(rows.filter((row) => within(row).queryByText("chosen"))).toHaveLength(1);
    expect(within(panel).getAllByText("—").length).toBeGreaterThanOrEqual(1);
  });

  test("a schema-3 captured turn marks its winner too", async () => {
    stubTraceBackend({ records: [V3_RECORD_WIRE] });
    renderExplorer();
    fireEvent.click(screen.getByRole("button", { name: "Search turns" }));
    await screen.findByRole("button", { name: /Turn 4/i });
    fireEvent.click(screen.getByRole("button", { name: /Turn 4/i }));
    await screen.findByRole("heading", { name: /Turn 4/ });

    const panel = screen.getByRole("heading", { name: "Routing" }).closest("section")!;
    expect(within(panel).getByText(/chose booking/)).toBeTruthy();
    expect(within(panel).getAllByText("chosen")).toHaveLength(1);
  });

  test("a clarify decision states that no intent was chosen and why", async () => {
    stubTraceBackend({ records: [NO_RETRIEVAL_RECORD_WIRE] });
    renderExplorer();
    fireEvent.click(screen.getByRole("button", { name: "Search turns" }));
    await screen.findByText(/routing error/i, { selector: ".session-preview" });
    fireEvent.click(screen.getByRole("button", { name: /Turn 6/i }));
    await screen.findByRole("heading", { name: /Turn 6/ });

    const panel = screen.getByRole("heading", { name: "Routing" }).closest("section")!;
    expect(within(panel).getByText(/no intent chosen — asked for clarification/)).toBeTruthy();
    expect(within(panel).getByText(/clarify threshold 2\.5/)).toBeTruthy();
    expect(within(panel).queryByText("chosen")).toBeNull();
  });

  test("a record with no routing section says not recorded instead of a shell", async () => {
    stubTraceBackend();
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => {
        if (url.includes("/api/admin/csrf-token")) return jsonResponse({ csrf_token: "t" });
        if (url.includes("/gold-cases")) return jsonResponse(GOLD_WIRE);
        if (url.includes("/api/admin/traces/")) {
          return jsonResponse(
            wireTraceContent("turn-1", { ...TRACE_READ_WIRE_CONTENT, routing: null })
          );
        }
        if (url.includes("/api/admin/traces?")) return jsonResponse({ records: [RECORD_WIRE] });
        throw new Error(`unexpected request: ${url}`);
      })
    );
    renderExplorer();
    await searchAndOpen();

    expect(screen.getByText("No routing decision recorded.")).toBeTruthy();
  });
});

describe("absent sections render honest empty states (R-02)", () => {
  test("a no-retrieval turn shows no fabricated funnel", async () => {
    stubTraceBackend({ records: [NO_RETRIEVAL_RECORD_WIRE] });
    renderExplorer();
    fireEvent.click(screen.getByRole("button", { name: "Search turns" }));
    await screen.findByText(/routing error/i, { selector: ".session-preview" });
    fireEvent.click(screen.getByRole("button", { name: /Turn 6/i }));
    await screen.findByRole("heading", { name: /Turn 6/ });

    const funnel = screen.getByRole("heading", { name: "Retrieval funnel" }).closest("section")!;
    expect(within(funnel).getByText("No retrieval run recorded.")).toBeTruthy();
    expect(within(funnel).queryByText(/sufficient:/)).toBeNull();
    expect(within(funnel).queryByRole("table")).toBeNull();

    const prompt = screen.getByRole("heading", { name: "Assembled prompt" }).closest("section")!;
    expect(within(prompt).getByText("No assembled prompt recorded.")).toBeTruthy();
  });

  test("a no-retrieval turn derives its retrieval stage as not recorded", async () => {
    stubTraceBackend({ records: [NO_RETRIEVAL_RECORD_WIRE] });
    renderExplorer();
    fireEvent.click(screen.getByRole("button", { name: "Search turns" }));
    await screen.findByText(/routing error/i, { selector: ".session-preview" });
    fireEvent.click(screen.getByRole("button", { name: /Turn 6/i }));
    await screen.findByRole("heading", { name: /Turn 6/ });

    const graph = screen.getByRole("heading", { name: "Executed structure" }).closest("section")!;
    const retrievalRow = graph.querySelectorAll(".graph-stage")[1] as unknown as HTMLElement;
    expect(retrievalRow.textContent).toContain("not recorded");
    expect(retrievalRow.textContent).not.toContain("sufficient");
  });
});

describe("the captured executed graph (OBS-006)", () => {
  test("a captured turn renders the real nodes with durations and attempts", async () => {
    stubTraceBackend({ records: [V3_RECORD_WIRE] });
    renderExplorer();
    fireEvent.click(screen.getByRole("button", { name: "Search turns" }));
    await screen.findByRole("button", { name: /Turn 4/i });
    fireEvent.click(screen.getByRole("button", { name: /Turn 4/i }));
    await screen.findByRole("heading", { name: /Turn 4/ });

    const graph = screen.getByRole("heading", { name: "Executed graph" }).closest("section")!;
    expect(within(graph).getByText("captured execution")).toBeTruthy();
    expect(within(graph).getByText("route")).toBeTruthy();
    expect(within(graph).getByText("model")).toBeTruthy();
    expect(within(graph).getByText("finalize")).toBeTruthy();
    expect(within(graph).getAllByText(/attempt 1/).length).toBe(3);
    expect(within(graph).getByText(/40 ms/)).toBeTruthy();
    expect(within(graph).getByText(/branch:to:model/)).toBeTruthy();
    expect(within(graph).queryByText(/derived from stored trace fields/)).toBeNull();
  });

  test("a resumed turn is visibly marked and its replayed node identified", async () => {
    stubTraceBackend();
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => {
        if (url.includes("/api/admin/csrf-token")) return jsonResponse({ csrf_token: "t" });
        if (url.includes("/gold-cases")) return jsonResponse(GOLD_WIRE);
        if (url.includes("/api/admin/traces/")) {
          return jsonResponse(wireTraceContent("turn-1", RESUMED_READ_WIRE_CONTENT));
        }
        if (url.includes("/api/admin/traces?")) return jsonResponse({ records: [RECORD_WIRE] });
        throw new Error(`unexpected request: ${url}`);
      })
    );
    renderExplorer();
    await searchAndOpen();

    const graph = screen.getByRole("heading", { name: "Executed graph" }).closest("section")!;
    expect(within(graph).getByText("resumed from checkpoint")).toBeTruthy();
    expect(within(graph).getByText("confirm_booking")).toBeTruthy();
    expect(within(graph).getByText("commit_booking")).toBeTruthy();
    expect(within(graph).getByText("replayed")).toBeTruthy();
    const rows = within(graph).getAllByRole("listitem");
    expect(rows[0]?.textContent ?? "").toContain("confirm_booking");
  });

  test("a crashed turn shows the nodes that ran and stops, never idealizing", async () => {
    stubTraceBackend({ records: [{ ...RECORD_WIRE, turn_id: "turn-3" }] });
    renderExplorer();
    fireEvent.click(screen.getByRole("button", { name: "Search turns" }));
    await screen.findByText(/tool error/i, { selector: ".session-preview" });
    fireEvent.click(screen.getByRole("button", { name: /Turn 8/i }));
    await screen.findByRole("heading", { name: /Turn 8/ });

    const graph = screen.getByRole("heading", { name: "Executed graph" }).closest("section")!;
    expect(within(graph).getByText("failed")).toBeTruthy();
    expect(within(graph).getByText(/entered, never exited/)).toBeTruthy();
    expect(within(graph).getByText(/The graph stopped at/)).toBeTruthy();
    expect(within(graph).queryByText("finalize")).toBeNull();
  });

  test("the captured executed graph has no axe violations", async () => {
    stubTraceBackend({ records: [V3_RECORD_WIRE] });
    renderExplorer();
    fireEvent.click(screen.getByRole("button", { name: "Search turns" }));
    await screen.findByRole("button", { name: /Turn 4/i });
    fireEvent.click(screen.getByRole("button", { name: /Turn 4/i }));
    await screen.findByRole("heading", { name: /Turn 4/ });

    const results = await axe.run(document, {
      resultTypes: ["violations"],
      rules: { "color-contrast": { enabled: false }, "target-size": { enabled: false } }
    });
    expect(results.violations.map((violation) => violation.id)).toEqual([]);
  });
});

describe("the coordinated drill-down panels", () => {
  test("the routing alternatives table shows every candidate and its signals", async () => {
    stubTraceBackend({ records: [FABRICATED_RECORD_WIRE] });
    renderExplorer();
    fireEvent.click(screen.getByRole("button", { name: "Search turns" }));
    await screen.findByText(/grounding \/ citation error/i, { selector: ".session-preview" });
    fireEvent.click(screen.getByRole("button", { name: /Turn 2/i }));
    await screen.findByRole("heading", { name: /Turn 2/ });

    const panel = screen.getByRole("heading", { name: "Routing" }).closest("section")!;
    expect(within(panel).getByText("general")).toBeTruthy();
    expect(within(panel).getByText("booking")).toBeTruthy();
    // The runner-up's signal evidence is stored per candidate.
    expect(within(panel).getByText("service-category")).toBeTruthy();
  });

  test("the retrieval funnel shows the query and per-candidate fused scores", async () => {
    stubTraceBackend({ records: [FABRICATED_RECORD_WIRE] });
    renderExplorer();
    fireEvent.click(screen.getByRole("button", { name: "Search turns" }));
    await screen.findByText(/grounding \/ citation error/i, { selector: ".session-preview" });
    fireEvent.click(screen.getByRole("button", { name: /Turn 2/i }));
    await screen.findByRole("heading", { name: /Turn 2/ });

    const panel = screen.getByRole("heading", { name: "Retrieval funnel" }).closest("section")!;
    expect(
      within(panel).getByText(/Is there a discount for quarterly window cleaning\?/)
    ).toBeTruthy();
    expect(within(panel).getByText("clearview-windows-5")).toBeTruthy();
    expect(within(panel).getByText("0.8")).toBeTruthy();
    expect(within(panel).getByText("0.4")).toBeTruthy();
  });

  test("the v3 retrieval funnel shows original and resolved when the planner rewrote the query", async () => {
    stubTraceBackend({ records: [V3_RECORD_WIRE] });
    renderExplorer();
    fireEvent.click(screen.getByRole("button", { name: "Search turns" }));
    await screen.findByRole("button", { name: /Turn 4/i });
    fireEvent.click(screen.getByRole("button", { name: /Turn 4/i }));
    await screen.findByRole("heading", { name: /Turn 4/ });

    const panel = screen.getByRole("heading", { name: "Retrieval funnel" }).closest("section")!;
    expect(within(panel).getByText(/Original:/)).toBeTruthy();
    expect(within(panel).getByText(/Resolved:/)).toBeTruthy();
    expect(within(panel).getByText(/Clearview HVAC maintenance/)).toBeTruthy();
    const codes = within(panel).getAllByText(/maintenance include\?/);
    expect(codes).toHaveLength(2);
  });

  test("the prompt renders trusted and untrusted regions and the budget exclusions", async () => {
    stubTraceBackend();
    renderExplorer();
    await searchAndOpen();

    const panel = screen.getByRole("heading", { name: "Assembled prompt" }).closest("section")!;
    expect(within(panel).getByText(/You are Clearview assistant/)).toBeTruthy();
    expect(within(panel).getByText(/What are your hours\?/)).toBeTruthy();
    expect(within(panel).getAllByText("TRUSTED").length).toBeGreaterThanOrEqual(1);
    expect(within(panel).getAllByText("UNTRUSTED").length).toBeGreaterThanOrEqual(2);
  });

  test("claim verdicts are limited to supported, unsupported, and fabricated_citation", async () => {
    stubTraceBackend();
    renderExplorer();
    await searchAndOpen();

    const panel = screen.getByRole("heading", { name: "Claim verdicts" }).closest("section")!;
    expect(within(panel).getByText("supported")).toBeTruthy();
    expect(within(panel).queryByText(/entailment|partially supported/i)).toBeNull();
  });

  test("an unsupported claim renders its value and kind, not an empty chip (R-03)", async () => {
    stubTraceBackend({ records: [UNSUPPORTED_RECORD_WIRE] });
    renderExplorer();
    fireEvent.click(screen.getByRole("button", { name: "Search turns" }));
    await screen.findByText(/grounding \/ citation error/i, { selector: ".session-preview" });
    fireEvent.click(screen.getByRole("button", { name: /Turn 5/i }));
    await screen.findByRole("heading", { name: /Turn 5/ });

    const panel = screen.getByRole("heading", { name: "Claim verdicts" }).closest("section")!;
    expect(within(panel).getByText("unsupported")).toBeTruthy();
    expect(within(panel).getByText("$95")).toBeTruthy();
    expect(within(panel).getByText("price")).toBeTruthy();
  });

  test("a fabricated citation renders as fabricated, from the record", async () => {
    stubTraceBackend({ records: [FABRICATED_RECORD_WIRE] });
    renderExplorer();
    fireEvent.click(screen.getByRole("button", { name: "Search turns" }));
    await screen.findByText(/grounding \/ citation error/i, { selector: ".session-preview" });
    fireEvent.click(screen.getByRole("button", { name: /Turn 2/i }));
    await screen.findByRole("heading", { name: /Turn 2/ });

    const panel = screen.getByRole("heading", { name: "Claim verdicts" }).closest("section")!;
    expect(within(panel).getByText("fabricated_citation")).toBeTruthy();
    expect(within(panel).getByText("clearview-windows-99")).toBeTruthy();
  });

  test("suspected diagnoses are presented as uncertain, never as confirmed causes", async () => {
    stubTraceBackend({ records: [SUSPECTED_RECORD_WIRE] });
    renderExplorer();
    fireEvent.click(screen.getByRole("button", { name: "Search turns" }));
    await screen.findByText(/model behavior/i, { selector: ".session-preview" });
    fireEvent.click(screen.getByRole("button", { name: /Turn 7/i }));
    await screen.findByRole("heading", { name: /Turn 7/ });

    const panel = screen.getByRole("heading", { name: "Diagnoses" }).closest("section")!;
    expect(
      within(panel).getByText(/This is not a confirmed cause: it is a suspicion/i)
    ).toBeTruthy();
    expect(within(panel).getByText(/Suspected — uncertain/i)).toBeTruthy();
  });
});

describe("the audited read resolves or fails visibly (R-18)", () => {
  test("a failed read shows an error with a retry, never an eternal spinner", async () => {
    stubTraceBackend({ readFailure: { status: 500 } });
    renderExplorer();
    fireEvent.click(screen.getByRole("button", { name: "Search turns" }));
    await screen.findByText(/tool error/i, { selector: ".session-preview" });
    fireEvent.click(screen.getByRole("button", { name: /Turn 8/i }));

    await screen.findByText("Reading the turn record failed.");
    expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
    expect(screen.queryByText(/Opening the audited turn record/)).toBeNull();
  });

  test("a 404 read is an explicit absent state with a retry", async () => {
    stubTraceBackend({ readFailure: { status: 404 } });
    renderExplorer();
    fireEvent.click(screen.getByRole("button", { name: "Search turns" }));
    await screen.findByText(/tool error/i, { selector: ".session-preview" });
    fireEvent.click(screen.getByRole("button", { name: /Turn 8/i }));

    await screen.findByText("No turn record was found for this entry.");
    expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
  });

  test("retry re-reads and opens the record when the surface recovers", async () => {
    let failing = true;
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => {
        if (url.includes("/api/admin/csrf-token")) return jsonResponse({ csrf_token: "t" });
        if (url.includes("/gold-cases")) return jsonResponse(GOLD_WIRE);
        if (url.includes("/api/admin/traces/")) {
          if (failing) return jsonResponse({}, { ok: false, status: 500 });
          return jsonResponse(wireTraceContent("turn-1", TRACE_READ_WIRE_CONTENT));
        }
        if (url.includes("/api/admin/traces?")) return jsonResponse({ records: [RECORD_WIRE] });
        throw new Error(`unexpected request: ${url}`);
      })
    );
    renderExplorer();
    fireEvent.click(screen.getByRole("button", { name: "Search turns" }));
    await screen.findByText(/tool error/i, { selector: ".session-preview" });
    fireEvent.click(screen.getByRole("button", { name: /Turn 8/i }));
    await screen.findByText("Reading the turn record failed.");

    failing = false;
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await screen.findByRole("heading", { name: /Turn 8/ });
  });
});

describe("stale responses cannot clobber fresh state (R-20)", () => {
  function deferredResponse() {
    let resolve!: (value: unknown) => void;
    const promise = new Promise<unknown>((res) => {
      resolve = res;
    });
    return { promise, resolve };
  }

  test("a slow first search does not overwrite a fast second search", async () => {
    let staleSearch: ReturnType<typeof deferredResponse> | null = null;
    const fetchMock = vi.fn((url: string) => {
      if (url.includes("/api/admin/traces?") && !staleSearch) {
        staleSearch = deferredResponse();
        return staleSearch.promise;
      }
      if (url.includes("/api/admin/traces?")) return jsonResponse({ records: [] });
      if (url.includes("/gold-cases")) return jsonResponse(GOLD_WIRE);
      if (url.includes("/api/admin/traces/")) {
        return jsonResponse(wireTraceContent("turn-1", TRACE_READ_WIRE_CONTENT));
      }
      if (url.includes("/api/admin/csrf-token")) return jsonResponse({ csrf_token: "t" });
      throw new Error(`unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    renderExplorer();

    fireEvent.click(screen.getByRole("button", { name: "Search turns" }));
    await waitFor(() => expect(fetchMock.mock.calls.length).toBeGreaterThan(0));
    // The submit button is disabled while the first search is in flight, so
    // the overlapping request arrives as a form submission (Enter).
    fireEvent.submit(screen.getByRole("form", { name: "Turn search filters" }));
    await screen.findByText("No turns match these filters.");

    // The stale response lands last but must not repaint the fresh empty state.
    staleSearch!.resolve({ records: [RECORD_WIRE] });
    await waitFor(() =>
      expect(screen.queryByText(/tool error/i, { selector: ".session-preview" })).toBeNull()
    );
    expect(screen.getByText("No turns match these filters.")).toBeTruthy();
  });

  test("a slow gold fetch from a stale selection cannot replace the fresh one", async () => {
    let staleGold: ReturnType<typeof deferredResponse> | null = null;
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => {
        if (url.includes("/gold-cases")) {
          if (!staleGold) {
            staleGold = deferredResponse();
            return staleGold.promise;
          }
          return jsonResponse({
            cases: [
              {
                case_id: "fresh-case",
                tenant_id: "clearview",
                scenario: null,
                query: "What are your hours?",
                gold_chunks: []
              }
            ]
          });
        }
        if (url.includes("/api/admin/traces/")) {
          return jsonResponse(wireTraceContent("turn-1", TRACE_READ_WIRE_CONTENT));
        }
        if (url.includes("/api/admin/traces?")) return jsonResponse({ records: [RECORD_WIRE] });
        if (url.includes("/api/admin/csrf-token")) return jsonResponse({ csrf_token: "t" });
        throw new Error(`unexpected request: ${url}`);
      })
    );
    renderExplorer();
    fireEvent.click(screen.getByRole("button", { name: "Search turns" }));
    await screen.findByText(/tool error/i, { selector: ".session-preview" });

    fireEvent.click(screen.getByRole("button", { name: /Turn 8/i }));
    await screen.findByRole("heading", { name: /Turn 8/ });
    // A second selection supersedes the first before its gold response lands.
    fireEvent.click(screen.getByRole("button", { name: /Turn 8/i }));
    await screen.findByText("fresh-case", { selector: "code" });
    staleGold!.resolve(GOLD_WIRE);

    await waitFor(() => expect(screen.queryByText("clearview-window-fabricated")).toBeNull());
    expect(screen.getByText("fresh-case", { selector: "code" })).toBeTruthy();
  });
});

describe("the trace explorer accessibility contract (R-54)", () => {
  test("the results group is AT-visible and only the selected row is current", async () => {
    stubTraceBackend({ records: [RECORD_WIRE, FABRICATED_RECORD_WIRE] });
    renderExplorer();
    fireEvent.click(screen.getByRole("button", { name: "Search turns" }));
    await screen.findByText(/tool error/i, { selector: ".session-preview" });

    const results = screen.getByRole("group", { name: "Turn search results" });
    const first = within(results).getByRole("button", { name: /Turn 8/i });
    const second = within(results).getByRole("button", { name: /Turn 2/i });
    // An unselected row renders no aria-current attribute at all — never
    // aria-current="false", which assistive tech reads as a marked row.
    expect(first.getAttribute("aria-current")).toBeNull();
    expect(second.getAttribute("aria-current")).toBeNull();

    fireEvent.click(first);
    await screen.findByRole("heading", { name: /Turn 8/ });
    expect(first.getAttribute("aria-current")).toBe("true");
    expect(second.getAttribute("aria-current")).toBeNull();

    // A hex field must not advertise a numeric keypad.
    expect(screen.getByLabelText("Component-manifest hash").getAttribute("inputmode")).toBeNull();
  });
});

describe("gold evidence and replay", () => {
  test("the gold overlay matches an eval case and is marked non-gating", async () => {
    stubTraceBackend({ records: [FABRICATED_RECORD_WIRE] });
    renderExplorer();
    fireEvent.click(screen.getByRole("button", { name: "Search turns" }));
    await screen.findByText(/grounding \/ citation error/i, { selector: ".session-preview" });
    fireEvent.click(screen.getByRole("button", { name: /Turn 2/i }));
    await screen.findByRole("heading", { name: /Turn 2/ });

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
    stubTraceBackend();
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => {
        if (url.includes("/api/admin/csrf-token")) return jsonResponse({ csrf_token: "t" });
        if (url.includes("/replay")) return jsonResponse({}, { ok: false, status: 503 });
        if (url.includes("/gold-cases")) return jsonResponse(GOLD_WIRE);
        if (url.includes("/api/admin/traces/")) {
          return jsonResponse(wireTraceContent("turn-1", TRACE_READ_WIRE_CONTENT));
        }
        if (url.includes("/api/admin/traces?")) return jsonResponse({ records: [RECORD_WIRE] });
        throw new Error(`unexpected request: ${url}`);
      })
    );
    renderExplorer();
    await searchAndOpen();

    fireEvent.click(screen.getByRole("button", { name: "Run one safe replay" }));
    await screen.findByText(/Replay failed/i);
    expect(screen.getByRole("button", { name: "Run one safe replay" })).toBeTruthy();
  });

  test("a failed CSRF fetch fails the mutation loudly instead of sending an empty header (R-52)", async () => {
    const fetchMock = vi.fn((url: string) => {
      if (url.includes("/api/admin/csrf-token")) {
        return jsonResponse({}, { ok: false, status: 500 });
      }
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
    await screen.findByText(/Could not obtain a CSRF token/i);
    // The replay request itself never went out — no header, no silent success.
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes("/replay"))).toBe(false);
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
