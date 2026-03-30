const byId = (id) => document.getElementById(id);
const byIdAny = (...ids) => ids.map((id) => byId(id)).find(Boolean) || null;

const elems = {
  serverMeta: byId("server-meta"),
  frontendMeta: byId("frontend-meta"),
  asr: byId("mod-asr"),
  llm: byId("mod-llm"),
  tts: byId("mod-tts"),
  saveModules: byId("save-modules"),
  moduleMsg: byId("module-msg"),
  sumDevice: byId("sum-device"),
  sumDuration: byId("sum-duration"),
  sumUtterances: byId("sum-utterances"),
  sumLatency: byId("sum-latency"),
  sumTraffic: byId("sum-traffic"),
  sumError: byId("sum-error"),
  fallbackText: byId("fallback-text"),
  fallbackSend: byId("send-fallback"),
  fallbackMsg: byId("fallback-msg"),
  eventSeverity: byIdAny("event-severity"),
  connectionsSummary: byIdAny("connections-summary", "connections"),
  activeDeviceRaw: byIdAny("active-device-raw", "active-device"),
  statsRaw: byIdAny("stats-raw", "stats"),
  connectionsRaw: byIdAny("connections-raw", "connections"),
  events: byIdAny("events"),
};

let pollIntervalMs = 1000;

function fmtJson(obj) {
  return JSON.stringify(obj, null, 2);
}

function fmtBytes(bytes) {
  const mib = bytes / (1024 * 1024);
  return `${mib.toFixed(2)} MiB`;
}

function fmtDuration(sec) {
  const s = Number(sec || 0);
  const mm = Math.floor(s / 60);
  const ss = s % 60;
  return `${mm}m ${ss}s`;
}

async function loadStatus() {
  const resp = await fetch("/api/admin/status");
  if (!resp.ok) throw new Error("Failed to load status");
  return await resp.json();
}

async function loadEvents() {
  const sev = encodeURIComponent((elems.eventSeverity && elems.eventSeverity.value) || "error,warning");
  const resp = await fetch(`/api/admin/events?limit=30&severity=${sev}`);
  if (!resp.ok) throw new Error("Failed to load events");
  return await resp.json();
}

async function loadConnections() {
  const resp = await fetch("/api/admin/connections");
  if (!resp.ok) throw new Error("Failed to load connections");
  return await resp.json();
}

async function applyModules() {
  elems.moduleMsg.textContent = "Applying...";
  const payload = {
    asr_enabled: elems.asr.checked,
    llm_enabled: elems.llm.checked,
    tts_enabled: elems.tts.checked,
  };

  const resp = await fetch("/api/admin/modules", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });

  if (!resp.ok) {
    elems.moduleMsg.textContent = "Failed to update module switches.";
    return;
  }

  const data = await resp.json();
  elems.moduleMsg.textContent = `Applied at ${data.updated_at}`;
}

async function sendFallbackText() {
  const text = (elems.fallbackText.value || "").trim();
  if (!text) {
    elems.fallbackMsg.textContent = "Please enter text first.";
    return;
  }

  if (elems.fallbackMsg) elems.fallbackMsg.textContent = "Sending...";
  const resp = await fetch("/api/admin/send-text", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  });

  const data = await resp.json();
  if (!resp.ok || !data.ok) {
    if (elems.fallbackMsg) elems.fallbackMsg.textContent = `${data.code || "FAILED"}: ${data.message || "send failed"}`;
    return;
  }

  if (elems.fallbackMsg) elems.fallbackMsg.textContent = `Sent (${data.bytes} bytes), id=${data.message_id}`;
  elems.fallbackText.value = "";
}

function renderConnectionsSummary(connections) {
  const rows = [];
  const active = connections.active;
  if (active) {
    rows.push(
      `ACTIVE  ${active.remote_ip}:${active.remote_port}  ${fmtDuration(active.duration_sec)}  utt=${active.utterances}  in=${fmtBytes(active.input_audio_bytes)} out=${fmtBytes(active.output_audio_bytes)}`
    );
  }

  const history = (connections.history || []).slice(-5).reverse();
  for (const item of history) {
    rows.push(
      `CLOSED  ${item.remote_ip}:${item.remote_port}  ${fmtDuration(item.duration_sec)}  utt=${item.utterances}  in=${fmtBytes(item.input_audio_bytes)} out=${fmtBytes(item.output_audio_bytes)}`
    );
  }
  return rows.length ? rows.join("\n") : "No connections yet.";
}

async function tick() {
  try {
    const [status, events, connections] = await Promise.all([
      loadStatus(),
      loadEvents(),
      loadConnections(),
    ]);

    pollIntervalMs = Number(status.poll_interval_ms || 1000);
    if (elems.serverMeta) {
      elems.serverMeta.textContent = `Server uptime: ${status.uptime_sec}s | Active client: ${status.active_client_count} | Processing: ${status.processing ? "yes" : "no"}`;
    }
    if (elems.frontendMeta) {
      const s = status.server || {};
      elems.frontendMeta.textContent = `Server PID ${s.pid || "?"} | WS ${s.ws_host || "?"}:${s.ws_port || "?"} | Admin ${s.admin_host || "?"}:${s.admin_port || "?"}`;
    }

    if (elems.asr) elems.asr.checked = !!status.modules.asr_enabled;
    if (elems.llm) elems.llm.checked = !!status.modules.llm_enabled;
    if (elems.tts) elems.tts.checked = !!status.modules.tts_enabled;

    const summary = status.active_client_summary || null;
    if (elems.sumDevice) elems.sumDevice.textContent = summary ? summary.remote : (status.active_client_id || "offline");
    if (elems.sumDuration) elems.sumDuration.textContent = summary ? fmtDuration(summary.duration_sec) : "-";
    if (elems.sumUtterances) elems.sumUtterances.textContent = summary ? String(summary.utterances) : "-";
    if (elems.sumLatency) elems.sumLatency.textContent = `${status.metrics.total_ms_avg || 0} ms`;
    if (elems.sumTraffic) elems.sumTraffic.textContent = `${fmtBytes(status.metrics.input_audio_bytes_total || 0)} / ${fmtBytes(status.metrics.output_audio_bytes_total || 0)}`;
    if (elems.sumError) elems.sumError.textContent = status.latest_error ? ((status.latest_error.details && status.latest_error.details.code) || status.latest_error.type) : "none";

    if (elems.connectionsSummary) elems.connectionsSummary.textContent = renderConnectionsSummary(connections);

    if (elems.activeDeviceRaw) elems.activeDeviceRaw.textContent = fmtJson(status.active_client || null);
    if (elems.statsRaw) elems.statsRaw.textContent = fmtJson(status.metrics || {});
    if (elems.connectionsRaw) elems.connectionsRaw.textContent = fmtJson(connections || {});
    if (elems.events) elems.events.textContent = fmtJson(events);
  } catch (err) {
    if (elems.serverMeta) elems.serverMeta.textContent = `Dashboard error: ${err.message}`;
  } finally {
    setTimeout(tick, pollIntervalMs);
  }
}

if (elems.saveModules) elems.saveModules.addEventListener("click", applyModules);
if (elems.fallbackSend) elems.fallbackSend.addEventListener("click", sendFallbackText);
if (elems.eventSeverity) {
  elems.eventSeverity.addEventListener("change", () => {
    setTimeout(tick, 10);
  });
}

if (elems.frontendMeta) {
  elems.frontendMeta.textContent = "Frontend app.js loaded (v2).";
}

tick();
