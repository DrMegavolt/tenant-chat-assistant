/**
 * Per-tenant visitor storage and consent record.
 *
 * Everything here lives in the visitor's browser for the length of the tab.
 * Server-side consent records, retention, export, and erasure are `PRIV-001`;
 * this module owns only what the widget itself keeps and what it lets the
 * visitor delete.
 *
 * The conversation ID is server-issued (``POST /api/chat/session``): this module
 * stores whatever the backend returned, it never invents one. `SEC-002`
 * replaces this stored value with a signed visitor credential that can also
 * carry an expiry and survive being copied out of a URL.
 */

import type { ConsentRecord } from "src/widget/types";

const SESSION_KEY = "tenant-chat-session-id";
const CONSENT_KEY = "tenant-chat-consent";

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
   * Background work — polling for staff replies — must use this rather than a
   * minting call: a visitor who opened the page and typed nothing has not
   * started a conversation, and writing an id for them would leave an
   * identifier in their browser they never asked for.
   *
   * The stored value is trusted only if the widget's UUID check accepts it; a
   * pre-cutover id from the prototype is not a session the API will accept.
   */
  existingSessionId(): string | null {
    return this.storage.getItem(this.key(SESSION_KEY));
  }

  /** Persist a session ID the backend issued for this tenant. */
  recordSession(sessionId: string): void {
    this.storage.setItem(this.key(SESSION_KEY), sessionId);
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

  /** Erase every trace of this tenant's conversation from the browser. */
  clear(): void {
    for (const prefix of [SESSION_KEY, CONSENT_KEY]) {
      this.storage.removeItem(this.key(prefix));
    }
  }
}
