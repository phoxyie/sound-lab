import WaveSurfer from "/static/vendor/wavesurfer.esm.js";
import RegionsPlugin from "/static/vendor/regions.esm.js";
import TimelinePlugin from "/static/vendor/timeline.esm.js";

const $ = (id) => document.getElementById(id);
const DND_TYPE = "application/x-soundlab";
const STORE_KEY = "soundlab.timeline"; // + "." + project name
const DEVICE_KEY = "soundlab.device";
const COLLAPSE_KEY = "soundlab.collapsed";
const COUNTDOWN_KEY = "soundlab.countdown";
const COUNTDOWN_SEC = 3;
const TIGHTEN_KEY = "soundlab.tightenPause";
const ENHANCE_KEY = "soundlab.enhance"; // denoiser choice + Resemble settings
const GLOBAL_SETTINGS_KEY = "soundlab.globalSettings";
const NFE_CHOICES = [8, 16, 32, 64, 128];
const JOB_POLL_MS = 300;
// Automated tests open the page with ?mute=1 so previews don't play through the speakers
const MUTED = new URLSearchParams(location.search).has("mute");
const MAX_LANES = 33;
const LANE_HEIGHT = { initial: 120, min: 60, max: 480 };
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

const state = {
  library: [],
  lanes: [],          // editor tracks, see createLane()
  activeLane: null,
  focus: "lane",      // what Space plays: "lane" (active track) or "timeline"
  project: null,
  timeline: [],
  recStartedAt: null,
  collapsed: loadCollapsed(),
};

// ---------- helpers ----------

function fmt(sec) {
  if (sec == null || Number.isNaN(sec)) return "—";
  const m = Math.floor(sec / 60);
  const s = sec - m * 60;
  return `${m}:${s.toFixed(1).padStart(4, "0")}`;
}

// Editor times to the millisecond (edits are stored and rendered at 1 ms)
function fmtMs(sec) {
  if (sec == null || Number.isNaN(sec)) return "—";
  const m = Math.floor(sec / 60);
  const s = sec - m * 60;
  return `${m}:${s.toFixed(3).padStart(6, "0")}`;
}

function toast(msg, kind = "ok", ms = 4000, actions = []) {
  const t = $("toast");
  t.textContent = msg;
  t.className = `toast ${kind}`;
  for (const action of actions) {
    const b = Object.assign(document.createElement("button"), { className: "btn", textContent: action.label });
    b.addEventListener("click", () => { t.classList.add("hidden"); action.onClick(); });
    t.append(b);
  }
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => t.classList.add("hidden"), ms);
}

const reportError = (e) => toast(e.message, "error", 6000);

// Enhance / export run as server jobs: start one, then poll its progress until it ends.
// onProgress gets {id, progress 0–1, elapsed} — first right after the start, so the caller can cancel.
async function runJob(path, body, onProgress) {
  const { job } = await api(path, body);
  onProgress?.({ id: job, progress: 0, elapsed: 0 });
  for (;;) {
    await new Promise((r) => setTimeout(r, JOB_POLL_MS));
    const j = await api(`/api/jobs/${job}`);
    if (j.status === "done") return j.result;
    if (j.status === "error") throw new Error(j.error);
    if (j.status === "cancelled") throw Object.assign(new Error("Cancelled"), { cancelled: true });
    onProgress?.(j);
  }
}

const cancelJob = (id) => api(`/api/jobs/${id}/cancel`, {}).catch(reportError);
const busyLabel = (j) => `${Math.round(j.progress * 100)}% · Cancel`;

async function api(path, body) {
  const res = await fetch(path, body === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

function loadTimeline() {
  try { return JSON.parse(localStorage.getItem(`${STORE_KEY}.${state.project}`)) || []; } catch { return []; }
}

function saveTimeline(rewind = true) {
  try { localStorage.setItem(`${STORE_KEY}.${state.project}`, JSON.stringify(state.timeline)); } catch { /* ignore */ }
  if (rewind) tlReset(); // clips changed: playback restarts from the beginning
}

const clipLength = (c) => (c.end - c.start) / c.speed;

function setDrag(e, payload) {
  e.dataTransfer.setData(DND_TYPE, JSON.stringify(payload));
  e.dataTransfer.effectAllowed = "copyMove";
}

function getDrag(e) {
  try { return JSON.parse(e.dataTransfer.getData(DND_TYPE)); } catch { return null; }
}

function dropZone(el, onDrop, onOver) {
  el.addEventListener("dragover", (e) => {
    if (!e.dataTransfer.types.includes(DND_TYPE)) return;
    e.preventDefault();
    el.classList.add("drop-target");
    onOver?.(e);
  });
  el.addEventListener("dragleave", (e) => {
    if (!el.contains(e.relatedTarget)) { el.classList.remove("drop-target"); onOver?.(null); }
  });
  el.addEventListener("drop", (e) => {
    e.preventDefault();
    el.classList.remove("drop-target");
    const data = getDrag(e);
    if (data) onDrop(data, e);
    onOver?.(null);
  });
}

// ---------- media library ----------

async function refreshLibrary(flashPath) {
  const { project, groups } = await api("/api/library");
  // A late response from the previous project must not overwrite the new project's list
  if (project !== state.project) return;
  state.library = groups;
  renderLibrary(flashPath);
  renderEnhanceWarning(); // needs to know which files are already enhanced
}

function findFile(path) {
  for (const g of state.library) {
    const f = g.files.find((x) => x.path === path);
    if (f) return f;
  }
  return null;
}

function loadCollapsed() {
  try { return JSON.parse(localStorage.getItem(COLLAPSE_KEY)) || { exports: true }; } catch { return { exports: true }; }
}

function toggleGroup(id) {
  state.collapsed[id] = !state.collapsed[id];
  try { localStorage.setItem(COLLAPSE_KEY, JSON.stringify(state.collapsed)); } catch { /* ignore */ }
  renderLibrary();
}

function itemMeta(f) {
  const prefix = f.is_video ? "video · " : "";
  if (f.source === "voice-records") return `${prefix}recording · ${f.folder}`;
  if (f.source === "inputs") return f.folder ? `${prefix}imported · ${f.folder}` : `${prefix}imported`;
  return f.folder ? `${prefix}${f.folder}` : (f.is_video ? "video" : "");
}

function renderLibrary(flashPath) {
  const q = $("filter").value.trim().toLowerCase();
  const list = $("library-list");
  const open = new Set(state.lanes.map((l) => l.path));
  list.innerHTML = "";
  for (const g of state.library) {
    const files = g.files.filter((f) => !q || f.path.toLowerCase().includes(q));
    if (flashPath && g.files.some((f) => f.path === flashPath)) state.collapsed[g.id] = false;
    const collapsible = g.id === "exports";
    const collapsed = collapsible && state.collapsed[g.id];

    const title = document.createElement(collapsible ? "button" : "div");
    title.className = `group-title${collapsible ? " collapsible" : ""}`;
    title.textContent = `${collapsible ? (collapsed ? "▸ " : "▾ ") : ""}${g.name} (${files.length})`;
    if (collapsible) title.addEventListener("click", () => toggleGroup(g.id));
    list.append(title);
    if (collapsed) continue;

    if (g.id === "media" && !g.files.length) {
      const empty = document.createElement("div");
      empty.className = "lib-empty";
      empty.textContent = "Drop audio files here from Finder, choose them with “+ Add files”, or record.";
      list.append(empty);
    }

    for (const f of files) {
      const item = document.createElement("div");
      item.className = "lib-item";
      if (open.has(f.path)) item.classList.add("active");
      if (f.path === flashPath) item.classList.add("flash");
      item.draggable = true;
      item.title = f.path;
      item.innerHTML = `<span class="lib-name"></span><span class="lib-dur mono">${fmt(f.duration)}</span>
        <span class="lib-meta"></span>
        <span class="lib-actions">
          <button class="mini" data-act="reveal" title="Show in Finder">Finder</button>
          <button class="mini danger" data-act="trash" title="Move to Trash">Delete</button>
        </span>`;
      item.querySelector(".lib-name").textContent = f.name;
      item.querySelector(".lib-meta").textContent = itemMeta(f);
      item.querySelector('[data-act="reveal"]').addEventListener("click", (e) => { e.stopPropagation(); reveal(f.path); });
      item.querySelector('[data-act="trash"]').addEventListener("click", (e) => { e.stopPropagation(); trashFile(f); });
      item.addEventListener("dragstart", (e) => setDrag(e, { kind: "file", path: f.path }));
      item.addEventListener("dblclick", () => addLane(f));
      list.append(item);
      if (f.path === flashPath) item.scrollIntoView({ block: "nearest" });
    }
  }
}

// ---------- projects ----------

function renderProjects({ projects, current }) {
  const sel = $("project-select");
  sel.innerHTML = "";
  for (const p of projects) sel.append(new Option(`${p.name} (${p.files})`, p.name));
  sel.value = current;
}

// Switching project closes the editor and loads that project's timeline and media
async function enterProject(payload) {
  renderProjects(payload);
  if (state.project === payload.current) return;
  state.project = payload.current;
  closeAllLanes();
  state.timeline = loadTimeline();
  renderTimeline();
  await refreshLibrary();
}

async function initProjects() {
  await enterProject(await api("/api/projects"));
}

$("project-select").addEventListener("change", async (e) => {
  try {
    await enterProject(await api("/api/projects/current", { name: e.target.value }));
  } catch (err) {
    e.target.value = state.project;
    reportError(err);
  }
});

$("new-project").addEventListener("click", async () => {
  const name = prompt("New project name:");
  if (!name?.trim()) return;
  try {
    await enterProject(await api("/api/projects", { name: name.trim() }));
    toast(`Project created: ${state.project}`);
  } catch (e) {
    reportError(e);
  }
});

// The saved timeline is keyed by project name and its clip paths contain the project folder: carry both over
function moveTimeline({ from, to, old_prefix, new_prefix }) {
  const fix = (clips) => clips.map((c) => (old_prefix && c.path.startsWith(`${old_prefix}/`)
    ? { ...c, path: new_prefix + c.path.slice(old_prefix.length) } : c));
  try {
    const saved = JSON.parse(localStorage.getItem(`${STORE_KEY}.${from}`));
    if (saved) localStorage.setItem(`${STORE_KEY}.${to}`, JSON.stringify(fix(saved)));
    localStorage.removeItem(`${STORE_KEY}.${from}`);
  } catch { /* ignore */ }
}

$("rename-project").addEventListener("click", async () => {
  const name = state.project;
  const newName = prompt(`Rename project "${name}" to:`, name)?.trim();
  if (!newName || newName === name) return;
  try {
    const payload = await api("/api/projects/rename", { name, new_name: newName });
    moveTimeline(payload.renamed);
    state.project = null; // file paths changed: reload the timeline and media under the new name
    await enterProject(payload);
    toast(`Project renamed: ${payload.renamed.from} → ${payload.renamed.to}`);
  } catch (e) {
    reportError(e);
  }
});

$("delete-project").addEventListener("click", async () => {
  const name = state.project;
  const files = [...$("project-select").options].find((o) => o.value === name)?.textContent.match(/\((\d+)\)$/)?.[1] ?? "?";
  if (!confirm(`Move project "${name}" and its ${files} audio file(s) to the Trash?`)) return;
  try {
    const payload = await api("/api/projects/delete", { name });
    try { localStorage.removeItem(`${STORE_KEY}.${name}`); } catch { /* ignore */ }
    state.project = null; // a new project with the same name must load fresh
    await enterProject(payload);
    toast(`"${name}" moved to the Trash · current project: ${state.project}`);
  } catch (e) {
    reportError(e);
  }
});

function setProjectControls(enabled) {
  for (const id of ["project-select", "new-project", "delete-project"]) $(id).disabled = !enabled;
}

// ---------- file actions ----------

async function importFiles(fileList, openInEditor = false) {
  const files = [...fileList];
  if (!files.length) return;
  const form = new FormData();
  for (const f of files) form.append("files", f, f.name);
  toast(`Importing ${files.length} file(s)…`, "ok", 60000);
  try {
    const res = await fetch("/api/import", { method: "POST", body: form });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    await refreshLibrary(data.imported[0]);
    const msg = `${data.imported.length} file(s) added to the project`;
    if (data.rejected.length) toast(`${msg}\n⚠ Skipped: ${data.rejected.join(", ")}`, "warn", 7000);
    else toast(msg);
    if (openInEditor) {
      for (const path of data.imported.slice(0, MAX_LANES - state.lanes.length)) {
        const f = findFile(path);
        if (f) addLane(f);
      }
    }
  } catch (e) {
    reportError(e);
  }
}

async function reveal(path) {
  try { await api("/api/reveal", { path }); } catch (e) { reportError(e); }
}

async function trashFile(f) {
  if (!confirm(`Move "${f.name}" to the Trash?`)) return;
  try {
    await api("/api/trash", { path: f.path });
    const before = state.timeline.length;
    state.timeline = state.timeline.filter((c) => c.path !== f.path);
    saveTimeline();
    clearEditing();
    for (const lane of state.lanes.filter((l) => l.path === f.path)) closeLane(lane);
    await refreshLibrary();
    renderTimeline();
    const removed = before - state.timeline.length;
    toast(`Moved to the Trash: ${f.name}${removed ? ` · ${removed} clip(s) removed from the timeline` : ""}`);
  } catch (e) {
    reportError(e);
  }
}

function fileDropZone(el, openInEditor) {
  // Files dragged from Finder (in-app drags are handled by dropZone)
  const isFiles = (e) => e.dataTransfer.types.includes("Files") && !e.dataTransfer.types.includes(DND_TYPE);
  el.addEventListener("dragover", (e) => {
    if (!isFiles(e)) return;
    e.preventDefault();
    el.classList.add("drop-target");
  });
  el.addEventListener("dragleave", (e) => {
    if (!el.contains(e.relatedTarget)) el.classList.remove("drop-target");
  });
  el.addEventListener("drop", (e) => {
    if (!isFiles(e)) return;
    e.preventDefault();
    el.classList.remove("drop-target");
    importFiles(e.dataTransfer.files, openInEditor);
  });
}

// ---------- settings ----------

const globalDefaults = { minLufs: -70, targetLufs: -16, mic: null };
let appConfig = null;

async function initConfig() {
  const cfg = await api("/api/config");
  appConfig = cfg;
  globalDefaults.minLufs = cfg.default_min_lufs ?? -70;
  globalDefaults.targetLufs = cfg.default_target ?? -16;
  const sel = $("preset");
  for (const p of cfg.presets) sel.append(new Option(p === cfg.default_preset ? `${p} (default)` : p, p));
  sel.value = cfg.default_preset;
  $("target").value = String(cfg.default_target);
  markInstalled("sidon", "Sidon", cfg.sidon.installed, "./scripts/install_sidon.sh");
  initResemble(cfg.resemble, { sidon: cfg.sidon.installed });
  initGlobalSettings();
}

function loadGlobalSettings() {
  try {
    return { ...globalDefaults, ...(JSON.parse(localStorage.getItem(GLOBAL_SETTINGS_KEY)) || {}) };
  } catch {
    return { ...globalDefaults };
  }
}

function saveGlobalSettings(s) {
  try {
    localStorage.setItem(GLOBAL_SETTINGS_KEY, JSON.stringify(s));
  } catch { /* ignore */ }
}

function initGlobalSettings() {
  const s = loadGlobalSettings();
  $("settings-min-lufs").value = s.minLufs ?? globalDefaults.minLufs;
  $("settings-target-lufs").value = s.targetLufs ?? globalDefaults.targetLufs;
  if (s.targetLufs) $("target").value = String(s.targetLufs);
  updateSettingsBtnIndicator();
}

function updateSettingsBtnIndicator() {
  const s = loadGlobalSettings();
  const changed = s.minLufs !== globalDefaults.minLufs || s.targetLufs !== globalDefaults.targetLufs;
  $("settings-btn").textContent = changed ? "⚙ Settings •" : "⚙ Settings";
}

// Global settings panel events
$("settings-btn").addEventListener("click", (e) => {
  e.stopPropagation();
  $("settings-panel").classList.toggle("hidden");
});
$("settings-panel").addEventListener("click", (e) => e.stopPropagation());
document.addEventListener("click", () => $("settings-panel").classList.add("hidden"));

$("settings-min-lufs").addEventListener("change", () => {
  const val = Number($("settings-min-lufs").value);
  const s = loadGlobalSettings();
  s.minLufs = Number.isFinite(val) ? Math.min(Math.max(val, -100), -30) : globalDefaults.minLufs;
  $("settings-min-lufs").value = s.minLufs;
  saveGlobalSettings(s);
  updateSettingsBtnIndicator();
});

$("settings-target-lufs").addEventListener("change", () => {
  const val = Number($("settings-target-lufs").value);
  const s = loadGlobalSettings();
  s.targetLufs = Number.isFinite(val) ? Math.min(Math.max(val, -30), -9) : globalDefaults.targetLufs;
  $("settings-target-lufs").value = s.targetLufs;
  $("target").value = String(s.targetLufs);
  saveGlobalSettings(s);
  updateSettingsBtnIndicator();
});

$("settings-mic").addEventListener("change", () => {
  const mic = $("settings-mic").value;
  $("device").value = mic;
  try { localStorage.setItem(DEVICE_KEY, mic); } catch { /* ignore */ }
});

$("settings-reset").addEventListener("click", () => {
  $("settings-min-lufs").value = globalDefaults.minLufs;
  $("settings-target-lufs").value = globalDefaults.targetLufs;
  $("target").value = String(globalDefaults.targetLufs);
  const defMic = globalDefaults.mic;
  if (defMic) {
    $("settings-mic").value = defMic;
    $("device").value = defMic;
    try { localStorage.setItem(DEVICE_KEY, defMic); } catch { /* ignore */ }
  }
  saveGlobalSettings({ ...globalDefaults });
  updateSettingsBtnIndicator();
  toast("Settings reset to defaults");
});

$("settings-done").addEventListener("click", () => $("settings-panel").classList.add("hidden"));

// Optional denoisers that aren't installed stay in the list, disabled, with how to install them
function markInstalled(value, label, installed, script) {
  const opt = $("denoiser").querySelector(`option[value="${value}"]`);
  opt.disabled = !installed;
  if (!installed) {
    opt.textContent = `${label} (not installed)`;
    opt.title = script;
  }
}

// ---------- Resemble Enhance settings (denoiser choice + tuning, kept in localStorage) ----------

const resemble = { defaults: null, installed: false, rate: null, confirmAbove: 60 };

function fmtEta(sec) {
  if (sec < 90) return `${Math.round(sec)} s`;
  const min = Math.round(sec / 60);
  return min < 60 ? `${min} min` : `${Math.floor(min / 60)} h ${min % 60} min`;
}

// Resemble runs at ~3–4× the audio's length on the CPU: a long job asks first, with the estimate
function resembleOk(seconds) {
  if ($("denoiser").value !== "resemble" || !resemble.rate) return true;
  const est = seconds * (resemble.rate[0] + resemble.rate[1] * resembleSettings().nfe);
  if (est <= resemble.confirmAbove) return true;
  return confirm(`Resemble Enhance will take about ${fmtEta(est)} for ${fmt(seconds)} of audio `
    + `(CPU, ~${(est / seconds).toFixed(1)}× its length — longer if the Mac gets hot and slows down).\n\n`
    + "Tip: edit first and enhance only the final export (Join & Export), or use DeepFilterNet "
    + "while you try things out.\n\nStart now?");
}

function initResemble(info, others) {
  resemble.defaults = info.defaults;
  resemble.installed = info.installed;
  resemble.rate = info.rate;
  resemble.confirmAbove = info.confirm_above_sec;
  markInstalled("resemble", "Resemble Enhance", info.installed, "./scripts/install_resemble.sh");
  for (const s of info.solvers) $("rs-solver").append(new Option(s === info.defaults.solver ? `${s} (default)` : s, s));
  $("rs-seed").max = info.seed_max;
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(ENHANCE_KEY)) || {}; } catch { /* ignore */ }
  const available = { resemble: info.installed, ...others };
  $("denoiser").value = available[saved.denoiser] ? saved.denoiser : "auto";
  setResembleFields({ ...info.defaults, ...(saved.resemble || {}) });
  updateDenoiserUI();
}

function setResembleFields(v) {
  $("rs-tau").value = v.tau;
  $("rs-lambd").value = v.lambd;
  const nfe = $("rs-nfe");
  nfe.replaceChildren(...[...new Set([...NFE_CHOICES, Number(v.nfe)])].sort((a, b) => a - b).map((n) => new Option(
    `${n}${n === resemble.defaults.nfe ? " (default)" : ""}`, n)));
  nfe.value = String(v.nfe);
  $("rs-solver").value = v.solver;
  $("rs-seed").value = v.seed;
  updateResembleValues();
}

function resembleSettings() {
  const seed = Math.round(Number($("rs-seed").value));
  return {
    tau: Number($("rs-tau").value),
    lambd: Number($("rs-lambd").value),
    nfe: Number($("rs-nfe").value),
    solver: $("rs-solver").value,
    seed: Number.isFinite(seed) ? Math.min(Math.max(seed, 0), Number($("rs-seed").max)) : resemble.defaults.seed,
  };
}

function updateResembleValues() {
  $("rs-tau-val").textContent = Number($("rs-tau").value).toFixed(2);
  $("rs-lambd-val").textContent = Number($("rs-lambd").value).toFixed(2);
}

function saveEnhanceSettings() {
  try {
    localStorage.setItem(ENHANCE_KEY, JSON.stringify({ denoiser: $("denoiser").value, resemble: resembleSettings() }));
  } catch { /* ignore */ }
}

function updateDenoiserUI() {
  const on = $("denoiser").value === "resemble";
  $("resemble-btn").classList.toggle("hidden", !on);
  if (!on) $("resemble-panel").classList.add("hidden");
  const d = resemble.defaults;
  const v = resembleSettings();
  const changed = d && ["tau", "lambd", "nfe", "solver", "seed"].some((k) => v[k] !== d[k]);
  $("resemble-btn").textContent = changed ? "⚙ Settings •" : "⚙ Settings";
  $("resemble-btn").title = `Resemble Enhance settings${changed ? " (changed from the defaults)" : ""}`;
}

const enhanceSettings = () => {
  const g = loadGlobalSettings();
  return {
    preset: $("preset").value,
    target: Number($("target").value),
    min_lufs: g.minLufs ?? globalDefaults.minLufs,
    format: $("format").value,
    denoiser: $("denoiser").value,
    resemble: resembleSettings(),
  };
};

function resultMessage(verb, r) {
  const parts = [`${verb}: ${r.path}`, fmt(r.duration)];
  if (r.lufs != null) parts.push(`${r.lufs.toFixed(1)} LUFS`);
  if (r.snr_in != null && r.snr_out != null) parts.push(`SNR ${r.snr_in.toFixed(0)} → ${r.snr_out.toFixed(0)} dB`);
  return parts.join(" · ");
}

function resultActions(path) {
  return [
    { label: "Open in editor", onClick: () => { const f = findFile(path); if (f) addLane(f); } },
    { label: "Show in Finder", onClick: () => reveal(path) },
  ];
}

// ---------- recording ----------

async function initDevices() {
  try {
    const { devices, default: def } = await api("/api/devices");
    globalDefaults.mic = def;
    const sel = $("device");
    const setSel = $("settings-mic");
    sel.innerHTML = "";
    setSel.innerHTML = "";
    for (const d of devices) {
      sel.append(new Option(d, d));
      setSel.append(new Option(d, d));
    }
    let saved = null;
    try { saved = localStorage.getItem(DEVICE_KEY); } catch { /* ignore */ }
    const chosen = devices.includes(saved) ? saved : def;
    sel.value = chosen;
    setSel.value = chosen;
    sel.addEventListener("change", () => {
      setSel.value = sel.value;
      try { localStorage.setItem(DEVICE_KEY, sel.value); } catch { /* ignore */ }
    });
  } catch (e) {
    toast(`Couldn't list microphones: ${e.message}`, "error");
  }
}

function setRecUI(recording) {
  $("rec-btn").classList.toggle("active", recording);
  $("rec-btn").classList.remove("counting");
  $("rec-label").textContent = recording ? "Stop" : "Record";
  for (const id of ["device", "take-name", "countdown"]) $(id).disabled = recording;
  setProjectControls(!recording);
  if (recording) startMeter();
  else stopMeter();
}

// Live input level while recording (and during the countdown, to check the level before speaking)
const METER_FLOOR = -60;
const meter = { on: false, timer: 0 };

function startMeter() {
  if (meter.on) return;
  meter.on = true;
  renderMeter(null, null);
  $("rec-meter").classList.remove("hidden");
  const poll = async () => {
    if (!meter.on) return;
    try {
      const s = await api("/api/record");
      if (meter.on && s.recording) renderMeter(s.level, s.peak_max);
    } catch { /* try again on the next poll */ }
    if (meter.on) meter.timer = setTimeout(poll, 100);
  };
  poll();
}

function stopMeter() {
  meter.on = false;
  clearTimeout(meter.timer);
  $("rec-meter").classList.add("hidden");
}

function renderMeter(level, max) {
  const frac = (db) => (db == null ? 0 : Math.min(1, Math.max(0, 1 - db / METER_FLOOR)));
  const el = $("rec-meter");
  $("rec-meter-mask").style.width = `${(1 - frac(level)) * 100}%`;
  $("rec-meter-hold").style.left = `${frac(max) * 100}%`;
  el.dataset.level = level == null ? "" : level.toFixed(1);
  el.title = level == null ? "Input level"
    : `Input peak ${level.toFixed(1)} dBFS · loudest so far ${max.toFixed(1)} dBFS`
      + (max > -3 ? " — too close to clipping, lower the input level" : max < -30 ? " — very quiet" : "");
  el.classList.toggle("hot", max != null && max > -3);
}

// 3-2-1: ffmpeg already records during the countdown (a mic needs a moment to start), and the server
// cuts everything before "go" (minus a short lead-in) when the take stops. Clicking cancels it.
let countdown = null;

function runCountdown() {
  setRecUI(true);
  $("rec-btn").classList.add("counting");
  $("rec-time").textContent = "ready…";
  let n = COUNTDOWN_SEC;
  const tick = async () => {
    if (n > 0) {
      $("rec-label").textContent = String(n--);
      countdown = setTimeout(tick, 1000);
      return;
    }
    countdown = null;
    try {
      await api("/api/record/mark", {});
    } catch (e) {
      reportError(e);
    }
    $("rec-btn").classList.remove("counting");
    $("rec-label").textContent = "Stop";
    state.recStartedAt = Date.now();
    tickRec();
  };
  tick();
}

async function cancelCountdown() {
  clearTimeout(countdown);
  countdown = null;
  setRecUI(false);
  $("rec-time").textContent = "00:00";
  try {
    await api("/api/record/cancel", {});
    toast("Recording cancelled (moved to the Trash)", "warn");
  } catch (e) {
    reportError(e);
  }
}

function tickRec() {
  if (!state.recStartedAt) return;
  const sec = (Date.now() - state.recStartedAt) / 1000;
  $("rec-time").textContent = `${String(Math.floor(sec / 60)).padStart(2, "0")}:${String(Math.floor(sec % 60)).padStart(2, "0")}`;
  requestAnimationFrame(tickRec);
}

async function toggleRecord() {
  if (countdown) { cancelCountdown(); return; }
  const btn = $("rec-btn");
  btn.disabled = true;
  try {
    if (!state.recStartedAt) {
      const s = await api("/api/record/start", { device: $("device").value, name: $("take-name").value.trim() || null });
      if ($("countdown").checked) {
        runCountdown();
        return;
      }
      state.recStartedAt = Date.now() - s.elapsed * 1000;
      setRecUI(true);
      tickRec();
    } else {
      state.recStartedAt = null;
      setRecUI(false);
      const r = await api("/api/record/stop", {});
      await refreshLibrary(r.path);
      const msg = `Saved: ${r.path} · ${fmt(r.duration)} · ${r.lufs.toFixed(1)} LUFS`;
      toast(r.warnings.length ? `${msg}\n⚠ ${r.warnings.join(" ")}` : msg, r.warnings.length ? "warn" : "ok", 6000);
      const f = findFile(r.path);
      if (f && state.lanes.length < MAX_LANES) addLane(f);
    }
  } catch (e) {
    state.recStartedAt = null;
    setRecUI(false);
    reportError(e);
  } finally {
    btn.disabled = false;
  }
}

async function syncRecordStatus() {
  // Show a recording that is still running after a page reload
  const s = await api("/api/record");
  if (s.recording) {
    state.recStartedAt = Date.now() - s.elapsed * 1000;
    setRecUI(true);
    tickRec();
  }
}

// ---------- editor (up to 3 tracks) ----------

let laneSeq = 0;

function createLane() {
  const el = $("lane-tpl").content.firstElementChild.cloneNode(true);
  const q = (sel) => el.querySelector(sel);
  const regions = RegionsPlugin.create();
  const ruler = TimelinePlugin.create({ height: 20, style: { color: css("--muted") } }); // labels: height / 2
  const ws = WaveSurfer.create({
    container: q(".lane-wave"),
    height: LANE_HEIGHT.initial,
    waveColor: css("--wave"),
    progressColor: css("--wave-progress"),
    cursorColor: "#ffffff",
    cursorWidth: 2,
    barWidth: 2,
    barGap: 1,
    barRadius: 2,
    dragToSeek: true, // press and drag anywhere on the waveform moves the playhead
    plugins: [regions, ruler],
  });
  if (MUTED) ws.setMuted(true);

  const lane = {
    id: ++laneSeq, el, ws, regions, region: null,
    path: null, name: null, duration: 0, start: 0, end: 0, speed: 1, editingIndex: null,
    ab: null, // A/B state of an enhanced file, see toggleAB()
    zoom: 0, // pixels per second; 0 = the whole recording fits (see setZoom)
    ruler,
    q,
  };

  ws.on("decode", (duration) => {
    if (!lane.path) return; // ws.empty() also fires "decode"
    lane.duration = duration;
    lane.start = Math.max(0, Math.min(lane.start, duration));
    lane.end = Math.min(lane.end ?? duration, duration);
    regions.clearRegions();
    lane.region = regions.addRegion({
      start: lane.start, end: lane.end, color: "rgba(79, 140, 255, 0.16)", drag: false, resize: true,
    });
    lockRegionBody(lane.region);
    keepInRange(ws.getCurrentTime());
    ws.setPlaybackRate(lane.speed, true);
    // a new file starts fitted (loadIntoLane resets zoom); the other version of an A/B pair keeps it
    tuneRuler(lane, lane.zoom || fitPx(lane));
    if ((ws.options.minPxPerSec || 0) !== lane.zoom) ws.setOptions({ minPxPerSec: lane.zoom });
    updateLaneUI(lane);
    updateZoomUI(lane);
    el.dataset.ready = "true";
  });
  regions.on("region-updated", (r) => {
    lane.start = r.start;
    lane.end = r.end;
    keepInRange(ws.getCurrentTime());
    updateLaneUI(lane);
  });
  // The playhead stays inside the trim range: clicks / drags outside it stop at the nearest edge,
  // and playback stops at the end of the range
  const keepInRange = (t) => {
    if (!lane.region) return;
    const clamped = Math.min(lane.end, Math.max(lane.start, t));
    if (Math.abs(clamped - t) > 0.001) ws.setTime(clamped);
  };
  ws.on("interaction", keepInRange);
  ws.on("timeupdate", (t) => {
    if (ws.isPlaying() && t >= lane.end) {
      ws.pause();
      ws.setTime(lane.start);
    } else {
      keepInRange(t);
    }
  });
  ws.on("error", (e) => toast(`Couldn't load audio: ${e?.message || e}`, "error", 6000));
  ws.on("timeupdate", (t) => { q(".lane-pos").textContent = fmtMs(t); });
  ws.on("play", () => { q(".lane-play").textContent = "❚❚ Pause"; });
  ws.on("pause", () => { q(".lane-play").textContent = "▶ Play"; });

  el.addEventListener("pointerdown", () => { setActiveLane(lane); state.focus = "lane"; });
  q(".lane-close").addEventListener("click", () => closeLane(lane));
  q(".lane-play").addEventListener("click", () => togglePlay(lane));
  q(".lane-tighten").addEventListener("click", () => tightenLane(lane));
  q(".lane-set-in").addEventListener("click", () => setInOut(lane, "in"));
  q(".lane-set-out").addEventListener("click", () => setInOut(lane, "out"));
  q(".lane-zoom-in").addEventListener("click", () => setZoom(lane, (lane.zoom || fitPx(lane)) * 2));
  q(".lane-zoom-out").addEventListener("click", () => setZoom(lane, (lane.zoom || fitPx(lane)) / 2));
  q(".lane-zoom-fit").addEventListener("click", () => setZoom(lane, 0));
  initLaneZoomGestures(lane);
  const pause = q(".lane-pause");
  try { pause.value = localStorage.getItem(TIGHTEN_KEY) ?? pause.value; } catch { /* ignore */ }
  const showPause = () => {
    const v = Number(pause.value);
    q(".lane-pause-val").textContent = v <= 0.05 ? "skip" : `${v.toFixed(2)} s`;
    pause.title = v <= 0.05
      ? "Skip silence, jump-cut style: everything 18 dB under your voice (room tone, breaths, word tails) "
        + "counts as silence and every gap longer than 0.1 s goes down to 0.05 s"
      : `Pauses longer than ${v.toFixed(2)} s are cut down to it; further left also treats quieter sounds `
        + "(room tone, breaths) as silence — full left: Skip silence";
  };
  pause.addEventListener("input", () => {
    showPause();
    try { localStorage.setItem(TIGHTEN_KEY, pause.value); } catch { /* ignore */ }
  });
  showPause();
  q(".lane-speed").addEventListener("input", (e) => {
    lane.speed = Number(e.target.value);
    ws.setPlaybackRate(lane.speed, true); // keep pitch
    updateLaneUI(lane);
  });
  q(".lane-chip").addEventListener("dragstart", (e) => setDrag(e, { kind: "lane", id: lane.id }));
  q(".lane-add").addEventListener("click", () => commitLane(lane));
  q(".lane-cancel").addEventListener("click", () => { lane.editingIndex = null; updateLaneUI(lane); renderTimeline(); });
  q(".lane-enhance").addEventListener("click", () => enhanceLane(lane));
  q(".lane-ab").addEventListener("click", () => toggleAB(lane));
  initLaneResize(lane);
  return lane;
}

// Only the edge handles resize the trim range; pressing inside it seeks / scrubs like the rest
// of the waveform (the region body no longer catches the pointer)
function lockRegionBody(region) {
  region.element.style.pointerEvents = "none";
  for (const h of region.element.querySelectorAll('[part~="region-handle"]')) {
    h.style.pointerEvents = "auto";
    h.style.width = "10px";
  }
}

function initLaneResize(lane) {
  const handle = lane.q(".lane-resize");
  handle.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    const startY = e.clientY;
    const startH = lane.ws.options.height;
    // Listen on window: the pointer quickly leaves the thin handle while dragging
    const move = (ev) => {
      const h = Math.round(Math.min(LANE_HEIGHT.max, Math.max(LANE_HEIGHT.min, startH + ev.clientY - startY)));
      if (h !== lane.ws.options.height) lane.ws.setOptions({ height: h });
    };
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      document.body.classList.remove("resizing");
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    document.body.classList.add("resizing");
  });
}

function loadIntoLane(lane, file, clip = null, editingIndex = null) {
  // The clip's values apply at once; decoding only clamps them to the file's length
  const c = clip || { start: 0, end: file.duration, speed: 1 };
  Object.assign(lane, {
    path: file.path, name: file.name, duration: file.duration,
    start: c.start, end: c.end, speed: c.speed, editingIndex, region: null, ab: null, zoom: 0,
  });
  lane.el.dataset.ready = "false";
  lane.q(".lane-speed").value = c.speed;
  lane.ws.setPlaybackRate(c.speed, true);
  updateLaneUI(lane);
  // Errors are reported via the "error" event; a load cancelled by the next one is ignored
  lane.ws.load(`/api/audio?path=${encodeURIComponent(file.path)}`).catch(() => {});
  setActiveLane(lane);
  renderLibrary();
  renderTimeline();
}

// `after`: open the new track right below that one (otherwise at the end)
function addLane(file, clip = null, editingIndex = null, after = null) {
  if (state.lanes.length >= MAX_LANES) {
    toast(`The editor holds up to ${MAX_LANES} recordings. Close one, or drop onto a track to replace it.`, "warn", 6000);
    return null;
  }
  const lane = createLane();
  const at = after ? state.lanes.indexOf(after) : -1;
  if (at >= 0) {
    state.lanes.splice(at + 1, 0, lane);
    after.el.after(lane.el);
  } else {
    state.lanes.push(lane);
    $("lanes").append(lane.el);
  }
  renderLanes();
  loadIntoLane(lane, file, clip, editingIndex);
  lane.el.scrollIntoView({ block: "nearest" }); // a new track below the visible ones would go unnoticed
  return lane;
}

function closeLane(lane) {
  lane.path = null;
  lane.ws.destroy();
  lane.el.remove();
  state.lanes = state.lanes.filter((l) => l !== lane);
  if (state.activeLane === lane) state.activeLane = state.lanes.at(-1) ?? null;
  renderLanes();
  renderLibrary();
  renderTimeline();
}

function closeAllLanes() {
  for (const lane of [...state.lanes]) closeLane(lane);
}

function setActiveLane(lane) {
  if (state.activeLane === lane) return;
  state.activeLane = lane;
  for (const l of state.lanes) l.el.classList.toggle("active", l === lane);
}

function renderLanes() {
  state.lanes.forEach((l, i) => {
    l.q(".lane-num").textContent = String(i + 1);
    l.el.classList.toggle("active", l === state.activeLane);
  });
  const n = state.lanes.length;
  $("track-count").textContent = `${n}/${MAX_LANES}`;
  $("editor-empty").classList.toggle("hidden", n > 0);
  $("lanes").classList.toggle("hidden", n === 0);
}

function updateLaneUI(lane) {
  const q = lane.q;
  q(".lane-title").textContent = lane.path ?? "";
  q(".lane-in-time").textContent = fmtMs(lane.start);
  q(".lane-out-time").textContent = fmtMs(lane.end);
  if (lane.region) q(".lane-pos").textContent = fmtMs(lane.ws.getCurrentTime());
  q(".lane-speed-val").textContent = `${Number(lane.speed).toFixed(2)}×`;
  q(".lane-chip-dur").textContent = fmt(clipLength(lane));
  const editing = lane.editingIndex != null;
  q(".lane-badge").textContent = editing ? `Editing timeline clip ${lane.editingIndex + 1}` : "";
  q(".lane-badge").classList.toggle("hidden", !editing);
  q(".lane-add").textContent = editing ? "Update clip ✓" : "Add to timeline ↓";
  q(".lane-add").title = editing
    ? "Write this trim / speed back to the timeline clip it came from"
    : "Add this selection to the end of the timeline";
  q(".lane-cancel").classList.toggle("hidden", !editing);
  const ab = q(".lane-ab");
  ab.classList.toggle("hidden", !findFile(lane.path)?.enhanced);
  ab.classList.toggle("active", lane.ab?.side === "A");
  const gain = lane.ab?.gain;
  const boost = gain == null || Math.abs(gain) < 0.5 ? "" : ` ${gain > 0 ? "+" : "−"}${Math.abs(gain).toFixed(0)} dB`;
  ab.textContent = lane.ab?.side === "A" ? `A · Original${boost}` : "B · Enhanced";
  ab.title = lane.ab?.side === "A" && boost
    ? `Original, ${gain > 0 ? "raised" : "lowered"} by ${Math.abs(gain).toFixed(1)} dB to the enhanced file's loudness, `
      + "so the louder version doesn't win by default (B)"
    : "A/B: switch between this enhanced file and its original (level-matched) at the same position (B)";
}

// A/B: the track plays either the enhanced file (B) or its original (A), which the server renders from
// the same clips, level-matched, so positions line up. Each side's peaks are kept after its first
// load, so later switches don't decode again. Trim, speed and "Add to timeline" stay on the B file.
async function toggleAB(lane) {
  if (!lane?.path || !findFile(lane.path)?.enhanced || lane.el.dataset.ready !== "true" || lane.ab?.busy) return;
  const ab = (lane.ab ??= { side: "B", url: { B: `/api/audio?path=${encodeURIComponent(lane.path)}` }, peaks: {} });
  const next = ab.side === "B" ? "A" : "B";
  const path = lane.path;
  ab.busy = true;
  try {
    if (!ab.url.A) ({ url: ab.url.A, gain: ab.gain } = await api("/api/original", { path }));
    if (lane.path !== path) return; // another file was loaded meanwhile
    const t = lane.ws.getCurrentTime();
    const playing = lane.ws.isPlaying();
    ab.peaks[ab.side] = lane.ws.exportPeaks({ channels: 1 });
    ab.side = next;
    lane.el.dataset.ready = "false";
    const peaks = ab.peaks[next];
    await lane.ws.load(ab.url[next], peaks, peaks ? lane.duration : undefined);
    lane.ws.setTime(Math.min(lane.end, Math.max(lane.start, t)));
    keepVisible(lane, lane.ws.getCurrentTime());
    if (playing) lane.ws.play();
  } catch (e) {
    reportError(e);
  } finally {
    ab.busy = false;
    updateLaneUI(lane);
  }
}

// I / O (or the ▲ under the start / end time): the trim start / end goes to the playhead; a start
// after the end (or an end before the start) is ignored
function setInOut(lane, which) {
  if (!lane?.region) return;
  const t = lane.ws.getCurrentTime();
  if (which === "in" && t < lane.end) lane.start = t;
  if (which === "out" && t > lane.start) lane.end = t;
  lane.region.setOptions({ start: lane.start, end: lane.end });
  updateLaneUI(lane);
}

// Tighten: the server writes a new "<name>_tight.wav" from the track's range (silence at both ends
// trimmed, longer inner pauses shortened) and it opens in a new track right below; this track stays
// as it is. Being a real file, Play, cards and export all match it.
async function tightenLane(lane) {
  if (!lane?.region) return;
  const btn = lane.q(".lane-tighten");
  const { path, start, end, speed } = lane;
  btn.disabled = true;
  btn.textContent = "Tightening…";
  try {
    const r = await api("/api/tighten", { path, start, end, max_pause: Number(lane.q(".lane-pause").value) });
    if (lane.path !== path) return;
    if (r.unchanged) { toast("Nothing to tighten: no long pauses or silence at the ends"); return; }
    await refreshLibrary(r.path);
    const f = findFile(r.path);
    const added = f && addLane(f, { start: 0, end: f.duration, speed }, null, lane);
    const parts = [`−${r.removed.toFixed(1)} s`];
    if (r.shortened) parts.push(`${r.shortened} pause${r.shortened === 1 ? "" : "s"} shortened`);
    if (r.trimmed[0] + r.trimmed[1] >= 0.05) parts.push(`ends trimmed ${r.trimmed[0].toFixed(1)} s / ${r.trimmed[1].toFixed(1)} s`);
    toast(`Tightened → ${r.path} (${parts.join(" · ")})${added ? "" : " — the editor is full, it's in Media"}`,
      added ? "ok" : "warn", 6000);
  } catch (e) {
    reportError(e);
  } finally {
    btn.textContent = "Tighten";
    btn.disabled = false;
  }
}

// ---------- zoom (per track) ----------

// Each track has its own zoom: 0 fits the whole recording, otherwise pixels per second up to
// ZOOM_MAX (1 px = 1 ms, the resolution edits are stored at). Zooming keeps the point under the
// mouse (or the playhead, from the keyboard / buttons) where it is on screen.
const ZOOM_MAX = 1000;
const fitPx = (lane) => lane.ws.getWidth() / (lane.duration || 1);

function setZoom(lane, px, anchorX = null) {
  if (!lane?.region) return;
  const ws = lane.ws;
  const fit = fitPx(lane);
  const target = px <= fit * 1.01 ? 0 : Math.min(ZOOM_MAX, px);
  if (target === lane.zoom) return;
  const oldPx = lane.zoom || fit;
  const ax = anchorX ?? Math.min(ws.getWidth(), Math.max(0, ws.getCurrentTime() * oldPx - ws.getScroll()));
  const anchorT = (ws.getScroll() + ax) / oldPx;
  lane.zoom = target;
  tuneRuler(lane, target || fit);
  ws.zoom(target);
  if (target) ws.setScroll(Math.max(0, anchorT * target - ax));
  updateZoomUI(lane);
}

function updateZoomUI(lane) {
  const msPerPx = 1000 / (lane.zoom || fitPx(lane));
  const per = msPerPx >= 10 ? msPerPx.toFixed(0) : msPerPx >= 1.05 ? msPerPx.toFixed(1) : "1";
  lane.q(".lane-zoom-val").textContent = `1 px = ${per} ms${lane.zoom ? "" : " · fit"}`;
  lane.q(".lane-zoom-out").disabled = !lane.zoom;
  lane.q(".lane-zoom-in").disabled = lane.zoom >= ZOOM_MAX;
  lane.q(".lane-zoom-fit").disabled = !lane.zoom;
}

// Ruler under the waveform: a tick at least ~12 px apart, a label every 5 or 10 ticks, with as many
// decimals as the ticks need (the plugin's own spacing stops at 1 s ticks)
const RULER_STEPS = [0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60];

function tuneRuler(lane, px) {
  const tick = RULER_STEPS.find((s) => s * px >= 12) ?? 60;
  const decimals = tick < 0.01 ? 3 : tick < 0.1 ? 2 : tick < 1 ? 1 : 0;
  Object.assign(lane.ruler.options, {
    timeInterval: tick,
    primaryLabelSpacing: tick * px >= 24 ? 5 : 10,
    primaryLabelInterval: 1e9, // labels by spacing only
    secondaryLabelInterval: 1e9,
    formatTimeCallback: (t) => {
      const m = Math.floor(t / 60);
      const sec = (t - m * 60).toFixed(decimals);
      return m ? `${m}:${sec.padStart(decimals ? decimals + 3 : 2, "0")}` : sec;
    },
  });
}

// Pinch (Safari: gesture events, others: ctrl + wheel) or ⌘ + scroll on the waveform zooms around
// the pointer; a plain scroll keeps scrolling the page / track list
function initLaneZoomGestures(lane) {
  const wave = lane.q(".lane-wave");
  const pointerX = (e) => e.clientX - wave.getBoundingClientRect().left;
  wave.addEventListener("wheel", (e) => {
    if (!(e.ctrlKey || e.metaKey) || !lane.region) return;
    e.preventDefault();
    setZoom(lane, (lane.zoom || fitPx(lane)) * Math.exp(-e.deltaY * 0.01), pointerX(e));
  }, { passive: false });
  let base = 0;
  wave.addEventListener("gesturestart", (e) => { e.preventDefault(); base = lane.zoom || fitPx(lane); });
  wave.addEventListener("gesturechange", (e) => {
    e.preventDefault();
    if (base) setZoom(lane, base * e.scale, e.clientX != null ? pointerX(e) : null);
  });
}

function keepVisible(lane, t) {
  if (!lane.zoom) return;
  const ws = lane.ws;
  const x = t * lane.zoom - ws.getScroll();
  if (x < 0 || x > ws.getWidth()) ws.setScroll(Math.max(0, t * lane.zoom - ws.getWidth() / 2));
}

// Arrow keys move the playhead in fixed time steps, so precision doesn't depend on pixels / zoom;
// like clicks, it stays inside the trim range
function nudge(lane, dir, step) {
  const t = Math.min(lane.end, Math.max(lane.start, +(lane.ws.getCurrentTime() + dir * step).toFixed(3)));
  lane.ws.setTime(t);
  keepVisible(lane, t);
}

function togglePlay(lane) {
  if (!lane?.path) return;
  setActiveLane(lane);
  if (lane.ws.isPlaying()) { lane.ws.pause(); return; }
  tlPause();
  pauseLanes(lane);
  const t = lane.ws.getCurrentTime();
  if (t < lane.start || t >= lane.end - 0.05) lane.ws.setTime(lane.start);
  lane.ws.play();
}

function pauseLanes(except = null) {
  for (const l of state.lanes) if (l !== except && l.ws.isPlaying()) l.ws.pause();
}

function laneClip(lane) {
  const { path, name, start, end, speed } = lane;
  return { path, name, start: +start.toFixed(3), end: +end.toFixed(3), speed };
}

function commitLane(lane, insertAt = null) {
  if (!lane?.path) return;
  const clip = laneClip(lane);
  if (lane.editingIndex != null && state.timeline[lane.editingIndex]) {
    state.timeline[lane.editingIndex] = clip;
    toast(`Timeline clip ${lane.editingIndex + 1} updated`);
    lane.editingIndex = null;
  } else {
    state.timeline.splice(insertAt ?? state.timeline.length, 0, clip);
  }
  updateLaneUI(lane);
  saveTimeline();
  renderTimeline();
}

// Timeline indexes shift when clips are removed or reordered: stop editing rather than point at the wrong clip
function clearEditing() {
  for (const lane of state.lanes) {
    if (lane.editingIndex != null) { lane.editingIndex = null; updateLaneUI(lane); }
  }
}

async function enhanceLane(lane) {
  if (!lane?.path) return;
  if (lane.job) { cancelJob(lane.job); return; } // a second click while it runs cancels it
  if (lane.enhancing) return; // starting
  if (!resembleOk(clipLength(lane))) return;
  const btn = lane.q(".lane-enhance");
  const title = btn.title;
  lane.enhancing = true;
  btn.classList.add("busy");
  btn.textContent = "Processing…";
  btn.title = "Click to cancel";
  try {
    const { path, start, end, speed } = laneClip(lane);
    const r = await runJob("/api/enhance", { path, start, end, speed, ...enhanceSettings() }, (j) => {
      lane.job = j.id;
      btn.textContent = busyLabel(j);
    });
    await refreshLibrary(r.path);
    // like Tighten: the enhanced version opens right below; trim and speed are already in the file
    const f = findFile(r.path);
    const added = f && addLane(f, { start: 0, end: f.duration, speed: 1 }, null, lane);
    toast(resultMessage("Enhanced", r) + (added ? "" : " — the editor is full, it's in Exports"),
      added ? "ok" : "warn", 8000, [{ label: "Show in Finder", onClick: () => reveal(r.path) }]);
  } catch (e) {
    if (e.cancelled) toast("Enhance cancelled", "warn");
    else reportError(e);
  } finally {
    lane.job = null;
    lane.enhancing = false;
    btn.classList.remove("busy");
    btn.textContent = "✨ Enhance";
    btn.title = title;
  }
}

function openCard(index, targetLane = null) {
  const c = state.timeline[index];
  if (!c) return;
  const existing = state.lanes.find((l) => l.editingIndex === index);
  if (existing && !targetLane) { setActiveLane(existing); return; }
  const f = findFile(c.path) || { path: c.path, name: c.name, duration: c.end };
  const lane = targetLane ?? (state.lanes.length < MAX_LANES ? null : state.activeLane);
  if (lane) loadIntoLane(lane, f, c, index);
  else addLane(f, c, index);
}

// ---------- timeline ----------

let insertIndex = null;

function renderTimeline() {
  const track = $("track");
  track.querySelectorAll(".card, .insert-marker").forEach((n) => n.remove());
  $("track-empty").classList.toggle("hidden", state.timeline.length > 0);
  const editing = new Set(state.lanes.map((l) => l.editingIndex).filter((i) => i != null));
  const widths = cardWidths();

  state.timeline.forEach((c, i) => {
    const card = document.createElement("div");
    card.className = "card";
    if (editing.has(i)) card.classList.add("editing");
    card.draggable = true;
    card.style.width = `${widths[i]}px`;
    card.innerHTML = `<button class="remove" title="Remove">×</button>
      <div class="name"></div>
      <div class="card-wave"><canvas></canvas><div class="played"></div></div>
      <div class="card-foot">
        <span class="meta mono">${fmt(c.start)} → ${fmt(c.end)}${c.speed !== 1 ? ` · ${c.speed.toFixed(2)}×` : ""}</span>
        <span class="dur mono">${fmt(clipLength(c))}</span>
      </div>`;
    card.querySelector(".name").textContent = `${i + 1}. ${c.name}`;
    card.title = `${c.path}\nClick to edit`;
    card.querySelector(".remove").addEventListener("click", (e) => {
      e.stopPropagation();
      state.timeline.splice(i, 1);
      clearEditing();
      saveTimeline();
      renderTimeline();
    });
    card.addEventListener("click", () => openCard(i));
    card.addEventListener("dragstart", (e) => {
      setDrag(e, { kind: "card", index: i });
      requestAnimationFrame(() => card.classList.add("dragging"));
    });
    card.addEventListener("dragend", () => card.classList.remove("dragging"));
    track.append(card);
  });

  const total = state.timeline.reduce((s, c) => s + clipLength(c), 0);
  $("total").textContent = fmt(total);
  $("export").disabled = state.timeline.length === 0;
  $("tl-play").disabled = state.timeline.length === 0;
  tl.starts = state.timeline.reduce((acc, c, i) => [...acc, i ? acc[i - 1] + clipLength(state.timeline[i - 1]) : 0], []);
  renderRuler();
  renderTlCursor();
  renderEnhanceWarning();
  drawCardWaves();
  schedulePreview();
}

// Cards are as wide as their clips are long (fit to the panel), side by side like one track
const CARD_GAP = 2;
const CARD_MIN_W = 48;

function cardWidths() {
  const n = state.timeline.length;
  if (!n) return [];
  const scroll = $("track-scroll");
  const pad = parseFloat(getComputedStyle(scroll).paddingLeft) + parseFloat(getComputedStyle(scroll).paddingRight);
  const avail = Math.max(200, scroll.clientWidth - pad - CARD_GAP * (n - 1));
  const total = state.timeline.reduce((s, c) => s + clipLength(c), 0) || 1;
  return state.timeline.map((c) => Math.max(CARD_MIN_W, Math.floor((clipLength(c) / total) * avail)));
}

// ---------- card waveforms ----------

// Decoded audio per file (mono samples), shared by all cards of that file
const decoded = new Map();
let decodeCtx = null;

function decodeFile(path) {
  if (!decoded.has(path)) {
    // An offline context can decode without a user gesture
    decodeCtx ??= new OfflineAudioContext(1, 1, 48000);
    decoded.set(path, fetch(`/api/audio?path=${encodeURIComponent(path)}`)
      .then((r) => r.arrayBuffer())
      .then((buf) => decodeCtx.decodeAudioData(buf))
      .then((ab) => ({ data: ab.getChannelData(0), rate: ab.sampleRate }))
      .catch(() => { decoded.delete(path); return null; }));
  }
  return decoded.get(path);
}

// Same look as the editor: 2 px bars with a 1 px gap, absolute scale
async function drawCardWave(canvas, clip) {
  const audio = await decodeFile(clip.path);
  if (!audio || !canvas.isConnected) return;
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth;
  const h = canvas.clientHeight;
  canvas.width = Math.round(w * dpr);
  canvas.height = Math.round(h * dpr);
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  ctx.fillStyle = css("--wave");
  const s0 = Math.floor(clip.start * audio.rate);
  const s1 = Math.min(audio.data.length, Math.floor(clip.end * audio.rate));
  const bars = Math.max(1, Math.floor(w / 3));
  const step = Math.max(1, (s1 - s0) / bars);
  for (let b = 0; b < bars; b++) {
    let peak = 0;
    const from = Math.floor(s0 + b * step);
    const to = Math.min(s1, Math.floor(from + step));
    for (let k = from; k < to; k++) peak = Math.max(peak, Math.abs(audio.data[k]));
    const bh = Math.max(1, peak * h);
    ctx.fillRect(b * 3, (h - bh) / 2, 2, bh);
  }
  canvas.dataset.drawn = "true";
}

function drawCardWaves() {
  $("track").querySelectorAll(".card canvas").forEach((cv, i) => {
    const c = state.timeline[i];
    if (c) drawCardWave(cv, c);
  });
}

// Enhancing already-enhanced clips again runs the denoiser twice and re-encodes lossy audio
function renderEnhanceWarning() {
  const n = state.timeline.filter((c) => findFile(c.path)?.enhanced).length;
  const warn = $("enhance-warn");
  warn.classList.toggle("hidden", !(n && $("enhance").checked));
  warn.textContent = `⚠ ${n} clip${n === 1 ? " is" : "s are"} already enhanced`;
  warn.title = "Enhancing again runs noise reduction a second time (duller consonants, artifacts) and "
    + "re-encodes lossy audio. Untick Enhance when joining already-enhanced clips.";
}

// Re-rendering cards mid-drag could remove the dragged element and cancel the drag,
// so only a single marker is moved around
const insertMarker = Object.assign(document.createElement("div"), { className: "insert-marker" });

function placeMarker(index) {
  insertIndex = index;
  if (index === null || !state.timeline.length) { insertMarker.remove(); return; }
  const cards = $("track").querySelectorAll(".card");
  if (index < cards.length) cards[index].before(insertMarker);
  else cards[cards.length - 1].after(insertMarker);
}

function indexFromPointer(e) {
  const cards = [...$("track").querySelectorAll(".card")];
  for (let i = 0; i < cards.length; i++) {
    const r = cards[i].getBoundingClientRect();
    if (e.clientX < r.left + r.width / 2) return i;
  }
  return cards.length;
}

dropZone($("timeline-drop"), (data) => {
  const at = insertIndex ?? state.timeline.length;
  placeMarker(null);
  if (data.kind === "lane") {
    const lane = state.lanes.find((l) => l.id === data.id);
    if (lane) commitLane(lane, lane.editingIndex == null ? at : null);
    return;
  }
  if (data.kind === "file") {
    const f = findFile(data.path);
    if (f) state.timeline.splice(at, 0, { path: f.path, name: f.name, start: 0, end: f.duration, speed: 1 });
  } else if (data.kind === "card") {
    const [c] = state.timeline.splice(data.index, 1);
    state.timeline.splice(at > data.index ? at - 1 : at, 0, c);
    clearEditing();
  }
  saveTimeline();
  renderTimeline();
}, (e) => {
  const next = e ? indexFromPointer(e) : null;
  if (next !== insertIndex) placeMarker(next);
});

// A track's chip dropped in the editor: the server writes its range as a recording of its own
// ("<name>_part.wav", like Tighten's file) and it opens in a new track — below the track it's dropped
// on (never replacing it: a full editor has no empty space), else at the end. The speed carries over.
// E.g. to cut a mistake out of a take: trim to the part before it, drag the chip out, then trim to
// the part after it.
async function extractLane(src, below) {
  const { path, start, end, speed } = src;
  try {
    const r = await api("/api/extract", { path, start, end });
    await refreshLibrary(r.path);
    const f = findFile(r.path);
    const added = f && addLane(f, { start: 0, end: f.duration, speed }, null, state.lanes.includes(below) ? below : null);
    toast(`New recording → ${r.path} (${fmt(r.duration)})${added ? "" : " — the editor is full, it's in Media"}`,
      added ? "ok" : "warn", 5000);
  } catch (e) {
    reportError(e);
  }
}

// Dropping onto a track replaces it; dropping elsewhere in the editor adds a new track
dropZone($("editor-drop"), (data, e) => {
  const laneEl = e.target.closest?.(".lane");
  const target = laneEl ? state.lanes.find((l) => l.el === laneEl) : null;
  if (data.kind === "lane") {
    const src = state.lanes.find((l) => l.id === data.id);
    if (src?.region && src !== target) extractLane(src, target);
  } else if (data.kind === "file") {
    const f = findFile(data.path);
    if (!f) return;
    if (target) loadIntoLane(target, f);
    else addLane(f);
  } else if (data.kind === "card") {
    openCard(data.index, target);
  }
});

$("clear-timeline").addEventListener("click", () => {
  if (!state.timeline.length || !confirm("Clear the timeline? (Files are not deleted)")) return;
  state.timeline = [];
  clearEditing();
  saveTimeline();
  renderTimeline();
});

const exporting = { job: null, starting: false };

$("export").addEventListener("click", async () => {
  if (exporting.job) { cancelJob(exporting.job); return; } // a second click while it runs cancels it
  if (exporting.starting) return;
  if ($("enhance").checked && !resembleOk(tlTotal())) return;
  const btn = $("export");
  exporting.starting = true;
  btn.classList.add("busy");
  btn.textContent = "Processing…";
  btn.title = "Click to cancel";
  try {
    const r = await runJob("/api/export", {
      clips: state.timeline.map(({ path, start, end, speed }) => ({ path, start, end, speed })),
      enhance: $("enhance").checked,
      ...enhanceSettings(),
    }, (j) => {
      exporting.job = j.id;
      btn.textContent = busyLabel(j);
    });
    await refreshLibrary(r.path);
    toast(resultMessage("Exported", r), "ok", 8000, resultActions(r.path));
  } catch (e) {
    if (e.cancelled) toast("Export cancelled", "warn");
    else reportError(e);
  } finally {
    exporting.job = null;
    exporting.starting = false;
    btn.classList.remove("busy");
    btn.textContent = "Join & Export";
    btn.title = "";
    btn.disabled = state.timeline.length === 0;
  }
});

// Resemble settings popover
$("denoiser").addEventListener("change", () => {
  saveEnhanceSettings();
  updateDenoiserUI();
  if ($("denoiser").value === "resemble") {
    toast("Resemble Enhance: rebuilds the voice and runs on the CPU — about 3–4× the recording's length. "
      + "Tune it under ⚙ Settings.", "ok", 6000);
  } else if ($("denoiser").value === "sidon") {
    toast("Sidon: rebuilds the voice at 48 kHz (generative) — fast, about half the recording's length. "
      + "No settings.", "ok", 6000);
  }
});
$("resemble-btn").addEventListener("click", (e) => {
  e.stopPropagation();
  $("resemble-panel").classList.toggle("hidden");
});
$("resemble-panel").addEventListener("click", (e) => e.stopPropagation());
document.addEventListener("click", () => $("resemble-panel").classList.add("hidden"));
for (const id of ["rs-tau", "rs-lambd", "rs-nfe", "rs-solver", "rs-seed"]) {
  $(id).addEventListener("input", () => { updateResembleValues(); updateDenoiserUI(); });
  $(id).addEventListener("change", () => {
    if (id === "rs-seed") $("rs-seed").value = resembleSettings().seed; // clamp typed values
    saveEnhanceSettings();
    updateDenoiserUI();
  });
}
$("rs-roll").addEventListener("click", () => {
  $("rs-seed").value = Math.floor(Math.random() * (Number($("rs-seed").max) + 1));
  saveEnhanceSettings();
  updateDenoiserUI();
});
$("rs-reset").addEventListener("click", () => {
  setResembleFields(resemble.defaults);
  saveEnhanceSettings();
  updateDenoiserUI();
});
$("rs-done").addEventListener("click", () => $("resemble-panel").classList.add("hidden"));

// ---------- timeline playback ----------

// Like an editor's "render preview": the server renders the whole timeline (same trim / speed as
// the export, without enhancement) into one file, so playback flows across clips without gaps.
const tl = {
  audio: new Audio(), key: null, url: null, pending: null, timer: 0,
  playing: false, raf: 0, t: 0, starts: [],
};
tl.audio.muted = MUTED;
tl.audio.preload = "auto";
tl.audio.addEventListener("ended", () => tlReset());

const timelineKey = () => JSON.stringify(state.timeline.map(({ path, start, end, speed }) => [path, start, end, speed]));
const tlTotal = () => state.timeline.reduce((s, c) => s + clipLength(c), 0);

function ensurePreview() {
  const key = timelineKey();
  if (!state.timeline.length) return Promise.resolve(null);
  if (tl.key === key && tl.url) return Promise.resolve(tl.url);
  if (tl.pending?.key === key) return tl.pending.promise;
  const clips = state.timeline.map(({ path, start, end, speed }) => ({ path, start, end, speed }));
  const promise = api("/api/preview", { clips }).then((r) => {
    if (timelineKey() !== key) return null; // the timeline changed meanwhile
    tl.key = key;
    tl.url = r.url;
    tl.audio.src = r.url;
    tl.audio.load();
    return r.url;
  }).finally(() => { if (tl.pending?.key === key) tl.pending = null; });
  tl.pending = { key, promise };
  return promise;
}

// Render in the background shortly after each change, so Play is usually instant
function schedulePreview() {
  clearTimeout(tl.timer);
  if (!state.timeline.length || tl.key === timelineKey()) return;
  tl.timer = setTimeout(() => ensurePreview().catch(() => {}), 250);
}

function startAudio() {
  const a = tl.audio;
  const seek = () => { a.currentTime = tl.t; };
  if (a.readyState >= 1) seek();
  else a.addEventListener("loadedmetadata", seek, { once: true });
  return a.play();
}

async function tlPlay() {
  if (!state.timeline.length) return;
  pauseLanes();
  if (tl.t >= tlTotal() - 0.02) tl.t = 0;
  tl.playing = true;
  $("tl-play").textContent = "❚❚ Pause";
  try {
    if (tl.key === timelineKey() && tl.url) {
      await startAudio(); // called right in the click handler so the browser allows sound
    } else {
      $("tl-play").textContent = "Rendering…";
      await ensurePreview();
      if (!tl.playing) return;
      $("tl-play").textContent = "❚❚ Pause";
      await startAudio();
    }
  } catch (e) {
    tlPause();
    if (e.name === "NotAllowedError") toast("The browser blocked playback — press Play again.", "warn");
    else if (e.name !== "AbortError") reportError(e);
    return;
  }
  cancelAnimationFrame(tl.raf);
  tl.raf = requestAnimationFrame(tlTick);
}

function tlPause() {
  tl.playing = false;
  tl.audio.pause();
  cancelAnimationFrame(tl.raf);
  $("tl-play").textContent = "▶ Play";
}

function tlReset() {
  tlPause();
  tl.t = 0;
  if (tl.audio.readyState >= 1) tl.audio.currentTime = 0;
  renderTlCursor();
}

function tlTick() {
  if (!tl.playing) return;
  if (!tl.audio.seeking) tl.t = tl.audio.currentTime;
  renderTlCursor();
  tl.raf = requestAnimationFrame(tlTick);
}

// Which clip a timeline time falls into, and how far into it (0–1)
function clipAt(t) {
  const n = state.timeline.length;
  for (let i = 0; i < n; i++) {
    const len = clipLength(state.timeline[i]);
    if (t < tl.starts[i] + len || i === n - 1) return [i, Math.min(1, Math.max(0, (t - tl.starts[i]) / len))];
  }
  return [0, 0];
}

const rulerSegs = () => [...$("ruler").children];

function renderRuler() {
  const ruler = $("ruler");
  ruler.innerHTML = "";
  for (const card of $("track").querySelectorAll(".card")) {
    const seg = document.createElement("div");
    seg.className = "ruler-seg";
    seg.style.left = `${card.offsetLeft}px`;
    seg.style.width = `${card.offsetWidth}px`;
    ruler.append(seg);
  }
}

function renderTlCursor() {
  const cursor = $("tl-cursor");
  const cards = $("track").querySelectorAll(".card");
  $("tl-pos").textContent = fmt(Math.min(tl.t, tlTotal()));
  if (!cards.length) { cursor.classList.add("hidden"); return; }
  const [i, frac] = clipAt(tl.t);
  cursor.style.left = `${cards[i].offsetLeft + frac * cards[i].offsetWidth}px`;
  cursor.classList.remove("hidden");
  // Tint the played part of each card, like the editor's progress colour
  cards.forEach((card, k) => card.style.setProperty("--played", k < i ? 1 : k === i ? frac : 0));
}

// Click or drag on the ruler to move the playhead (across clips)
function tlSeekAt(clientX) {
  const segs = rulerSegs();
  if (!segs.length) return;
  let i = segs.findIndex((s) => clientX < s.getBoundingClientRect().right);
  if (i < 0) i = segs.length - 1;
  const r = segs[i].getBoundingClientRect();
  const frac = Math.min(1, Math.max(0, (clientX - r.left) / r.width));
  tl.t = tl.starts[i] + frac * clipLength(state.timeline[i]);
  if (tl.audio.readyState >= 1) tl.audio.currentTime = tl.t;
  renderTlCursor();
}

$("ruler").addEventListener("pointerdown", (e) => {
  if (!e.target.classList.contains("ruler-seg")) return;
  e.preventDefault();
  tlSeekAt(e.clientX);
  const move = (ev) => tlSeekAt(ev.clientX);
  const up = () => {
    window.removeEventListener("pointermove", move);
    window.removeEventListener("pointerup", up);
  };
  window.addEventListener("pointermove", move);
  window.addEventListener("pointerup", up);
});

$("tl-play").addEventListener("click", () => (tl.playing ? tlPause() : tlPlay()));
$("timeline-drop").addEventListener("pointerdown", () => { state.focus = "timeline"; });
$("enhance").addEventListener("change", renderEnhanceWarning);
let resizeTimer = 0;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => {
    renderTimeline(); // card widths follow the panel width
    for (const lane of state.lanes) if (lane.region) updateZoomUI(lane); // "fit" covers more / less per pixel
  }, 100);
});

// ---------- general ----------

$("rec-btn").addEventListener("click", toggleRecord);
$("add-files").addEventListener("click", () => $("file-input").click());
$("file-input").addEventListener("change", (e) => { importFiles(e.target.files); e.target.value = ""; });
fileDropZone($("library-drop"), false);
fileDropZone($("editor-drop"), true);
try { $("countdown").checked = localStorage.getItem(COUNTDOWN_KEY) !== "off"; } catch { /* ignore */ }
$("countdown").addEventListener("change", (e) => {
  try { localStorage.setItem(COUNTDOWN_KEY, e.target.checked ? "on" : "off"); } catch { /* ignore */ }
});
$("device").addEventListener("change", (e) => {
  try { localStorage.setItem(DEVICE_KEY, e.target.value); } catch { /* ignore */ }
});
$("refresh").addEventListener("click", () => refreshLibrary().catch(reportError));
$("filter").addEventListener("input", () => renderLibrary());

// Shortcuts act on the active (last clicked) track; Space plays the timeline after clicking in it;
// ← / → move its playhead (⇧ 100 ms, ⌥ 1 ms, else 10 ms);
// + / − / 0 zoom it in / out / to fit
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("resemble-panel").classList.contains("hidden")) {
    $("resemble-panel").classList.add("hidden");
    return;
  }
  if (e.target.matches("input, select, textarea")) return; // fields and sliders keep their own keys
  const lane = state.activeLane;
  if (state.focus === "lane" && !e.metaKey && !e.ctrlKey && lane?.region) {
    if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
      e.preventDefault();
      nudge(lane, e.key === "ArrowRight" ? 1 : -1, e.altKey ? 0.001 : e.shiftKey ? 0.1 : 0.01);
      return;
    }
    if (e.key === "+" || e.key === "=") { setZoom(lane, (lane.zoom || fitPx(lane)) * 2); return; }
    if (e.key === "-") { setZoom(lane, (lane.zoom || fitPx(lane)) / 2); return; }
    if (e.key === "0") { setZoom(lane, 0); return; }
  }
  if (e.code === "Space") {
    e.preventDefault();
    if (state.focus === "timeline") (tl.playing ? tlPause() : tlPlay());
    else togglePlay(lane);
  }
  else if (e.key === "i" || e.key === "I") setInOut(lane, "in");
  else if (e.key === "o" || e.key === "O") setInOut(lane, "out");
  else if ((e.key === "b" || e.key === "B") && !e.metaKey && !e.ctrlKey) toggleAB(lane);
});

renderLanes();
initProjects().catch(reportError); // media + timeline load from the current project
initConfig().catch((e) => toast(`Couldn't load settings: ${e.message}`, "error"));
initDevices();
syncRecordStatus().catch(reportError);
