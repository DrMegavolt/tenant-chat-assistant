import { useLayoutEffect, useRef, useState, type FormEvent, type ReactNode } from "react";

import { OutcomeBadge } from "src/admin/components/StatBar";
import { clockTime, isoTime, relativeTime } from "src/admin/time";
import {
  outcomeOf,
  type AdminMessage,
  type PendingConfirmation,
  type SessionDetail as Session
} from "src/admin/types";

function speaker(message: AdminMessage): string {
  if (message.source === "admin") return "Staff";
  if (message.source === "system" || message.role === "system") return "System";
  return message.role === "user" ? "Visitor" : "Assistant";
}

function Card({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="admin-card">
      <h2>{title}</h2>
      {children}
    </section>
  );
}

function Empty({ children }: { children: ReactNode }) {
  return <p className="muted-copy">{children}</p>;
}

/**
 * The lead line as the operator reads it. The domain files an urgency it could
 * not parse under "unknown", so rendering it unconditionally made the card read
 * "… · unknown" — as if the *service* had failed to resolve (N-07). The parsed
 * service string is what the lead carries, so it is what the card shows; a
 * known urgency is still reported alongside it.
 */
function leadServiceLine(lead: { service: string; urgency: string }): string {
  return lead.urgency === "unknown" ? lead.service : `${lead.service} · ${lead.urgency}`;
}

function PendingCard({ pending }: { pending: PendingConfirmation }) {
  const awaitingBooking = pending.awaiting === "booking_confirmation";
  return (
    <Card title="Pending confirmation">
      <div className="record-item">
        <strong>{pending.customerName}</strong>
        <span>
          {pending.service}
          {pending.slot ? ` · ${pending.slot}` : ""}
        </span>
        {pending.contact && <span>{pending.contact}</span>}
        {pending.address && <p>{pending.address}</p>}
        {pending.summary && <p>{pending.summary}</p>}
        <p className="muted-copy">
          {awaitingBooking
            ? "The visitor has not confirmed this appointment yet."
            : "The visitor has not confirmed this callback request yet."}
        </p>
      </div>
    </Card>
  );
}

function Transcript({ messages }: { messages: AdminMessage[] }) {
  const logRef = useRef<HTMLDivElement>(null);
  const pinnedRef = useRef(true);

  useLayoutEffect(() => {
    const log = logRef.current;
    if (log && pinnedRef.current) log.scrollTop = log.scrollHeight;
  }, [messages.length]);

  return (
    <div
      ref={logRef}
      className="admin-transcript"
      role="log"
      aria-label="Transcript"
      tabIndex={0}
      onScroll={(event) => {
        const log = event.currentTarget;
        pinnedRef.current = log.scrollHeight - log.scrollTop - log.clientHeight <= 40;
      }}
    >
      {messages.length === 0 && <Empty>This chat has no messages yet.</Empty>}
      {messages.map((message) => (
        <article
          key={message.id}
          className={`admin-message ${message.role} ${message.source ?? ""}`}
        >
          <span>{speaker(message)}</span>
          <p>{message.content}</p>
          <time dateTime={isoTime(message.createdAt)} title={isoTime(message.createdAt)}>
            {clockTime(message.createdAt)}
          </time>
        </article>
      ))}
    </div>
  );
}

function ReplyComposer({ onSend }: { onSend: (content: string) => Promise<void> }) {
  const [value, setValue] = useState("");
  const [isSending, setSending] = useState(false);

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const content = value.trim();
    if (!content || isSending) return;
    setSending(true);
    setValue("");
    await onSend(content);
    setSending(false);
  };

  return (
    <form className="admin-reply" onSubmit={(event) => void handleSubmit(event)}>
      <label className="visually-hidden" htmlFor="adminReply">
        Staff message
      </label>
      <input
        id="adminReply"
        name="message"
        autoComplete="off"
        placeholder="Send a staff message into this chat…"
        value={value}
        onChange={(event) => setValue(event.target.value)}
      />
      <button type="submit" disabled={isSending || !value.trim()}>
        Send
      </button>
    </form>
  );
}

export interface SessionDetailProps {
  session: Session | null;
  isLoading: boolean;
  onSendStaffMessage: (content: string) => Promise<void>;
}

export function SessionDetail({ session, isLoading, onSendStaffMessage }: SessionDetailProps) {
  if (!session) {
    return (
      <section className="admin-detail" aria-label="Selected chat">
        <div className="empty-state">
          <h2>{isLoading ? "Loading chats…" : "No chat selected"}</h2>
          <p>Open the website demo and send a message to create a live session.</p>
        </div>
      </section>
    );
  }

  const outcome = outcomeOf(session);

  return (
    <section className="admin-detail" aria-label="Selected chat">
      <div className="session-detail-header">
        <div>
          <p className="eyebrow">{session.tenantName}</p>
          <h2>{session.sessionId}</h2>
        </div>
        <div className="detail-status">
          {session.active ? (
            <span className="live-pill">
              <span aria-hidden="true" />
              Live
            </span>
          ) : (
            // "Live" and "Active" say the same thing; only one of them shows.
            <OutcomeBadge outcome={outcome} />
          )}
          {session.active && outcome !== "active" && <OutcomeBadge outcome={outcome} />}
          <span className="muted-copy">updated {relativeTime(session.updatedAt)}</span>
        </div>
      </div>

      <div className="admin-detail-grid">
        <section className="admin-card transcript-card">
          <h2>Transcript</h2>
          <Transcript messages={session.messages ?? []} />
          <ReplyComposer onSend={onSendStaffMessage} />
        </section>

        <aside className="admin-side-stack">
          {session.pending && <PendingCard pending={session.pending} />}

          <Card title="Bookings">
            {session.bookings?.length ? (
              session.bookings.map((booking, index) => (
                <div key={`${booking.customerName}-${index}`} className="record-item">
                  <strong>{booking.customerName}</strong>
                  <span>{booking.contact}</span>
                  <span>
                    {booking.service} · {booking.slot}
                  </span>
                  <p>{booking.address}</p>
                </div>
              ))
            ) : (
              <Empty>No booked appointments for this chat yet.</Empty>
            )}
          </Card>

          <Card title="Lead info">
            {session.leads?.length ? (
              session.leads.map((lead, index) => (
                <div key={`${lead.customerName}-${index}`} className="record-item">
                  <strong>{lead.customerName}</strong>
                  <span>{lead.contact}</span>
                  <span>{leadServiceLine(lead)}</span>
                  <p>{lead.summary}</p>
                </div>
              ))
            ) : (
              <Empty>No captured leads for this chat yet.</Empty>
            )}
          </Card>

          <Card title="Tool calls">
            {session.toolEvents?.length ? (
              // Newest first: the last call is the one being debugged.
              session.toolEvents
                .slice()
                .reverse()
                .map((event, index) => (
                  <div key={`${event.name}-${index}`} className="record-item tool">
                    <strong>{event.name}</strong>
                    <code>{JSON.stringify(event.result)}</code>
                  </div>
                ))
            ) : (
              <Empty>No tools called yet.</Empty>
            )}
          </Card>
        </aside>
      </div>
    </section>
  );
}
