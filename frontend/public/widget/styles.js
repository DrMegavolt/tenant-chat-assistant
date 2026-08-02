/**
 * Widget palette and stylesheet.
 *
 * The widget renders inside a shadow root, so these styles are the widget's
 * only styles: nothing from the host page reaches them and nothing here leaks
 * out. Colors are declared once here and referenced by the CSS below so a token
 * and its usage cannot drift apart; `tests/contrast.test.js` asserts every pair
 * the widget actually paints meets WCAG 2.2 AA.
 */

export const WIDGET_PALETTE = {
  surface: "#ffffff",
  surfaceMuted: "#f4f6f6",
  ink: "#16211f",
  inkMuted: "#4f595d",
  line: "#78868a",
  accent: "#0b5f59",
  accentInk: "#ffffff",
  headerBg: "#102328",
  headerInk: "#ffffff",
  headerInkMuted: "#c3cdcf",
  dangerSurface: "#fff4ea",
  dangerInk: "#8a3a05",
  noticeSurface: "#fff6ec",
  noticeInk: "#6b3c11",
  proactiveSurface: "#eef5f6",
  staffSurface: "#fff7e4",
  focusRing: "#0b5f59"
};

const tokens = Object.entries(WIDGET_PALETTE)
  .map(([name, value]) => `      --${camelToKebab(name)}: ${value};`)
  .join("\n");

function camelToKebab(name) {
  return name.replace(/[A-Z]/g, (letter) => `-${letter.toLowerCase()}`);
}

export const WIDGET_CSS = `
    :host {
      /* Drop every inherited declaration from the embedding page before the
         widget sets its own. Custom properties are unaffected by \`all\`. */
      all: initial;
      display: block;
${tokens}
      --edge: 20px;
      --launcher-size: 64px;
      --radius: 8px;
      --shadow: 0 18px 48px rgba(20, 34, 39, 0.16);
      font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
      font-size: 16px;
      line-height: 1.45;
      color: var(--ink);
    }

    *,
    *::before,
    *::after {
      box-sizing: border-box;
    }

    button,
    input,
    select,
    textarea {
      font: inherit;
      color: inherit;
    }

    :focus-visible {
      outline: 3px solid var(--focus-ring);
      outline-offset: 2px;
    }

    .chat-header :focus-visible {
      outline-color: var(--header-ink);
    }

    .visually-hidden {
      position: absolute;
      width: 1px;
      height: 1px;
      margin: -1px;
      padding: 0;
      overflow: hidden;
      clip-path: inset(50%);
      white-space: nowrap;
    }

    .chat-launcher {
      position: fixed;
      right: var(--edge);
      bottom: var(--edge);
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: var(--launcher-size);
      height: var(--launcher-size);
      border: 0;
      border-radius: 50%;
      background: var(--accent);
      color: var(--accent-ink);
      box-shadow: var(--shadow);
      cursor: pointer;
    }

    .chat-launcher svg {
      width: 29px;
      height: 29px;
    }

    .chat-window {
      position: fixed;
      right: var(--edge);
      bottom: calc(var(--edge) + var(--launcher-size) + 16px);
      display: flex;
      flex-direction: column;
      width: min(420px, calc(100vw - 2 * var(--edge)));
      height: min(680px, calc(100vh - 2 * var(--edge) - var(--launcher-size) - 16px));
      height: min(680px, calc(100dvh - 2 * var(--edge) - var(--launcher-size) - 16px));
      overflow: hidden;
      border: 1px solid rgba(16, 35, 40, 0.12);
      border-radius: var(--radius);
      background: var(--surface);
      box-shadow: var(--shadow);
    }

    [hidden] {
      display: none !important;
    }

    .chat-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 14px;
      padding: 14px 16px;
      background: var(--header-bg);
      color: var(--header-ink);
    }

    .chat-title {
      display: grid;
      gap: 2px;
    }

    .chat-title strong {
      font-size: 0.98rem;
    }

    .chat-title span {
      color: var(--header-ink-muted);
      font-size: 0.8rem;
    }

    .icon-button {
      display: inline-grid;
      place-items: center;
      width: 36px;
      height: 36px;
      border: 0;
      border-radius: 6px;
      background: rgba(255, 255, 255, 0.14);
      color: currentColor;
      cursor: pointer;
    }

    .icon-button svg {
      width: 20px;
      height: 20px;
    }

    .messages {
      display: flex;
      flex: 1 1 auto;
      flex-direction: column;
      gap: 10px;
      min-height: 96px;
      overflow-y: auto;
      padding: 16px;
      background: var(--surface-muted);
      scroll-behavior: smooth;
    }

    /* The footer grows with the quick actions and the privacy panel, so cap it
       and let it scroll: the transcript must never be squeezed out of view. */
    .chat-footer {
      flex: 0 1 auto;
      max-height: 65%;
      overflow-y: auto;
      background: var(--surface);
    }

    .message {
      max-width: 86%;
      padding: 10px 12px;
      border-radius: var(--radius);
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }

    .message.assistant {
      align-self: flex-start;
      border: 1px solid #dce2e3;
      background: var(--surface);
    }

    .message.assistant.admin {
      border-color: #d7b36c;
      background: var(--staff-surface);
    }

    .message.assistant.proactive {
      border-color: #c8d8dc;
      background: var(--proactive-surface);
    }

    .message.user {
      align-self: flex-end;
      background: var(--accent);
      color: var(--accent-ink);
    }

    .tool-call {
      align-self: flex-start;
      max-width: 92%;
      padding: 8px 10px;
      border-left: 3px solid var(--notice-ink);
      background: var(--notice-surface);
      color: var(--notice-ink);
      font-size: 0.84rem;
      overflow-wrap: anywhere;
    }

    .assistant-status {
      margin: 0;
      padding: 0 16px;
      background: var(--surface-muted);
      color: var(--ink-muted);
      font-size: 0.84rem;
    }

    .assistant-status:empty {
      display: none;
    }

    .booking-form-card {
      align-self: flex-start;
      display: grid;
      gap: 10px;
      width: min(94%, 340px);
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: var(--radius);
      background: var(--surface);
    }

    .booking-form-card > strong {
      font-size: 0.98rem;
    }

    .booking-form-card label {
      display: grid;
      gap: 5px;
    }

    .booking-form-card label > span {
      color: var(--ink-muted);
      font-size: 0.78rem;
      font-weight: 700;
      letter-spacing: 0.04em;
      text-transform: uppercase;
    }

    .booking-form-card input,
    .booking-form-card select {
      min-height: 40px;
      min-width: 0;
      padding: 0 10px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--surface);
    }

    .booking-form-card input[aria-invalid="true"] {
      border-color: var(--danger-ink);
      border-width: 2px;
    }

    .consent-field {
      display: grid;
      grid-template-columns: auto minmax(0, 1fr);
      align-items: start;
      gap: 10px;
      padding: 10px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--surface-muted);
    }

    .consent-field input {
      width: 22px;
      height: 22px;
      margin: 0;
      accent-color: var(--accent);
    }

    .consent-field span {
      color: var(--ink);
      font-size: 0.86rem;
    }

    .booking-form-card button[type="submit"] {
      min-height: 44px;
      border: 0;
      border-radius: 6px;
      background: var(--accent);
      color: var(--accent-ink);
      cursor: pointer;
      font-weight: 700;
    }

    .booking-form-card button[disabled] {
      cursor: not-allowed;
      opacity: 0.75;
    }

    .form-error {
      display: block;
      margin: 0;
      color: var(--danger-ink);
      font-size: 0.88rem;
    }

    .form-error:empty {
      display: none;
    }

    .quick-actions {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      padding: 10px 12px 0;
      border-top: 1px solid #e1e6e7;
      background: var(--surface);
    }

    .quick-actions button {
      min-height: 34px;
      padding: 0 12px;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: var(--surface);
      color: var(--ink);
      cursor: pointer;
    }

    .composer {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 44px;
      gap: 8px;
      padding: 10px 12px 6px;
      background: var(--surface);
    }

    .composer input {
      min-width: 0;
      min-height: 44px;
      padding: 0 12px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--surface);
    }

    .composer button {
      display: grid;
      place-items: center;
      min-height: 44px;
      border: 0;
      border-radius: 6px;
      background: var(--accent);
      color: var(--accent-ink);
      cursor: pointer;
    }

    .composer button:disabled,
    .composer input:disabled {
      cursor: not-allowed;
      opacity: 0.75;
    }

    .composer svg {
      width: 20px;
      height: 20px;
    }

    .privacy-bar {
      display: flex;
      flex-wrap: wrap;
      align-items: baseline;
      gap: 4px 10px;
      padding: 0 12px 10px;
      background: var(--surface);
      color: var(--ink-muted);
      font-size: 0.78rem;
    }

    .link-button {
      padding: 4px 0;
      border: 0;
      background: none;
      color: var(--accent);
      cursor: pointer;
      font-size: inherit;
      font-weight: 700;
      text-decoration: underline;
    }

    .privacy-panel {
      display: grid;
      gap: 10px;
      padding: 14px 16px;
      border-top: 1px solid #e1e6e7;
      background: var(--surface);
      font-size: 0.88rem;
    }

    .privacy-panel h2 {
      margin: 0;
      font-size: 0.95rem;
    }

    .privacy-panel ul {
      margin: 0;
      padding-left: 20px;
      display: grid;
      gap: 4px;
    }

    .privacy-panel button {
      justify-self: start;
      min-height: 40px;
      padding: 0 14px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--surface);
      color: var(--ink);
      cursor: pointer;
      font-weight: 700;
    }

    .widget-error {
      position: fixed;
      right: var(--edge);
      bottom: var(--edge);
      max-width: min(380px, calc(100vw - 2 * var(--edge)));
      padding: 14px 16px;
      border: 1px solid var(--danger-ink);
      border-radius: var(--radius);
      background: var(--danger-surface);
      color: var(--danger-ink);
      box-shadow: var(--shadow);
    }

    .widget-error h2 {
      margin: 0 0 6px;
      font-size: 0.95rem;
    }

    .widget-error p {
      margin: 0;
      font-size: 0.88rem;
    }

    /* A single-column phone, or a landscape phone whose height cannot fit a
       floating panel, gets the full viewport instead of a cramped card. */
    @media (max-width: 560px), (max-height: 520px) {
      .chat-window {
        inset: 0;
        width: 100%;
        height: 100vh;
        height: 100dvh;
        border: 0;
        border-radius: 0;
      }

      .chat-launcher {
        right: 12px;
        bottom: 12px;
      }
    }

    @media (prefers-reduced-motion: reduce) {
      .messages {
        scroll-behavior: auto;
      }

      *,
      *::before,
      *::after {
        animation-duration: 0.01ms !important;
        animation-iteration-count: 1 !important;
        transition-duration: 0.01ms !important;
      }
    }

    @media (forced-colors: active) {
      .chat-window,
      .chat-launcher,
      .booking-form-card {
        border: 1px solid CanvasText;
      }
    }
`;
