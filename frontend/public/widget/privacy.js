/**
 * Per-tenant visitor storage and consent record.
 *
 * Everything here lives in the visitor's browser for the length of the tab.
 * Server-side consent records, retention, export, and erasure are `PRIV-001`;
 * this module owns only what the widget itself keeps and what it lets the
 * visitor delete.
 */

const SESSION_KEY = "tenant-chat-session-id";
const CONSENT_KEY = "tenant-chat-consent";
const CONTACT_KEY = "tenant-chat-contact";

/**
 * Return `window.sessionStorage`, or an equivalent in-memory shim when the
 * embedding page blocks storage. A third-party embed in a browser configured to
 * reject storage must still be able to hold a conversation.
 */
function safeStorage() {
  try {
    const probe = "tenant-chat-probe";
    window.sessionStorage.setItem(probe, "1");
    window.sessionStorage.removeItem(probe);
    return window.sessionStorage;
  } catch (error) {
    const values = new Map();
    return {
      getItem: (key) => (values.has(key) ? values.get(key) : null),
      setItem: (key, value) => values.set(key, String(value)),
      removeItem: (key) => values.delete(key)
    };
  }
}

/** The sentence a visitor agrees to before contact details are submitted. */
export function consentStatement(tenantName) {
  return (
    `I agree that ${tenantName} may store the name, address, and contact ` +
    `details I enter here in order to arrange this appointment and follow up ` +
    `about it.`
  );
}

export class VisitorData {
  constructor(tenantId, storage = safeStorage()) {
    this.tenantId = tenantId;
    this.storage = storage;
  }

  key(prefix) {
    return `${prefix}:${this.tenantId}`;
  }

  /** Stable per-tenant conversation id, created on first read. */
  sessionId() {
    const existing = this.storage.getItem(this.key(SESSION_KEY));
    if (existing) return existing;
    const value = `web-${this.tenantId}-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    this.storage.setItem(this.key(SESSION_KEY), value);
    return value;
  }

  hasConsent() {
    return this.consent() !== null;
  }

  /** The stored consent record, or null when the visitor has not agreed. */
  consent() {
    const raw = this.storage.getItem(this.key(CONSENT_KEY));
    if (!raw) return null;
    try {
      return JSON.parse(raw);
    } catch (error) {
      return null;
    }
  }

  recordConsent(statement) {
    const record = { grantedAt: new Date().toISOString(), statement };
    this.storage.setItem(this.key(CONSENT_KEY), JSON.stringify(record));
    return record;
  }

  /**
   * Contact details from an earlier form in this conversation.
   *
   * WCAG 2.2 §3.3.7 (Redundant Entry): a visitor who already gave their name
   * and address must not have to type them again in the same session.
   */
  contact() {
    const raw = this.storage.getItem(this.key(CONTACT_KEY));
    if (!raw) return null;
    try {
      return JSON.parse(raw);
    } catch (error) {
      return null;
    }
  }

  rememberContact(details) {
    this.storage.setItem(this.key(CONTACT_KEY), JSON.stringify(details));
  }

  /** Erase every trace of this tenant's conversation from the browser. */
  clear() {
    for (const prefix of [SESSION_KEY, CONSENT_KEY, CONTACT_KEY]) {
      this.storage.removeItem(this.key(prefix));
    }
  }
}
