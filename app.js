const state = {
  tenantId: "apex",
  sessionId: getSessionId(),
  tenantConfigs: {},
  isOpen: true,
  messages: [],
  isWaiting: false,
  seenServerMessageIds: new Set(),
  proactiveTimer: null,
  proactiveNudgeShown: false
};

const API_BASE_URL = resolveApiBaseUrl();

const icons = {
  chat: `<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M21 15a4 4 0 0 1-4 4H8l-5 3V7a4 4 0 0 1 4-4h10a4 4 0 0 1 4 4z"/></svg>`,
  close: `<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" d="M18 6 6 18M6 6l12 12"/></svg>`,
  send: `<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="m22 2-7 20-4-9-9-4Z"/><path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" d="M22 2 11 13"/></svg>`
};

function tenant() {
  return state.tenantConfigs[state.tenantId];
}

async function init() {
  const root = document.querySelector("#tenant-chat");
  state.tenantId = root.dataset.companyId || "apex";
  renderShell(root);
  await loadTenants();
  renderTenantPage();
  resetConversation();
  window.setInterval(syncSessionMessages, 2500);

  document.querySelector("#tenantSelect").addEventListener("change", (event) => {
    state.tenantId = event.target.value;
    document.querySelector("#tenant-chat").dataset.companyId = state.tenantId;
    state.sessionId = getSessionId(state.tenantId);
    state.seenServerMessageIds.clear();
    state.proactiveNudgeShown = false;
    renderTenantPage();
    resetConversation();
  });
}

function renderShell(root) {
  root.innerHTML = `
    <section class="chat-window" id="chatWindow">
      <header class="chat-header">
        <div class="chat-title">
          <strong id="chatCompany"></strong>
          <span id="chatTagline"></span>
        </div>
        <button class="icon-button" id="closeChat" type="button" aria-label="Close chat">${icons.close}</button>
      </header>
      <div class="messages" id="messages"></div>
      <div>
        <div class="quick-actions" id="quickActions"></div>
        <form class="composer" id="composer">
          <input id="chatInput" autocomplete="off" placeholder="Ask a question..." />
          <button type="submit" aria-label="Send message">${icons.send}</button>
        </form>
      </div>
    </section>
    <button class="chat-launcher" id="openChat" type="button" aria-label="Open chat" hidden>${icons.chat}</button>
  `;

  document.querySelector("#closeChat").addEventListener("click", () => {
    state.isOpen = false;
    renderVisibility();
  });

  document.querySelector("#openChat").addEventListener("click", () => {
    state.isOpen = true;
    renderVisibility();
  });

  document.querySelector("#composer").addEventListener("submit", (event) => {
    event.preventDefault();
    const input = document.querySelector("#chatInput");
    const value = input.value.trim();
    if (!value) return;
    input.value = "";
    handleUserMessage(value);
  });
}

async function loadTenants() {
  const response = await fetch(apiUrl("/api/tenants"));
  if (!response.ok) {
    throw new Error("Unable to load tenant configuration from backend.");
  }
  const payload = await response.json();
  state.tenantConfigs = payload.tenants;
}

function renderTenantPage() {
  const config = tenant();
  document.querySelector("#tenantSelect").value = state.tenantId;
  document.querySelector("#siteName").textContent = config.name;
  document.querySelector("#headline").textContent = config.site.headline;
  document.querySelector("#description").textContent = config.site.description;
  document.querySelector("#chatCompany").textContent = config.assistantName;
  document.querySelector("#chatTagline").textContent = config.tagline;

  document.querySelector("#configSummary").innerHTML = `
    <div><dt>Knowledge</dt><dd>${config.address}<br>${config.phone}<br>${config.hours}</dd></div>
    <div><dt>Pricing</dt><dd>${config.pricingPolicy === "never" ? "Never reveal pricing; route to phone." : "Allowed to quote fixed approved prices."}</dd></div>
    <div><dt>Booking</dt><dd>${config.bookingEnabled ? "Allowed after service and slot confirmation." : "Disabled; route to phone team."}</dd></div>
    <div><dt>Lead capture</dt><dd>${config.leadCaptureEnabled ? "Enabled; backend creates follow-up leads." : "Disabled for this company."}</dd></div>
    <div><dt>Proactive</dt><dd>${config.proactiveLeadCapture ? "Enabled; politely offers callback after intent." : "Disabled."}</dd></div>
    <div><dt>Service groups</dt><dd>${config.services.join(", ")}</dd></div>
  `;

  renderQuickActions();
  renderVisibility();
}

function resetConversation() {
  state.messages = [];
  clearProactiveTimer();
  document.querySelector("#messages").innerHTML = "";
  const config = tenant();
  addMessage(
    "assistant",
    `Hi, I’m the ${config.assistantName}. I can answer questions about ${config.name}, check whether a ZIP code is served, ${config.bookingEnabled ? "help find appointment slots" : `connect you with the team at ${config.phone}`}, and create a follow-up lead.`
  );
}

function renderVisibility() {
  document.querySelector("#chatWindow").hidden = !state.isOpen;
  document.querySelector("#openChat").hidden = state.isOpen;
}

function renderQuickActions() {
  const actions = document.querySelector("#quickActions");
  actions.innerHTML = "";
  tenant().quickActions.forEach((label) => {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = label;
    button.addEventListener("click", () => handleUserMessage(label));
    actions.append(button);
  });
}

function addMessage(role, text, options = {}) {
  state.messages.push({ role, text, source: options.source || role });
  const messages = document.querySelector("#messages");
  const bubble = document.createElement("div");
  bubble.className = `message ${role} ${options.source || ""}`;
  bubble.textContent = text;
  messages.append(bubble);
  messages.scrollTop = messages.scrollHeight;
}

function addToolCall(name, payload, result) {
  const messages = document.querySelector("#messages");
  const item = document.createElement("div");
  item.className = "tool-call";
  item.textContent = `Tool: ${name}(${JSON.stringify(payload)}) -> ${JSON.stringify(result)}`;
  messages.append(item);
  messages.scrollTop = messages.scrollHeight;
}

async function handleUserMessage(rawText) {
  if (state.isWaiting) return;
  const text = rawText.trim();
  clearProactiveTimer();
  addMessage("user", text);
  setWaiting(true);

  try {
    const response = await fetch(apiUrl("/api/chat"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        tenantId: state.tenantId,
        sessionId: state.sessionId,
        messages: state.messages.map((message) => ({
          role: message.role,
          content: message.text,
          source: message.source
        }))
      })
    });

    if (!response.ok) {
      throw new Error(`Chat request failed with ${response.status}`);
    }

    const payload = await response.json();
    for (const event of payload.toolEvents || []) {
      addToolCall(event.name, event.arguments, event.result);
      maybeRenderBookingForm(event);
    }
    addMessage("assistant", payload.reply);
    scheduleProactiveNudge();
  } catch (error) {
    addMessage(
      "assistant",
      "I could not reach the chat backend. Start the Python server and try again."
    );
  } finally {
    setWaiting(false);
  }
}

function apiUrl(path) {
  return `${API_BASE_URL}${path}`;
}

function resolveApiBaseUrl() {
  const configured =
    window.CHAT_API_BASE_URL ||
    document.currentScript?.dataset.apiBaseUrl ||
    document.querySelector("#tenant-chat")?.dataset.apiBaseUrl ||
    "";
  if (configured.trim()) {
    return configured.trim().replace(/\/+$/, "");
  }
  return window.location.protocol === "file:" ? "http://127.0.0.1:8000" : "";
}

function maybeRenderBookingForm(event) {
  if (event.name !== "get_availability") return;
  const service = event.result?.service;
  const slots = event.result?.slots || [];
  if (!service || !slots.length || !tenant().bookingEnabled) return;
  renderBookingForm(service, slots);
}

function renderBookingForm(service, slots) {
  document.querySelectorAll(".booking-form-card").forEach((element) => element.remove());
  const messages = document.querySelector("#messages");
  const card = document.createElement("form");
  card.className = "booking-form-card";
  card.innerHTML = `
    <strong>Book ${escapeHtml(toTitle(service))}</strong>
    <label>
      <span>Available slot</span>
      <select name="slot" required>
        ${slots.map((slot) => `<option value="${escapeHtml(slot)}">${escapeHtml(slot)}</option>`).join("")}
      </select>
    </label>
    <label>
      <span>Name</span>
      <input name="customerName" autocomplete="name" required />
    </label>
    <label>
      <span>Address</span>
      <input name="address" autocomplete="street-address" required />
    </label>
    <label>
      <span>Phone or email</span>
      <input name="contact" autocomplete="email" required />
    </label>
    <button type="submit">Book selected slot</button>
  `;
  card.addEventListener("submit", async (event) => {
    event.preventDefault();
    await submitBookingForm(card, service);
  });
  messages.append(card);
  messages.scrollTop = messages.scrollHeight;
}

async function submitBookingForm(form, service) {
  const button = form.querySelector("button");
  button.disabled = true;
  button.textContent = "Booking...";
  const data = new FormData(form);

  try {
    const response = await fetch(apiUrl("/api/book"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        tenantId: state.tenantId,
        sessionId: state.sessionId,
        service,
        slot: data.get("slot"),
        customerName: data.get("customerName"),
        address: data.get("address"),
        contact: data.get("contact")
      })
    });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(bookingErrorMessage(payload.toolEvent?.result));
    }
    addToolCall(payload.toolEvent.name, payload.toolEvent.arguments, payload.toolEvent.result);
    addMessage("assistant", payload.reply);
    form.remove();
  } catch (error) {
    const errorNode = form.querySelector(".booking-error") || document.createElement("p");
    errorNode.className = "booking-error";
    errorNode.textContent = error.message;
    form.append(errorNode);
    button.disabled = false;
    button.textContent = "Book selected slot";
  }
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

function getSessionId(tenantId = "apex") {
  const key = `tenant-chat-session-id:${tenantId}`;
  const existing = window.sessionStorage.getItem(key);
  if (existing) return existing;
  const value = `web-${tenantId}-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  window.sessionStorage.setItem(key, value);
  return value;
}

async function syncSessionMessages() {
  if (!state.sessionId) return;
  try {
    const response = await fetch(
      apiUrl(`/api/chat/session?sessionId=${encodeURIComponent(state.sessionId)}`)
    );
    if (!response.ok) return;
    const payload = await response.json();
    const session = payload.session;
    if (!session) return;
    for (const message of session.messages || []) {
      if (message.source !== "admin" || state.seenServerMessageIds.has(message.id)) {
        continue;
      }
      state.seenServerMessageIds.add(message.id);
      addMessage("assistant", message.content, { source: "admin" });
    }
  } catch (error) {
    // Polling is best-effort for the prototype.
  }
}

function scheduleProactiveNudge() {
  const config = tenant();
  if (!config.proactiveLeadCapture || state.proactiveNudgeShown || hasContactInfo()) {
    return;
  }
  const userMessageCount = state.messages.filter((message) => message.role === "user").length;
  if (userMessageCount === 0) return;
  clearProactiveTimer();
  state.proactiveTimer = window.setTimeout(() => {
    if (state.isWaiting || state.proactiveNudgeShown || hasContactInfo()) return;
    state.proactiveNudgeShown = true;
    addMessage(
      "assistant",
      "If it helps, I can have the team follow up. Send your name and phone or email, and I’ll create a callback request.",
      { source: "proactive" }
    );
  }, 12000);
}

function clearProactiveTimer() {
  if (state.proactiveTimer) {
    window.clearTimeout(state.proactiveTimer);
    state.proactiveTimer = null;
  }
}

function hasContactInfo() {
  const text = state.messages.map((message) => message.text).join("\n");
  return /[\w.+-]+@[\w.-]+\.\w+/.test(text) ||
    /(?:\+?1[\s.-]?)?(?:\(?\d{3}\)?[\s.-]?)\d{3}[\s.-]?\d{4}/.test(text);
}

function toTitle(text) {
  return text.replace(/\w\S*/g, (word) => word.charAt(0).toUpperCase() + word.slice(1));
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function setWaiting(isWaiting) {
  state.isWaiting = isWaiting;
  const input = document.querySelector("#chatInput");
  const button = document.querySelector(".composer button");
  input.disabled = isWaiting;
  button.disabled = isWaiting;
  input.placeholder = isWaiting ? "Waiting for assistant..." : "Ask a question...";
}

init().catch((error) => {
  document.querySelector("#tenant-chat").innerHTML = "";
  document.body.insertAdjacentHTML(
    "beforeend",
    `<div class="backend-error">Backend unavailable: ${error.message}</div>`
  );
});
