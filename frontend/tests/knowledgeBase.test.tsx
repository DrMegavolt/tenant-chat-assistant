import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, test, vi } from "vitest";

import { AdminApi } from "src/admin/adminApi";
import { KnowledgeBase } from "src/admin/components/KnowledgeBase";
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

const SOURCE_WIRE = {
  source_id: "source-1",
  tenant_id: "clearview",
  domain: "financing",
  kind: "upload",
  display_name: "Brochures",
  enabled: true,
  documents: [
    {
      document_id: "doc-1",
      source_id: "source-1",
      external_key: "plan-terms.md",
      title: "Plan terms",
      deleted: false,
      versions: [
        {
          version_id: "version-1",
          revision: 1,
          state: "draft",
          indexing_state: "pending",
          safety_state: "clear",
          visibility: "public",
          checksum: "c".repeat(64),
          byte_size: 120,
          media_type: "text/markdown",
          approved_at: null,
          published_at: null,
          superseded_at: null,
          indexed_at: null,
          effective_at: null,
          expires_at: null,
          index_error_code: null,
          generation_status: null,
          chunk_count: 0,
          embedding_model: null
        },
        {
          version_id: "version-2",
          revision: 2,
          state: "published",
          indexing_state: "indexed",
          safety_state: "clear",
          visibility: "public",
          checksum: "d".repeat(64),
          byte_size: 220,
          media_type: "text/markdown",
          approved_at: "2026-08-06T12:00:00Z",
          published_at: "2026-08-06T12:05:00Z",
          superseded_at: null,
          indexed_at: "2026-08-06T12:06:00Z",
          effective_at: "2026-08-06T12:05:00Z",
          expires_at: null,
          index_error_code: null,
          generation_status: "complete",
          chunk_count: 3,
          embedding_model: "test-embedding"
        }
      ]
    }
  ]
};

const FINDING_WIRE = {
  code: "index_lag",
  tenant_id: "clearview",
  document_id: "doc-1",
  version_id: "version-2",
  generation_id: "gen-1",
  detected_at: "2026-08-06T13:00:00Z",
  detail: { threshold_hours: 24, state: "pending" },
  source_name: "Brochures",
  document_title: "Plan terms",
  revision: 2
};

const VERSION_RESPONSE_WIRE = {
  version: {
    version_id: "version-1",
    revision: 1,
    state: "approved",
    indexing_state: "pending",
    safety_state: "clear",
    visibility: "public",
    checksum: "c".repeat(64),
    byte_size: 120,
    media_type: "text/markdown",
    approved_at: "2026-08-06T12:00:00Z",
    published_at: null,
    superseded_at: null,
    indexed_at: null,
    effective_at: null,
    expires_at: null,
    index_error_code: null,
    generation_status: null,
    chunk_count: 0,
    embedding_model: null
  },
  job: null
};

const PREVIEW_WIRE = {
  version_id: "version-1",
  document_id: "doc-1",
  title: "Plan terms",
  media_type: "text/markdown",
  parser_version: "markdown.v1",
  chunk_count: 3,
  blocks: [{ location: "Rates", text: "0% APR for 12 months." }]
};

const RELATED_TURN_WIRE = {
  turn_id: "turn-abc",
  session_id: "session-1",
  trace_id: null,
  recorded_at: "2026-08-06T12:10:00Z",
  outcome: "answered",
  component_manifest_hash: "a".repeat(64),
  diagnosis_causes: [],
  diagnosis_statuses: [],
  turn_index: 3,
  trace_schema_version: "1",
  source_generation_ids: ["gen-1"]
};

function stubKnowledgeBackend({ grantsTraceRead = true } = {}) {
  let version1State = "draft";
  const tree = () => ({
    sources: [
      {
        ...SOURCE_WIRE,
        documents: [
          {
            ...SOURCE_WIRE.documents[0]!,
            versions: SOURCE_WIRE.documents[0]!.versions.map((version) =>
              version.version_id === "version-1" ? { ...version, state: version1State } : version
            )
          }
        ]
      }
    ]
  });
  const fetchMock = vi.fn((url: string, init?: RequestInit) => {
    if (url.includes("/api/admin/csrf-token")) return jsonResponse({ csrf_token: "token-kb" });
    if (url.includes("/api/admin/knowledge?") || url.endsWith("/api/admin/knowledge")) {
      return jsonResponse(tree());
    }
    if (url.includes("/api/admin/knowledge/index-integrity-check")) {
      return jsonResponse({ findings: [FINDING_WIRE] });
    }
    if (url.includes("/api/admin/knowledge/index-findings")) {
      return jsonResponse({ findings: [FINDING_WIRE] });
    }
    if (url.includes("/api/admin/traces?")) {
      if (!grantsTraceRead) return jsonResponse({}, { ok: false, status: 403 });
      return jsonResponse({ records: [RELATED_TURN_WIRE] });
    }
    if (url.includes("/api/admin/knowledge/sources") && init?.method === "POST") {
      return jsonResponse(SOURCE_WIRE);
    }
    if (url.includes("/api/admin/knowledge/sources/") && init?.method === "POST") {
      return jsonResponse({
        ...SOURCE_WIRE,
        enabled: url.includes("enabled") ? !SOURCE_WIRE.enabled : true
      });
    }
    if (url.includes("/api/admin/knowledge/uploads")) {
      return jsonResponse({
        version_id: "version-3",
        document_id: "doc-1",
        revision: 3
      });
    }
    if (url.includes("/preview")) {
      return jsonResponse(PREVIEW_WIRE);
    }
    if (url.includes("/api/admin/knowledge/documents/") && init?.method === "DELETE") {
      return jsonResponse({ document: { ...SOURCE_WIRE.documents[0], deleted: true } });
    }
    if (url.includes("/api/admin/knowledge/versions/")) {
      const action = (url.split("/").pop() ?? "").split("?")[0] ?? "";
      if (action === "approve") {
        version1State = "approved";
        return jsonResponse(VERSION_RESPONSE_WIRE);
      }
      if (action === "publish") {
        version1State = "published";
        return jsonResponse({
          version: { ...VERSION_RESPONSE_WIRE.version, state: "published" },
          job: { job_id: "job-1" }
        });
      }
      if (action === "reindex") return jsonResponse(VERSION_RESPONSE_WIRE);
      if (action === "expire") return jsonResponse(VERSION_RESPONSE_WIRE);
    }
    throw new Error(`unexpected request: ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return { fetchMock };
}

function renderKnowledge(tenantId: string | null = "clearview") {
  document.documentElement.lang = "en";
  document.title = "Chat Admin";
  render(<KnowledgeBase api={new AdminApi("")} tenants={TENANTS} initialTenantId={tenantId} />);
}

describe("the FEAT-001 knowledge base", () => {
  test("lists sources, documents, and versions with indexing status", async () => {
    const { fetchMock } = stubKnowledgeBackend();
    renderKnowledge();

    await screen.findByRole("article", { name: /brochures/i });
    expect(screen.getByText("Plan terms")).toBeTruthy();
    expect(screen.getAllByText(/rev 1/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/rev 2/).length).toBeGreaterThan(0);
    // Draft never looks published: both states and the index status show.
    expect(screen.getAllByText(/draft/i).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/published/i).length).toBeGreaterThan(0);
    expect(screen.getByText(/3 chunks/)).toBeTruthy();
    expect(screen.queryByText(/index error/i)).toBeNull();
    const listUrl = String(
      fetchMock.mock.calls
        .map(([url]) => url)
        .find((url) => String(url).includes("/api/admin/knowledge?"))
    );
    expect(listUrl).toContain("tenant_id=clearview");
  });

  test("shows index-integrity findings linked to the source version", async () => {
    stubKnowledgeBackend();
    renderKnowledge();

    await screen.findByText(/Excessive index lag/i);
    expect(screen.getByText(/Plan terms · rev 2 · Brochures/)).toBeTruthy();
    expect(screen.getByText(/related turns/i)).toBeTruthy();
  });

  test("runs the integrity check with the CSRF token", async () => {
    const { fetchMock } = stubKnowledgeBackend();
    renderKnowledge();
    await screen.findByText(/Excessive index lag/i);

    fireEvent.click(screen.getByRole("button", { name: "Run integrity check" }));

    await waitFor(() => {
      const call = fetchMock.mock.calls.find(
        ([url, init]) =>
          String(url).includes("/api/admin/knowledge/index-integrity-check") &&
          init?.method === "POST"
      );
      expect(call).toBeTruthy();
    });
    const call = fetchMock.mock.calls.find(([url]) =>
      String(url).includes("/api/admin/knowledge/index-integrity-check")
    );
    const headers = call?.[1]?.headers as Record<string, string>;
    expect(headers["X-CSRF-Token"]).toBe("token-kb");
  });

  test("approves and publishes a draft through audited mutations", async () => {
    const { fetchMock } = stubKnowledgeBackend();
    renderKnowledge();

    await screen.findByRole("article", { name: /brochures/i });
    fireEvent.click(screen.getByRole("button", { name: "Approve" }));

    await screen.findByText(/Approving revision 1 complete/i);
    fireEvent.click(screen.getByRole("button", { name: "Publish" }));
    await screen.findByText(/Publishing revision 1 complete/i);

    const mutations = fetchMock.mock.calls.filter(
      ([url, init]) =>
        String(url).includes("/api/admin/knowledge/versions/") && init?.method === "POST"
    );
    expect(mutations.length).toBeGreaterThan(0);
    const headers = mutations[0]?.[1]?.headers as Record<string, string>;
    expect(headers["X-CSRF-Token"]).toBe("token-kb");
  });

  test("previews a version's parsed content in a bounded panel", async () => {
    stubKnowledgeBackend();
    renderKnowledge();

    await screen.findByRole("article", { name: /brochures/i });
    fireEvent.click(screen.getAllByRole("button", { name: "Preview" })[0]!);

    await screen.findByRole("complementary", { name: /Preview of Plan terms/i });
    expect(screen.getByText(/markdown.v1 · 3 chunks/)).toBeTruthy();
    expect(screen.getByText(/0% APR for 12 months/)).toBeTruthy();
  });

  test("links a finding to its related turns, gated by the trace-read grant", async () => {
    const { fetchMock } = stubKnowledgeBackend();
    renderKnowledge();

    await screen.findByText(/Excessive index lag/i);
    fireEvent.click(screen.getByRole("button", { name: "Related turns" }));

    await screen.findByText(/turn-abc/);
    const searchUrl = String(
      fetchMock.mock.calls
        .map(([url]) => url)
        .find((url) => String(url).includes("/api/admin/traces?"))
    );
    expect(searchUrl).toContain("generation_id=gen-1");
    expect(searchUrl).toContain("tenant_id=clearview");
  });

  test("reports when the operator lacks the trace-read grant", async () => {
    stubKnowledgeBackend({ grantsTraceRead: false });
    renderKnowledge();

    await screen.findByText(/Excessive index lag/i);
    fireEvent.click(screen.getByRole("button", { name: "Related turns" }));

    await screen.findByText(/Related turns require the trace-read grant/i);
  });

  test("deleting a document confirms in the console's dialog and sends the tombstone request", async () => {
    // Deletion is irreversible; the confirmation is the console's own
    // focus-managed dialog now, not a native confirm() this page does not
    // style or control.
    const { fetchMock } = stubKnowledgeBackend();
    renderKnowledge();

    await screen.findByRole("article", { name: /brochures/i });
    const deleteButtons = screen.getAllByRole("button", { name: "Delete document" });
    fireEvent.click(deleteButtons[0]!);

    const dialog = await screen.findByRole("dialog");
    expect(dialog.textContent).toContain("Delete “Plan terms”?");

    fireEvent.click(within(dialog).getByRole("button", { name: "Delete document" }));

    await screen.findByText(/Deleting “Plan terms” complete/i);
    const call = fetchMock.mock.calls.find(
      ([url, init]) =>
        String(url).includes("/api/admin/knowledge/documents/") && init?.method === "DELETE"
    );
    expect(call).toBeTruthy();
    const body = call?.[1]?.body;
    expect(typeof body).toBe("string");
    expect(JSON.parse(body as string)).toEqual({ tenant_id: "clearview" });
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  test("cancelling a document deletion keeps the document", async () => {
    const { fetchMock } = stubKnowledgeBackend();
    renderKnowledge();

    await screen.findByRole("article", { name: /brochures/i });
    const deleteButtons = screen.getAllByRole("button", { name: "Delete document" });
    fireEvent.click(deleteButtons[0]!);

    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));

    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(
      fetchMock.mock.calls.some(
        ([url, init]) =>
          String(url).includes("/api/admin/knowledge/documents/") && init?.method === "DELETE"
      )
    ).toBe(false);
  });

  test("closing the delete dialog returns focus to the control that opened it", async () => {
    // A real click focuses the invoking button before the dialog mounts, so
    // the dialog has an opener to hand focus back to; capturing it after the
    // dialog had taken focus yielded the detached dialog and dropped the
    // keyboard operator at <body>.
    const user = userEvent.setup();
    stubKnowledgeBackend();
    renderKnowledge();

    await screen.findByRole("article", { name: /brochures/i });
    const deleteButtons = screen.getAllByRole("button", { name: "Delete document" });
    await user.click(deleteButtons[0]!);

    const dialog = await screen.findByRole("dialog");
    expect(dialog.contains(document.activeElement)).toBe(true);

    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

    const opener = screen.getAllByRole("button", { name: "Delete document" })[0]!;
    expect(document.activeElement).toBe(opener);
  });

  test("a stale tenant's tree is dropped when it answers after the new tenant's", async () => {
    // The old panel fetched through the previous render's closure and also
    // raced its own refresh: one tenant's documents could land under another
    // tenant's heading. The generation guard makes the loser a no-op.
    const pending: Array<{ url: string; release: (body: unknown) => void }> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => {
        if (String(url).includes("/api/admin/csrf-token")) {
          return jsonResponse({ csrf_token: "token-kb" });
        }
        return new Promise((resolve) => {
          pending.push({ url: String(url), release: (body) => resolve(jsonResponse(body)) });
        });
      })
    );
    render(<KnowledgeBase api={new AdminApi("")} tenants={TENANTS} initialTenantId="clearview" />);

    const release = async (fragment: string, body: unknown) => {
      await waitFor(() => expect(pending.some((entry) => entry.url.includes(fragment))).toBe(true));
      pending
        .splice(
          pending.findIndex((entry) => entry.url.includes(fragment)),
          1
        )[0]!
        .release(body);
    };
    const clearviewTree = { sources: [SOURCE_WIRE] };
    const apexTree = {
      sources: [{ ...SOURCE_WIRE, source_id: "source-apex", display_name: "Apex manuals" }]
    };

    // The mount's clearview reads are left in flight.
    fireEvent.change(screen.getByLabelText("Knowledge base tenant"), {
      target: { value: "apex" }
    });
    // The newer apex reads resolve first.
    await release("/api/admin/knowledge?tenant_id=apex", apexTree);
    await release("/api/admin/knowledge/index-findings?tenant_id=apex", { findings: [] });
    await screen.findByRole("article", { name: /apex manuals/i });

    // The superseded clearview tree answers last, and must change nothing.
    await release("/api/admin/knowledge?tenant_id=clearview", clearviewTree);
    await release("/api/admin/knowledge/index-findings?tenant_id=clearview", {
      findings: [FINDING_WIRE]
    });
    await flush();

    expect(screen.queryByRole("article", { name: /brochures/i })).toBeNull();
    expect(screen.getByRole("article", { name: /apex manuals/i })).toBeTruthy();
  });

  test("expires and reindexes a published version", async () => {
    const { fetchMock } = stubKnowledgeBackend();
    renderKnowledge();

    await screen.findByRole("article", { name: /brochures/i });
    fireEvent.click(screen.getByRole("button", { name: "Expire now" }));
    await screen.findByText(/Expiring revision 2 complete/i);
    fireEvent.click(screen.getByRole("button", { name: "Reindex" }));
    await screen.findByText(/Reindexing revision 2 complete/i);

    const mutations = fetchMock.mock.calls.filter(
      ([url, init]) =>
        String(url).includes("/api/admin/knowledge/versions/") && init?.method === "POST"
    );
    expect(mutations.some(([url]) => String(url).includes("/expire"))).toBe(true);
    expect(mutations.some(([url]) => String(url).includes("/reindex"))).toBe(true);
  });

  test("enables and disables a source with an audited toggle", async () => {
    const { fetchMock } = stubKnowledgeBackend();
    renderKnowledge();

    await screen.findByRole("article", { name: /brochures/i });
    fireEvent.click(screen.getByRole("button", { name: "Disable" }));
    await screen.findByText(/Disabling “Brochures” complete/i);

    const toggle = fetchMock.mock.calls.find(
      ([url, init]) =>
        String(url).includes("/api/admin/knowledge/sources/") && init?.method === "POST"
    );
    expect(toggle).toBeTruthy();
    const headers = toggle?.[1]?.headers as Record<string, string>;
    expect(headers["X-CSRF-Token"]).toBe("token-kb");
  });

  test("creates a source from the empty state", async () => {
    const empty = vi.fn((url: string, init?: RequestInit) => {
      if (url.includes("/api/admin/csrf-token")) return jsonResponse({ csrf_token: "token-kb" });
      if (url.includes("/api/admin/knowledge?") || url.endsWith("/api/admin/knowledge")) {
        return jsonResponse({ sources: [] });
      }
      if (url.includes("/api/admin/knowledge/index-findings")) {
        return jsonResponse({ findings: [] });
      }
      if (url.includes("/api/admin/knowledge/sources") && init?.method === "POST") {
        return jsonResponse(SOURCE_WIRE);
      }
      throw new Error(`unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", empty);
    renderKnowledge();

    await screen.findByText(/No sources yet/i);
    fireEvent.click(screen.getByRole("button", { name: "New source" }));
    fireEvent.change(screen.getByLabelText("Source name"), { target: { value: "Rate sheets" } });
    fireEvent.change(screen.getByLabelText("Domain"), { target: { value: "financing" } });
    fireEvent.submit(screen.getByLabelText("Source name").closest("form") as HTMLFormElement);

    await waitFor(() => {
      const create = empty.mock.calls.find(
        ([url, init]) =>
          String(url).endsWith("/api/admin/knowledge/sources") && init?.method === "POST"
      );
      expect(create).toBeTruthy();
      const body = create?.[1]?.body;
      expect(typeof body).toBe("string");
      expect(JSON.parse(body as string)).toMatchObject({
        tenant_id: "clearview",
        display_name: "Rate sheets",
        domain: "financing"
      });
    });
  });

  test("uploads a document into a source", async () => {
    const { fetchMock } = stubKnowledgeBackend();
    renderKnowledge();

    await screen.findByRole("article", { name: /brochures/i });
    fireEvent.click(screen.getByRole("button", { name: "Upload document" }));
    const file = new File(["# Terms"], "terms.md", { type: "text/markdown" });
    fireEvent.change(screen.getByLabelText("Document file"), { target: { files: [file] } });
    fireEvent.submit(screen.getByLabelText("Document file").closest("form") as HTMLFormElement);

    await waitFor(() => {
      const upload = fetchMock.mock.calls.find(
        ([url, init]) =>
          String(url).includes("/api/admin/knowledge/uploads") && init?.method === "POST"
      );
      expect(upload).toBeTruthy();
      const headers = upload?.[1]?.headers as Record<string, string>;
      expect(headers["X-CSRF-Token"]).toBe("token-kb");
    });
  });
});
