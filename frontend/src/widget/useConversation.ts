/**
 * The visitor's side of one conversation: the visible transcript, the request
 * the backend is sent, and the timers that outlive a single turn.
 *
 * The hook is scoped to one tenant. `ChatWidget` is keyed by tenant id, so
 * switching tenants remounts it and this state starts clean rather than being
 * torn down field by field.
 *
 * The conversation is named by a server-issued visitor credential
 * (`POST /api/chat/session` mints it; every response reissues one, so an
 * active conversation never lets its credential expire), and a booking is not
 * committed until the visitor approves the assistant's proposed `pending`
 * confirmation.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import type { ChatApi } from "src/widget/api";
import { CredentialRejectedError, SourceUnavailableError } from "src/widget/api";
import type { ChatTurnResponse, SourceView, TenantConfig, TranscriptEntry } from "src/widget/types";
import type { VisitorData } from "src/widget/visitorData";

const POLL_INTERVAL_MS = 2500;
const PROACTIVE_DELAY_MS = 12000;

const EMAIL_PATTERN = /[\w.+-]+@[\w.-]+\.\w+/;
const PHONE_PATTERN = /(?:\+?1[\s.-]?)?(?:\(?\d{3}\)?[\s.-]?)\d{3}[\s.-]?\d{4}/;
const CREDENTIAL_PATTERN = /^tc\.v1\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/;

/** A server-issued visitor credential, or null for anything else. */
function isValidCredential(credential: string | null): credential is string {
  return credential !== null && CREDENTIAL_PATTERN.test(credential);
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
  /** How the visitor rated a turn, when they did (`FEAT-008`). */
  ratingFor: (turnId: string) => "up" | "down" | null;
  /** Rate one turn the assistant answered; a thumbs-down enqueues a review. */
  rate: (turnId: string, rating: "up" | "down", reason?: string) => Promise<void>;
  /**
   * Fetch the authorized view of a cited source for this conversation.
   *
   * @throws {SourceUnavailableError} when the credential is unusable or the
   * backend cannot answer the source for this tenant.
   */
  viewSource: (sourceId: string) => Promise<SourceView>;
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
  const [ratings, setRatings] = useState<Record<string, "up" | "down">>({});

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
  const hydratedRef = useRef(false);
  const proactiveTimer = useRef<number | null>(null);
  const proactiveShown = useRef(false);
  const isSendingRef = useRef(false);
  const credentialRef = useRef<string | null>(visitor.existingCredential());

  const clearProactiveTimer = useCallback(() => {
    if (proactiveTimer.current !== null) {
      window.clearTimeout(proactiveTimer.current);
      proactiveTimer.current = null;
    }
  }, []);

  useEffect(() => clearProactiveTimer, [clearProactiveTimer]);

  useEffect(() => {
    if (hydratedRef.current) return;
    hydratedRef.current = true;

    const stored = visitor.existingCredential();
    if (!isValidCredential(stored)) return;

    const hydrate = async () => {
      const snapshot = await api.session(stored);
      if (!snapshot) return;
      if ((!snapshot.messages || snapshot.messages.length === 0) && !snapshot.pending) {
        return;
      }

      credentialRef.current = snapshot.credential;
      visitor.recordCredential(snapshot.credential);

      const hydrated: TranscriptEntry[] = [];
      for (const msg of snapshot.messages ?? []) {
        seenServerMessageIds.current.add(msg.messageId);
        if (msg.role === "system" || msg.role === "tool") continue;

        hydrated.push({
          kind: "message",
          id: `hydrate-${msg.messageId}`,
          role: msg.role === "visitor" ? "user" : "assistant",
          source: msg.role === "staff" ? "admin" : msg.role === "visitor" ? "user" : "assistant",
          text: msg.content
        });
      }

      if (snapshot.pending) {
        hydrated.push({
          kind: "booking",
          id: nextId("booking"),
          pending: snapshot.pending
        });
      }

      setEntries(() => hydrated);
    };

    hydrate().catch(() => {
      /* Hydration failed; stay on the welcome entry. */
    });
  }, [api, setEntries, visitor]);

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
    // The reset announcement outlives the retry that follows it: clearing it
    // would hide from the visitor that their old conversation was discarded.
    setStatus((current) =>
      sending ? WAITING_STATUS : current === RESET_STATUS ? RESET_STATUS : ""
    );
  }, []);

  /**
   * Obtain (or reuse) the visitor credential for this tenant.
   *
   * A stored value the server never issued — the prototype's
   * `web-<tenant>-<ts>-<rand>` session id, for example — is not retried
   * forever: the API rejects it and this widget would only ever show the
   * failure bubble. Any value that is not a credential is discarded and a
   * fresh one is minted.
   */
  const ensureCredential = useCallback(async (): Promise<string> => {
    if (isValidCredential(credentialRef.current)) return credentialRef.current;
    const session = await api.openSession({ tenantId });
    visitor.recordCredential(session.credential);
    credentialRef.current = session.credential;
    return session.credential;
  }, [api, tenantId, visitor]);

  /** Replace the stored credential with the one a response reissued. */
  const refreshCredential = useCallback(
    (credential: string) => {
      credentialRef.current = credential;
      visitor.recordCredential(credential);
    },
    [visitor]
  );

  /** Append one turn's assistant output (tool events + reply/confirmation). */
  const applyTurn = useCallback(
    (payload: ChatTurnResponse) => {
      refreshCredential(payload.credential);
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
          text: payload.reply,
          ...(payload.turnId ? { turnId: payload.turnId } : {}),
          ...(payload.citations.length ? { citations: payload.citations } : {}),
          ...(payload.committed.length ? { actions: payload.committed } : {})
        });
      }
      setEntries((previous) =>
        appended.some((entry) => entry.kind === "booking")
          ? [...previous.filter((entry) => entry.kind !== "booking"), ...appended]
          : [...previous, ...appended]
      );
      scheduleProactiveNudge();
    },
    [refreshCredential, scheduleProactiveNudge, setEntries]
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

      const deliver = async (credential: string) => {
        const payload = await api.chat(credential, { message: text });
        applyTurn(payload);
      };

      try {
        const credential = await ensureCredential();
        await deliver(credential);
      } catch (error) {
        if (error instanceof CredentialRejectedError) {
          // The stored credential is gone or forged; discard it and start a
          // fresh conversation, then deliver the message once with the new one.
          visitor.clear();
          credentialRef.current = null;
          setStatus(RESET_STATUS);
          try {
            const fresh = await api.openSession({ tenantId });
            visitor.recordCredential(fresh.credential);
            credentialRef.current = fresh.credential;
            await deliver(fresh.credential);
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
          }
        } else {
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
        }
      } finally {
        setSending(false);
      }
    },
    [
      api,
      applyTurn,
      clearProactiveTimer,
      ensureCredential,
      setEntries,
      setSending,
      tenantId,
      visitor
    ]
  );

  /** Answer a booking the assistant proposed and is waiting on. */
  const decide = useCallback(
    async (decision: "approved" | "declined") => {
      if (isSendingRef.current) return;
      const credential = credentialRef.current;
      if (!isValidCredential(credential)) return;
      setSending(true);
      try {
        // Approving submits the visitor's name, address, and contact, so it is
        // also the moment consent is given (the confirmation gated the approve
        // button on the consent checkbox). The grant is recorded server-side
        // before the confirmation is submitted, because the backend refuses to
        // store a booking without it; the statement echoed back is the one the
        // tenant's policy published.
        if (decision === "approved") {
          const granted = await api.consent({
            credential,
            purposes: ["booking", "follow_up"]
          });
          visitor.recordConsent(granted.statement);
        }
        const payload = await api.confirm(credential, { decision });
        // The visitor has answered the pending question; drop the confirmation
        // card before showing what the answer produced.
        setEntries((previous) => previous.filter((entry) => entry.kind !== "booking"));
        applyTurn(payload);
      } catch (error) {
        if (error instanceof CredentialRejectedError) {
          // The conversation the stored credential names is gone; the pending
          // booking cannot be answered on a fresh session. Announce the reset
          // and drop the confirmation card so the visitor is not left facing a
          // decision nothing will ever answer.
          visitor.clear();
          credentialRef.current = null;
          setStatus(RESET_STATUS);
          setEntries((previous) => previous.filter((entry) => entry.kind !== "booking"));
        } else {
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
        }
      } finally {
        setSending(false);
      }
    },
    [api, applyTurn, setEntries, setSending, visitor]
  );

  /** How the visitor rated a turn, when they did. */
  const ratingFor = useCallback((turnId: string) => ratings[turnId] ?? null, [ratings]);

  /**
   * Fetch one cited source through the conversation's own credential.
   *
   * The source route rechecks tenant and audience authorization server-side, so
   * the widget's only job is to present whatever the server authorizes — and to
   * degrade without a trace of detail when it refuses.
   */
  const viewSource = useCallback(
    async (sourceId: string): Promise<SourceView> => {
      const credential = credentialRef.current;
      if (!isValidCredential(credential)) throw new SourceUnavailableError();
      return api.source(credential, sourceId);
    },
    [api]
  );

  /**
   * Rate one turn the assistant answered (`FEAT-008`). Only turns the visitor's
   * own conversation produced can be rated; the server enforces that. The
   * rating is best-effort: a network failure leaves the control available.
   */
  const rate = useCallback(
    async (turnId: string, rating: "up" | "down", reason?: string) => {
      const credential = credentialRef.current;
      if (!isValidCredential(credential)) return;
      try {
        await api.feedback(credential, {
          turnId,
          rating,
          ...(reason ? { reason } : {})
        });
        setRatings((current) => ({ ...current, [turnId]: rating }));
      } catch {
        // The visitor can retry; nothing here is worth a failure bubble.
      }
    },
    [api]
  );

  const forget = useCallback(() => {
    visitor.clear();
    seenServerMessageIds.current.clear();
    proactiveShown.current = false;
    credentialRef.current = null;
    clearProactiveTimer();
    setEntries(() => [welcomeEntry(config)]);
    setUnreadStaffCount(0);
    setRatings({});
    setStatus(RESET_STATUS);
  }, [clearProactiveTimer, config, setEntries, visitor]);

  const markRead = useCallback(() => setUnreadStaffCount(0), []);

  // Staff can answer from the operator console while the visitor is still on
  // the page, so an open conversation polls for replies it did not cause. Only
  // `staff` messages are new arrivals: model replies and the visitor's own
  // messages already rendered as part of the turn that produced them.
  useEffect(() => {
    const poll = async () => {
      const credential = visitor.existingCredential();
      if (!isValidCredential(credential)) return;
      const session = await api.session(credential);
      if (session) refreshCredential(session.credential);
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
  }, [api, isOpen, refreshCredential, setEntries, visitor]);

  return {
    entries,
    isSending,
    status,
    unreadStaffCount,
    ratingFor,
    rate,
    viewSource,
    send,
    decide,
    forget,
    markRead
  };
}
