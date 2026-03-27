const byId = (id) => document.getElementById(id);

const elems = {
  serverMeta: byId("server-meta"),
  asr: byId("mod-asr"),
  llm: byId("mod-llm"),
  tts: byId("mod-tts"),
  saveModules: byId("save-modules"),
  moduleMsg: byId("module-msg"),
  activeDevice: byId("active-device"),
  stats: byId("stats"),
  connections: byId("connections"),
  events: byId("events"),
};

let pollIntervalMs = 1000;

function fmtJson(obj) {
  return JSON.stringify(obj, null, 2);
}

async function loadStatus() {
  const resp = await fetch("/api/admin/status");
  if (!resp.ok) throw new Error("Failed to load status");
  return await resp.json();
}

async function loadEvents() {
  const resp = await fetch("/api/admin/events?limit=30");
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

async function tick() {
  try {
    const [status, events, connections] = await Promise.all([
      loadStatus(),
      loadEvents(),
      loadConnections(),
    ]);

    pollIntervalMs = Number(status.poll_interval_ms || 1000);
    elems.serverMeta.textContent = `Server uptime: ${status.uptime_sec}s | Active client: ${status.active_client_count}`;

    elems.asr.checked = !!status.modules.asr_enabled;
    elems.llm.checked = !!status.modules.llm_enabled;
    elems.tts.checked = !!status.modules.tts_enabled;

    elems.activeDevice.textContent = fmtJson(status.active_client || null);
    elems.stats.textContent = fmtJson(status.metrics || {});
    elems.connections.textContent = fmtJson(connections);
    elems.events.textContent = fmtJson(events);
  } catch (err) {
    elems.serverMeta.textContent = `Dashboard error: ${err.message}`;
  } finally {
    setTimeout(tick, pollIntervalMs);
  }
}

elems.saveModules.addEventListener("click", applyModules);
tick();
