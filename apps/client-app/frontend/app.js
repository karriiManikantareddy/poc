const API = "/api";

let agents = [];
let tools = [];
let selectedAgentId = null;
let editingAgentId = null; // null => creating new

async function api(method, path, body) {
  const res = await fetch(`${API}${path}`, {
    method,
    headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const data = res.status === 204 ? null : await res.json().catch(() => null);
  if (!res.ok) {
    const message = (data && data.detail) || `${method} ${path} failed: ${res.status}`;
    throw new Error(message);
  }
  return data;
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str == null ? "" : String(str);
  return div.innerHTML;
}

function feedback(el, kind, html) {
  el.innerHTML = kind ? `<div class="msg-${kind}">${html}</div>` : "";
}

// ---------------- tab switching ----------------
document.querySelectorAll("nav.side .item").forEach((el) => {
  el.addEventListener("click", () => {
    document.querySelectorAll("nav.side .item").forEach((n) => n.classList.remove("active"));
    document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
    el.classList.add("active");
    document.getElementById(`tab-${el.dataset.tab}`).classList.add("active");
  });
});

// ---------------- AGENTS ----------------
async function loadAgents() {
  agents = await api("GET", "/agents");
  renderAgentList();
}

async function loadTools() {
  tools = await api("GET", "/tools");
}

function renderAgentList() {
  const list = document.getElementById("agent-list");
  list.innerHTML = "";
  if (!agents.length) {
    list.innerHTML = `<p class="hint">No agents yet — click "+ New Agent" to create one.</p>`;
    return;
  }
  agents.forEach((a) => {
    const card = document.createElement("div");
    card.className = "client-card" + (a.id === selectedAgentId ? " selected" : "");
    card.innerHTML = `
      <div class="name">${escapeHtml(a.name)}</div>
      <div class="meta"><span class="pill ${a.status === "published" ? "obo-on" : "obo-off"}">${escapeHtml(a.status)}</span></div>
      <div class="meta">${escapeHtml(a.model || "no model selected")}</div>
    `;
    card.addEventListener("click", () => selectAgent(a.id));
    list.appendChild(card);
  });
}

async function selectAgent(id) {
  selectedAgentId = id;
  renderAgentList();
  const agent = agents.find((a) => a.id === id);
  const runs = await api("GET", `/agents/${id}/runs`);
  renderAgentDetail(agent, runs);
}

function renderAgentDetail(agent, runs) {
  const detail = document.getElementById("agent-detail");
  const attachedTools = tools.filter((t) => (agent.tools || []).includes(t.id));

  detail.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:flex-start;">
      <div>
        <h2 style="margin:0 0 2px;font-size:18px;">${escapeHtml(agent.name)}</h2>
        <div class="hint" style="margin:0;">${escapeHtml(agent.model || "no model selected")}</div>
      </div>
      <div style="display:flex;gap:8px;">
        <button class="btn secondary small" id="edit-agent-btn">Edit</button>
        <button class="btn danger small" id="delete-agent-btn">Delete</button>
      </div>
    </div>

    <div class="section-title">System prompt</div>
    <div class="code" style="white-space:pre-wrap;">${escapeHtml(agent.prompt || "(empty)")}</div>

    <div class="section-title">Tools attached</div>
    ${
      attachedTools.length
        ? `<ul style="margin:0;padding-left:20px;">${attachedTools.map((t) => `<li><strong>${escapeHtml(t.name)}</strong> — ${escapeHtml(t.description || "")}</li>`).join("")}</ul>`
        : `<p class="hint">No tools attached — this agent can only think, not act.</p>`
    }

    <div class="section-title">Ask this agent</div>
    <div style="display:flex;gap:10px;">
      <input type="text" id="run-question" placeholder="Ask a question..." style="flex:1;">
      <button class="btn" id="run-agent-btn">Run</button>
    </div>
    <div id="run-result" class="feedback"></div>

    <div class="section-title">Run history</div>
    <div id="run-history">${renderRunHistory(runs)}</div>
  `;

  document.getElementById("edit-agent-btn").addEventListener("click", () => openAgentModal(agent));
  document.getElementById("delete-agent-btn").addEventListener("click", async () => {
    if (!confirm(`Delete agent "${agent.name}"?`)) return;
    await api("DELETE", `/agents/${agent.id}`, undefined);
    selectedAgentId = null;
    document.getElementById("agent-detail").innerHTML = `<div class="empty-state"><p>Select an agent on the left, or create a new one.</p></div>`;
    await loadAgents();
  });

  const runBtn = document.getElementById("run-agent-btn");
  runBtn.addEventListener("click", async () => {
    const question = document.getElementById("run-question").value.trim();
    if (!question) return;
    const resultEl = document.getElementById("run-result");
    runBtn.disabled = true;
    feedback(resultEl, "pending", "Running — thinking, and acting on any tools it needs...");
    try {
      const record = await api("POST", `/agents/${agent.id}/run`, { question });
      feedback(resultEl, "ok", renderTrace(record));
      const runs2 = await api("GET", `/agents/${agent.id}/runs`);
      document.getElementById("run-history").innerHTML = renderRunHistory(runs2);
    } catch (e) {
      feedback(resultEl, "err", escapeHtml(e.message));
    } finally {
      runBtn.disabled = false;
    }
  });
}

function renderTrace(record) {
  const steps = record.trace
    .map((s) => {
      if (s.step === "think") {
        return `<div><strong>Think:</strong> ${escapeHtml(s.content || "(deciding what to do)")}${s.tool_calls.length ? ` <em>→ calling ${s.tool_calls.map(escapeHtml).join(", ")}</em>` : ""}</div>`;
      }
      return `<div><strong>Act (${escapeHtml(s.tool)}):</strong> ${escapeHtml(JSON.stringify(s.args))} → ${escapeHtml(s.result)}</div>`;
    })
    .join("");
  return `<div style="margin-bottom:8px;"><strong>Answer:</strong> ${escapeHtml(record.answer || "(no final answer produced)")}</div><div class="hint">${steps}</div>`;
}

function renderRunHistory(runs) {
  const sorted = [...runs].sort((a, b) => (a.timestamp < b.timestamp ? 1 : -1));
  if (!sorted.length) return `<p class="hint">No runs yet.</p>`;
  return `<table>
    <thead><tr><th>Question</th><th>Answer</th><th>When</th></tr></thead>
    <tbody>${sorted
      .map(
        (r) =>
          `<tr><td>${escapeHtml(r.question)}</td><td>${escapeHtml((r.answer || "").slice(0, 80))}</td><td>${new Date(r.timestamp).toLocaleString()}</td></tr>`
      )
      .join("")}</tbody>
  </table>`;
}

// ---------------- Agent modal (create/edit) ----------------
document.getElementById("new-agent-btn").addEventListener("click", () => openAgentModal(null));
document.getElementById("agent-modal-cancel").addEventListener("click", closeAgentModal);

function closeAgentModal() {
  document.getElementById("agent-modal").hidden = true;
}

async function openAgentModal(agent) {
  editingAgentId = agent ? agent.id : null;
  document.getElementById("agent-modal-title").textContent = agent ? "Edit Agent" : "New Agent";
  document.getElementById("ag-name").value = agent ? agent.name : "";
  document.getElementById("ag-prompt").value = agent ? agent.prompt || "" : "";

  const modelSelect = document.getElementById("ag-model");
  modelSelect.innerHTML = `<option value="">Loading endpoints...</option>`;
  document.getElementById("agent-modal").hidden = false;

  try {
    const endpoints = await api("GET", "/model-endpoints");
    modelSelect.innerHTML =
      `<option value="">Select a serving endpoint...</option>` +
      endpoints
        .map((e) => `<option value="${escapeHtml(e.name)}" ${agent && agent.model === e.name ? "selected" : ""}>${escapeHtml(e.name)} (${escapeHtml(e.ready)})</option>`)
        .join("");
  } catch (e) {
    modelSelect.innerHTML = `<option value="">Could not load endpoints: ${escapeHtml(e.message)}</option>`;
  }

  await loadTools();
  renderToolCheckboxes(agent ? agent.tools || [] : []);
}

function renderToolCheckboxes(selectedToolIds) {
  const container = document.getElementById("ag-tools-list");
  const list = tools
    .map(
      (t) => `
    <label class="checkbox-label" style="margin:4px 0;">
      <input type="checkbox" class="ag-tool-checkbox" value="${escapeHtml(t.id)}" ${selectedToolIds.includes(t.id) ? "checked" : ""}>
      ${escapeHtml(t.name)} — <span class="hint" style="margin:0;">${escapeHtml(t.description || "")}</span>
    </label>`
    )
    .join("");
  container.innerHTML = (list || `<p class="hint">No tools created yet.</p>`) + `<button class="btn secondary small" id="ag-new-tool-btn" style="margin-top:8px;">+ New Tool</button>`;
  document.getElementById("ag-new-tool-btn").addEventListener("click", () => openToolModal());
}

document.getElementById("agent-modal-save").addEventListener("click", async () => {
  const name = document.getElementById("ag-name").value.trim();
  const prompt = document.getElementById("ag-prompt").value;
  const model = document.getElementById("ag-model").value;
  const selectedTools = Array.from(document.querySelectorAll(".ag-tool-checkbox:checked")).map((el) => el.value);

  if (!name) return;

  const body = { name, prompt, model, tools: selectedTools, status: "published" };
  if (editingAgentId) {
    await api("PUT", `/agents/${editingAgentId}`, body);
  } else {
    await api("POST", "/agents", body);
  }
  closeAgentModal();
  await loadAgents();
  if (editingAgentId) selectAgent(editingAgentId);
});

// ---------------- Tool modal (create) ----------------
document.getElementById("tool-modal-cancel").addEventListener("click", () => {
  document.getElementById("tool-modal").hidden = true;
});

function openToolModal() {
  document.getElementById("tl-name").value = "";
  document.getElementById("tl-description").value = "";
  document.getElementById("tl-code").value = "";
  document.getElementById("tool-modal").hidden = false;
}

document.getElementById("tool-modal-save").addEventListener("click", async () => {
  const name = document.getElementById("tl-name").value.trim();
  const description = document.getElementById("tl-description").value.trim();
  const code = document.getElementById("tl-code").value;
  if (!name || !code) return;
  await api("POST", "/tools", { name, description, code });
  document.getElementById("tool-modal").hidden = true;
  await loadTools();
  renderToolCheckboxes(Array.from(document.querySelectorAll(".ag-tool-checkbox:checked")).map((el) => el.value));
});

// ---------------- init ----------------
(async function init() {
  await loadTools();
  await loadAgents();
})();
