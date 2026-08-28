import { describe, expect, test } from "vitest";

import { entriesFromMessages, mergeTranscript, serverEntryId } from "src/widget/transcript";
import type { ServerMessage, TranscriptEntry } from "src/widget/types";

const CREDENTIAL_RECORD = {
  messageId: "m2",
  role: "assistant" as const,
  content: "You're booked for tomorrow at nine.",
  createdAt: "2026-08-27T09:00:00Z"
};

function userMessage(text: string, id = "m1"): ServerMessage {
  return { messageId: id, role: "visitor", content: text, createdAt: "2026-08-27T08:59:00Z" };
}

function localMessage(
  text: string,
  role: "user" | "assistant",
  source: "user" | "assistant" | "admin"
): TranscriptEntry {
  return { kind: "message", id: `local-${text}`, role, source, text };
}

describe("mapping the session snapshot onto the visible transcript", () => {
  test("a bare transcript row renders as its bubble and nothing else", () => {
    const [entry] = entriesFromMessages([userMessage("My boiler is out.")]);
    expect(entry).toMatchObject({
      kind: "message",
      id: "server-m1",
      role: "user",
      source: "user",
      text: "My boiler is out."
    });
    expect(entry).not.toHaveProperty("citations");
    expect(entry).not.toHaveProperty("turnId");
  });

  test("a staff reply is attributed to staff, and runtime rows never render", () => {
    const entries = entriesFromMessages([
      userMessage("Anyone there?", "m1"),
      { messageId: "m2", role: "staff", content: "On my way.", createdAt: "2026-08-27T09:00:00Z" },
      {
        messageId: "m3",
        role: "system",
        content: "A member of the team joined.",
        createdAt: "2026-08-27T09:00:01Z"
      },
      { messageId: "m4", role: "tool", content: '{"ok":true}', createdAt: "2026-08-27T09:00:02Z" }
    ]);
    expect(entries.map((entry) => (entry.kind === "message" ? entry.source : ""))).toEqual([
      "user",
      "admin"
    ]);
  });

  test("an enriched row carries the citation chips, feedback target, and action notes a live turn would have", () => {
    // The resume contract: whatever the wire publishes beside the transcript
    // row reaches the bubble. A mapper that dropped these fields is why a
    // resumed conversation lost every chip and rating control it had shown
    // before the reload.
    const [entry] = entriesFromMessages([
      {
        ...CREDENTIAL_RECORD,
        turnId: "turn-7",
        citations: [
          {
            sourceId: "src-1",
            title: "Plan terms",
            sourceName: "Policies",
            location: "Maintenance",
            revision: 2,
            effectiveAt: "2026-07-01T00:00:00Z"
          }
        ],
        actions: [{ action: "book_appointment", reference: "booking-1", replayed: false }]
      }
    ]);
    expect(entry).toMatchObject({
      kind: "message",
      id: "server-m2",
      turnId: "turn-7",
      citations: [{ sourceId: "src-1", title: "Plan terms" }],
      actions: [{ action: "book_appointment", reference: "booking-1", replayed: false }]
    });
  });

  test("every mapped entry gets the stable server id, whichever path renders it", () => {
    expect(serverEntryId("abc")).toBe("server-abc");
  });
});

describe("merging a snapshot into the live transcript", () => {
  const WELCOME: TranscriptEntry = {
    kind: "message",
    id: "welcome",
    role: "assistant",
    source: "assistant",
    text: "Hi, I’m the assistant."
  };

  test("a message the visitor sent while the snapshot was loading survives", () => {
    // The snapshot fetch is asynchronous; the wholesale replace this merge
    // replaced deleted exactly such a message — the visitor watched their own
    // bubble vanish when hydration landed.
    const previous: TranscriptEntry[] = [
      WELCOME,
      localMessage("What are your hours?", "user", "user")
    ];
    const hydrated = entriesFromMessages([
      {
        messageId: "m1",
        role: "assistant",
        content: "We open at seven.",
        createdAt: "2026-08-26T09:00:00Z"
      }
    ]);

    const merged = mergeTranscript(previous, hydrated, null);

    expect(merged.map((entry) => (entry.kind === "message" ? entry.text : ""))).toEqual([
      "We open at seven.",
      "What are your hours?"
    ]);
  });

  test("a local copy of a message the server knows is replaced by the server's row, not duplicated", () => {
    const previous: TranscriptEntry[] = [
      WELCOME,
      localMessage("What are your hours?", "user", "user"),
      localMessage("We open at seven.", "assistant", "assistant")
    ];
    const hydrated = entriesFromMessages([
      userMessage("What are your hours?", "m1"),
      {
        messageId: "m2",
        role: "assistant",
        content: "We open at seven.",
        createdAt: "2026-08-26T09:01:00Z"
      }
    ]);

    const merged = mergeTranscript(previous, hydrated, null);

    expect(merged).toHaveLength(2);
    expect(merged.map((entry) => (entry.kind === "message" ? entry.id : ""))).toEqual([
      "server-m1",
      "server-m2"
    ]);
  });

  test("a reply still in flight when the snapshot was taken survives it", () => {
    // The snapshot races the turn: the POST is out but the answer is not
    // stored yet. The server transcript must not erase the reply when it
    // arrives a moment later through the turn response.
    const previous: TranscriptEntry[] = [
      localMessage("Is the duct unit serviceable?", "user", "user"),
      localMessage("Let me check availability.", "assistant", "assistant")
    ];
    const hydrated = entriesFromMessages([userMessage("Is the duct unit serviceable?", "m1")]);

    const merged = mergeTranscript(previous, hydrated, null);

    expect(merged.map((entry) => (entry.kind === "message" ? entry.text : ""))).toEqual([
      "Is the duct unit serviceable?",
      "Let me check availability."
    ]);
    expect(merged[0]).toMatchObject({ id: "server-m1" });
  });

  test("a real transcript replaces the greeting, and an empty one keeps it", () => {
    const previous: TranscriptEntry[] = [WELCOME];
    const hydrated = entriesFromMessages([userMessage("Hello", "m1")]);
    expect(mergeTranscript(previous, hydrated, null)).toHaveLength(1);
    expect(mergeTranscript(previous, [], null)).toEqual([WELCOME]);
  });

  test("the confirmation card the server reports is restored, and a local one is replaced", () => {
    const previous: TranscriptEntry[] = [
      WELCOME,
      {
        kind: "booking",
        id: "booking-1",
        pending: {
          awaiting: "booking_confirmation",
          service: "HVAC",
          slot: "Tomorrow 09:00",
          customerName: "Dana",
          address: ""
        }
      }
    ];
    const pending = {
      awaiting: "lead_confirmation",
      service: "HVAC",
      slot: "",
      customerName: "Dana",
      address: "",
      contact: "dana@example.com",
      summary: "Furnace noise"
    };

    const merged = mergeTranscript(previous, [], pending);

    expect(merged).toHaveLength(2);
    expect(merged.at(-1)).toMatchObject({ kind: "booking", pending });
  });

  test("the same inputs always merge to the same transcript", () => {
    // Resume used to be non-deterministic: bubbles vanished depending on which
    // in-flight write won. The merge is a pure function of what the visitor
    // sees and what the server returned.
    const previous: TranscriptEntry[] = [
      WELCOME,
      localMessage("One radiator is cold.", "user", "user"),
      localMessage("A technician is on the way.", "assistant", "admin")
    ];
    const hydrated = entriesFromMessages([
      userMessage("One radiator is cold.", "m1"),
      {
        messageId: "m2",
        role: "staff",
        content: "A technician is on the way.",
        createdAt: "2026-08-26T09:02:00Z"
      }
    ]);

    const first = mergeTranscript(previous, hydrated, null);
    const second = mergeTranscript(previous, hydrated, null);
    expect(first).toEqual(second);
    expect(first).toHaveLength(2);
  });

  test("a question re-sent while the snapshot loads is not consumed by the historical row of the same text", () => {
    // The visitor repeats a question that is already in the stored history
    // while the snapshot is in flight. Matching from the front bound the
    // re-send to that older row, so the transcript showed the text once, at
    // its historical position — one bubble short. Matching newest-first binds
    // the newest local copy to the newest server row, and a local the snapshot
    // predates always survives.
    const previous: TranscriptEntry[] = [
      WELCOME,
      localMessage("What are your hours?", "user", "user")
    ];
    const hydrated = entriesFromMessages([
      userMessage("What are your hours?", "m1"),
      {
        messageId: "m2",
        role: "assistant",
        content: "We open at seven.",
        createdAt: "2026-08-26T09:00:00Z"
      }
    ]);

    const merged = mergeTranscript(previous, hydrated, null);

    expect(merged.map((entry) => (entry.kind === "message" ? entry.text : ""))).toEqual([
      "What are your hours?",
      "We open at seven.",
      "What are your hours?"
    ]);
    // The survivor is the re-send, appended where the visitor typed it.
    expect(merged.at(-1)).toMatchObject({ id: "local-What are your hours?" });
  });

  test("two local copies of one text bind newest-first, and the surplus send survives", () => {
    // A double-tapped send button leaves two identical local bubbles; the
    // snapshot holds only the first send. One local binds the server row and
    // the other survives, so the merge counts both sends — the stored row,
    // then the copy the snapshot predates.
    const previous: TranscriptEntry[] = [
      WELCOME,
      localMessage("Anyone there?", "user", "user"),
      localMessage("Anyone there?", "user", "user")
    ];
    const hydrated = entriesFromMessages([userMessage("Anyone there?", "m1")]);

    const merged = mergeTranscript(previous, hydrated, null);

    expect(merged.map((entry) => (entry.kind === "message" ? entry.id : ""))).toEqual([
      "server-m1",
      "local-Anyone there?"
    ]);
  });
});
