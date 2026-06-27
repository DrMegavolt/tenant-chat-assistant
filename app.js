const state = {
  tenantId: "apex",
  tenantConfigs: {},
  isOpen: true,
  messages: [],
  isWaiting: false
};

const API_BASE_URL = window.location.protocol === "file:" ? "http://127.0.0.1:8000" : "";

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

  document.querySelector("#tenantSelect").addEventListener("change", (event) => {
    state.tenantId = event.target.value;
    document.querySelector("#tenant-chat").dataset.companyId = state.tenantId;
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
    <div><dt>Service groups</dt><dd>${config.services.join(", ")}</dd></div>
  `;

  renderQuickActions();
  renderVisibility();
}

function resetConversation() {
  state.messages = [];
  document.querySelector("#messages").innerHTML = "";
  const config = tenant();
  addMessage(
    "assistant",
    `Hi, I’m the ${config.assistantName}. I can answer questions about ${config.name}, check whether a ZIP code is served, and ${config.bookingEnabled ? "help find appointment slots." : `connect you with the team at ${config.phone}.`}`
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

function addMessage(role, text) {
  state.messages.push({ role, text });
  const messages = document.querySelector("#messages");
  const bubble = document.createElement("div");
  bubble.className = `message ${role}`;
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
  addMessage("user", text);
  setWaiting(true);

  try {
    const response = await fetch(apiUrl("/api/chat"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        tenantId: state.tenantId,
        messages: state.messages.map((message) => ({
          role: message.role,
          content: message.text
        }))
      })
    });

    if (!response.ok) {
      throw new Error(`Chat request failed with ${response.status}`);
    }

    const payload = await response.json();
    for (const event of payload.toolEvents || []) {
      addToolCall(event.name, event.arguments, event.result);
    }
    addMessage("assistant", payload.reply);
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
