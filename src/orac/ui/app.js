async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) {
    throw new Error(await response.text());
  }
  return response.json();
}

async function postJson(path, body) {
  return api(path, { method: "POST", body: JSON.stringify(body) });
}

let manualRunBusy = false;

function text(value) {
  return value === null || value === undefined || value === "" ? "n/a" : String(value);
}

function html(value) {
  return text(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function formatMoney(value) {
  const number = Number(value || 0);
  return `$${number.toFixed(2)}`;
}

function formatTime(value) {
  if (!value) return "never";
  const date = typeof value === "number" ? new Date(value * 1000) : new Date(value);
  if (Number.isNaN(date.valueOf())) return "unknown";
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function relativeTime(value) {
  if (!value) return "never";
  const date = typeof value === "number" ? new Date(value * 1000) : new Date(value);
  const seconds = Math.max(0, Math.round((Date.now() - date.valueOf()) / 1000));
  if (!Number.isFinite(seconds)) return "unknown";
  if (seconds < 5) return "just now";
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  return `${hours}h ago`;
}

function latestLog(task) {
  const log = task.work_log || [];
  return log.length ? log[log.length - 1] : null;
}

function isActiveTask(task) {
  return ["in_progress", "pending_approval", "clarifying", "review", "ready"].includes(task.status);
}

function currentFocusTask(tasks) {
  const priority = ["in_progress", "pending_approval", "clarifying", "review", "ready", "blocked", "backlog"];
  for (const status of priority) {
    const task = tasks.find((candidate) => candidate.status === status);
    if (task) return task;
  }
  return null;
}

function tickTouchedCount(lastTick) {
  return Number(lastTick?.result?.touched_tasks ?? lastTick?.touched_tasks ?? 0);
}

function nextActionFor(task) {
  if (task.status === "clarifying") return "Answer the latest intent question.";
  if (task.status === "pending_approval") return "Review and approve or deny the parked action.";
  if (task.status === "blocked") return "Inspect the blocker and unblock or revise the task.";
  if (task.status === "ready") return "Next scrum tick can start the doer session.";
  if (task.status === "in_progress") return "Agent work is underway.";
  if (task.status === "review") return "Review the result.";
  if (task.status === "backlog") return "Awaiting intent clarification.";
  return "No action needed.";
}

function renderStats(stats) {
  const target = document.querySelector("#stats");
  target.innerHTML = Object.entries(stats)
    .map(([key, value]) => `<div class="stat"><strong>${html(value)}</strong>${html(key)}</div>`)
    .join("");
}

function renderTasks(tasks) {
  const target = document.querySelector("#tasks");
  if (!tasks.length) {
    target.innerHTML = `<p class="empty-state">No tasks yet.</p>`;
    return;
  }
  target.innerHTML = tasks.map(renderTaskCard).join("");
}

function renderTaskCard(task) {
  const log = latestLog(task);
  const kind = task.work_kind ? ` · ${task.work_kind}` : "";
  const assignee = task.assignee || "unassigned";
  const message = log ? `${log.agent}: ${log.message}` : nextActionFor(task);
  return `
    <article class="task">
      <div class="task-title">
        <strong>${html(task.title)}</strong>
        <span class="status-${html(task.status)}">${html(task.status)}</span>
      </div>
      <div class="task-meta">${html(task.id)} · ${html(task.points)} point(s) · ${html(assignee)}${html(kind)}</div>
      <div class="task-message">${html(message)}</div>
      <div class="task-meta">Next: ${html(nextActionFor(task))}</div>
    </article>`;
}

function renderRunStatus(state, loopStatus) {
  const target = document.querySelector("#run-status");
  const running = Boolean(loopStatus.running);
  const stopping = Boolean(loopStatus.stopping);
  const current = currentFocusTask(state.tasks);
  const decision = loopStatus.last_tick?.model_decision || state.model_policy;
  const lastTick = loopStatus.last_tick;
  const lastTouched = lastTick ? `${tickTouchedCount(lastTick)} task(s) touched` : "No tick yet";
  const nextWake = running && lastTick?.at && state.settings?.daemon_interval_seconds
    ? new Date((lastTick.at + Number(state.settings.daemon_interval_seconds)) * 1000)
    : null;
  const nextWakeText = nextWake ? formatTime(nextWake) : "n/a";
  const stateClass = loopStatus.last_error ? "error" : stopping ? "stopping" : running ? "running" : "stopped";
  target.className = `run-status panel ${stateClass}`;
  target.innerHTML = `
    <div class="run-state">
      <strong>${stopping ? "Stopping" : running ? "Running" : "Stopped"}</strong>
      <span>${html(lastTouched)} · last tick ${html(relativeTime(lastTick?.at))}</span>
    </div>
    <div class="run-focus">
      <strong>${html(current ? current.title : "No current task")}</strong><br />
      ${html(current ? `${current.status} · ${nextActionFor(current)}` : "Add a request to give ORAC work.")}
      ${loopStatus.last_error ? `<br /><span class="status-blocked">Loop error ${html(relativeTime(loopStatus.last_error_at))}: ${html(loopStatus.last_error)}</span>` : ""}
    </div>
    <div class="run-meta">
      Model: <strong>${html(decision.brain)}/${html(decision.model)}</strong><br />
      Next wake: ${html(nextWakeText)}
    </div>`;
  renderLoopControls(loopStatus);
}

function setPressedButton(selector, pressed, disabled = false) {
  const button = document.querySelector(selector);
  button.setAttribute("aria-pressed", pressed ? "true" : "false");
  button.classList.toggle("is-active", pressed);
  button.disabled = disabled;
}

function renderLoopControls(loopStatus) {
  const running = Boolean(loopStatus.running);
  const stopping = Boolean(loopStatus.stopping);
  setPressedButton("#run-cycle", manualRunBusy, running || manualRunBusy);
  setPressedButton("#start-loop", running && !stopping, running || manualRunBusy);
  setPressedButton("#stop-loop", stopping, !running || stopping);
}

function renderAttention(state, loopStatus) {
  const items = [];
  if (loopStatus.last_error) {
    items.push({ type: "error", title: "Loop error", meta: "Runtime", message: loopStatus.last_error });
  }
  const review = state.review_queue || {};
  if (Number(review.pending_approvals || 0) > 0) {
    items.push({
      type: "approval",
      title: `${review.pending_approvals} approval(s) pending`,
      meta: "Review queue",
      message: "Approve, deny, or inspect parked actions before they can resume.",
    });
  }
  if (Number(review.unacked_notifications || 0) > 0) {
    items.push({
      type: "approval",
      title: `${review.unacked_notifications} action(s) awaiting review`,
      meta: "Review queue",
      message: "Acknowledge accepted actions or roll back anything the review rejects.",
    });
  }
  for (const task of state.tasks) {
    if (task.status === "clarifying") {
      const log = latestLog(task);
      items.push({ type: "clarifying", title: task.title, meta: `${task.id} · intent`, message: log?.message || nextActionFor(task) });
    } else if (task.status === "pending_approval") {
      items.push({ type: "approval", title: task.title, meta: `${task.id} · approval`, message: nextActionFor(task) });
    } else if (task.status === "blocked") {
      const log = latestLog(task);
      items.push({ type: "blocked", title: task.title, meta: `${task.id} · blocked`, message: log?.message || task.description });
    }
  }
  if (state.resources && state.resources.disk_free_gb !== null && Number(state.resources.disk_free_gb) < 10.0) {
    items.push({
      type: "blocked",
      title: "Low disk space warning",
      meta: "System resources",
      message: `Only ${state.resources.disk_free_gb} GB of disk space remaining.`,
    });
  }

  document.querySelector("#attention-count").textContent = String(items.length);
  const target = document.querySelector("#attention-list");
  if (!items.length) {
    target.innerHTML = `<p class="empty-state">No clarifications, approvals, blockers, or loop errors need attention.</p>`;
    return;
  }
  target.innerHTML = items.slice(0, 6).map((item) => `
    <article class="attention-item ${html(item.type)}">
      <div class="item-title"><strong>${html(item.title)}</strong><span>${html(item.type)}</span></div>
      <div class="item-meta">${html(item.meta)}</div>
      <div class="item-message">${html(item.message)}</div>
    </article>`).join("");
}

function renderActiveWork(tasks) {
  const target = document.querySelector("#active-work");
  const active = tasks.filter(isActiveTask);
  const visible = active.length ? active : tasks.filter((task) => task.status === "backlog").slice(0, 3);
  if (!visible.length) {
    target.innerHTML = `<p class="empty-state">No active work. Add a request or start the loop when work is available.</p>`;
    return;
  }
  target.innerHTML = visible.map(renderTaskCard).join("");
}

function resourceSeverity(value, warning = 60, danger = 85) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "";
  if (number >= danger) return "danger";
  if (number >= warning) return "warning";
  return "";
}

function renderResourceSummary(resources, policy) {
  const cards = [
    ["CPU", resources.cpu_percent === null ? "n/a" : `${resources.cpu_percent}%`, resourceSeverity(resources.cpu_percent, 60, 85)],
    ["Memory", resources.memory_percent === null ? "n/a" : `${resources.memory_percent}%`, resourceSeverity(resources.memory_percent, 70, 85)],
    ["GPU", resources.gpu_percent === null ? "n/a" : `${resources.gpu_percent}%`, resourceSeverity(resources.gpu_percent, 60, 85)],
    ["VRAM", resources.vram_percent === null ? "n/a" : `${resources.vram_percent}%`, resourceSeverity(resources.vram_percent, 70, 85)],
    ["Routing", `${policy.brain}/${policy.model}`, resources.busy ? "warning" : ""],
    ["Budget left", formatMoney(policy.foundation_remaining_today_usd), Number(policy.foundation_remaining_today_usd) <= 0 ? "warning" : ""],
  ];
  document.querySelector("#resource-summary").innerHTML = cards.map(([label, value, severity]) => `
    <div class="resource-card ${html(severity)}">
      <span>${html(label)}</span>
      <strong>${html(value)}</strong>
    </div>`).join("");
}

function renderResources(resources) {
  document.querySelector("#resources").innerHTML = [
    ["CPU used %", resources.cpu_percent],
    ["Memory used %", resources.memory_percent],
    ["Memory available GB", resources.memory_available_gb],
    ["Memory total GB", resources.memory_total_gb],
    ["GPU compute used %", resources.gpu_percent],
    ["GPU memory used %", resources.vram_percent],
    ["Disk free GB", resources.disk_free_gb],
    ["Tier", resources.recommended_tier],
    ["Reason", resources.reason],
  ]
    .map(([key, value]) => `<div class="resource-row"><span>${html(key)}</span><strong>${html(value)}</strong></div>`)
    .join("");
}

function renderModelPolicy(policy) {
  const loadedNames = window.oracLoadedModels && window.oracLoadedModels.length
    ? window.oracLoadedModels.map((model) => model.identifier || model.modelKey || model.path || model.displayName || model.id).join(", ")
    : "none";
  document.querySelector("#model-policy").innerHTML = [
    ["Currently loaded local model", loadedNames],
    ["Brain", policy.brain],
    ["Model", policy.model],
    ["Reason", policy.reason],
    ["Daily cap", formatMoney(policy.daily_foundation_cap_usd)],
    ["Spent today", formatMoney(policy.foundation_spent_today_usd)],
    ["Remaining", formatMoney(policy.foundation_remaining_today_usd)],
  ]
    .map(([key, value]) => `<div class="resource-row"><span>${html(key)}</span><strong>${html(value)}</strong></div>`)
    .join("");
}

function renderDecisions(state, loopStatus) {
  const decisions = [];
  const decision = loopStatus.last_tick?.model_decision || state.model_policy;
  decisions.push({
    title: `Model policy selected ${decision.brain}/${decision.model}`,
    meta: "Routing decision",
    message: decision.reason,
  });
  const review = state.review_queue || {};
  decisions.push({
    title: review.is_clear ? "Review queue is clear" : "Review queue needs attention",
    meta: `${review.pending_approvals || 0} approval(s), ${review.unacked_notifications || 0} unacked notification(s)`,
    message: review.is_clear ? "No parked or unacknowledged actions are blocking the cockpit." : "Inspect the review queue before expecting parked work to resume.",
  });
  for (const event of state.interactions || []) {
    const lower = String(event.message || "").toLowerCase();
    const isDecision = ["intent", "system", "registry"].includes(String(event.agent || "").toLowerCase())
      || lower.includes("selected")
      || lower.includes("locked")
      || lower.includes("blocked")
      || lower.includes("approval")
      || lower.includes("clarify")
      || lower.includes("ready");
    if (!isDecision) continue;
    decisions.push({
      title: `${event.agent} · ${event.task_title}`,
      meta: event.created_at,
      message: event.message,
    });
    if (decisions.length >= 6) break;
  }
  document.querySelector("#decisions").innerHTML = decisions.slice(0, 6).map((item) => `
    <article class="decision">
      <div class="decision-title"><strong>${html(item.title)}</strong></div>
      <div class="decision-meta">${html(item.meta)}</div>
      <div class="decision-message">${html(item.message)}</div>
    </article>`).join("");
}

function renderAudio(audio) {
  document.querySelector("#audio-status").innerHTML = [
    ["Current default microphone", audio.default_microphone || "none detected"],
    ["Current default speaker", audio.default_speaker || "none detected"],
    ["Whisper", audio.whisper_available ? "available" : "not installed"],
    ["Local TTS", audio.tts_available ? "available" : "not available"],
  ]
    .map(([key, value]) => `<div class="resource-row"><span>${html(key)}</span><strong>${html(value)}</strong></div>`)
    .join("");
}

function credentialCount(credentials) {
  const values = Object.values(credentials || {});
  return `${values.filter((item) => item.stored).length}/${values.length}`;
}

function renderAllowlist(channel, senders) {
  const target = document.querySelector(`#${channel}-allowlist`);
  if (!senders || !senders.length) {
    target.innerHTML = `<p class="empty-state">No allowed senders.</p>`;
    return;
  }
  target.innerHTML = senders.map((sender) => `
    <span class="allow-pill">
      <span>${html(sender)}</span>
      <button type="button" class="remove-sender" data-channel="${html(channel)}" data-sender="${html(sender)}">Remove</button>
    </span>`).join("");
}

function renderQrBox(whatsapp) {
  const target = document.querySelector("#whatsapp-qr");
  const bridge = whatsapp.bridge || {};
  const qr = bridge.qr || "";
  if (qr && String(qr).startsWith("data:image/")) {
    target.innerHTML = `<img src="${html(qr)}" alt="WhatsApp pairing QR" />`;
    return;
  }
  if (qr) {
    target.innerHTML = `<pre>${html(qr)}</pre>`;
    return;
  }
  target.innerHTML = `<span>${html(bridge.message || "Bridge not reachable.")}</span>`;
}

function renderChat(chat) {
  const slack = chat.channels?.slack || {};
  const whatsapp = chat.channels?.whatsapp || {};
  const runtime = chat.runtime || {};
  const bridgeRuntime = runtime.whatsapp_bridge || {};
  const connectorsRuntime = runtime.connectors || {};
  document.querySelector("#chat-master-status").textContent = chat.enabled ? "on" : "off";
  document.querySelector("#slack-status").textContent =
    `${slack.enabled ? "on" : "off"} - secrets ${credentialCount(slack.credentials)}`;
  document.querySelector("#whatsapp-status").textContent =
    `${whatsapp.enabled ? "on" : "off"} - bridge ${whatsapp.bridge?.reachable ? "ready" : "offline"}`;
  document.querySelector("#whatsapp-bridge-runtime").textContent =
    bridgeRuntime.running
      ? `running pid ${bridgeRuntime.pid}`
      : bridgeRuntime.external_running
        ? "running externally"
        : "stopped";
  document.querySelector("#chat-connectors-runtime").textContent =
    connectorsRuntime.running ? `running pid ${connectorsRuntime.pid}` : "stopped";
  document.querySelector("#comms-log-path").textContent =
    bridgeRuntime.log_dir || connectorsRuntime.log_dir || "n/a";
  document.querySelector("#whatsapp-bridge-url").value = whatsapp.bridge_url || "http://localhost:8788";
  renderAllowlist("slack", slack.authorized_senders || []);
  renderAllowlist("whatsapp", whatsapp.authorized_senders || []);
  renderQrBox(whatsapp);
}

function openSettingsPane() {
  document.querySelector("#settings-backdrop").hidden = false;
  document.querySelector("#settings-pane").classList.add("open");
  document.querySelector("#settings-pane").setAttribute("aria-hidden", "false");
}

function closeSettingsPane() {
  document.querySelector("#settings-pane").classList.remove("open");
  document.querySelector("#settings-pane").setAttribute("aria-hidden", "true");
  document.querySelector("#settings-backdrop").hidden = true;
}

function renderSettings(settings) {
  document.querySelector("#setting-monthly-foundation").value = settings.monthly_foundation_budget_usd;
  document.querySelector("#setting-cycle-cost").value = settings.estimated_foundation_cycle_usd;
  document.querySelector("#setting-loop-interval").value = settings.daemon_interval_seconds;
  document.querySelector("#setting-loop-cycles").value = settings.daemon_cycles;
  document.querySelector("#setting-lmstudio-url").value = settings.lmstudio_url;
  document.querySelector("#setting-standard-model").value = settings.lmstudio_standard_model;
  document.querySelector("#setting-small-model").value = settings.lmstudio_small_model;
}

function renderLoopStatusText(status) {
  const last = status.last_tick ? `Last tick touched ${tickTouchedCount(status.last_tick)} task(s).` : "No tick yet.";
  const state = status.stopping ? "Loop stopping." : status.running ? "Loop running." : "Loop stopped.";
  const error = status.last_error ? ` Last error: ${status.last_error}` : "";
  document.querySelector("#loop-status").textContent =
    `${state} The wake interval controls how often agents check for work; it is not a keepalive. ${last}${error}`;
}

function renderTimeline(events) {
  const target = document.querySelector("#timeline");
  if (!events.length) {
    target.innerHTML = `<p class="empty-state">No interaction yet.</p>`;
    return;
  }
  target.innerHTML = events
    .map(
      (event) => `
        <article class="event event-${html(event.kind)}">
          <div class="event-head">
            <span><span class="speaker">${html(event.agent)}</span> - ${html(event.task_title)}</span>
            <time>${html(event.created_at)}</time>
          </div>
          <div class="event-message">${html(event.message)}</div>
        </article>`
    )
    .join("");
}

async function refresh() {
  const [state, loopStatus, chat] = await Promise.all([
    api("/api/state"),
    api("/api/loop/status"),
    api("/api/chat"),
  ]);
  window.oracLoadedModels = state.loaded_models || [];
  renderRunStatus(state, loopStatus);
  renderAttention(state, loopStatus);
  renderActiveWork(state.tasks);
  renderResourceSummary(state.resources, state.model_policy);
  renderResources(state.resources);
  renderModelPolicy(state.model_policy);
  renderDecisions(state, loopStatus);
  renderStats(state.stats);
  renderTasks(state.tasks);
  renderAudio(state.audio);
  renderSettings(state.settings);
  renderTimeline(state.interactions);
  renderLoopStatusText(loopStatus);
  renderChat(chat);
  renderServices(loopStatus, chat);
}

// The main-page Services panel: one start/stop pair per long-running service,
// reusing the same endpoints as the controls in the Connections pane. The
// webpage (UI server) is the persistent shell; these toggle the services it hosts.
function setServiceRow(prefix, running, label) {
  const status = document.querySelector(`#${prefix}-status`);
  if (status) {
    status.textContent = label;
    status.classList.toggle("svc-on", running);
    status.classList.toggle("svc-off", !running);
  }
  const start = document.querySelector(`#${prefix}-start`);
  const stop = document.querySelector(`#${prefix}-stop`);
  if (start) start.disabled = running;
  if (stop) stop.disabled = !running;
}

function renderServices(loopStatus, chat) {
  const runtime = chat.runtime || {};
  const bridge = runtime.whatsapp_bridge || {};
  const connectors = runtime.connectors || {};
  const loopRunning = Boolean(loopStatus.running);
  setServiceRow(
    "svc-loop",
    loopRunning,
    loopStatus.stopping ? "stopping" : loopRunning ? "running" : "stopped",
  );
  const bridgeRunning = Boolean(bridge.running || bridge.external_running);
  setServiceRow(
    "svc-wa",
    bridgeRunning,
    bridge.running
      ? `running pid ${bridge.pid}`
      : bridge.external_running
        ? "running externally"
        : "stopped",
  );
  const connRunning = Boolean(connectors.running);
  setServiceRow("svc-chat", connRunning, connRunning ? `running pid ${connectors.pid}` : "stopped");
}

function wireServiceButton(selector, path) {
  const button = document.querySelector(selector);
  if (!button) return;
  button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      await postJson(path, {});
    } finally {
      await refresh();
    }
  });
}

wireServiceButton("#svc-loop-start", "/api/loop/start");
wireServiceButton("#svc-loop-stop", "/api/loop/stop");
wireServiceButton("#svc-wa-start", "/api/chat/runtime/whatsapp/start");
wireServiceButton("#svc-wa-stop", "/api/chat/runtime/whatsapp/stop");
wireServiceButton("#svc-chat-start", "/api/chat/runtime/connectors/start");
wireServiceButton("#svc-chat-stop", "/api/chat/runtime/connectors/stop");

document.querySelector("#request-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  await postJson("/api/requests", {
    title: document.querySelector("#request-title").value,
    description: document.querySelector("#request-description").value,
    points: Number(document.querySelector("#request-points").value || 1),
  });
  event.target.reset();
  document.querySelector("#request-points").value = 1;
  document.querySelector("#add-request-drawer").open = false;
  await refresh();
});

document.querySelector("#show-add-request").addEventListener("click", () => {
  const drawer = document.querySelector("#add-request-drawer");
  drawer.open = true;
  document.querySelector("#request-title").focus();
});

document.querySelector("#open-settings-pane").addEventListener("click", openSettingsPane);
document.querySelector("#open-connections-pane").addEventListener("click", openSettingsPane);
document.querySelector("#close-settings-pane").addEventListener("click", closeSettingsPane);
document.querySelector("#settings-backdrop").addEventListener("click", closeSettingsPane);
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeSettingsPane();
});

document.querySelector("#slack-connect-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const result = await postJson("/api/chat/slack/connect", {
    bot_token: document.querySelector("#slack-bot-token").value,
    app_token: document.querySelector("#slack-app-token").value,
  });
  document.querySelector("#slack-bot-token").value = "";
  document.querySelector("#slack-app-token").value = "";
  document.querySelector("#chat-message").textContent = "Slack connected.";
  renderChat(result);
});

document.querySelector("#whatsapp-connect-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const result = await postJson("/api/chat/whatsapp/connect", {
    bridge_url: document.querySelector("#whatsapp-bridge-url").value,
  });
  document.querySelector("#chat-message").textContent = result.channels?.whatsapp?.bridge?.reachable
    ? "WhatsApp bridge ready."
    : "WhatsApp bridge offline.";
  renderChat(result);
});

document.querySelectorAll(".allow-form").forEach((form) => {
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const channel = form.dataset.channel;
    const input = form.querySelector("input");
    const result = await postJson("/api/chat/allow", { channel, sender: input.value });
    document.querySelector("#chat-message").textContent = `${channel} sender allowed.`;
    input.value = "";
    renderChat(result);
  });
});

async function runChatAction(path, message) {
  const target = document.querySelector("#chat-message");
  target.textContent = message;
  const result = await postJson(path, {});
  renderChat(result);
  target.textContent = "Ready.";
}

document.querySelector("#start-whatsapp-bridge").addEventListener("click", async () => {
  await runChatAction("/api/chat/runtime/whatsapp/start", "Starting WhatsApp bridge...");
});

document.querySelector("#stop-whatsapp-bridge").addEventListener("click", async () => {
  await runChatAction("/api/chat/runtime/whatsapp/stop", "Stopping WhatsApp bridge...");
});

document.querySelector("#restart-whatsapp-bridge").addEventListener("click", async () => {
  await runChatAction("/api/chat/runtime/whatsapp/restart", "Restarting WhatsApp bridge...");
});

document.querySelector("#start-chat-connectors").addEventListener("click", async () => {
  await runChatAction("/api/chat/runtime/connectors/start", "Starting chat control...");
});

document.querySelector("#stop-chat-connectors").addEventListener("click", async () => {
  await runChatAction("/api/chat/runtime/connectors/stop", "Stopping chat control...");
});

document.querySelector("#restart-chat-connectors").addEventListener("click", async () => {
  await runChatAction("/api/chat/runtime/connectors/restart", "Restarting chat control...");
});

document.querySelector(".chat-panel").addEventListener("click", async (event) => {
  const removeButton = event.target.closest(".remove-sender");
  if (removeButton) {
    const result = await postJson("/api/chat/disallow", {
      channel: removeButton.dataset.channel,
      sender: removeButton.dataset.sender,
    });
    document.querySelector("#chat-message").textContent = `${removeButton.dataset.channel} sender removed.`;
    renderChat(result);
    return;
  }
  const disconnectButton = event.target.closest(".disconnect-chat");
  if (disconnectButton) {
    const result = await postJson("/api/chat/disconnect", {
      channel: disconnectButton.dataset.channel,
    });
    document.querySelector("#chat-message").textContent = `${disconnectButton.dataset.channel} disconnected.`;
    renderChat(result);
  }
});

document.querySelector("#run-cycle").addEventListener("click", async () => {
  manualRunBusy = true;
  setPressedButton("#run-cycle", true, true);
  try {
    await postJson("/api/run", { cycles: 1 });
  } finally {
    manualRunBusy = false;
    await refresh();
  }
});

let audioStream = null;
let mediaRecorder = null;
let audioChunks = [];
let lastTranscript = "";

document.querySelector("#enable-audio").addEventListener("click", async () => {
  audioStream = await navigator.mediaDevices.getUserMedia({ audio: true });
  const devices = await navigator.mediaDevices.enumerateDevices();
  const inputCount = devices.filter((device) => device.kind === "audioinput").length;
  const outputCount = devices.filter((device) => device.kind === "audiooutput").length;
  document.querySelector("#audio-transcript").textContent =
    `Browser audio enabled. Inputs: ${inputCount}. Outputs: ${outputCount}.`;
  document.querySelector("#record-audio").disabled = false;
});

document.querySelector("#record-audio").addEventListener("click", () => {
  if (!audioStream) return;
  audioChunks = [];
  mediaRecorder = new MediaRecorder(audioStream);
  mediaRecorder.addEventListener("dataavailable", (event) => {
    if (event.data.size > 0) audioChunks.push(event.data);
  });
  mediaRecorder.addEventListener("stop", transcribeRecording);
  mediaRecorder.start();
  document.querySelector("#record-audio").disabled = true;
  document.querySelector("#stop-audio").disabled = false;
  document.querySelector("#audio-transcript").textContent = "Recording...";
});

document.querySelector("#stop-audio").addEventListener("click", () => {
  if (!mediaRecorder) return;
  mediaRecorder.stop();
  document.querySelector("#stop-audio").disabled = true;
});

document.querySelector("#speak-audio").addEventListener("click", async () => {
  if (!lastTranscript) return;
  try {
    await postJson("/api/audio/speak", { text: lastTranscript });
  } catch {
    if ("speechSynthesis" in window) {
      window.speechSynthesis.speak(new SpeechSynthesisUtterance(lastTranscript));
    }
  }
});

document.querySelector("#install-audio").addEventListener("click", async () => {
  const transcript = document.querySelector("#audio-transcript");
  transcript.textContent = "Installing audio stack. This can take a few minutes...";
  try {
    const result = await postJson("/api/audio/install", {});
    transcript.textContent = result.ok ? "Audio stack installed." : result.output;
    await refresh();
  } catch (error) {
    transcript.textContent = error.message;
  }
});

document.querySelector("#save-settings").addEventListener("click", async () => {
  await postJson("/api/settings", readSettingsForm());
  await refresh();
});

document.querySelector("#start-loop").addEventListener("click", async () => {
  await postJson("/api/loop/start", {});
  await refresh();
});

document.querySelector("#stop-loop").addEventListener("click", async () => {
  await postJson("/api/loop/stop", {});
  await refresh();
});

function readSettingsForm() {
  return {
    monthly_foundation_budget_usd: Number(document.querySelector("#setting-monthly-foundation").value),
    estimated_foundation_cycle_usd: Number(document.querySelector("#setting-cycle-cost").value),
    daemon_interval_seconds: Number(document.querySelector("#setting-loop-interval").value),
    daemon_cycles: Number(document.querySelector("#setting-loop-cycles").value),
    lmstudio_url: document.querySelector("#setting-lmstudio-url").value,
    lmstudio_standard_model: document.querySelector("#setting-standard-model").value,
    lmstudio_small_model: document.querySelector("#setting-small-model").value,
  };
}

async function transcribeRecording() {
  const blob = new Blob(audioChunks, { type: mediaRecorder.mimeType || "audio/webm" });
  const audio_base64 = await blobToBase64(blob);
  try {
    const result = await postJson("/api/audio/transcribe", {
      audio_base64,
      mime: blob.type || "audio/webm",
    });
    lastTranscript = result.text || "";
    document.querySelector("#audio-transcript").textContent = lastTranscript || "No speech detected.";
    if (lastTranscript) {
      const detail = document.querySelector("#request-description");
      detail.value = detail.value ? `${detail.value}\n${lastTranscript}` : lastTranscript;
      document.querySelector("#speak-audio").disabled = false;
      document.querySelector("#add-request-drawer").open = true;
    }
  } catch (error) {
    document.querySelector("#audio-transcript").textContent = error.message;
  } finally {
    document.querySelector("#record-audio").disabled = false;
  }
}

function blobToBase64(blob) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onloadend = () => resolve(String(reader.result).split(",", 2)[1] || "");
    reader.onerror = reject;
    reader.readAsDataURL(blob);
  });
}

refresh();
setInterval(refresh, 5000);
