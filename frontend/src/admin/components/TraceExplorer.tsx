import { useId, useState } from "react";

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
  type TraceSearchFilters,
  type TraceSearchRecord
} from "src/admin/traceTypes";

const EMPTY_FILTERS: TraceSearchFilters = {};

/** A `datetime-local` value (local wall clock) becomes the UTC ISO the API
 * expects, so a filter boundary means the same instant everywhere. */
function toIso(localDateTime: string): string {
  if (/[zZ]|[+-]\d{2}:?\d{2}$/.test(localDateTime)) return localDateTime;
  const date = new Date(localDateTime);
  return Number.isNaN(date.getTime()) ? localDateTime : date.toISOString();
}

/** Backend trace timestamps are ISO strings, unlike the queue's unix seconds. */
function relativeTraceTime(recordedAt: string): string {
  const seconds = new Date(recordedAt).getTime() / 1000;
  return relativeTime(Number.isFinite(seconds) ? seconds : undefined);
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
  const [selected, setSelected] = useState<TraceSearchRecord | null>(null);
  const [gold, setGold] = useState<GoldCase[]>([]);
  const [hasSearched, setHasSearched] = useState(false);
  const [isLoading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const runSearch = async (overrides?: TraceSearchFilters) => {
    const active = { ...filters, ...overrides };
    const wired: TraceSearchFilters = {};
    if (active.since) wired.since = toIso(active.since);
    if (active.until) wired.until = toIso(active.until);
    if (active.outcome) wired.outcome = active.outcome;
    if (active.cause) wired.cause = active.cause;
    if (active.diagnosisStatus) wired.diagnosisStatus = active.diagnosisStatus;
    if (active.manifestHash) wired.manifestHash = active.manifestHash;
    setLoading(true);
    setError(null);
    setSelected(null);
    try {
      setRecords(await api.searchTraces(tenantId, wired));
      setHasSearched(true);
    } catch {
      setError("Could not reach the trace surface. Retrying…");
    } finally {
      setLoading(false);
    }
  };

  const open = async (record: TraceSearchRecord) => {
    setSelected(record);
    try {
      const [detail, goldCases] = await Promise.all([
        api.trace(record.turnId, tenantId),
        api.goldCases(tenantId)
      ]);
      setGold(goldCases);
      if (detail) setSelected({ ...record, traceId: detail.traceId ?? record.traceId });
    } catch {
      setError("Could not open the turn record.");
    }
  };

  const switchingTenant = (next: string) => {
    setTenantId(next);
    setRecords([]);
    setHasSearched(false);
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

      {error && (
        <p className="admin-alert" role="alert">
          {error}
        </p>
      )}

      {hasSearched && records.length === 0 && (
        <p className="muted-copy">No turns match these filters.</p>
      )}

      {records.length > 0 && (
        <div className="trace-results" aria-label="Turn search results">
          {records.map((record) => (
            <button
              key={record.turnId}
              type="button"
              className={`session-item${selected?.turnId === record.turnId ? " selected" : ""}`}
              aria-current={selected?.turnId === record.turnId}
              onClick={() => void open(record)}
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
              <span className="session-meta mono">{record.componentManifestHash.slice(0, 12)}</span>
            </button>
          ))}
        </div>
      )}

      {selected && (
        <TraceDetail
          key={selected.turnId}
          api={api}
          tenantId={tenantId}
          record={selected}
          gold={gold}
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
          inputMode="numeric"
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
