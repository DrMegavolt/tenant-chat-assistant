declare global {
  interface Window {
    /** Set by an embedding page to point the widget at a specific backend. */
    CHAT_API_BASE_URL?: string;
  }
}

export {};
