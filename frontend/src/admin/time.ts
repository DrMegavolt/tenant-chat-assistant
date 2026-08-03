/** Timestamp formatting for the console. Backend timestamps are unix seconds. */

const relative = new Intl.RelativeTimeFormat(undefined, { numeric: "auto", style: "narrow" });
const clock = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" });

const UNITS: [Intl.RelativeTimeFormatUnit, number][] = [
  ["day", 86400],
  ["hour", 3600],
  ["minute", 60]
];

export function toDate(seconds: number | undefined): Date | null {
  return seconds ? new Date(seconds * 1000) : null;
}

export function clockTime(seconds: number | undefined): string {
  const date = toDate(seconds);
  return date ? clock.format(date) : "—";
}

export function isoTime(seconds: number | undefined): string {
  return toDate(seconds)?.toISOString() ?? "";
}

/**
 * "3m ago" rather than a wall-clock time.
 *
 * A dispatcher scanning the queue needs to know how stale a conversation is,
 * which is a duration; the exact time stays available as the element's title.
 */
export function relativeTime(seconds: number | undefined, now: number = Date.now()): string {
  const date = toDate(seconds);
  if (!date) return "—";
  const elapsed = Math.round((date.getTime() - now) / 1000);
  for (const [unit, size] of UNITS) {
    if (Math.abs(elapsed) >= size) return relative.format(Math.round(elapsed / size), unit);
  }
  return relative.format(0, "second");
}
