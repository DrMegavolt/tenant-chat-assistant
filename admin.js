const adminState = {
  sessions: [],
  selectedSessionId: null,
  selectedSession: null
};

const ADMIN_API_BASE = window.location.protocol === "file:" ? "http://127.0.0.1:8000" : "";

initAdmin();

async function initAdmin() {
  await refreshAdmin();
  window.setInterval(refreshAdmin, 2000);
}

async function refreshAdmin() {
  const viewState = captureViewState();
  const response = await fetch(adminApiUrl("/api/admin/chats"));
  if (!response.ok) return;
  const payload = await response.json();
  adminState.sessions = payload.sessions || [];

  if (!adminState.selectedSessionId && adminState.sessions.length) {
    adminState.selectedSessionId = adminState.sessions[0].sessionId;
  }

  await loadSelectedSession();
  renderAdmin(viewState);
}

async function loadSelectedSession() {
  if (!adminState.selectedSessionId) {
    adminState.selectedSession = null;
    return;
  }
  const response = await fetch(
    adminApiUrl(`/api/admin/chats/${encodeURIComponent(adminState.selectedSessionId)}`)
  );
  if (!response.ok) {
    adminState.selectedSession = null;
    return;
  }
  const payload = await response.json();
  adminState.selectedSession = payload.session;
}

function renderAdmin(viewState = null) {
  renderStats();
  renderSessionList();
  renderSessionDetail();
  document.querySelector("#lastUpdated").textContent = new Date().toLocaleTimeString();
  restoreViewState(viewState);
}

function renderStats() {
  const active = adminState.sessions.filter((session) => session.active).length;
  const leads = adminState.sessions.reduce((sum, session) => sum + session.leadCount, 0);
  const messages = adminState.sessions.reduce((sum, session) => sum + session.messageCount, 0);
  const archived = adminState.sessions.filter((session) => session.status === "archived").length;
  const booked = adminState.sessions.filter((session) => session.outcome === "booked").length;
  const abandoned = adminState.sessions.filter((session) => session.outcome === "abandoned").length;
  const completed = adminState.sessions.filter((session) => session.outcome === "completed").length;
  const stats = document.querySelector("#adminStats");
  stats.innerHTML = "";
  [
    ["Active", active],
    ["Archived", archived],
    ["Booked", booked],
    ["Leads", leads],
    ["Abandoned", abandoned],
    ["Completed", completed],
    ["Messages", messages]
  ].forEach(([label, value]) => {
    const item = document.createElement("div");
    item.className = "stat-tile";
    item.innerHTML = `<span>${label}</span><strong>${value}</strong>`;
    stats.append(item);
  });
}

function renderSessionList() {
  const list = document.querySelector("#sessionList");
  list.innerHTML = "";
  if (!adminState.sessions.length) {
    list.innerHTML = `<p class="muted-copy">No chats saved yet.</p>`;
    return;
  }

  adminState.sessions.forEach((session) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `session-item outcome-${session.outcome || "active"} ${session.sessionId === adminState.selectedSessionId ? "selected" : ""}`;
    button.addEventListener("click", () => {
      adminState.selectedSessionId = session.sessionId;
      loadSelectedSession().then(() => renderAdmin());
    });

    const lastText = session.lastMessage?.content || "No messages yet";
    button.innerHTML = `
      <span class="session-row">
        <strong>${escapeHtml(session.tenantName)}</strong>
        ${outcomeBadge(session)}
      </span>
      <span class="session-preview">${escapeHtml(lastText)}</span>
      <span class="session-meta">${session.messageCount} messages · ${session.leadCount} leads · ${session.status}</span>
    `;
    list.append(button);
  });
}

function renderSessionDetail() {
  const detail = document.querySelector("#sessionDetail");
  const session = adminState.selectedSession;
  if (!session) {
    detail.innerHTML = `
      <div class="empty-state">
        <h2>No chat selected</h2>
        <p>Open the website demo and send a message to create a live session.</p>
      </div>
    `;
    return;
  }

  detail.innerHTML = "";
  detail.append(renderSessionHeader(session));

  const grid = document.createElement("div");
  grid.className = "admin-detail-grid";
  grid.append(renderTranscript(session));
  grid.append(renderSidePanel(session));
  detail.append(grid);
}

function renderSessionHeader(session) {
  const header = document.createElement("div");
  header.className = "session-detail-header";
  header.innerHTML = `
    <div>
      <p class="eyebrow">${escapeHtml(session.tenantName)}</p>
      <h2>${escapeHtml(session.sessionId)}</h2>
    </div>
    <div class="status-pill outcome-${escapeHtml(session.outcome || "active")} ${session.active ? "live" : ""}">
      ${outcomeLabel(session.outcome)} · ${session.status} · updated ${formatTime(session.updatedAt)}
    </div>
  `;
  return header;
}

function renderTranscript(session) {
  const section = document.createElement("section");
  section.className = "admin-card transcript-card";

  const messages = document.createElement("div");
  messages.className = "admin-transcript";
  for (const message of session.messages || []) {
    const item = document.createElement("div");
    item.className = `admin-message ${message.role} ${message.source || ""}`;
    item.innerHTML = `
      <span>${messageLabel(message)}</span>
      <p>${escapeHtml(message.content)}</p>
      <time>${formatTime(message.createdAt)}</time>
    `;
    messages.append(item);
  }

  const form = document.createElement("form");
  form.className = "admin-reply";
  form.innerHTML = `
    <input name="message" autocomplete="off" placeholder="Send a staff message into this chat..." />
    <button type="submit">Send</button>
  `;
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const input = form.elements.message;
    const content = input.value.trim();
    if (!content) return;
    input.value = "";
    await sendAdminMessage(session.sessionId, content);
  });

  section.append(sectionTitle("Transcript"));
  section.append(messages);
  section.append(form);
  return section;
}

function renderSidePanel(session) {
  const side = document.createElement("aside");
  side.className = "admin-side-stack";

  const leads = document.createElement("section");
  leads.className = "admin-card";
  leads.append(sectionTitle("Lead Info"));
  if (!session.leads?.length) {
    leads.append(emptyCopy("No captured leads for this chat yet."));
  } else {
    session.leads.forEach((lead) => {
      const item = document.createElement("div");
      item.className = "lead-item";
      item.innerHTML = `
        <strong>${escapeHtml(lead.customerName)}</strong>
        <span>${escapeHtml(lead.contact)}</span>
        <span>${escapeHtml(lead.service)} · ${escapeHtml(lead.urgency)}</span>
        <p>${escapeHtml(lead.summary)}</p>
      `;
      leads.append(item);
    });
  }

  const tools = document.createElement("section");
  tools.className = "admin-card";
  tools.append(sectionTitle("Tool Calls"));
  if (!session.toolEvents?.length) {
    tools.append(emptyCopy("No tools called yet."));
  } else {
    session.toolEvents.slice().reverse().forEach((event) => {
      const item = document.createElement("div");
      item.className = "admin-tool";
      item.innerHTML = `
        <strong>${escapeHtml(event.name)}</strong>
        <code>${escapeHtml(JSON.stringify(event.result))}</code>
      `;
      tools.append(item);
    });
  }

  side.append(leads);
  side.append(tools);
  return side;
}

async function sendAdminMessage(sessionId, content) {
  await fetch(adminApiUrl(`/api/admin/chats/${encodeURIComponent(sessionId)}/messages`), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content })
  });
  await refreshAdmin();
}

function captureViewState() {
  const activeElement = document.activeElement;
  const replyInput = document.querySelector(".admin-reply input");
  return {
    windowX: window.scrollX,
    windowY: window.scrollY,
    sessionListTop: document.querySelector("#sessionList")?.scrollTop || 0,
    transcriptTop: document.querySelector(".admin-transcript")?.scrollTop || 0,
    replyValue: replyInput?.value || "",
    replyFocused: activeElement === replyInput,
    selectionStart: replyInput?.selectionStart || 0,
    selectionEnd: replyInput?.selectionEnd || 0
  };
}

function restoreViewState(viewState) {
  if (!viewState) return;
  window.requestAnimationFrame(() => {
    const sessionList = document.querySelector("#sessionList");
    if (sessionList) {
      sessionList.scrollTop = viewState.sessionListTop;
    }

    const transcript = document.querySelector(".admin-transcript");
    if (transcript) {
      transcript.scrollTop = viewState.transcriptTop;
    }

    const replyInput = document.querySelector(".admin-reply input");
    if (replyInput) {
      replyInput.value = viewState.replyValue;
      if (viewState.replyFocused) {
        replyInput.focus({ preventScroll: true });
        replyInput.setSelectionRange(viewState.selectionStart, viewState.selectionEnd);
      }
    }

    window.scrollTo(viewState.windowX, viewState.windowY);
  });
}

function sectionTitle(label) {
  const title = document.createElement("h2");
  title.textContent = label;
  return title;
}

function emptyCopy(text) {
  const copy = document.createElement("p");
  copy.className = "muted-copy";
  copy.textContent = text;
  return copy;
}

function messageLabel(message) {
  if (message.source === "admin") return "Staff";
  if (message.role === "user") return "Visitor";
  return "Assistant";
}

function outcomeBadge(session) {
  const outcome = session.outcome || "active";
  return `<em class="outcome-badge outcome-${escapeHtml(outcome)}"><span></span>${escapeHtml(outcomeLabel(outcome))}</em>`;
}

function outcomeLabel(outcome) {
  const labels = {
    active: "Active",
    abandoned: "Abandoned",
    lead: "Lead",
    booked: "Booked",
    handoff: "Handoff",
    completed: "Completed",
    empty: "Empty"
  };
  return labels[outcome] || outcome;
}

function formatTime(timestamp) {
  if (!timestamp) return "-";
  return new Date(timestamp * 1000).toLocaleTimeString();
}

function adminApiUrl(path) {
  return `${ADMIN_API_BASE}${path}`;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}
