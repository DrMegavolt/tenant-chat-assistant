/**
 * The embeddable chat widget.
 *
 * The widget renders into a shadow root so an embedding page can neither style
 * its internals nor be styled by them, and so its element ids cannot collide
 * with the host document's. Callers interact with it only through the methods
 * on `ChatWidget`.
 */

import { VisitorData, consentStatement } from "./privacy.js";
import { WIDGET_CSS } from "./styles.js";

const POLL_INTERVAL_MS = 2500;
const PROACTIVE_DELAY_MS = 12000;

const EMAIL_PATTERN = /[\w.+-]+@[\w.-]+\.\w+/;
const PHONE_PATTERN = /(?:\+?1[\s.-]?)?(?:\(?\d{3}\)?[\s.-]?)\d{3}[\s.-]?\d{4}/;

const icons = {
  chat: `<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M21 15a4 4 0 0 1-4 4H8l-5 3V7a4 4 0 0 1 4-4h10a4 4 0 0 1 4 4z"/></svg>`,
  close: `<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" d="M18 6 6 18M6 6l12 12"/></svg>`,
  send: `<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="m22 2-7 20-4-9-9-4Z"/><path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" d="M22 2 11 13"/></svg>`
};

const BOOKING_FIELDS = [
  { name: "customerName", label: "Name", autocomplete: "name" },
  { name: "address", label: "Address", autocomplete: "street-address" },
  { name: "contact", label: "Phone or email", autocomplete: "email" }
];

export class ChatWidget {
  constructor({ mount, api }) {
    this.api = api;
    this.root = mount.shadowRoot ?? mount.attachShadow({ mode: "open" });
    this.root.innerHTML = `<style>${WIDGET_CSS}</style><div id="surface"></div>`;
    this.surface = this.root.querySelector("#surface");

    this.tenantId = null;
    this.config = null;
    this.visitor = null;
    this.messages = [];
    this.isOpen = true;
    this.isWaiting = false;
    this.seenServerMessageIds = new Set();
    this.proactiveTimer = null;
    this.proactiveNudgeShown = false;
    this.pollTimer = null;
  }

  find(selector) {
    return this.root.querySelector(selector);
  }

  get sessionId() {
    return this.visitor?.sessionId() ?? null;
  }

  /** Point the widget at a tenant, rendering its branding and a fresh chat. */
  setTenant(tenantId, config) {
    const isFirstTenant = this.tenantId === null;
    this.tenantId = tenantId;
    this.config = config;
    this.visitor = new VisitorData(tenantId);
    this.seenServerMessageIds.clear();
    this.proactiveNudgeShown = false;

    if (isFirstTenant) {
      this.renderShell();
    }
    this.renderBranding();
    this.renderQuickActions();
    this.resetConversation();
    this.renderVisibility();
  }

  /**
   * Replace the widget with an accessible failure state.
   *
   * A visitor whose backend is down must be told so in a live region rather
   * than left with a chat box that silently swallows every message.
   */
  showFatalError(message) {
    this.stopPolling();
    this.clearProactiveTimer();
    this.surface.replaceChildren(
      element("div", { class: "widget-error", role: "alert", tabindex: "-1" }, [
        element("h2", {}, ["Chat is unavailable"]),
        element("p", {}, [message])
      ])
    );
  }

  startPolling(intervalMs = POLL_INTERVAL_MS) {
    this.stopPolling();
    this.pollTimer = window.setInterval(() => this.syncSessionMessages(), intervalMs);
  }

  stopPolling() {
    if (this.pollTimer) {
      window.clearInterval(this.pollTimer);
      this.pollTimer = null;
    }
  }

  renderShell() {
    const closeButton = element("button", {
      type: "button",
      class: "icon-button",
      id: "closeChat"
    });
    closeButton.innerHTML = `${icons.close}<span class="visually-hidden">Close chat</span>`;

    const sendButton = element("button", { type: "submit" });
    sendButton.innerHTML = `${icons.send}<span class="visually-hidden">Send message</span>`;

    const launcher = element("button", {
      type: "button",
      class: "chat-launcher",
      id: "openChat",
      "aria-expanded": "true",
      "aria-controls": "chatWindow"
    });
    launcher.innerHTML = `${icons.chat}<span class="visually-hidden">Open chat</span>`;

    const composer = element("form", { class: "composer", id: "composer" }, [
      element("label", { class: "visually-hidden", for: "chatInput" }, ["Message"]),
      element("input", {
        id: "chatInput",
        name: "message",
        type: "text",
        autocomplete: "off",
        "aria-describedby": "privacyNote"
      }),
      sendButton
    ]);

    const window_ = element(
      "section",
      {
        class: "chat-window",
        id: "chatWindow",
        role: "dialog",
        "aria-labelledby": "chatCompany",
        "aria-describedby": "chatTagline"
      },
      [
        element("header", { class: "chat-header" }, [
          element("div", { class: "chat-title" }, [
            element("strong", { id: "chatCompany" }),
            element("span", { id: "chatTagline" })
          ]),
          closeButton
        ]),
        element("div", {
          class: "messages",
          id: "messages",
          role: "log",
          "aria-live": "polite",
          "aria-relevant": "additions",
          "aria-label": "Conversation",
          tabindex: "0"
        }),
        element("div", { class: "chat-footer" }, [
          element("p", { class: "assistant-status", id: "assistantStatus", role: "status" }),
          element("div", {
            class: "quick-actions",
            id: "quickActions",
            role: "group",
            "aria-label": "Suggested questions"
          }),
          composer,
          this.buildPrivacyBar(),
          this.buildPrivacyPanel()
        ])
      ]
    );

    this.surface.replaceChildren(window_, launcher);
    this.bindShellEvents();
  }

  buildPrivacyBar() {
    const toggle = element(
      "button",
      {
        type: "button",
        class: "link-button",
        id: "privacyToggle",
        "aria-expanded": "false",
        "aria-controls": "privacyPanel"
      },
      ["Privacy and your data"]
    );
    return element("p", { class: "privacy-bar", id: "privacyNote" }, [
      element("span", {}, ["Messages are stored so the team can answer and follow up."]),
      toggle
    ]);
  }

  buildPrivacyPanel() {
    return element(
      "div",
      { class: "privacy-panel", id: "privacyPanel", hidden: "", "aria-labelledby": "privacyTitle" },
      [
        element("h2", { id: "privacyTitle" }, ["How this chat uses your data"]),
        element("ul", {}, [
          element("li", {}, ["Your messages are stored so staff can read and answer them."]),
          element("li", {}, [
            "Name, address, and contact details are sent only when you fill in a form and agree first."
          ]),
          element("li", {}, [
            "This browser tab keeps a conversation id so replies from staff reach you."
          ]),
          element("li", {}, ["Closing the tab ends the conversation id."])
        ]),
        element("button", { type: "button", id: "clearVisitorData" }, [
          "Delete this conversation from my browser"
        ])
      ]
    );
  }

  bindShellEvents() {
    this.find("#closeChat").addEventListener("click", () => this.close());
    this.find("#openChat").addEventListener("click", () => this.open());

    this.surface.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && this.isOpen) {
        event.stopPropagation();
        this.close();
      }
    });

    this.find("#composer").addEventListener("submit", (event) => {
      event.preventDefault();
      const input = this.find("#chatInput");
      const value = input.value.trim();
      if (!value) return;
      input.value = "";
      this.handleUserMessage(value);
    });

    this.find("#privacyToggle").addEventListener("click", () => this.togglePrivacyPanel());
    this.find("#clearVisitorData").addEventListener("click", () => this.clearVisitorData());
  }

  renderBranding() {
    this.find("#chatCompany").textContent = this.config.assistantName;
    this.find("#chatTagline").textContent = this.config.tagline;
  }

  renderQuickActions() {
    const actions = this.find("#quickActions");
    actions.replaceChildren(
      ...this.config.quickActions.map((label) => {
        const button = element("button", { type: "button" }, [label]);
        button.addEventListener("click", () => this.handleUserMessage(label));
        return button;
      })
    );
  }

  renderVisibility() {
    this.find("#chatWindow").hidden = !this.isOpen;
    const launcher = this.find("#openChat");
    launcher.hidden = this.isOpen;
    launcher.setAttribute("aria-expanded", String(this.isOpen));
  }

  open() {
    this.isOpen = true;
    this.renderVisibility();
    this.find("#chatInput").focus();
  }

  close() {
    this.isOpen = false;
    this.renderVisibility();
    this.find("#openChat").focus();
  }

  togglePrivacyPanel() {
    const toggle = this.find("#privacyToggle");
    const panel = this.find("#privacyPanel");
    const nextOpen = toggle.getAttribute("aria-expanded") !== "true";
    toggle.setAttribute("aria-expanded", String(nextOpen));
    panel.hidden = !nextOpen;
    if (nextOpen) {
      this.find("#clearVisitorData").focus();
    }
  }

  clearVisitorData() {
    this.visitor.clear();
    this.seenServerMessageIds.clear();
    this.proactiveNudgeShown = false;
    this.resetConversation();
    this.announce("This conversation was deleted from your browser and a new one has started.");
  }

  resetConversation() {
    this.messages = [];
    this.clearProactiveTimer();
    this.find("#messages").replaceChildren();
    const config = this.config;
    const routing = config.bookingEnabled
      ? "help find appointment slots"
      : `connect you with the team at ${config.phone}`;
    this.addMessage(
      "assistant",
      `Hi, I’m the ${config.assistantName}. I can answer questions about ${config.name}, ` +
        `check whether a ZIP code is served, ${routing}, and create a follow-up lead.`
    );
  }

  /** Announce transient state to assistive technology without adding a bubble. */
  announce(text) {
    this.find("#assistantStatus").textContent = text;
  }

  addMessage(role, text, options = {}) {
    this.messages.push({ role, text, source: options.source || role });
    const source = options.source || "";
    const bubble = element("div", { class: `message ${role} ${source}`.trim() }, [
      element("span", { class: "visually-hidden" }, [`${speakerLabel(role, source)}: `]),
      element("span", {}, [text])
    ]);
    this.appendToLog(bubble);
    return bubble;
  }

  addToolCall(name, args, result) {
    this.appendToLog(
      element("div", { class: "tool-call" }, [
        `Tool: ${name}(${JSON.stringify(args)}) -> ${JSON.stringify(result)}`
      ])
    );
  }

  appendToLog(node) {
    const messages = this.find("#messages");
    messages.append(node);
    messages.scrollTop = messages.scrollHeight;
  }

  async handleUserMessage(rawText) {
    if (this.isWaiting) return;
    const text = rawText.trim();
    this.clearProactiveTimer();
    this.addMessage("user", text);
    this.setWaiting(true);

    try {
      const payload = await this.api.chat({
        tenantId: this.tenantId,
        sessionId: this.sessionId,
        messages: this.messages.map((message) => ({
          role: message.role,
          content: message.text,
          source: message.source
        }))
      });
      for (const event of payload.toolEvents || []) {
        this.addToolCall(event.name, event.arguments, event.result);
        this.maybeRenderBookingForm(event);
      }
      this.addMessage("assistant", payload.reply);
      this.scheduleProactiveNudge();
    } catch (error) {
      this.addMessage(
        "assistant",
        "I could not reach the chat service just now. Please try again in a moment."
      );
    } finally {
      this.setWaiting(false);
    }
  }

  setWaiting(isWaiting) {
    this.isWaiting = isWaiting;
    const input = this.find("#chatInput");
    const button = this.find(".composer button");
    input.disabled = isWaiting;
    button.disabled = isWaiting;
    this.find("#messages").setAttribute("aria-busy", String(isWaiting));
    this.announce(isWaiting ? "Waiting for the assistant to reply." : "");
  }

  maybeRenderBookingForm(event) {
    if (event.name !== "get_availability" || !this.config.bookingEnabled) return;
    const service = event.result?.service;
    const slots = event.result?.slots || [];
    if (!service || !slots.length) return;
    this.renderBookingForm(service, slots);
  }

  renderBookingForm(service, slots) {
    for (const stale of this.root.querySelectorAll(".booking-form-card")) {
      stale.remove();
    }

    const remembered = this.visitor.contact() || {};
    const statement = consentStatement(this.config.name);

    const slotSelect = element(
      "select",
      { id: "bookingSlot", name: "slot", required: "" },
      slots.map((slot) => element("option", { value: slot }, [slot]))
    );

    const fields = BOOKING_FIELDS.map(({ name, label, autocomplete }) =>
      element("label", { for: `booking-${name}` }, [
        element("span", {}, [label]),
        element("input", {
          id: `booking-${name}`,
          name,
          type: "text",
          autocomplete,
          required: "",
          value: remembered[name] || ""
        })
      ])
    );

    const consentCheckbox = element("input", {
      id: "bookingConsent",
      name: "consent",
      type: "checkbox",
      "aria-describedby": "bookingError"
    });

    const form = element(
      "form",
      { class: "booking-form-card", "aria-labelledby": "bookingTitle" },
      [
        element("strong", { id: "bookingTitle" }, [`Book ${toTitle(service)}`]),
        element("label", { for: "bookingSlot" }, [
          element("span", {}, ["Available slot"]),
          slotSelect
        ]),
        ...fields,
        element("label", { class: "consent-field", for: "bookingConsent" }, [
          consentCheckbox,
          element("span", {}, [statement])
        ]),
        element("p", { class: "form-error", id: "bookingError", role: "alert" }),
        element("button", { type: "submit" }, ["Book selected slot"])
      ]
    );

    form.addEventListener("submit", (event) => {
      event.preventDefault();
      this.submitBookingForm(form, service, statement);
    });

    this.appendToLog(form);
  }

  async submitBookingForm(form, service, statement) {
    const errorNode = form.querySelector("#bookingError");
    const consentCheckbox = form.querySelector("#bookingConsent");
    if (!consentCheckbox.checked) {
      this.failBooking(form, "Please agree to the data notice before booking.", consentCheckbox);
      return;
    }

    const consent = this.visitor.recordConsent(statement);
    const details = Object.fromEntries(
      BOOKING_FIELDS.map(({ name }) => [name, form.querySelector(`#booking-${name}`).value])
    );

    const button = form.querySelector("button[type='submit']");
    button.disabled = true;
    button.textContent = "Booking…";
    errorNode.textContent = "";

    const { ok, payload } = await this.api.book({
      tenantId: this.tenantId,
      sessionId: this.sessionId,
      service,
      slot: form.querySelector("#bookingSlot").value,
      ...details,
      consent
    });

    if (!ok) {
      button.disabled = false;
      button.textContent = "Book selected slot";
      this.failBooking(form, bookingErrorMessage(payload.toolEvent?.result));
      return;
    }

    this.visitor.rememberContact(details);
    this.addToolCall(payload.toolEvent.name, payload.toolEvent.arguments, payload.toolEvent.result);
    this.addMessage("assistant", payload.reply);
    form.remove();
    this.find("#chatInput").focus();
  }

  /** Surface a booking failure in the form's alert region and focus the fix. */
  failBooking(form, message, focusTarget = null) {
    form.querySelector("#bookingError").textContent = message;
    const target = focusTarget || form.querySelector("button[type='submit']");
    if (focusTarget) {
      focusTarget.setAttribute("aria-invalid", "true");
      focusTarget.addEventListener("change", () => focusTarget.removeAttribute("aria-invalid"), {
        once: true
      });
    }
    target.focus();
  }

  async syncSessionMessages() {
    if (!this.sessionId) return;
    const session = await this.api.session(this.sessionId);
    if (!session) return;
    for (const message of session.messages || []) {
      if (message.source !== "admin" || this.seenServerMessageIds.has(message.id)) {
        continue;
      }
      this.seenServerMessageIds.add(message.id);
      this.addMessage("assistant", message.content, { source: "admin" });
    }
  }

  scheduleProactiveNudge() {
    if (!this.config.proactiveLeadCapture || this.proactiveNudgeShown || this.hasContactInfo()) {
      return;
    }
    if (!this.messages.some((message) => message.role === "user")) return;
    this.clearProactiveTimer();
    this.proactiveTimer = window.setTimeout(() => {
      if (this.isWaiting || this.proactiveNudgeShown || this.hasContactInfo()) return;
      this.proactiveNudgeShown = true;
      this.addMessage(
        "assistant",
        "If it helps, I can have the team follow up. Send your name and phone or email, " +
          "and I’ll create a callback request.",
        { source: "proactive" }
      );
    }, PROACTIVE_DELAY_MS);
  }

  clearProactiveTimer() {
    if (this.proactiveTimer) {
      window.clearTimeout(this.proactiveTimer);
      this.proactiveTimer = null;
    }
  }

  hasContactInfo() {
    const text = this.messages.map((message) => message.text).join("\n");
    return EMAIL_PATTERN.test(text) || PHONE_PATTERN.test(text);
  }
}

function speakerLabel(role, source) {
  if (source === "admin") return "Staff";
  return role === "user" ? "You" : "Assistant";
}

function bookingErrorMessage(result = {}) {
  if (result.message) return result.message;
  if (result.error === "missing_required_fields") {
    return `Missing: ${result.missingFields.join(", ")}.`;
  }
  if (result.error === "slot_unavailable") {
    return "That slot is no longer available. Please choose another slot.";
  }
  return "Booking failed. Please check the form and try again.";
}

function toTitle(text) {
  return text.replace(/\w\S*/g, (word) => word.charAt(0).toUpperCase() + word.slice(1));
}

/** Build an element from attributes and children; children are always text-safe. */
function element(tag, attributes = {}, children = []) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attributes)) {
    node.setAttribute(name, value);
  }
  for (const child of children) {
    node.append(child);
  }
  return node;
}
