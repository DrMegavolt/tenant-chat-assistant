/**
 * The visitor's side of one conversation: the visible transcript, the request
 * the backend is sent, and the timers that outlive a single turn.
 *
 * The hook is scoped to one tenant. `ChatWidget` is keyed by tenant id, so
 * switching tenants remounts it and this state starts clean rather than being
 * torn down field by field.
 *
 * The conversation is a single server-issued session (`POST /api/chat/session`
 * mints the ID; this hook only persists it), and a booking is not committed
 * until the visitor approves the assistant's proposed `pending` confirmation.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import type { ChatApi } from "src/widget/api";
import type { ChatTurnResponse, TenantConfig, TranscriptEntry } from "src/widget/types";
import type { VisitorData } from "src/widget/visitorData";
import { consentStatement } from "src/widget/visitorData";

const POLL_INTERVAL_MS = 2500;
const PROACTIVE_DELAY_MS = 12000;

const EMAIL_PATTERN = /[\w.+-]+@[\w.-]+\.\w+/;
const PHONE_PATTERN = /(?:\+?1[\s.-]?)?(?:\(?\d{3}\)?[\s.-]?)\d{3}[\s.-]?\d{4}/;
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** A server-issued session ID, or null for anything else. */
function isValidSessionId(id: string | null): id is string {
  return id !== null && UUID_PATTERN.test(id);
}

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

function hasContactInfo(entries: readonly TranscriptEntry[]): boolean {
  const text = entries
    .filter((entry) => entry.kind === "message" && entry.role === "user")
    .map((entry) => (entry.kind === "message" ? entry.text : ""))
    .join("\n");
  return EMAIL_PATTERN.test(text) || PHONE_PATTERN.test(text);
}

export interface Conversation {
  entries: readonly TranscriptEntry[];
  isSending: boolean;
  /** Transient text for assistive technology; never a transcript bubble. */
  status: string;
  /** Staff replies that arrived while the panel was closed. */
  unreadStaffCount: number;
  send: (text: string) => Promise<void>;
  /** Approve or decline a booking the assistant proposed and is waiting on. */
  decide: (decision: "approved" | "declined") => Promise<void>;
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
  const sessionRef = useRef<string | null>(visitor.existingSessionId());

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

  /**
   * Obtain (or reuse) the server-issued session ID for this tenant.
   *
   * A stored value the server never minted — the prototype's
   * `web-<tenant>-<ts>-<rand>` shape, for example — is not retried forever:
   * the API rejects it and this widget would only ever show the failure
   * bubble. Any value that is not a UUID is discarded and a fresh session is
   * minted.
   */
  const ensureSession = useCallback(async (): Promise<string> => {
    if (isValidSessionId(sessionRef.current)) return sessionRef.current;
    const session = await api.openSession({ tenantId });
    visitor.recordSession(session.sessionId);
    sessionRef.current = session.sessionId;
    return session.sessionId;
  }, [api, tenantId, visitor]);

  /** Append one turn's assistant output (tool events + reply/confirmation). */
  const applyTurn = useCallback(
    (payload: ChatTurnResponse) => {
      const appended: TranscriptEntry[] = [];
      if (payload.pending) {
        appended.push({
          kind: "booking",
          id: nextId("booking"),
          pending: payload.pending
        });
      } else if (payload.reply) {
        appended.push({
          kind: "message",
          id: nextId("assistant"),
          role: "assistant",
          source: "assistant",
          text: payload.reply
        });
      }
      setEntries((previous) =>
        appended.some((entry) => entry.kind === "booking")
          ? [...previous.filter((entry) => entry.kind !== "booking"), ...appended]
          : [...previous, ...appended]
      );
      scheduleProactiveNudge();
    },
    [scheduleProactiveNudge, setEntries]
  );

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
        const sessionId = await ensureSession();
        const payload = await api.chat({ tenantId, sessionId, message: text });
        applyTurn(payload);
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
    [api, applyTurn, clearProactiveTimer, ensureSession, setEntries, setSending, tenantId]
  );

  /** Answer a booking the assistant proposed and is waiting on. */
  const decide = useCallback(
    async (decision: "approved" | "declined") => {
      if (isSendingRef.current) return;
      const sessionId = sessionRef.current;
      if (!sessionId) return;
      setSending(true);
      try {
        // Approving submits the visitor's name, address, and contact, so it is
        // also the moment consent is given (the confirmation gated the approve
        // button on the consent checkbox).
        if (decision === "approved") {
          visitor.recordConsent(consentStatement(config.name));
        }
        const payload = await api.confirm({ tenantId, sessionId, decision });
        // The visitor has answered the pending question; drop the confirmation
        // card before showing what the answer produced.
        setEntries((previous) => previous.filter((entry) => entry.kind !== "booking"));
        applyTurn(payload);
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
    [api, applyTurn, config.name, setEntries, setSending, tenantId, visitor]
  );

  const forget = useCallback(() => {
    visitor.clear();
    seenServerMessageIds.current.clear();
    proactiveShown.current = false;
    sessionRef.current = null;
    clearProactiveTimer();
    setEntries(() => [welcomeEntry(config)]);
    setUnreadStaffCount(0);
    setStatus(RESET_STATUS);
  }, [clearProactiveTimer, config, setEntries, visitor]);

  const markRead = useCallback(() => setUnreadStaffCount(0), []);

  // Staff can answer from the operator console while the visitor is still on
  // the page, so an open conversation polls for replies it did not cause. Only
  // `staff` messages are new arrivals: model replies and the visitor's own
  // messages already rendered as part of the turn that produced them.
  useEffect(() => {
    const poll = async () => {
      const sessionId = visitor.existingSessionId();
      if (!isValidSessionId(sessionId)) return;
      const session = await api.session(sessionId, tenantId);
      const staff = (session?.messages ?? []).filter(
        (message) =>
          message.role === "staff" && !seenServerMessageIds.current.has(message.messageId)
      );
      if (!staff.length) return;
      for (const message of staff) seenServerMessageIds.current.add(message.messageId);
      setEntries((previous) => [
        ...previous,
        ...staff.map((message): TranscriptEntry => ({
          kind: "message",
          id: `staff-${message.messageId}`,
          role: "assistant",
          source: "admin",
          text: message.content
        }))
      ]);
      if (!isOpen) setUnreadStaffCount((count) => count + staff.length);
    };

    const timer = window.setInterval(() => void poll(), POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [api, isOpen, setEntries, tenantId, visitor]);

  return { entries, isSending, status, unreadStaffCount, send, decide, forget, markRead };
}
