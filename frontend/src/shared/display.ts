/**
 * Small display helpers shared by the operator console's panels.
 *
 * These were re-implemented per panel and drifted; they now have one home.
 * `TraceExplorer` and `TraceDetail` are being aligned separately and should
 * import from here rather than re-deriving their own copies.
 */

import { relativeTime } from "src/admin/time";

/**
 * A `datetime-local` value (local wall clock) becomes the UTC ISO the API
 * expects, so a filter boundary means the same instant everywhere.
 */
export function toIso(localDateTime: string): string {
  if (/[zZ]|[+-]\d{2}:?\d{2}$/.test(localDateTime)) return localDateTime;
  const date = new Date(localDateTime);
  return Number.isNaN(date.getTime()) ? localDateTime : date.toISOString();
}

/**
 * An ISO timestamp as "3m ago". Backend audit, review, and knowledge
 * timestamps are ISO strings, unlike the chat queue's unix seconds; an absent
 * value renders as nothing and an unparseable one falls back to "—".
 */
export function relativeIsoTime(iso: string | null | undefined): string {
  if (!iso) return "";
  const seconds = new Date(iso).getTime() / 1000;
  return relativeTime(Number.isFinite(seconds) ? seconds : undefined);
}

/** Whether a diagnosis status must never be presented as a confirmed cause. */
export function isUncertainStatus(status: string | undefined): boolean {
  return status === "suspected" || status === "inconclusive";
}

/**
 * A principal id as a person would read it. Subjects that are already
 * human-readable (an operator label, a service identity) pass through; a raw
 * directory UUID shortens to its first segment, which is enough to tell two
 * operators apart without printing a 36-character id.
 */
export function shortSubject(subject: string | null | undefined): string {
  if (!subject) return "";
  return /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/.test(
    subject
  )
    ? subject.slice(0, 8)
    : subject;
}
