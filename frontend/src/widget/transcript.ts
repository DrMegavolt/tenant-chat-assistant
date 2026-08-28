/**
 * Rebuilding the visible transcript from the server's snapshot.
 *
 * Two contracts live here. First, hydration must never destroy what the
 * visitor can already see: the snapshot fetch is asynchronous, and a message
 * typed while it is in flight is a real message the server may not have yet.
 * Second, a resumed transcript must carry whatever enrichment the wire rows
 * publish — the turn id a rating targets, the citations an answer was grounded
 * in, the actions it committed — exactly as the live turn that produced them
 * did, instead of collapsing every row to bare text.
 *
 * The merge is a pure function of its inputs: the same previous transcript and
 * the same snapshot always produce the same output.
 */

import type { PendingBooking, ServerMessage, TranscriptEntry } from "src/widget/types";

/** The id every server-known message entry carries, from any path that renders it. */
export function serverEntryId(messageId: string): string {
  return `server-${messageId}`;
}

/**
 * Map the snapshot's transcript rows onto visible entries.
 *
 * System and tool rows are runtime bookkeeping, never bubbles. Enrichment is
 * copied through when the row carries it and omitted when it does not — a
 * backend that sends bare rows still gets a complete transcript, and one that
 * sends the enrichments gets chips, feedback controls, and action notes.
 */
export function entriesFromMessages(messages: readonly ServerMessage[]): TranscriptEntry[] {
  const entries: TranscriptEntry[] = [];
  for (const message of messages) {
    if (message.role === "system" || message.role === "tool") continue;
    entries.push({
      kind: "message",
      id: serverEntryId(message.messageId),
      role: message.role === "visitor" ? "user" : "assistant",
      source:
        message.role === "staff" ? "admin" : message.role === "visitor" ? "user" : "assistant",
      text: message.content,
      ...(message.turnId ? { turnId: message.turnId } : {}),
      ...(message.citations?.length ? { citations: message.citations } : {}),
      ...(message.actions?.length ? { actions: message.actions } : {})
    });
  }
  return entries;
}

function isSameMessage(local: TranscriptEntry, server: TranscriptEntry): boolean {
  if (local.kind !== "message" || server.kind !== "message") return false;
  return local.role === server.role && local.source === server.source && local.text === server.text;
}

/**
 * Merge the in-flight transcript with the snapshot the server just returned.
 *
 * The server's rows are authoritative for every message they contain: a local
 * copy of the same message is dropped in favour of the server's, which carries
 * the stable id and any enrichment. Matching walks both sequences from the
 * end — newest server row first, newest local copy first — because matching
 * from the front would let an older historical row consume a local re-send of
 * the same text (a visitor repeating a question while the snapshot is in
 * flight), showing the text once at its historical position and silently
 * dropping the repeat. Locals the snapshot does not know about — a message
 * typed during the fetch, a turn still being written, a repeat beyond the rows
 * the server has stored — survive untouched, appended after the server's rows
 * in the order the visitor created them. The greeting and any confirmation
 * card are replaced wholesale by what the server reports.
 */
export function mergeTranscript(
  previous: readonly TranscriptEntry[],
  hydrated: readonly TranscriptEntry[],
  pending: PendingBooking | null
): TranscriptEntry[] {
  // A non-empty server transcript supersedes the greeting: the conversation
  // it belongs to already said whatever the greeting was placeholder for.
  const local = previous.filter((entry) =>
    entry.id === "welcome" ? hydrated.length === 0 : entry.kind !== "booking"
  );
  const matched = new Set<TranscriptEntry>();
  let candidate = local.length - 1;
  for (let index = hydrated.length - 1; index >= 0; index -= 1) {
    const server = hydrated[index]!;
    while (candidate >= 0) {
      const localEntry = local[candidate]!;
      candidate -= 1;
      if (isSameMessage(localEntry, server)) {
        matched.add(localEntry);
        break;
      }
    }
  }
  const merged: TranscriptEntry[] = [...hydrated];
  for (const entry of local) {
    if (!matched.has(entry)) merged.push(entry);
  }
  if (pending) {
    merged.push({ kind: "booking", id: `pending-${pending.awaiting}`, pending });
  }
  return merged;
}
