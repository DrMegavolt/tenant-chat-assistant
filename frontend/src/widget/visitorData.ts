/**
 * Per-tenant visitor storage and consent record.
 *
 * Everything here lives in the visitor's browser for the length of the tab.
 * Server-side consent records, retention, export, and erasure are `PRIV-001`;
 * this module owns only what the widget itself keeps and what it lets the
 * visitor delete.
 */

import type { BookingContact, ConsentRecord } from "src/widget/types";

const SESSION_KEY = "tenant-chat-session-id";
const CONSENT_KEY = "tenant-chat-consent";
const CONTACT_KEY = "tenant-chat-contact";

/** The subset of the Storage interface this module uses. */
export interface VisitorStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

/**
 * Return `window.sessionStorage`, or an equivalent in-memory shim when the
 * embedding page blocks storage. A third-party embed in a browser configured to
 * reject storage must still be able to hold a conversation.
 */
export function safeStorage(): VisitorStorage {
  try {
    const probe = "tenant-chat-probe";
    window.sessionStorage.setItem(probe, "1");
    window.sessionStorage.removeItem(probe);
    return window.sessionStorage;
  } catch {
    const values = new Map<string, string>();
    return {
      getItem: (key) => values.get(key) ?? null,
      setItem: (key, value) => void values.set(key, value),
      removeItem: (key) => void values.delete(key)
    };
  }
}

/** The sentence a visitor agrees to before contact details are submitted. */
export function consentStatement(tenantName: string): string {
  return (
    `I agree that ${tenantName} may store the name, address, and contact ` +
    `details I enter here in order to arrange this appointment and follow up ` +
    `about it.`
  );
}

function parse<T>(raw: string | null): T | null {
  if (!raw) return null;
  try {
    return JSON.parse(raw) as T;
  } catch {
    return null;
  }
}

export class VisitorData {
  readonly tenantId: string;
  private readonly storage: VisitorStorage;

  constructor(tenantId: string, storage: VisitorStorage = safeStorage()) {
    this.tenantId = tenantId;
    this.storage = storage;
  }

  private key(prefix: string): string {
    return `${prefix}:${this.tenantId}`;
  }

  /**
   * The conversation id already issued to this visitor, without issuing one.
   *
   * Background work — polling for staff replies — must use this rather than
   * `sessionId()`: a visitor who opened the page and typed nothing has not
   * started a conversation, and writing an id for them would leave a
   * identifier in their browser they never asked for.
   */
  existingSessionId(): string | null {
    return this.storage.getItem(this.key(SESSION_KEY));
  }

  /** Stable per-tenant conversation id, created on first use. */
  sessionId(): string {
    const existing = this.existingSessionId();
    if (existing) return existing;
    const value = `web-${this.tenantId}-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    this.storage.setItem(this.key(SESSION_KEY), value);
    return value;
  }

  hasConsent(): boolean {
    return this.consent() !== null;
  }

  /** The stored consent record, or null when the visitor has not agreed. */
  consent(): ConsentRecord | null {
    return parse<ConsentRecord>(this.storage.getItem(this.key(CONSENT_KEY)));
  }

  recordConsent(statement: string): ConsentRecord {
    const record: ConsentRecord = { grantedAt: new Date().toISOString(), statement };
    this.storage.setItem(this.key(CONSENT_KEY), JSON.stringify(record));
    return record;
  }

  /**
   * Contact details from an earlier form in this conversation.
   *
   * WCAG 2.2 §3.3.7 (Redundant Entry): a visitor who already gave their name
   * and address must not have to type them again in the same session.
   */
  contact(): BookingContact | null {
    return parse<BookingContact>(this.storage.getItem(this.key(CONTACT_KEY)));
  }

  rememberContact(details: BookingContact): void {
    this.storage.setItem(this.key(CONTACT_KEY), JSON.stringify(details));
  }

  /** Erase every trace of this tenant's conversation from the browser. */
  clear(): void {
    for (const prefix of [SESSION_KEY, CONSENT_KEY, CONTACT_KEY]) {
      this.storage.removeItem(this.key(prefix));
    }
  }
}
