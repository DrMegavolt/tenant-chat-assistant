import { OUTCOME_LABELS, outcomeOf, type Outcome, type SessionSummary } from "src/admin/types";

/** Counts a dispatcher acts on, ordered by how urgent acting is. */
const TILES: { key: string; label: string; of(sessions: SessionSummary[]): number }[] = [
  { key: "active", label: "Live now", of: (rows) => rows.filter((row) => row.active).length },
  { key: "lead", label: "Leads", of: (rows) => sum(rows, (row) => row.leadCount ?? 0) },
  {
    key: "booked",
    label: "Booked",
    of: (rows) => rows.filter((row) => outcomeOf(row) === "booked").length
  },
  {
    key: "handoff",
    label: "Handoffs",
    of: (rows) => rows.filter((row) => outcomeOf(row) === "handoff").length
  },
  {
    key: "abandoned",
    label: "Abandoned",
    of: (rows) => rows.filter((row) => outcomeOf(row) === "abandoned").length
  },
  { key: "messages", label: "Messages", of: (rows) => sum(rows, (row) => row.messageCount ?? 0) }
];

function sum(rows: SessionSummary[], of: (row: SessionSummary) => number): number {
  return rows.reduce((total, row) => total + of(row), 0);
}

export function StatBar({ sessions }: { sessions: SessionSummary[] }) {
  return (
    <section className="stat-bar" aria-label="Chat volume summary" id="adminStats">
      {TILES.map((tile) => (
        <div key={tile.key} className={`stat-tile outcome-${tile.key}`}>
          <span>{tile.label}</span>
          <strong>{tile.of(sessions)}</strong>
        </div>
      ))}
    </section>
  );
}

export function OutcomeBadge({ outcome }: { outcome: Outcome }) {
  return (
    <span className={`outcome-badge outcome-${outcome}`}>
      <span aria-hidden="true" />
      {OUTCOME_LABELS[outcome]}
    </span>
  );
}
