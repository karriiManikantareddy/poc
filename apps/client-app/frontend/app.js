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
let connectorsTabLoaded = false;
document.querySelectorAll("nav.side .item").forEach((el) => {
  el.addEventListener("click", () => {
    document.querySelectorAll("nav.side .item").forEach((n) => n.classList.remove("active"));
    document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
    el.classList.add("active");
    document.getElementById(`tab-${el.dataset.tab}`).classList.add("active");
    if (el.dataset.tab === "connectors" && !connectorsTabLoaded) {
      connectorsTabLoaded = true;
      loadConnections();
      loadConnectors();
    }
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

    <div class="section-title">Visible to</div>
    <p class="hint" style="margin:0;">${
      (agent.visible_to_groups || []).length
        ? `Restricted to: ${agent.visible_to_groups.map(escapeHtml).join(", ")}`
        : "Everyone in this workspace (no restriction set)"
    }</p>

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
  return `<div style="margin-bottom:8px;"><strong>Answer:</strong> ${escapeHtml(record.answer || "(no final answer produced)")}</div><div class="hint">Ran as: ${escapeHtml(record.ran_as || "unknown")}</div><div class="hint">${steps}</div>`;
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

  const groupsContainer = document.getElementById("ag-groups-list");
  groupsContainer.innerHTML = `<p class="hint">Loading groups...</p>`;
  try {
    const allGroups = await api("GET", "/groups");
    const selected = agent ? agent.visible_to_groups || [] : [];
    groupsContainer.innerHTML =
      allGroups
        .map(
          (g) => `<label class="checkbox-label" style="margin:4px 0;">
        <input type="checkbox" class="ag-group-checkbox" value="${escapeHtml(g)}" ${selected.includes(g) ? "checked" : ""}>
        ${escapeHtml(g)}
      </label>`
        )
        .join("") || `<p class="hint">No groups found in this workspace.</p>`;
  } catch (e) {
    groupsContainer.innerHTML = `<p class="hint">Could not load groups: ${escapeHtml(e.message)}</p>`;
  }
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
  const selectedGroups = Array.from(document.querySelectorAll(".ag-group-checkbox:checked")).map((el) => el.value);

  if (!name) return;

  const body = { name, prompt, model, tools: selectedTools, visible_to_groups: selectedGroups, status: "published" };
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

// ---------------- CONNECTORS ----------------
let connectorTypes = [];

async function loadConnectorTypes() {
  if (connectorTypes.length) return connectorTypes;
  connectorTypes = await api("GET", "/connector-types");
  return connectorTypes;
}

async function loadConnections() {
  const listEl = document.getElementById("connections-list");
  listEl.innerHTML = `<p class="hint">Loading...</p>`;
  try {
    const items = await api("GET", "/connections");
    if (!items.length) {
      listEl.innerHTML = `<p class="hint">No connections yet in this workspace.</p>`;
      return;
    }
    listEl.innerHTML = `<table>
      <thead><tr><th>Name</th><th>Type</th><th>Owner</th><th></th></tr></thead>
      <tbody>${items
        .map(
          (c) => `<tr>
            <td>${escapeHtml(c.name)}</td>
            <td>${escapeHtml(c.connection_type || "")}</td>
            <td>${escapeHtml(c.owner || "")}</td>
            <td><button class="btn danger small" data-name="${escapeHtml(c.name)}">Delete</button></td>
          </tr>`
        )
        .join("")}</tbody>
    </table>`;
    listEl.querySelectorAll("button[data-name]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        if (!confirm(`Delete connection "${btn.dataset.name}"? Any pipeline using it will start failing.`)) return;
        try {
          await api("DELETE", `/connections/${encodeURIComponent(btn.dataset.name)}`, undefined);
          await loadConnections();
        } catch (e) {
          alert(e.message);
        }
      });
    });
  } catch (e) {
    listEl.innerHTML = `<div class="msg-err">${escapeHtml(e.message)}</div>`;
  }
}

document.getElementById("refresh-connections-btn").addEventListener("click", loadConnections);

let priorConnectionNames = new Set();
let cnPollTimer = null;

function stopConnectionPoll() {
  if (cnPollTimer) {
    clearInterval(cnPollTimer);
    cnPollTimer = null;
  }
}

function startConnectionPoll(expectedType) {
  stopConnectionPoll();
  cnPollTimer = setInterval(async () => {
    let items;
    try {
      items = await api("GET", "/connections");
    } catch {
      return; // transient error while polling — just try again next tick
    }
    const found = items.find((c) => !priorConnectionNames.has(c.name) && c.connection_type === expectedType);
    if (found) {
      stopConnectionPoll();
      const area = document.getElementById("cn-form-area");
      area.innerHTML = `<div class="msg-ok" style="margin-top:12px;">Found it — <strong>${escapeHtml(found.name)}</strong> is ready to use. You can close this and create an ingestion pipeline with it.</div>`;
      loadConnections();
    }
  }, 4000);
}

document.getElementById("new-connection-btn").addEventListener("click", async () => {
  document.getElementById("cn-name").value = "";
  document.getElementById("cn-form-area").innerHTML = "";
  feedback(document.getElementById("cn-modal-feedback"), "", "");
  stopConnectionPoll();
  const typeSelect = document.getElementById("cn-type");
  typeSelect.innerHTML = `<option value="">Loading types...</option>`;
  document.getElementById("connection-modal").hidden = false;
  try {
    const types = await loadConnectorTypes();
    typeSelect.innerHTML =
      `<option value="">Select a source type...</option>` +
      types.map((t) => `<option value="${escapeHtml(t.type)}">${escapeHtml(t.type)}${t.form === "external" ? " (finish in Databricks)" : ""}</option>`).join("");
  } catch (e) {
    typeSelect.innerHTML = `<option value="">Could not load types: ${escapeHtml(e.message)}</option>`;
  }
  try {
    priorConnectionNames = new Set((await api("GET", "/connections")).map((c) => c.name));
  } catch {
    priorConnectionNames = new Set();
  }
});

document.getElementById("cn-type").addEventListener("change", () => {
  const type = document.getElementById("cn-type").value;
  const info = connectorTypes.find((t) => t.type === type);
  const area = document.getElementById("cn-form-area");
  stopConnectionPoll();
  if (!info) {
    area.innerHTML = "";
  } else if (info.form === "host") {
    area.innerHTML = info.fields
      .map((f) => {
        const label = `${f.name}${f.required ? "" : " (optional)"}`;
        if (f.name === "GoogleServiceAccountKeyJson") {
          return `<label for="cn-field-${f.name}">${label}</label><textarea id="cn-field-${f.name}" rows="6" class="code-input" placeholder="Paste the full contents of the service account key .json file"></textarea>`;
        }
        const inputType = f.name === "password" ? "password" : "text";
        return `<label for="cn-field-${f.name}">${label}</label><input type="${inputType}" id="cn-field-${f.name}" placeholder="${f.name}">`;
      })
      .join("");
  } else {
    area.innerHTML = `<div class="msg-pending" style="margin-top:12px;">
      This source needs an OAuth sign-in Databricks doesn't expose an API for — that step has to happen on Databricks' own page.
      In your Databricks workspace: <strong>Catalog</strong> &rarr; gear icon &rarr; <strong>Connections</strong> &rarr; <strong>Create connection</strong>,
      pick <strong>${escapeHtml(type)}</strong>, and sign in. Leave this open — it'll detect the new connection automatically and update below,
      no need to click Refresh yourself.
    </div>`;
    startConnectionPoll(type);
  }
});

document.getElementById("connection-modal-cancel").addEventListener("click", () => {
  stopConnectionPoll();
  document.getElementById("connection-modal").hidden = true;
});

document.getElementById("connection-modal-save").addEventListener("click", async () => {
  const name = document.getElementById("cn-name").value.trim();
  const type = document.getElementById("cn-type").value;
  const info = connectorTypes.find((t) => t.type === type);
  const resultEl = document.getElementById("cn-modal-feedback");
  if (!name || !type) return;
  if (!info || info.form !== "host") {
    feedback(resultEl, "err", "This source type must be created in Databricks first — see the instructions above.");
    return;
  }
  const fields = {};
  const missingRequired = [];
  for (const f of info.fields) {
    const value = document.getElementById(`cn-field-${f.name}`).value;
    fields[f.name] = value;
    if (f.required && !value.trim()) missingRequired.push(f.name);
  }
  if (missingRequired.length) {
    feedback(resultEl, "err", `Missing required field(s): ${escapeHtml(missingRequired.join(", "))}`);
    return;
  }
  if (fields.GoogleServiceAccountKeyJson) {
    try {
      JSON.parse(fields.GoogleServiceAccountKeyJson);
    } catch {
      feedback(resultEl, "err", "GoogleServiceAccountKeyJson isn't valid JSON — paste the full, unmodified contents of the key file.");
      return;
    }
  }
  feedback(resultEl, "pending", "Creating...");
  try {
    await api("POST", "/connections", { name, connection_type: type, fields });
    document.getElementById("connection-modal").hidden = true;
    await loadConnections();
  } catch (e) {
    feedback(resultEl, "err", escapeHtml(e.message));
  }
});

async function loadConnectors() {
  const listEl = document.getElementById("connectors-list");
  listEl.innerHTML = `<p class="hint">Loading...</p>`;
  try {
    const items = await api("GET", "/connectors");
    if (!items.length) {
      listEl.innerHTML = `<p class="hint">No ingestion pipelines yet — click "+ New Ingestion Pipeline" to create one.</p>`;
      return;
    }
    listEl.innerHTML = `<table>
      <thead><tr><th>Label</th><th>Connection</th><th>Destination</th><th>State</th><th>Last run</th><th></th></tr></thead>
      <tbody>${items
        .map(
          (c) => `<tr>
            <td>${escapeHtml(c.label)}</td>
            <td>${escapeHtml(c.connection_name)}</td>
            <td>${escapeHtml(c.destination_catalog)}.${escapeHtml(c.destination_schema)}</td>
            <td>${escapeHtml(c.state || "unknown")}</td>
            <td>${escapeHtml(c.last_update_state || "never run")}</td>
            <td style="display:flex;gap:6px;">
              <button class="btn secondary small" data-run="${escapeHtml(c.id)}">Run now</button>
              <button class="btn danger small" data-del="${escapeHtml(c.id)}">Delete</button>
            </td>
          </tr>`
        )
        .join("")}</tbody>
    </table>`;
    listEl.querySelectorAll("button[data-run]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        btn.disabled = true;
        try {
          await api("POST", `/connectors/${btn.dataset.run}/run`, {});
          await loadConnectors();
        } catch (e) {
          alert(e.message);
          btn.disabled = false;
        }
      });
    });
    listEl.querySelectorAll("button[data-del]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        if (!confirm("Delete this ingestion pipeline? This does not delete already-synced data.")) return;
        try {
          await api("DELETE", `/connectors/${btn.dataset.del}`, undefined);
          await loadConnectors();
        } catch (e) {
          alert(e.message);
        }
      });
    });
  } catch (e) {
    listEl.innerHTML = `<div class="msg-err">${escapeHtml(e.message)}</div>`;
  }
}

document.getElementById("new-connector-btn").addEventListener("click", async () => {
  document.getElementById("cc-label").value = "";
  document.getElementById("cc-source-schema").value = "";
  document.getElementById("cc-tables").value = "";
  document.getElementById("cc-cron").value = "";
  document.getElementById("cc-sync-mode").value = "schema";
  document.getElementById("cc-tables-area").style.display = "none";
  feedback(document.getElementById("cc-modal-feedback"), "", "");

  const connSelect = document.getElementById("cc-connection");
  connSelect.innerHTML = `<option value="">Loading connections...</option>`;
  const catSelect = document.getElementById("cc-catalog");
  catSelect.innerHTML = `<option value="">Loading catalogs...</option>`;
  document.getElementById("cc-schema").innerHTML = `<option value="">Pick a catalog first</option>`;

  document.getElementById("connector-modal").hidden = false;

  try {
    const conns = await api("GET", "/connections");
    connSelect.innerHTML = conns.length
      ? `<option value="">Select a connection...</option>` + conns.map((c) => `<option value="${escapeHtml(c.name)}">${escapeHtml(c.name)} (${escapeHtml(c.connection_type)})</option>`).join("")
      : `<option value="">No connections yet — create one first</option>`;
  } catch (e) {
    connSelect.innerHTML = `<option value="">Could not load: ${escapeHtml(e.message)}</option>`;
  }

  try {
    const cats = await api("GET", "/catalogs");
    catSelect.innerHTML = `<option value="">Select a catalog...</option>` + cats.map((c) => `<option value="${escapeHtml(c)}">${escapeHtml(c)}</option>`).join("");
  } catch (e) {
    catSelect.innerHTML = `<option value="">Could not load: ${escapeHtml(e.message)}</option>`;
  }
});

document.getElementById("cc-sync-mode").addEventListener("change", (e) => {
  document.getElementById("cc-tables-area").style.display = e.target.value === "tables" ? "block" : "none";
});

document.getElementById("cc-catalog").addEventListener("change", async () => {
  const catalog = document.getElementById("cc-catalog").value;
  const schemaSelect = document.getElementById("cc-schema");
  if (!catalog) {
    schemaSelect.innerHTML = `<option value="">Pick a catalog first</option>`;
    return;
  }
  schemaSelect.innerHTML = `<option value="">Loading schemas...</option>`;
  try {
    const schemas = await api("GET", `/catalogs/${encodeURIComponent(catalog)}/schemas`);
    schemaSelect.innerHTML = `<option value="">Select a schema...</option>` + schemas.map((s) => `<option value="${escapeHtml(s)}">${escapeHtml(s)}</option>`).join("");
  } catch (e) {
    schemaSelect.innerHTML = `<option value="">Could not load: ${escapeHtml(e.message)}</option>`;
  }
});

document.getElementById("connector-modal-cancel").addEventListener("click", () => {
  document.getElementById("connector-modal").hidden = true;
});

document.getElementById("connector-modal-save").addEventListener("click", async () => {
  const resultEl = document.getElementById("cc-modal-feedback");
  const label = document.getElementById("cc-label").value.trim();
  const connection_name = document.getElementById("cc-connection").value;
  const source_schema = document.getElementById("cc-source-schema").value.trim();
  const sync_mode = document.getElementById("cc-sync-mode").value;
  const destination_catalog = document.getElementById("cc-catalog").value;
  const destination_schema = document.getElementById("cc-schema").value;
  const schedule_cron = document.getElementById("cc-cron").value.trim();
  const tables = document.getElementById("cc-tables").value.split(",").map((t) => t.trim()).filter(Boolean);

  if (!label || !connection_name || !source_schema || !destination_catalog || !destination_schema) {
    feedback(resultEl, "err", "Fill in label, connection, source schema, and destination catalog/schema.");
    return;
  }
  if (sync_mode === "tables" && !tables.length) {
    feedback(resultEl, "err", "List at least one table, or switch to 'Entire schema'.");
    return;
  }

  feedback(resultEl, "pending", "Creating pipeline...");
  try {
    await api("POST", "/connectors", {
      label,
      connection_name,
      destination_catalog,
      destination_schema,
      sync_mode,
      source_schema,
      tables,
      schedule_cron,
    });
    document.getElementById("connector-modal").hidden = true;
    await loadConnectors();
  } catch (e) {
    feedback(resultEl, "err", escapeHtml(e.message));
  }
});

// ---------------- init ----------------
(async function init() {
  try {
    const who = await api("GET", "/whoami");
    document.getElementById("whoami-badge").textContent = who.email
      ? `Signed in as ${who.email}${who.is_admin ? " (admin)" : ""}`
      : "Local dev — no forwarded identity, full access";
  } catch {
    /* non-critical, leave badge blank */
  }
  await loadTools();
  await loadAgents();
})();
