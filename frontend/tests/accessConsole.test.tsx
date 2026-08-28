import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import axe from "axe-core";
import { describe, expect, test, vi } from "vitest";

import { AccessConsole } from "src/admin/components/AccessConsole";
import { AdminApi } from "src/admin/adminApi";
import { jsonResponse } from "tests/support/backend";

/** Let every already-resolved promise settle, without touching timers. */
async function flush() {
  await act(async () => {
    await Promise.resolve();
  });
}

const TENANTS = [
  { tenantId: "clearview", name: "Clearview Heating" },
  { tenantId: "apex", name: "Apex Home Services" }
];

const PERMISSIONS_WIRE = {
  roles: [
    {
      tenant_id: "clearview",
      subject: "operator-7",
      role: "tenant_admin",
      granted_by: "platform-1",
      granted_at: "2026-08-01T10:00:00Z",
      updated_at: "2026-08-01T10:00:00Z"
    },
    {
      tenant_id: "clearview",
      subject: "operator-8",
      role: "support_agent",
      granted_by: "platform-1",
      granted_at: "2026-08-02T09:00:00Z",
      updated_at: "2026-08-02T09:00:00Z"
    }
  ],
  grants: [
    {
      tenant_id: "clearview",
      subject: "operator-9",
      granted_by: "platform-1",
      granted_at: "2026-08-03T12:00:00Z",
      expires_at: null
    }
  ]
};

const AUDIT_WIRE = {
  events: [
    {
      action: "trace.read_refused",
      actor_type: "staff",
      principal: "operator-9",
      tenant_id: "clearview",
      request_id: "req-refused",
      trace_id: null,
      resource_type: "turn_record",
      resource_id: null,
      occurred_at: "2026-08-07T08:00:00Z",
      permission: "no permission — the read was refused"
    },
    {
      action: "membership_assigned",
      actor_type: "staff",
      principal: "platform-1",
      tenant_id: "clearview",
      request_id: "req-assign",
      trace_id: "trace-abc",
      resource_type: "tenant_membership",
      resource_id: null,
      occurred_at: "2026-08-06T23:00:00Z",
      permission: "platform_admin — directory role"
    },
    {
      action: "staff_reply_sent",
      actor_type: "staff",
      principal: "operator-8",
      tenant_id: "clearview",
      request_id: "req-reply",
      trace_id: null,
      resource_type: "chat_session",
      resource_id: "session-1",
      occurred_at: "2026-08-06T22:00:00Z",
      permission: "support_agent — tenant membership"
    }
  ]
};

function stubAccessBackend({
  notFound = false,
  auditWire = AUDIT_WIRE
}: { notFound?: boolean; auditWire?: unknown } = {}) {
  const fetchMock = vi.fn((url: string, _init?: RequestInit) => {
    if (url.includes("/api/admin/audit") || url.includes("/api/admin/permissions")) {
      if (notFound) return jsonResponse({}, { ok: false, status: 404 });
      if (url.includes("/api/admin/audit")) return jsonResponse(auditWire);
      return jsonResponse(PERMISSIONS_WIRE);
    }
    throw new Error(`unexpected request: ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function renderAccess(tenantId: string | null = "clearview") {
  document.documentElement.lang = "en";
  document.title = "Chat Admin";
  render(<AccessConsole api={new AdminApi("")} tenants={TENANTS} initialTenantId={tenantId} />);
}

describe("the FEAT-016 access console permissions view", () => {
  test("lists the tenant's roles and trace-read grants as separate controls", async () => {
    stubAccessBackend();
    renderAccess();

    const rolesSection = await screen.findByRole("region", {
      name: "Admin roles (tenant memberships)"
    });
    expect(within(rolesSection).getByText("operator-7")).toBeTruthy();
    expect(within(rolesSection).getByText("Tenant admin")).toBeTruthy();
    expect(within(rolesSection).getByText("operator-8")).toBeTruthy();

    const grantsSection = screen.getByRole("region", { name: "Trace-read grants (PRIV-002)" });
    expect(within(grantsSection).getByText("operator-9")).toBeTruthy();
    expect(within(grantsSection).getByText("Never")).toBeTruthy();
    // The grant holder is a trace viewer, never shown as a role holder.
    expect(within(rolesSection).queryByText("operator-9")).toBeNull();
    // The two controls are told apart explicitly, not by inference.
    expect(screen.getByText(/Two different controls/i)).toBeTruthy();
    expect(screen.getByText(/never confers the other/i)).toBeTruthy();
  });

  test("shows who granted each role and grant", async () => {
    stubAccessBackend();
    renderAccess();

    const rolesSection = await screen.findByRole("region", {
      name: "Admin roles (tenant memberships)"
    });
    expect(within(rolesSection).getAllByText("platform-1").length).toBe(2);
    const grantsSection = screen.getByRole("region", { name: "Trace-read grants (PRIV-002)" });
    expect(within(grantsSection).getByText("platform-1")).toBeTruthy();
  });

  test("the audit trail shows each action beside the permission and its holders", async () => {
    stubAccessBackend();
    renderAccess();

    await screen.findByText(/staff_reply_sent/);
    const table = screen.getByRole("table", { name: "Audit trail" });

    // "who did it": the acting principal; "who could have": the live holders.
    const replyRow = [...table.querySelectorAll("tr")].find((row) =>
      row.textContent?.includes("staff_reply_sent")
    );
    expect(replyRow?.textContent).toContain("support_agent — tenant membership");
    expect(replyRow?.textContent).toContain("could have: operator-8");
    expect(replyRow?.textContent).toContain("operator-8");

    // A refusal authorizes nothing, so no "could have" line is invented.
    const refusalRow = [...table.querySelectorAll("tr")].find((row) =>
      row.textContent?.includes("trace.read_refused")
    );
    expect(refusalRow?.textContent).toContain("no permission — the read was refused");
    expect(refusalRow?.textContent).not.toContain("could have:");
  });

  test("no content field is rendered from any audit row", async () => {
    stubAccessBackend({
      auditWire: {
        events: [
          {
            action: "staff_reply_sent",
            actor_type: "staff",
            principal: "operator-8",
            tenant_id: "clearview",
            request_id: "req-content",
            trace_id: null,
            resource_type: "chat_session",
            resource_id: "session-1",
            occurred_at: "2026-08-06T22:00:00Z",
            permission: "support_agent — tenant membership",
            details: {
              prompt: "the assembled prompt",
              answer: "the model answer",
              contact: "dana@example.com"
            }
          }
        ]
      }
    });
    renderAccess();

    await screen.findByText(/staff_reply_sent/);
    expect(screen.queryByText(/the assembled prompt/i)).toBeNull();
    expect(screen.queryByText(/the model answer/i)).toBeNull();
    expect(screen.queryByText(/dana@example\.com/i)).toBeNull();
  });
});

describe("the FEAT-016 audit trail filters", () => {
  test("sends the bounded filters, never free text", async () => {
    const fetchMock = stubAccessBackend();
    renderAccess();
    await screen.findByText(/staff_reply_sent/);

    fireEvent.change(screen.getByLabelText("From"), { target: { value: "2026-08-06T00:00" } });
    fireEvent.change(screen.getByLabelText("Until"), { target: { value: "2026-08-07T00:00" } });
    fireEvent.change(screen.getByLabelText("Action"), {
      target: { value: "membership_assigned" }
    });
    fireEvent.change(screen.getByLabelText("Principal"), { target: { value: "platform-1" } });
    fireEvent.click(screen.getByRole("button", { name: "Read the trail" }));

    await waitFor(() => {
      const auditUrl = String(
        fetchMock.mock.calls.find(
          ([url]) =>
            String(url).includes("/api/admin/audit") &&
            String(url).includes("action=membership_assigned")
        )?.[0]
      );
      expect(auditUrl).not.toBe("undefined");
      const params = new URLSearchParams(auditUrl.split("?")[1]);
      expect(params.get("tenant_id")).toBe("clearview");
      expect(params.get("action")).toBe("membership_assigned");
      expect(params.get("principal")).toBe("platform-1");
      expect(params.get("since")).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/);
      expect(params.get("until")).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/);
    });
  });

  test("keyboard navigation reaches the filters and Enter submits the trail read", async () => {
    const fetchMock = stubAccessBackend();
    renderAccess();
    await screen.findByText(/staff_reply_sent/);

    // Every filter is focusable in tab order, so a keyboard-only operator can
    // reach the whole surface.
    const principal = screen.getByLabelText("Principal");
    principal.focus();
    expect(document.activeElement).toBe(principal);
    fireEvent.change(principal, { target: { value: "platform-1" } });

    const form = screen.getByLabelText("Audit trail filters");
    fireEvent.submit(form);

    await waitFor(() => {
      const auditCalls = fetchMock.mock.calls.filter(([url]) =>
        String(url).includes("/api/admin/audit")
      );
      expect(auditCalls.length).toBeGreaterThanOrEqual(2);
      const lastParams = new URLSearchParams(String(auditCalls.at(-1)?.[0]).split("?")[1]);
      expect(lastParams.get("principal")).toBe("platform-1");
    });
  });

  test("a request for an inaccessible tenant is a clear 404, not a leak", async () => {
    stubAccessBackend({ notFound: true });
    renderAccess();

    await screen.findByText(/This tenant cannot be opened/i);
    expect(screen.queryByText(/operator-8/)).toBeNull();
    expect(screen.queryByText(/staff_reply_sent/)).toBeNull();
  });
});

describe("overlapping reads never publish a superseded response", () => {
  test("a stale tenant's trail is dropped when it answers after the new tenant's", async () => {
    // Every trail read and permissions read races the next one; without the
    // generation guard a slow response landed last and put one tenant's audit
    // rows under another tenant's heading.
    const pending: Array<{ url: string; release: (body: unknown) => void }> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => {
        if (String(url).includes("/api/admin/csrf-token")) {
          return jsonResponse({ csrf_token: "token-1" });
        }
        return new Promise((resolve) => {
          pending.push({ url: String(url), release: (body) => resolve(jsonResponse(body)) });
        });
      })
    );
    renderAccess();

    const release = async (fragment: string, body: unknown) => {
      await waitFor(() => expect(pending.some((entry) => entry.url.includes(fragment))).toBe(true));
      pending
        .splice(
          pending.findIndex((entry) => entry.url.includes(fragment)),
          1
        )[0]!
        .release(body);
    };
    const apexPermissions = {
      roles: [
        {
          tenant_id: "apex",
          subject: "apex-operator",
          role: "support_agent",
          granted_by: "platform-1",
          granted_at: "2026-08-01T10:00:00Z",
          updated_at: "2026-08-01T10:00:00Z"
        }
      ],
      grants: []
    };
    const apexAudit = {
      events: [
        {
          action: "staff_reply_sent",
          actor_type: "staff",
          principal: "apex-operator",
          tenant_id: "apex",
          request_id: "req-apex",
          trace_id: null,
          resource_type: "chat_session",
          resource_id: "session-2",
          occurred_at: "2026-08-07T09:00:00Z",
          permission: "support_agent — tenant membership"
        }
      ]
    };

    // The mount's clearview reads are left in flight.
    fireEvent.change(screen.getByLabelText("Access console tenant"), {
      target: { value: "apex" }
    });
    // The newer apex reads resolve first.
    await release("/api/admin/permissions?tenant_id=apex", apexPermissions);
    await release("/api/admin/audit?tenant_id=apex", apexAudit);
    await screen.findByText("req-apex");

    // The superseded clearview trail answers last, and must change nothing.
    await release("/api/admin/permissions?tenant_id=clearview", PERMISSIONS_WIRE);
    await release("/api/admin/audit?tenant_id=clearview", AUDIT_WIRE);
    await flush();

    expect(screen.queryByText("req-reply")).toBeNull();
    expect(screen.queryByText(/operator-8/)).toBeNull();
    expect(screen.getByText("req-apex")).toBeTruthy();
  });
});

describe("automated accessibility checks", () => {
  test("the access console has no axe violations", async () => {
    stubAccessBackend();
    renderAccess();
    await screen.findByText(/staff_reply_sent/);

    const results = await axe.run(document, {
      resultTypes: ["violations"],
      rules: { "color-contrast": { enabled: false }, "target-size": { enabled: false } }
    });
    expect(results.violations.map((violation) => violation.id)).toEqual([]);
  });
});
