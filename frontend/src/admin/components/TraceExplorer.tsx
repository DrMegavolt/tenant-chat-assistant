import { useCallback, useId, useRef, useState } from "react";

import type { AdminApi } from "src/admin/adminApi";
import { TraceDetail } from "src/admin/components/TraceDetail";
import { relativeTime } from "src/admin/time";
import {
  DIAGNOSIS_CAUSES,
  DIAGNOSIS_CAUSE_LABELS,
  DIAGNOSIS_STATUSES,
  DIAGNOSIS_STATUS_LABELS,
  isUncertainStatus,
  OUTCOMES,
  OUTCOME_LABELS,
  type GoldCase,
  type TraceRead,
  type TraceSearchFilters,
  type TraceSearchPage,
  type TraceSearchRecord
} from "src/admin/traceTypes";

const EMPTY_FILTERS: TraceSearchFilters = {};

const TURN_UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** A `datetime-local` value (local wall clock) becomes the UTC ISO the API
 * expects, so a filter boundary means the same instant everywhere. */
function toIso(localDateTime: string): string {
  if (/[zZ]|[+-]\d{2}:?\d{2}$/.test(localDateTime)) return localDateTime;
  const date = new Date(localDateTime);
  return Number.isNaN(date.getTime()) ? localDateTime : date.toISOString();
}

/** The filters as the API accepts them: present fields only, datetimes in UTC. */
function wiredFilters(filters: TraceSearchFilters): TraceSearchFilters {
  const wired: TraceSearchFilters = {};
  if (filters.since) wired.since = toIso(filters.since);
  if (filters.until) wired.until = toIso(filters.until);
  if (filters.outcome) wired.outcome = filters.outcome;
  if (filters.cause) wired.cause = filters.cause;
  if (filters.diagnosisStatus) wired.diagnosisStatus = filters.diagnosisStatus;
  if (filters.manifestHash) wired.manifestHash = filters.manifestHash;
  return wired;
}

/** Backend trace timestamps are ISO strings, unlike the queue's unix seconds. */
function relativeTraceTime(recordedAt: string): string {
  const seconds = new Date(recordedAt).getTime() / 1000;
  return relativeTime(Number.isFinite(seconds) ? seconds : undefined);
}

/** A record-shaped index entry for a turn opened by id, so the drill-in renders
 * it exactly like a search hit; the content itself is the trace the lookup
 * already read, never a re-fetch. */
function recordFromTrace(trace: TraceRead): TraceSearchRecord {
  return {
    turnId: trace.turnId,
    sessionId: trace.sessionId,
    traceId: trace.traceId,
    recordedAt: trace.recordedAt,
    outcome: trace.content.outcome?.status ?? "unknown",
    componentManifestHash: trace.content.manifestHash ?? "",
    diagnosisCauses: (trace.content.diagnoses ?? []).map((diagnosis) => diagnosis.cause),
    diagnosisStatuses: (trace.content.diagnoses ?? []).map((diagnosis) => diagnosis.status),
    turnIndex: trace.content.turnIndex ?? 0,
    traceSchemaVersion: trace.content.schemaVersion ?? "",
    sourceGenerationIds: []
  };
}

export interface TraceExplorerProps {
  api: AdminApi;
  tenants: { tenantId: string; name: string }[];
  initialTenantId: string | null;
}

/**
 * The AI turn explorer: six content-free filters over the inference plane.
 *
 * The search surface is deliberately content-free — results are index entries
 * (outcome, manifest hash, causes, statuses, time). Content is fetched only
 * when an operator drills into one turn, through the audited single-read.
 */
export function TraceExplorer({ api, tenants, initialTenantId }: TraceExplorerProps) {
  const [tenantId, setTenantId] = useState(initialTenantId ?? tenants[0]?.tenantId ?? "");
  const [filters, setFilters] = useState<TraceSearchFilters>(EMPTY_FILTERS);
  const [records, setRecords] = useState<TraceSearchRecord[]>([]);
  // The unbounded match count the backend reports beside each page, so the
  // explorer can say "showing N of M" and offer more without pretending a
  // truncated page was everything (R-36).
  const [total, setTotal] = useState(0);
  const [selected, setSelected] = useState<TraceSearchRecord | null>(null);
  const [gold, setGold] = useState<GoldCase[]>([]);
  const [hasSearched, setHasSearched] = useState(false);
  const [isLoading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lookupId, setLookupId] = useState("");
  const [lookupError, setLookupError] = useState<string | null>(null);
  /** The record an id lookup already read, handed to the detail so the id
   * path costs one audited read like every other path. */
  const [lookupTrace, setLookupTrace] = useState<TraceRead | null>(null);

  // A slower earlier response must not land last and win: every read claims a
  // generation before its first await and publishes only while still newest —
  // the same architecture useAdminConsole established (review R-20).
  const generationRef = useRef(0);
  const claimGeneration = useCallback((): number => {
    generationRef.current += 1;
    return generationRef.current;
  }, []);

  // `isLoading` is owned by the operation that set it: its cleanup settles the
  // flag unless a newer operation has taken ownership. Checking ownership
  // instead of generation currency is what keeps a superseded lookup from
  // leaving both submit buttons disabled forever (review R-20 follow-up).
  const loadingOwnerRef = useRef(0);
  const takeLoading = (generation: number) => {
    loadingOwnerRef.current = generation;
    setLoading(true);
  };
  const settleLoading = (generation: number) => {
    if (loadingOwnerRef.current === generation) setLoading(false);
  };

  const runSearch = async (overrides?: TraceSearchFilters) => {
    const wired = wiredFilters({ ...filters, ...overrides });
    const generation = claimGeneration();
    const isCurrent = () => generation === generationRef.current;
    takeLoading(generation);
    setError(null);
    setSelected(null);
    try {
      const page: TraceSearchPage = await api.searchTraces(tenantId, wired, 0);
      if (!isCurrent()) return;
      setRecords(page.records);
      setTotal(page.total);
      setHasSearched(true);
    } catch {
      if (!isCurrent()) return;
      setError("Could not reach the trace surface. Try the search again.");
    } finally {
      settleLoading(generation);
    }
  };

  const loadMore = async () => {
    const generation = claimGeneration();
    const isCurrent = () => generation === generationRef.current;
    takeLoading(generation);
    setError(null);
    try {
      const page: TraceSearchPage = await api.searchTraces(
        tenantId,
        wiredFilters(filters),
        records.length
      );
      if (!isCurrent()) return;
      setRecords((loaded) => [...loaded, ...page.records]);
      setTotal(page.total);
    } catch {
      if (!isCurrent()) return;
      setError("Could not load more turns. Try again.");
    } finally {
      settleLoading(generation);
    }
  };

  const open = (record: TraceSearchRecord) => {
    const generation = claimGeneration();
    const isCurrent = () => generation === generationRef.current;
    setLookupTrace(null);
    setSelected(record);
    // No trace read here: TraceDetail performs the one audited read for this
    // click, so a drill-in costs a single trace.read (review R-19).
    void (async () => {
      try {
        const goldCases = await api.goldCases(tenantId);
        if (isCurrent()) setGold(goldCases);
      } catch {
        if (isCurrent()) setError("Could not load the gold cases for this tenant.");
      }
    })();
  };

  const openById = async () => {
    const value = lookupId.trim();
    if (!value) return;
    const generation = claimGeneration();
    const isCurrent = () => generation === generationRef.current;
    takeLoading(generation);
    setLookupError(null);
    setError(null);
    try {
      // The detail endpoint takes a turn UUID; a trace id resolves through the
      // by-trace-id read. An unresolvable id is a 404, reported as not found.
      const detail = TURN_UUID_RE.test(value)
        ? await api.trace(value, tenantId)
        : await api.traceByTraceId(value, tenantId);
      if (!isCurrent()) return;
      if (!detail) {
        setLookupError("No turn record found for that id.");
        return;
      }
      setLookupId("");
      // The audited read this lookup performed is handed to the detail, which
      // must not read the same record a second time (review R-19).
      setLookupTrace(detail);
      setSelected(recordFromTrace(detail));
    } catch {
      if (!isCurrent()) return;
      setLookupError("Could not reach the trace surface. Try again.");
    } finally {
      settleLoading(generation);
    }
  };

  const switchingTenant = (next: string) => {
    claimGeneration();
    setTenantId(next);
    setRecords([]);
    setTotal(0);
    setHasSearched(false);
    setLookupTrace(null);
    setSelected(null);
  };

  return (
    <section className="trace-explorer" aria-labelledby="traceTitle">
      <div className="admin-panel-header">
        <h2 id="traceTitle">AI turn explorer</h2>
        {tenants.length > 1 && (
          <label className="tenant-picker">
            <span className="visually-hidden">Tenant</span>
            <select
              value={tenantId}
              onChange={(event) => switchingTenant(event.target.value)}
              aria-label="Explorer tenant"
            >
              {tenants.map((tenant) => (
                <option key={tenant.tenantId} value={tenant.tenantId}>
                  {tenant.name}
                </option>
              ))}
            </select>
          </label>
        )}
      </div>

      <TraceFilters
        filters={filters}
        onFiltersChange={setFilters}
        onSearch={() => void runSearch()}
        isLoading={isLoading}
      />

      <form
        className="trace-filters"
        aria-label="Open a turn by id"
        onSubmit={(event) => {
          event.preventDefault();
          void openById();
        }}
      >
        <label className="trace-filter trace-filter-wide">
          <span className="trace-filter-label">Turn id or trace id</span>
          <input
            type="text"
            placeholder="Turn UUID or trace id"
            value={lookupId}
            onChange={(event) => setLookupId(event.target.value)}
          />
        </label>
        <button type="submit" className="ghost-button" disabled={isLoading}>
          {isLoading ? "Opening…" : "Open turn"}
        </button>
      </form>

      {error && (
        <p className="admin-alert" role="alert">
          {error}
        </p>
      )}
      {lookupError && (
        <p className="admin-alert" role="alert">
          {lookupError}
        </p>
      )}

      {hasSearched && records.length === 0 && (
        <p className="muted-copy">No turns match these filters.</p>
      )}

      {records.length > 0 && (
        <>
          <p className="muted-copy" role="status">
            Showing {records.length} of {total} turn{total === 1 ? "" : "s"}
          </p>
          <div className="trace-results" role="group" aria-label="Turn search results">
            {records.map((record) => (
              <button
                key={record.turnId}
                type="button"
                className={`session-item${selected?.turnId === record.turnId ? " selected" : ""}`}
                aria-current={selected?.turnId === record.turnId ? "true" : undefined}
                onClick={() => open(record)}
              >
                <span className="session-row">
                  <strong>
                    Turn {record.turnIndex} · {OUTCOME_LABELS[record.outcome] ?? record.outcome}
                  </strong>
                  <span className="session-meta">{relativeTraceTime(record.recordedAt)}</span>
                </span>
                <span className="session-preview">
                  {record.diagnosisCauses.length
                    ? record.diagnosisCauses
                        .map((cause) => DIAGNOSIS_CAUSE_LABELS[cause] ?? cause)
                        .join(" · ")
                    : "No diagnosis"}
                  {record.diagnosisStatuses.some(isUncertainStatus) && (
                    <span className="uncertain-chip">uncertain</span>
                  )}
                </span>
                <span className="session-meta mono">
                  {record.componentManifestHash.slice(0, 12)}
                </span>
              </button>
            ))}
          </div>
          {records.length < total && (
            <button
              type="button"
              className="ghost-button"
              disabled={isLoading}
              onClick={() => void loadMore()}
            >
              {isLoading ? "Loading…" : `Load ${total - records.length} more`}
            </button>
          )}
        </>
      )}

      {selected && (
        <TraceDetail
          key={selected.turnId}
          api={api}
          tenantId={tenantId}
          record={selected}
          gold={gold}
          preloadedTrace={lookupTrace?.turnId === selected.turnId ? lookupTrace : undefined}
        />
      )}
    </section>
  );
}

interface TraceFiltersProps {
  filters: TraceSearchFilters;
  onFiltersChange: (filters: TraceSearchFilters) => void;
  onSearch: () => void;
  isLoading: boolean;
}

function TraceFilters({ filters, onFiltersChange, onSearch, isLoading }: TraceFiltersProps) {
  const formId = useId();
  const set = (field: keyof TraceSearchFilters, value: string) =>
    onFiltersChange({ ...filters, [field]: value || undefined });

  return (
    <form
      className="trace-filters"
      aria-label="Turn search filters"
      onSubmit={(event) => {
        event.preventDefault();
        onSearch();
      }}
    >
      <label className="trace-filter">
        <span className="trace-filter-label" id={`${formId}-since`}>
          From
        </span>
        <input
          type="datetime-local"
          aria-labelledby={`${formId}-since`}
          value={filters.since ?? ""}
          onChange={(event) => set("since", event.target.value)}
        />
      </label>
      <label className="trace-filter">
        <span className="trace-filter-label" id={`${formId}-until`}>
          Until
        </span>
        <input
          type="datetime-local"
          aria-labelledby={`${formId}-until`}
          value={filters.until ?? ""}
          onChange={(event) => set("until", event.target.value)}
        />
      </label>
      <label className="trace-filter">
        <span className="trace-filter-label" id={`${formId}-outcome`}>
          Outcome
        </span>
        <select
          aria-labelledby={`${formId}-outcome`}
          value={filters.outcome ?? ""}
          onChange={(event) => set("outcome", event.target.value)}
        >
          <option value="">Any</option>
          {OUTCOMES.map((outcome) => (
            <option key={outcome} value={outcome}>
              {OUTCOME_LABELS[outcome]}
            </option>
          ))}
        </select>
      </label>
      <label className="trace-filter">
        <span className="trace-filter-label" id={`${formId}-cause`}>
          Diagnosis cause
        </span>
        <select
          aria-labelledby={`${formId}-cause`}
          value={filters.cause ?? ""}
          onChange={(event) => set("cause", event.target.value)}
        >
          <option value="">Any</option>
          {DIAGNOSIS_CAUSES.map((cause) => (
            <option key={cause} value={cause}>
              {DIAGNOSIS_CAUSE_LABELS[cause]}
            </option>
          ))}
        </select>
      </label>
      <label className="trace-filter">
        <span className="trace-filter-label" id={`${formId}-status`}>
          Diagnosis status
        </span>
        <select
          aria-labelledby={`${formId}-status`}
          value={filters.diagnosisStatus ?? ""}
          onChange={(event) => set("diagnosisStatus", event.target.value)}
        >
          <option value="">Any</option>
          {DIAGNOSIS_STATUSES.map((status) => (
            <option key={status} value={status}>
              {DIAGNOSIS_STATUS_LABELS[status]}
            </option>
          ))}
        </select>
      </label>
      <label className="trace-filter trace-filter-wide">
        <span className="trace-filter-label" id={`${formId}-hash`}>
          Component-manifest hash
        </span>
        <input
          type="text"
          pattern="[0-9a-f]{64}"
          placeholder="64-character SHA-256"
          aria-labelledby={`${formId}-hash`}
          value={filters.manifestHash ?? ""}
          onChange={(event) => set("manifestHash", event.target.value)}
        />
      </label>
      <button type="submit" className="ghost-button" disabled={isLoading}>
        {isLoading ? "Searching…" : "Search turns"}
      </button>
    </form>
  );
}
