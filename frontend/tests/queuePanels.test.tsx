import { render, screen, within } from "@testing-library/react";
import { describe, expect, test } from "vitest";

import { SessionDetail } from "src/admin/components/SessionDetail";
import { SessionList } from "src/admin/components/SessionList";
import { StatBar } from "src/admin/components/StatBar";
import type { SessionDetail as SessionDetailData, SessionSummary } from "src/admin/types";

const QUEUE_ROW: SessionSummary = {
  sessionId: "session-1",
  tenantName: "Apex Home Services",
  active: true,
  status: "active",
  outcome: "lead",
  messageCount: 21,
  leadCount: 1,
  lastMessage: { content: "Someone will call you within the hour." },
  updatedAt: 1_787_000_000
};

const BARE_ROW: SessionSummary = {
  sessionId: "session-2",
  tenantName: "Clearview Heating",
  active: false,
  status: "closed",
  outcome: "completed",
  updatedAt: 1_787_000_000
};

describe("the chat queue's honest numbers", () => {
  test("a row shows the last message and the message and lead counts the response carries", () => {
    render(<SessionList sessions={[QUEUE_ROW]} selectedId={null} onSelect={() => {}} />);

    const row = screen.getByRole("button", { name: /Apex Home Services/ });
    // The search box advertises last-message search; the row has to show the
    // field that search runs on.
    expect(row.textContent).toContain("Someone will call you within the hour.");
    expect(row.textContent).toContain("21 messages");
    expect(row.textContent).toContain("1 leads");
  });

  test("a row without those fields degrades to its placeholders instead of invented zeros", () => {
    render(<SessionList sessions={[BARE_ROW]} selectedId={null} onSelect={() => {}} />);

    const row = screen.getByRole("button", { name: /Clearview Heating/ });
    expect(row.textContent).toContain("Open to read the transcript");
    expect(row.textContent).not.toContain("messages");
    expect(row.textContent).not.toContain("leads");
  });

  test("the stat strip sums the counts the queue actually carries", () => {
    // LEADS 0 · MESSAGES 0 next to a 21-message transcript with a captured
    // lead was the strip lying; the tiles read the real fields now.
    const { container } = render(
      <StatBar sessions={[QUEUE_ROW, { ...QUEUE_ROW, sessionId: "session-3", leadCount: 0 }]} />
    );
    const strip = within(container.querySelector("#adminStats")!);
    expect(strip.getByText("Messages").nextElementSibling?.textContent).toBe("42");
    expect(strip.getByText("Leads").nextElementSibling?.textContent).toBe("1");
  });
});

describe("the chat detail's side cards", () => {
  test("a pending confirmation is shown while the visitor has not answered it", () => {
    const session: SessionDetailData = {
      ...QUEUE_ROW,
      pending: {
        awaiting: "booking_confirmation",
        service: "HVAC",
        slot: "Tomorrow 09:00",
        customerName: "Dana Ruiz",
        address: "12 Alder Court",
        contact: "",
        summary: ""
      }
    };
    render(
      <SessionDetail
        session={session}
        isLoading={false}
        onSendStaffMessage={() => Promise.resolve()}
      />
    );

    const card = screen.getByRole("heading", { name: "Pending confirmation" }).parentElement!;
    expect(card.textContent).toContain("Dana Ruiz");
    expect(card.textContent).toContain("Tomorrow 09:00");
    expect(card.textContent).toContain("has not confirmed this appointment yet");
  });

  test("an unanswered lead request names itself, not a booking", () => {
    const session: SessionDetailData = {
      ...QUEUE_ROW,
      pending: {
        awaiting: "lead_confirmation",
        service: "HVAC",
        slot: "",
        customerName: "Dana Ruiz",
        address: "",
        contact: "dana@example.com",
        summary: "Furnace is making a grinding noise."
      }
    };
    render(
      <SessionDetail
        session={session}
        isLoading={false}
        onSendStaffMessage={() => Promise.resolve()}
      />
    );

    const card = screen.getByRole("heading", { name: "Pending confirmation" }).parentElement!;
    expect(card.textContent).toContain("dana@example.com");
    expect(card.textContent).toContain("has not confirmed this callback request yet");
  });

  test("leads, bookings, and tool calls render from the response, and stay honest when it carries none", () => {
    const session: SessionDetailData = {
      ...QUEUE_ROW,
      leads: [
        {
          customerName: "Dana Ruiz",
          contact: "dana@example.com",
          service: "HVAC",
          urgency: "today",
          summary: "Furnace is making a grinding noise."
        }
      ],
      bookings: [],
      toolEvents: [{ name: "check_availability", result: { slots: 2 } }]
    };
    render(
      <SessionDetail
        session={session}
        isLoading={false}
        onSendStaffMessage={() => Promise.resolve()}
      />
    );

    expect(screen.getByText("Dana Ruiz")).toBeTruthy();
    expect(screen.getByText("check_availability")).toBeTruthy();
    expect(screen.getByText("No booked appointments for this chat yet.")).toBeTruthy();
  });

  test("a lead whose urgency was never parsed shows its service, not a dangling unknown", () => {
    // N-07, live-observed: the domain files an urgency it could not parse
    // under "unknown", and rendering it unconditionally made the card read
    // "HVAC repair - AC not cooling · unknown" — as if the service had failed
    // to resolve. The parsed service string is the information the lead
    // carries, so that is what shows.
    const session: SessionDetailData = {
      ...QUEUE_ROW,
      leads: [
        {
          customerName: "Jane Tester",
          contact: "jane@example.com",
          service: "HVAC repair - AC not cooling",
          urgency: "unknown",
          summary: "The AC is not cooling."
        }
      ],
      bookings: [],
      toolEvents: []
    };
    render(
      <SessionDetail
        session={session}
        isLoading={false}
        onSendStaffMessage={() => Promise.resolve()}
      />
    );

    const card = screen.getByRole("heading", { name: "Lead info" }).parentElement!;
    expect(card.textContent).toContain("HVAC repair - AC not cooling");
    expect(card.textContent).not.toContain("unknown");
  });

  test("a lead with a parsed urgency still reports it beside the service", () => {
    const session: SessionDetailData = {
      ...QUEUE_ROW,
      leads: [
        {
          customerName: "Dana Ruiz",
          contact: "dana@example.com",
          service: "HVAC",
          urgency: "today",
          summary: "Furnace is making a grinding noise."
        }
      ],
      bookings: [],
      toolEvents: []
    };
    render(
      <SessionDetail
        session={session}
        isLoading={false}
        onSendStaffMessage={() => Promise.resolve()}
      />
    );

    const card = screen.getByRole("heading", { name: "Lead info" }).parentElement!;
    expect(card.textContent).toContain("HVAC · today");
  });

  test("a detail without the side fields shows the empty states, not fabricated cards", () => {
    render(
      <SessionDetail
        session={QUEUE_ROW}
        isLoading={false}
        onSendStaffMessage={() => Promise.resolve()}
      />
    );

    expect(screen.getByText("No booked appointments for this chat yet.")).toBeTruthy();
    expect(screen.getByText("No captured leads for this chat yet.")).toBeTruthy();
    expect(screen.getByText("No tools called yet.")).toBeTruthy();
    expect(screen.queryByRole("heading", { name: "Pending confirmation" })).toBeNull();
  });
});
