import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";

import { AdminApi } from "src/admin/adminApi";
import { HandoffQueue } from "src/admin/components/HandoffQueue";
import { jsonResponse } from "tests/support/backend";
import { tick } from "tests/support/timers";

const TENANTS = [{ tenantId: "apex", name: "Apex Home Services" }];

/** A wire-format row, exactly as `GET /api/admin/handoffs` publishes it. */
interface HandoffWire {
  handoff_id: string;
  tenant_id: string;
  session_id: string;
  status: string;
  reason: string;
  summary: string;
  assigned_principal_id: string | null;
  requested_at: string;
  assigned_at: string | null;
  released_at: string | null;
  resolved_at: string | null;
  resolved_by_principal_id: string | null;
}

const OPEN: HandoffWire = {
  handoff_id: "HO-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
  tenant_id: "apex",
  session_id: "session-1",
  status: "requested",
  reason: "customer_request",
  summary: "Customer asked to speak to a person about a warranty claim.",
  assigned_principal_id: null,
  requested_at: "2026-08-06T12:00:00Z",
  assigned_at: null,
  released_at: null,
  resolved_at: null,
  resolved_by_principal_id: null
};

const HELD: HandoffWire = {
  ...OPEN,
  handoff_id: "HO-BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB",
  summary: "Customer wants to change an appointment to a morning slot.",
  status: "assigned",
  assigned_principal_id: "operator-7",
  assigned_at: "2026-08-06T12:05:00Z"
};

function stubHandoffBackend(rows: HandoffWire[], operatorSubject = "operator-7") {
  const fetchMock = vi.fn((url: string, init?: RequestInit) => {
    if (url.endsWith("/api/admin/csrf-token")) return jsonResponse({ csrf_token: "token-1" });
    if (url.includes("/api/admin/handoffs?") && init?.method !== "POST") {
      return jsonResponse({ handoffs: rows, operator_subject: operatorSubject, limit: 200 });
    }
    if (url.includes("/api/admin/handoffs/") && init?.method === "POST") {
      const action = url.split("/").at(-2);
      const current = rows[0];
      if (!current) throw new Error("no row to act on");
      const updated: HandoffWire = { ...current };
      if (action === "accept") {
        updated.status = "assigned";
        updated.assigned_principal_id = operatorSubject;
        updated.assigned_at = "2026-08-06T12:10:00Z";
      }
      if (action === "release") {
        updated.status = "queued";
        updated.assigned_principal_id = null;
        updated.assigned_at = null;
        updated.released_at = "2026-08-06T12:10:00Z";
      }
      if (action === "resolve") {
        updated.status = "resolved";
        updated.resolved_at = "2026-08-06T12:10:00Z";
        updated.resolved_by_principal_id = operatorSubject;
      }
      rows = [updated];
      return jsonResponse({ handoff: updated }, { status: 201 });
    }
    throw new Error(`unexpected request: ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

async function renderQueue(rows: HandoffWire[], operatorSubject = "operator-7") {
  stubHandoffBackend(rows, operatorSubject);
  render(<HandoffQueue api={new AdminApi("")} tenants={TENANTS} initialTenantId="apex" />);
  await screen.findByRole("heading", { name: "Handoff queue" });
  if (rows.length) {
    // Wait for the queue itself, not just the chrome around it.
    await screen.findByText(rows[0]!.summary, { selector: ".session-preview" });
  }
}

describe("the handoff queue", () => {
  test("lists open escalation tickets with their reason and status", async () => {
    await renderQueue([OPEN, HELD]);

    const list = within(screen.getByLabelText("Handoff queue entries"));
    expect(
      list.getByText("Customer asked to speak to a person about a warranty claim.")
    ).toBeTruthy();
    expect(
      list.getByText("Customer wants to change an appointment to a morning slot.")
    ).toBeTruthy();
    expect(list.getAllByText("Waiting for staff").length).toBeGreaterThan(0);
    expect(list.getByText(/held by you/)).toBeTruthy();
    expect(list.getByText(/unassigned/)).toBeTruthy();
  });

  test("accepting posts with the CSRF token and the open tenant", async () => {
    const fetchMock = stubHandoffBackend([OPEN]);
    render(<HandoffQueue api={new AdminApi("")} tenants={TENANTS} initialTenantId="apex" />);
    await screen.findByRole("button", { name: "Accept conversation" });

    fireEvent.click(screen.getByRole("button", { name: "Accept conversation" }));

    await waitFor(() => {
      const posted = fetchMock.mock.calls.find(([url]) => url.includes("/accept?"));
      expect(posted).toBeTruthy();
    });
    const posted = fetchMock.mock.calls.find(([url]) => url.includes("/accept?"))!;
    expect(posted[0]).toContain("tenant_id=apex");
    const init = posted[1] as RequestInit;
    expect((init.headers as Record<string, string>)["X-CSRF-Token"]).toBe("token-1");
  });

  test("a row the current operator holds offers release but not accept", async () => {
    await renderQueue([HELD]);

    expect(screen.getByRole("button", { name: "Release to assistant" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Accept conversation" })).toBeNull();
  });

  test("an unowned row offers accept but not release", async () => {
    await renderQueue([OPEN]);

    expect(screen.getByRole("button", { name: "Accept conversation" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Release to assistant" })).toBeNull();
  });

  test("a hidden tab stops polling the handoff queue", async () => {
    vi.useFakeTimers();
    const fetchMock = stubHandoffBackend([OPEN]);
    render(<HandoffQueue api={new AdminApi("")} tenants={TENANTS} initialTenantId="apex" />);
    await tick();
    const baseline = fetchMock.mock.calls.filter(([url]) =>
      url.includes("/api/admin/handoffs?")
    ).length;

    vi.spyOn(document, "hidden", "get").mockReturnValue(true);
    await tick(9000);

    const after = fetchMock.mock.calls.filter(([url]) =>
      url.includes("/api/admin/handoffs?")
    ).length;
    expect(after).toBe(baseline);
  });

  test("an empty queue says so", async () => {
    await renderQueue([]);

    expect(screen.getByText("No conversations are waiting for a person right now.")).toBeTruthy();
  });
});
