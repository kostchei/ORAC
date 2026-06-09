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

function renderStats(stats) {
  const target = document.querySelector("#stats");
  target.innerHTML = Object.entries(stats)
    .map(([key, value]) => `<div class="stat"><strong>${html(value)}</strong>${html(key)}</div>`)
    .join("");
}

function renderTasks(tasks) {
  const target = document.querySelector("#tasks");
  if (!tasks.length) {
    target.innerHTML = `<p>No tasks yet.</p>`;
    return;
  }
  target.innerHTML = tasks
    .map(
      (task) => `
        <article class="task">
          <div class="task-title">
            <strong>${html(task.title)}</strong>
            <span class="status-${html(task.status)}">${html(task.status)}</span>
          </div>
          <div class="task-meta">${html(task.id)} - ${html(task.points)} point(s) - ${html(task.assignee)}</div>
          <div class="task-meta">${html(task.description || "")}</div>
        </article>`
    )
    .join("");
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
    ["Daily cap", `$${policy.daily_foundation_cap_usd}`],
    ["Spent today", `$${policy.foundation_spent_today_usd}`],
    ["Remaining", `$${policy.foundation_remaining_today_usd}`],
  ]
    .map(([key, value]) => `<div class="resource-row"><span>${html(key)}</span><strong>${html(value)}</strong></div>`)
    .join("");
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

function renderSettings(settings) {
  document.querySelector("#setting-monthly-foundation").value = settings.monthly_foundation_budget_usd;
  document.querySelector("#setting-cycle-cost").value = settings.estimated_foundation_cycle_usd;
  document.querySelector("#setting-loop-interval").value = settings.daemon_interval_seconds;
  document.querySelector("#setting-loop-cycles").value = settings.daemon_cycles;
  document.querySelector("#setting-lmstudio-url").value = settings.lmstudio_url;
  document.querySelector("#setting-standard-model").value = settings.lmstudio_standard_model;
  document.querySelector("#setting-small-model").value = settings.lmstudio_small_model;
}

async function renderLoopStatus() {
  const status = await api("/api/loop/status");
  const last = status.last_tick ? `Last tick touched ${status.last_tick.result.touched_tasks} task(s).` : "No tick yet.";
  document.querySelector("#loop-status").textContent =
    `${status.running ? "Loop running." : "Loop stopped."} The wake interval controls how often agents check for work; it is not a keepalive. ${last} ${status.last_error || ""}`;
}

function renderTimeline(events) {
  const target = document.querySelector("#timeline");
  if (!events.length) {
    target.innerHTML = `<p>No interaction yet.</p>`;
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
  const state = await api("/api/state");
  window.oracLoadedModels = state.loaded_models || [];
  renderStats(state.stats);
  renderTasks(state.tasks);
  renderResources(state.resources);
  renderModelPolicy(state.model_policy);
  renderAudio(state.audio);
  renderSettings(state.settings);
  renderTimeline(state.interactions);
  await renderLoopStatus();
}

document.querySelector("#request-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  await postJson("/api/requests", {
    title: document.querySelector("#request-title").value,
    description: document.querySelector("#request-description").value,
    points: Number(document.querySelector("#request-points").value || 1),
  });
  event.target.reset();
  document.querySelector("#request-points").value = 1;
  await refresh();
});

document.querySelector("#run-cycle").addEventListener("click", async () => {
  await postJson("/api/run", { cycles: 1 });
  await refresh();
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
  await renderLoopStatus();
});

document.querySelector("#stop-loop").addEventListener("click", async () => {
  await postJson("/api/loop/stop", {});
  await renderLoopStatus();
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
