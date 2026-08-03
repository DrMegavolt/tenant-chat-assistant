/**
 * The visitor's side of one conversation: the visible transcript, the request
 * the backend is sent, and the timers that outlive a single turn.
 *
 * The hook is scoped to one tenant. `ChatWidget` is keyed by tenant id, so
 * switching tenants remounts it and this state starts clean rather than being
 * torn down field by field.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import type { ChatApi } from "src/widget/api";
import type {
  BookingContact,
  ChatTurn,
  TenantConfig,
  ToolEvent,
  TranscriptEntry
} from "src/widget/types";
import type { VisitorData } from "src/widget/visitorData";
import { consentStatement } from "src/widget/visitorData";

const POLL_INTERVAL_MS = 2500;
const PROACTIVE_DELAY_MS = 12000;

const EMAIL_PATTERN = /[\w.+-]+@[\w.-]+\.\w+/;
const PHONE_PATTERN = /(?:\+?1[\s.-]?)?(?:\(?\d{3}\)?[\s.-]?)\d{3}[\s.-]?\d{4}/;

const WAITING_STATUS = "Waiting for the assistant to reply.";
const RESET_STATUS = "This conversation was deleted from your browser and a new one has started.";
const CHAT_FAILURE = "I could not reach the chat service just now. Please try again in a moment.";

let entrySequence = 0;
const nextId = (prefix: string) => `${prefix}-${(entrySequence += 1)}`;

/** The opening turn, which states what this assistant is allowed to do. */
function welcomeEntry(config: TenantConfig): TranscriptEntry {
  const routing = config.bookingEnabled
    ? "help find appointment slots"
    : `connect you with the team at ${config.phone}`;
  return {
    kind: "message",
    id: nextId("welcome"),
    role: "assistant",
    source: "assistant",
    text:
      `Hi, I’m the ${config.assistantName}. I can answer questions about ${config.name}, ` +
      `check whether a ZIP code is served, ${routing}, and create a follow-up lead.`
  };
}

function toTurns(entries: readonly TranscriptEntry[]): ChatTurn[] {
  return entries
    .filter((entry) => entry.kind === "message")
    .map((entry) => ({ role: entry.role, content: entry.text, source: entry.source }));
}

function hasContactInfo(entries: readonly TranscriptEntry[]): boolean {
  const text = toTurns(entries)
    .map((turn) => turn.content)
    .join("\n");
  return EMAIL_PATTERN.test(text) || PHONE_PATTERN.test(text);
}

/** A `get_availability` result the tenant is allowed to turn into a booking. */
function bookableSlots(event: ToolEvent, config: TenantConfig): string[] | null {
  if (event.name !== "get_availability" || !config.bookingEnabled) return null;
  const slots = event.result.slots;
  if (typeof event.result.service !== "string" || !Array.isArray(slots) || !slots.length) {
    return null;
  }
  return slots.filter((slot): slot is string => typeof slot === "string");
}

export interface BookingOutcome {
  ok: boolean;
  message?: string;
}

export interface Conversation {
  entries: readonly TranscriptEntry[];
  isSending: boolean;
  /** Transient text for assistive technology; never a transcript bubble. */
  status: string;
  /** Staff replies that arrived while the panel was closed. */
  unreadStaffCount: number;
  send: (text: string) => Promise<void>;
  book: (
    entryId: string,
    request: BookingContact & { service: string; slot: string }
  ) => Promise<BookingOutcome>;
  /** Erase this tenant's browser data and start a fresh conversation. */
  forget: () => void;
  markRead: () => void;
}

export interface ConversationOptions {
  api: ChatApi;
  tenantId: string;
  config: TenantConfig;
  visitor: VisitorData;
  isOpen: boolean;
}

export function useConversation({
  api,
  tenantId,
  config,
  visitor,
  isOpen
}: ConversationOptions): Conversation {
  const [entries, setEntriesState] = useState<TranscriptEntry[]>(() => [welcomeEntry(config)]);
  const [isSending, setIsSending] = useState(false);
  const [status, setStatus] = useState("");
  const [unreadStaffCount, setUnreadStaffCount] = useState(0);

  // A turn has to post the transcript *including* the message just typed, so
  // the sender reads the list it is extending rather than the last render's.
  const entriesRef = useRef(entries);
  const setEntries = useCallback(
    (update: (previous: readonly TranscriptEntry[]) => TranscriptEntry[]) => {
      entriesRef.current = update(entriesRef.current);
      setEntriesState(entriesRef.current);
    },
    []
  );

  const seenServerMessageIds = useRef(new Set<string>());
  const proactiveTimer = useRef<number | null>(null);
  const proactiveShown = useRef(false);
  const isSendingRef = useRef(false);

  const clearProactiveTimer = useCallback(() => {
    if (proactiveTimer.current !== null) {
      window.clearTimeout(proactiveTimer.current);
      proactiveTimer.current = null;
    }
  }, []);

  useEffect(() => clearProactiveTimer, [clearProactiveTimer]);

  const scheduleProactiveNudge = useCallback(() => {
    if (!config.proactiveLeadCapture || proactiveShown.current) return;
    if (hasContactInfo(entriesRef.current)) return;
    if (!entriesRef.current.some((entry) => entry.kind === "message" && entry.role === "user")) {
      return;
    }
    clearProactiveTimer();
    proactiveTimer.current = window.setTimeout(() => {
      if (isSendingRef.current || proactiveShown.current) return;
      if (hasContactInfo(entriesRef.current)) return;
      proactiveShown.current = true;
      setEntries((previous) => [
        ...previous,
        {
          kind: "message",
          id: nextId("proactive"),
          role: "assistant",
          source: "proactive",
          text:
            "If it helps, I can have the team follow up. Send your name and phone or email, " +
            "and I’ll create a callback request."
        }
      ]);
    }, PROACTIVE_DELAY_MS);
  }, [clearProactiveTimer, config.proactiveLeadCapture, setEntries]);

  const setSending = useCallback((sending: boolean) => {
    isSendingRef.current = sending;
    setIsSending(sending);
    setStatus(sending ? WAITING_STATUS : "");
  }, []);

  const send = useCallback(
    async (rawText: string) => {
      const text = rawText.trim();
      if (!text || isSendingRef.current) return;

      clearProactiveTimer();
      setEntries((previous) => [
        ...previous,
        { kind: "message", id: nextId("user"), role: "user", source: "user", text }
      ]);
      setSending(true);

      try {
        const payload = await api.chat({
          tenantId,
          sessionId: visitor.sessionId(),
          messages: toTurns(entriesRef.current)
        });
        const appended: TranscriptEntry[] = [];
        for (const event of payload.toolEvents ?? []) {
          appended.push({ kind: "tool", id: nextId("tool"), event });
          const slots = bookableSlots(event, config);
          if (slots) {
            appended.push({
              kind: "booking",
              id: nextId("booking"),
              service: event.result.service as string,
              slots
            });
          }
        }
        appended.push({
          kind: "message",
          id: nextId("assistant"),
          role: "assistant",
          source: "assistant",
          text: payload.reply
        });
        // Only one booking form is open at a time: an older one offers slots the
        // backend has already moved past.
        setEntries((previous) =>
          appended.some((entry) => entry.kind === "booking")
            ? [...previous.filter((entry) => entry.kind !== "booking"), ...appended]
            : [...previous, ...appended]
        );
        scheduleProactiveNudge();
      } catch {
        setEntries((previous) => [
          ...previous,
          {
            kind: "message",
            id: nextId("assistant"),
            role: "assistant",
            source: "assistant",
            text: CHAT_FAILURE
          }
        ]);
      } finally {
        setSending(false);
      }
    },
    [
      api,
      clearProactiveTimer,
      config,
      scheduleProactiveNudge,
      setEntries,
      setSending,
      tenantId,
      visitor
    ]
  );

  const book = useCallback(
    async (entryId: string, request: BookingContact & { service: string; slot: string }) => {
      const consent = visitor.recordConsent(consentStatement(config.name));
      const { customerName, address, contact, service, slot } = request;
      const result = await api.book({
        tenantId,
        sessionId: visitor.sessionId(),
        service,
        slot,
        customerName,
        address,
        contact,
        consent
      });

      if (!result.ok) {
        return { ok: false, message: bookingErrorMessage(result.payload) };
      }

      visitor.rememberContact({ customerName, address, contact });
      setEntries((previous) => [
        ...previous.filter((entry) => entry.id !== entryId),
        { kind: "tool", id: nextId("tool"), event: result.payload.toolEvent },
        {
          kind: "message",
          id: nextId("assistant"),
          role: "assistant",
          source: "assistant",
          text: result.payload.reply
        }
      ]);
      return { ok: true };
    },
    [api, config.name, setEntries, tenantId, visitor]
  );

  const forget = useCallback(() => {
    visitor.clear();
    seenServerMessageIds.current.clear();
    proactiveShown.current = false;
    clearProactiveTimer();
    setEntries(() => [welcomeEntry(config)]);
    setUnreadStaffCount(0);
    setStatus(RESET_STATUS);
  }, [clearProactiveTimer, config, setEntries, visitor]);

  const markRead = useCallback(() => setUnreadStaffCount(0), []);

  // Staff can answer from the operator console while the visitor is still on
  // the page, so an open conversation polls for replies it did not cause.
  useEffect(() => {
    const poll = async () => {
      const sessionId = visitor.existingSessionId();
      if (!sessionId) return;
      const session = await api.session(sessionId);
      const staff = (session?.messages ?? []).filter(
        (message) => message.source === "admin" && !seenServerMessageIds.current.has(message.id)
      );
      if (!staff.length) return;
      for (const message of staff) seenServerMessageIds.current.add(message.id);
      setEntries((previous) => [
        ...previous,
        ...staff.map((message): TranscriptEntry => ({
          kind: "message",
          id: `staff-${message.id}`,
          role: "assistant",
          source: "admin",
          text: message.content
        }))
      ]);
      if (!isOpen) setUnreadStaffCount((count) => count + staff.length);
    };

    const timer = window.setInterval(() => void poll(), POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [api, isOpen, setEntries, visitor]);

  return { entries, isSending, status, unreadStaffCount, send, book, forget, markRead };
}

function bookingErrorMessage(payload: {
  toolEvent?: { result?: { message?: string; error?: string; missingFields?: string[] } };
}): string {
  const result = payload.toolEvent?.result ?? {};
  if (result.message) return result.message;
  if (result.error === "missing_required_fields") {
    return `Missing: ${(result.missingFields ?? []).join(", ")}.`;
  }
  if (result.error === "slot_unavailable") {
    return "That slot is no longer available. Please choose another slot.";
  }
  return "Booking failed. Please check the form and try again.";
}
