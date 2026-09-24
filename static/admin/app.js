// --- session expiry: auto-redirect to login on any 401 ---
(function () {
  var _fetch = window.fetch;
  window.fetch = function () {
    return _fetch.apply(this, arguments).then(function (resp) {
      if (resp && resp.status === 401) {
        var next = encodeURIComponent(window.location.pathname + window.location.search);
        window.location.href = "/login?next=" + next;
        throw new Error("session expired");
      }
      return resp;
    });
  };
})();

// --- tab switching ---
document.querySelectorAll(".tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    const panel = document.getElementById("panel-" + btn.dataset.tab);
    if (panel) panel.classList.add("active");
    if (typeof onTabShow === "function") onTabShow(btn.dataset.tab);
  });
});

// Reload a tab's data when it becomes visible so nothing is stale.
function onTabShow(tab) {
  const loaders = {
    stt: () => loadSTTModels().then(() => { loadSTTSettings(); loadSTTStatus(); }),
    tts: () => { refreshModels(); loadEngineSettings(); loadTtsGeneral(); loadGeneralSettings(); },
    avatars: () => { loadAvatarDefaults(); loadAvatars(); loadAvatarGeneral(); loadDittoStatus(); loadDittoSettings(); },
    personalities: () => { loadPersDefault(); loadPersonalities(); },
    llms: () => { loadLlmDefaults(); loadLlmModels(); },
    general: () => { loadMemorySettings(); loadServerSettings(); loadAuthSettings(); loadToolcalling(); },
    status: () => { loadStatus(); loadLogs(); loadLogSettings(); },
    profiles: () => loadProfDropdowns().then(() => loadTalkProfiles()),
  };
  const fn = loaders[tab];
  if (fn) fn();
}

// TTS Server Web UI
const $ = (id) => document.getElementById(id);

let models = [];
let voices = [];
let editingId = null;      // profile id being edited (null = create mode)
let recChunks = [];
let recTimer = null;
let recSeconds = 0;

// ---------- health / models ----------
async function refreshHealth() {
  try {
    const r = await fetch("/api/health");
    const d = await r.json();
    const parts = d.models.map(m =>
      `${m.name}:${m.loaded ? "loaded" : (m.available ? "ready" : "off")}`);
    $("health").textContent = parts.join("  ·  ");
    $("health").className = "health " + (d.models.some(m => m.loaded) ? "ok" : "");
    for (const m of (d.models || [])) {
      const cur = models.find(x => x.name === m.name);
      if (cur) { cur.loaded = m.loaded; cur.available = m.available; }
    }
    renderModelsList();
  } catch (e) {
    $("health").textContent = "server unreachable";
    $("health").className = "health err";
  }
}

async function refreshModels() {
  const r = await fetch("/api/tts/status");
  const d = await r.json();
  models = d.models;
  const sel = $("model");
  const cur = sel.value;
  sel.innerHTML = "";
  for (const m of models) {
    const opt = document.createElement("option");
    opt.value = m.name;
    let label = m.name;
    if (m.name === "breeze" && m.active_engine) label += ` [${m.active_engine}]`;
    if (!m.available) label += " (unavailable)";
    opt.textContent = label;
    sel.appendChild(opt);
  }
  if (cur && models.some(m => m.name === cur)) sel.value = cur;
  onModelChange();
  renderModelsList();
}

function renderModelsList() {
  const box = $("models-list");
  if (!box) return;
  box.innerHTML = "";
  for (const m of models) {
    const row = document.createElement("div");
    row.className = "model-row";
    const name = document.createElement("span");
    name.className = "model-name";
    name.textContent = m.name;
    const st = document.createElement("span");
    st.className = "model-status " + (m.loaded ? "on" : "off");
    st.textContent = m.loaded ? "loaded" : (m.available ? "idle" : "unavailable");
    const loadBtn = document.createElement("button");
    loadBtn.className = "secondary";
    loadBtn.textContent = "Load";
    loadBtn.disabled = m.loaded || !m.available;
    loadBtn.onclick = () => loadModel(m.name);
    const btn = document.createElement("button");
    btn.className = "secondary";
    btn.textContent = "Unload";
    btn.disabled = !m.loaded;
    btn.onclick = () => unloadModel(m.name);
    row.appendChild(name);
    row.appendChild(st);
    row.appendChild(loadBtn);
    row.appendChild(btn);
    box.appendChild(row);
  }
}

async function loadModel(name) {
  setStatus("gen-status", "Loading " + name + "...", "");
  const fd = new FormData();
  fd.append("model", name);
  try {
    const r = await fetch("/api/models/load", { method: "POST", body: fd });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || "failed");
    await refreshModels();
    await refreshHealth();
    setStatus("gen-status", name + " loaded", "ok");
  } catch (e) {
    setStatus("gen-status", "Load failed: " + e.message, "err");
  }
}

async function unloadModel(name) {
  setStatus("gen-status", "Unloading " + name + "...", "");
  const fd = new FormData();
  fd.append("model", name);
  try {
    const r = await fetch("/api/models/unload", { method: "POST", body: fd });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || "failed");
    await refreshModels();
    await refreshHealth();
    setStatus("gen-status", name + " unloaded", "ok");
  } catch (e) {
    setStatus("gen-status", "Unload failed: " + e.message, "err");
  }
}

function onModelChange() {
  const model = $("model").value;
  const isBreeze = (model === "breeze" || model === "omnivoice");
  $("breeze-opts").style.display = isBreeze ? "" : "none";
  $("instr-field").style.display = isBreeze ? "" : "none";
  refreshVoices(model);
}

// ---------- voices ----------
async function refreshVoices(model) {
  const r = await fetch("/api/voices");
  const d = await r.json();
  voices = d.voices;
  const sel = $("voice");
  const instr = $("instruction");
  const cur = sel.value;
  const curInstr = instr.value;
  sel.innerHTML = "";
  instr.innerHTML = "";
  if (model === "kokoro") {
    const m = models.find(x => x.name === "kokoro");
    for (const v of (m ? m.voices : [])) {
      const opt = document.createElement("option");
      opt.value = v.id;
      opt.textContent = v.name;
      sel.appendChild(opt);
    }
  } else {
    // breeze: clone profiles -> voice dropdown, design profiles -> instruction dropdown
    const clones = voices.filter(v => (v.model === "breeze" || v.model === "omnivoice" || !v.model) && v.kind === "clone");
    const designs = voices.filter(v => (v.model === "breeze" || v.model === "omnivoice" || !v.model) && v.kind === "design");

    const optNone = document.createElement("option");
    optNone.value = "";
    optNone.textContent = "(no clone)";
    sel.appendChild(optNone);
    for (const v of clones) {
      const opt = document.createElement("option");
      opt.value = v.id;
      opt.textContent = v.name;
      sel.appendChild(opt);
    }

    const optNone2 = document.createElement("option");
    optNone2.value = "";
    optNone2.textContent = "(no instruction)";
    instr.appendChild(optNone2);
    for (const v of designs) {
      const opt = document.createElement("option");
      opt.value = v.id;
      opt.textContent = v.name;
      instr.appendChild(opt);
    }
  }
  if (cur && [...sel.options].some(o => o.value === cur)) sel.value = cur;
  if (curInstr && [...instr.options].some(o => o.value === curInstr)) instr.value = curInstr;
  renderProfiles();
}

function renderProfileList(box, list, emptyMsg) {
  box.innerHTML = "";
  if (list.length === 0) {
    box.innerHTML = '<p class="hint">' + emptyMsg + '</p>';
    return;
  }
  for (const v of list) {
    const row = document.createElement("div");
    row.className = "profile";
    const refNote = v.kind === "clone"
      ? (v.ref_audio_exists ? "ref audio ok" : "ref audio MISSING")
      : `“${(v.instruction || "").slice(0, 60)}${(v.instruction || "").length > 60 ? "…" : ""}”`;
    row.innerHTML = `
      <div class="profile-info">
        <strong>${escapeHtml(v.name)}</strong>
        <span class="tag">${v.kind}</span>
        ${(v.engines || [v.model || "breeze"]).map(e => `<span class="tag">${e}</span>`).join("")}
        <div class="hint">${escapeHtml(refNote)}</div>
        ${v.transcript ? `<div class="hint transcript">“${escapeHtml(v.transcript.slice(0, 120))}${v.transcript.length > 120 ? "…" : ""}”</div>` : ""}
      </div>
      <div class="profile-actions">
        <button class="secondary edit-btn" data-id="${v.id}">Edit</button>
        <button class="danger del-btn" data-id="${v.id}">Delete</button>
      </div>`;
    box.appendChild(row);
  }
  box.querySelectorAll(".edit-btn").forEach(b => b.onclick = () => editProfile(b.dataset.id));
  box.querySelectorAll(".del-btn").forEach(b => b.onclick = () => deleteProfile(b.dataset.id));
}

function renderProfiles() {
  const clones = voices.filter(v => v.kind === "clone");
  const designs = voices.filter(v => v.kind === "design");
  renderProfileList($("profiles-clones"), clones, "No clones yet. Clone a voice above.");
  renderProfileList($("profiles-designs"), designs, "No designs yet. Design a voice above.");
}

function escapeHtml(s) {
  return (s || "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ---------- TTS ----------
let chunkQueue = [];

$("generate").onclick = async () => {
  const text = $("text").value;
  if (!text.trim()) { setStatus("gen-status", "enter some text", "err"); return; }
  const model = $("model").value;
  const voice = $("voice").value;
  const fmt = $("format").value;
  const rtype = $("return-type").value;
  if (model === "breeze" && !voice && !$("instruction").value && !$("instruction-text").value.trim()) {
    setStatus("gen-status", "select a clone voice, an instruction, or both", "err");
    return;
  }
  const fd = new FormData();
  fd.append("text", text);
  fd.append("model", model);
  if (model === "breeze" || model === "omnivoice") {
    const manualInstr = $("instruction-text").value.trim();
    fd.append("voice_id", voice);
    if (manualInstr) {
      fd.append("instruction", manualInstr);
    } else {
      fd.append("instruction_id", $("instruction").value);
    }
    const cfg = $("cfg").value; if (cfg) fd.append("cfg_scale", cfg);
    const seed = $("seed").value; if (seed) fd.append("seed", seed);
  } else {
    fd.append("voice", voice);
  }
  fd.append("output_format", fmt);
  fd.append("return_type", rtype);

  $("result").style.display = "none";
  $("chunk-result").style.display = "none";

  if (rtype === "chunked") {
    await runChunked(fd);
  } else {
    await runFull(fd);
  }
};

async function runFull(fd) {
  setStatus("gen-status", "generating…", "");
  $("generate").disabled = true;
  const t0 = Date.now();
  try {
    const r = await fetch("/api/tts", { method: "POST", body: fd });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || d.error || JSON.stringify(d));
    $("player").src = d.audio_url;
    $("download").href = d.audio_url;
    $("result").style.display = "";
    $("result-meta").textContent =
      `${d.model} · ${d.voice} · ${d.duration_sec}s · ${d.chunks || 1} chunks · ${((Date.now() - t0) / 1000).toFixed(1)}s gen`;
    setStatus("gen-status", "done", "ok");
    $("player").play().catch(() => {});
  } catch (e) {
    setStatus("gen-status", "error: " + e.message, "err");
  } finally {
    $("generate").disabled = false;
  }
}

async function runChunked(fd) {
  setStatus("gen-status", "starting…", "");
  $("generate").disabled = true;
  $("chunk-result").style.display = "";
  $("chunk-list").innerHTML = "";
  chunkQueue = [];
  try {
    const r = await fetch("/api/tts", { method: "POST", body: fd });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || d.error || JSON.stringify(d));
    const rid = d.request_id;
    setStatus("gen-status", `streaming 0/${d.total_chunks} chunks…`, "");
    let seen = 0;
    const poll = async () => {
      try {
        const sr = await fetch("/api/tts/status/" + rid);
        const st = await sr.json();
        if (!sr.ok) throw new Error(st.detail || st.error || JSON.stringify(st));
        const chunks = st.chunks || [];
        for (let i = seen; i < chunks.length; i++) addChunk(chunks[i]);
        seen = chunks.length;
        $("chunk-progress").textContent =
          `${st.status === "done" ? "done" : "streaming"} · ${chunks.length}/${st.total_chunks} chunks`;
        if (st.status === "processing") {
          setTimeout(poll, 800);
        } else if (st.status === "done") {
          setStatus("gen-status", `done · ${chunks.length} chunks`, "ok");
          $("generate").disabled = false;
        } else {
          setStatus("gen-status", "error: " + (st.error || "unknown"), "err");
          $("generate").disabled = false;
        }
      } catch (e) {
        setStatus("gen-status", "error: " + e.message, "err");
        $("generate").disabled = false;
      }
    };
    poll();
  } catch (e) {
    setStatus("gen-status", "error: " + e.message, "err");
    $("generate").disabled = false;
  }
}

function addChunk(chunk) {
  const row = document.createElement("div");
  row.className = "chunk";
  row.innerHTML = `<div class="hint">${escapeHtml(chunk.text)}</div>`;
  const a = document.createElement("audio");
  a.controls = true;
  a.preload = "auto";
  a.src = chunk.audio_url;
  row.appendChild(a);
  $("chunk-list").appendChild(row);
  chunkQueue.push(a);
  a.addEventListener("ended", () => {
    const idx = chunkQueue.indexOf(a);
    if (idx >= 0 && idx + 1 < chunkQueue.length) chunkQueue[idx + 1].play().catch(() => {});
  });
  if (chunkQueue.length === 1) a.play().catch(() => {});
}

async function deleteProfile(id) {
  if (!confirm("Delete this voice profile?")) return;
  await fetch("/api/voices/" + id, { method: "DELETE" });
  refreshVoices($("model").value);
}

// ---------- edit profile ----------
function editProfile(id) {
  const v = voices.find(x => x.id === id);
  if (!v) return;
  if (v.kind === "design") {
    openDesignWizard(v);
  } else {
    openCloneEdit(v);
  }
}

let editingCloneId = null;
function openCloneEdit(v) {
  editingCloneId = v.id;
  document.getElementById("clone-edit-name").value = v.name || "";
  document.getElementById("clone-edit-transcript").value = v.transcript || "";
  setEngines("clone-edit-engines", v.engines || [v.model || "breeze"]);
  document.getElementById("clone-edit-modal").style.display = "flex";
}
document.getElementById("clone-edit-save").onclick = async () => {
  const st = document.getElementById("clone-edit-status");
  const name = document.getElementById("clone-edit-name").value.trim();
  if (!name) { st.textContent = "name required"; return; }
  const transcript = document.getElementById("clone-edit-transcript").value.trim();
  const engines = getEngines("clone-edit-engines");
  st.textContent = "Saving...";
  const fd = new FormData();
  fd.append("name", name);
  fd.append("transcript", transcript);
  fd.append("engines", engines.join(","));
  try {
    const r = await fetch("/api/voices/" + editingCloneId, { method: "PUT", body: fd });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || d.error || JSON.stringify(d));
    st.textContent = "Saved.";
    document.getElementById("clone-edit-modal").style.display = "none";
    refreshVoices($("model").value);
  } catch (e) {
    st.textContent = "Save error: " + e.message;
  }
};
document.getElementById("clone-edit-cancel").onclick = () => { document.getElementById("clone-edit-modal").style.display = "none"; };







function setStatus(id, msg, cls) {
  const el = $(id);
  el.textContent = msg;
  el.className = "status " + (cls || "");
}

// ---------- init ----------
$("model").onchange = onModelChange;
refreshHealth();
refreshModels();
setInterval(refreshHealth, 10000);


// --- STT tab ---
function sttShow(view) {
  document.querySelectorAll("[data-stt-view]").forEach((b) => b.classList.toggle("active", b.dataset.sttView === view));
  document.getElementById("stt-settings").classList.toggle("active", view === "settings");
  document.getElementById("stt-download").classList.toggle("active", view === "download");
}
document.querySelectorAll("[data-stt-view]").forEach((b) => b.addEventListener("click", () => sttShow(b.dataset.sttView)));

async function loadSTTSettings() {
  try {
    const r = await fetch("/api/stt/settings");
    const d = await r.json();
    const set = (id, v) => { const el = document.getElementById(id); if (el && v !== undefined && v !== null) el.value = v; };
    set("stt-model", d.model);
    set("stt-language", d.language || "");
    set("stt-device", d.device);
    updateComputeOptions(d.device, d.compute_type);
    set("stt-beam", d.beam_size);
    const en = document.getElementById("stt-enabled");
    if (en) en.checked = !!d.enabled;
  } catch (e) {}
}

const STT_COMPUTE_BY_DEVICE = {
  cpu: ["float32", "int8"],
  cuda: ["float16", "float32", "int8", "int8_float16"],
};

function updateComputeOptions(device, preferred) {
  const sel = document.getElementById("stt-compute");
  if (!sel) return;
  const allowed = STT_COMPUTE_BY_DEVICE[device] || STT_COMPUTE_BY_DEVICE.cuda;
  sel.innerHTML = "";
  for (const ct of allowed) {
    const o = document.createElement("option");
    o.value = ct; o.textContent = ct;
    sel.appendChild(o);
  }
  if (preferred && allowed.includes(preferred)) sel.value = preferred;
  else sel.value = allowed[0];
}

document.getElementById("stt-device").addEventListener("change", () => {
  updateComputeOptions(document.getElementById("stt-device").value);
});

async function loadSTTModels() {
  try {
    const r = await fetch("/api/stt/models");
    const d = await r.json();
    const models = d.models || [];
    // settings dropdown: installed models only
    const sel = document.getElementById("stt-model");
    const cur = sel.value;
    sel.innerHTML = "";
    for (const m of models) {
      if (!m.installed) continue;
      const opt = document.createElement("option");
      opt.value = m.id; opt.textContent = m.id;
      sel.appendChild(opt);
    }
    if (cur && [...sel.options].some(o => o.value === cur)) sel.value = cur;
    // download list
    const box = document.getElementById("stt-models");
    box.innerHTML = "";
    for (const m of models) {
      const row = document.createElement("div");
      row.className = "profile";
      const info = document.createElement("div");
      info.className = "profile-info";
      const name = document.createElement("strong");
      name.textContent = m.id;
      const repo = document.createElement("span");
      repo.className = "hint";
      repo.textContent = (m.installed ? "installed" : "not installed") + (m.current ? " · current" : "");
      info.appendChild(name); info.appendChild(repo);
      const actions = document.createElement("div");
      actions.className = "profile-actions";
      if (m.installed) {
        const del = document.createElement("button");
        del.className = "danger"; del.textContent = "Delete";
        del.onclick = async () => {
          if (!confirm("Delete model " + m.id + "?")) return;
          await fetch("/api/stt/delete", { method: "POST", body: new URLSearchParams({model: m.id}) });
          loadSTTModels(); loadSTTSettings();
        };
        actions.appendChild(del);
      } else {
        const dl = document.createElement("button");
        dl.className = "secondary"; dl.textContent = "Download";
        dl.onclick = async () => {
          dl.disabled = true; dl.textContent = "Downloading...";
          await fetch("/api/stt/download", { method: "POST", body: new URLSearchParams({model: m.id}) });
        };
        actions.appendChild(dl);
      }
      row.appendChild(info); row.appendChild(actions);
      box.appendChild(row);
    }
  } catch (e) {}
}

document.getElementById("stt-save").onclick = async () => {
  const fd = new FormData();
  fd.append("model", document.getElementById("stt-model").value);
  fd.append("language", document.getElementById("stt-language").value);
  fd.append("device", document.getElementById("stt-device").value);
  fd.append("compute_type", document.getElementById("stt-compute").value);
  fd.append("beam_size", document.getElementById("stt-beam").value);
  fd.append("enabled", document.getElementById("stt-enabled").checked ? "1" : "0");
  const st = document.getElementById("stt-status");
  st.textContent = "Saving...";
  try {
    const r = await fetch("/api/stt/settings", { method: "POST", body: fd });
    st.textContent = r.ok ? "Saved" : "Save failed";
  } catch (e) { st.textContent = "Save failed"; }
};

const STT_LANGS = ["af","Afrikaans","am","Amharic","ar","Arabic","as","Assamese","az","Azerbaijani","ba","Bashkir","be","Belarusian","bg","Bulgarian","bn","Bengali","bo","Tibetan","br","Breton","bs","Bosnian","ca","Catalan","cs","Czech","cy","Welsh","da","Danish","de","German","el","Greek","en","English","es","Spanish","et","Estonian","eu","Basque","fa","Persian","fi","Finnish","fo","Faroese","fr","French","gl","Galician","gu","Gujarati","ha","Hausa","haw","Hawaiian","he","Hebrew","hi","Hindi","hr","Croatian","ht","Haitian Creole","hu","Hungarian","hy","Armenian","id","Indonesian","is","Icelandic","it","Italian","ja","Japanese","jw","Javanese","ka","Georgian","kk","Kazakh","km","Khmer","kn","Kannada","ko","Korean","la","Latin","lb","Luxembourgish","ln","Lingala","lo","Lao","lt","Lithuanian","lv","Latvian","mg","Malagasy","mi","Maori","mk","Macedonian","ml","Malayalam","mn","Mongolian","mr","Marathi","ms","Malay","mt","Maltese","my","Burmese","ne","Nepali","nl","Dutch","nn","Norwegian Nynorsk","no","Norwegian","oc","Occitan","pa","Punjabi","pl","Polish","ps","Pashto","pt","Portuguese","ro","Romanian","ru","Russian","sa","Sanskrit","sd","Sindhi","si","Sinhala","sk","Slovak","sl","Slovenian","sn","Shona","so","Somali","sq","Albanian","sr","Serbian","su","Sundanese","sv","Swedish","sw","Swahili","ta","Tamil","te","Telugu","tg","Tajik","th","Thai","tk","Turkmen","tl","Tagalog","tr","Turkish","tt","Tatar","uk","Ukrainian","ur","Urdu","uz","Uzbek","vi","Vietnamese","yi","Yiddish","yo","Yoruba","zh","Chinese"];

function populateLanguages() {
  const sel = document.getElementById("stt-language");
  if (!sel) return;
  const opt = document.createElement("option");
  opt.value = ""; opt.textContent = "auto-detect";
  sel.appendChild(opt);
  for (let i = 0; i < STT_LANGS.length; i += 2) {
    const o = document.createElement("option");
    o.value = STT_LANGS[i]; o.textContent = STT_LANGS[i + 1];
    sel.appendChild(o);
  }
}

document.getElementById("stt-reset").onclick = async () => {
  const sel = document.getElementById("stt-model");
  if ([...sel.options].some(o => o.value === "large-v3-turbo")) sel.value = "large-v3-turbo";
  document.getElementById("stt-language").value = "";
  document.getElementById("stt-device").value = "cuda";
  document.getElementById("stt-compute").value = "float16";
  document.getElementById("stt-beam").value = 5;
  const fd = new FormData();
  fd.append("model", sel.value);
  fd.append("language", "");
  fd.append("device", "cuda");
  fd.append("compute_type", "float16");
  fd.append("beam_size", "5");
  const st = document.getElementById("stt-status");
  st.textContent = "Saving...";
  try {
    const r = await fetch("/api/stt/settings", { method: "POST", body: fd });
    st.textContent = r.ok ? "Reset to defaults" : "Reset failed";
    loadSTTSettings(); loadSTTModels();
  } catch (e) { st.textContent = "Reset failed"; }
};

async function loadSTTStatus() {
  try {
    const d = await (await fetch("/api/stt/status")).json();
    renderSTTStatus(d);
  } catch (e) {}
}

function renderSTTStatus(d) {
  const st = document.getElementById("stt-model-status");
  if (d.loaded) {
    st.textContent = "loaded (" + d.model + ")";
    st.className = "status ok";
  } else {
    st.textContent = "not loaded";
    st.className = "status";
  }
}

let sttPollTimer = null;
function pollSTTStatus() {
  if (sttPollTimer) clearInterval(sttPollTimer);
  let attempts = 0;
  sttPollTimer = setInterval(async () => {
    try {
      const d = await (await fetch("/api/stt/status")).json();
      renderSTTStatus(d);
      if (d.loaded || ++attempts >= 45) { clearInterval(sttPollTimer); sttPollTimer = null; }
    } catch (e) {
      if (++attempts >= 45) { clearInterval(sttPollTimer); sttPollTimer = null; }
    }
  }, 2000);
}

document.getElementById("stt-load").onclick = async () => {
  try {
    await fetch("/api/stt/load", { method: "POST" });
    pollSTTStatus();
  } catch (e) {}
};

document.getElementById("stt-unload").onclick = async () => {
  try {
    await fetch("/api/stt/unload", { method: "POST" });
    loadSTTStatus();
  } catch (e) {}
};

populateLanguages();
loadSTTModels().then(() => { loadSTTSettings(); loadSTTStatus(); });
setInterval(loadSTTModels, 15000);

// --- TTS side menu ---
function ttsShow(view) {
  document.querySelectorAll("[data-tts-view]").forEach((b) => b.classList.toggle("active", b.dataset.ttsView === view));
  ["models", "test", "cloning", "design", "engines", "general"].forEach((v) => {
    const el = document.getElementById("tts-" + v);
    if (el) el.classList.toggle("active", v === view);
  });
}
document.querySelectorAll("[data-tts-view]").forEach((b) => b.addEventListener("click", () => ttsShow(b.dataset.ttsView)));

// --- engine tag helpers ---
function getEngines(containerId) {
  const box = document.getElementById(containerId);
  if (!box) return [];
  return [...box.querySelectorAll("input[type=checkbox]:checked")].map(x => x.value);
}
function setEngines(containerId, engines) {
  const box = document.getElementById(containerId);
  if (!box) return;
  const list = (engines && engines.length) ? engines : ["breeze"];
  box.querySelectorAll("input[type=checkbox]").forEach(c => { c.checked = list.includes(c.value); });
}

// --- engine settings ---
async function loadEngineSettings() {
  try {
    const [r, vr] = await Promise.all([
      fetch("/api/engines/settings"),
      fetch("/api/voices"),
    ]);
    const d = await r.json();
    const vd = await vr.json();
    const esVoices = vd.voices || [];
    const esClones = esVoices.filter((v) => v.kind === "clone");
    const esDesigns = esVoices.filter((v) => v.kind === "design");
    const box = document.getElementById("engine-settings");
    if (!box) return;
    box.innerHTML = "";
    for (const eng of (d.engines || [])) {
      const card = document.createElement("section");
      card.className = "card";
      const h = document.createElement("h2");
      h.textContent = eng.name;
      card.appendChild(h);
      for (const s of eng.settings) {
        const field = document.createElement("div");
        field.className = "field";
        const label = document.createElement("label");
        label.textContent = s.label;
        const hint = document.createElement("span");
        hint.className = "hint";
        hint.textContent = " - " + s.desc;
        label.appendChild(hint);
        field.appendChild(label);
        let input;
        if (s.type === "bool") {
          input = document.createElement("input");
          input.type = "checkbox";
          input.checked = !!s.value;
        } else if (s.type === "voice") {
          input = document.createElement("select");
          const vnone = document.createElement("option"); vnone.value = ""; vnone.textContent = "(none)"; input.appendChild(vnone);
          for (const v of esClones) { const o = document.createElement("option"); o.value = v.id; o.textContent = v.name; input.appendChild(o); }
          input.value = s.value || "";
        } else if (s.type === "design") {
          input = document.createElement("select");
          const dnone = document.createElement("option"); dnone.value = ""; dnone.textContent = "(none)"; input.appendChild(dnone);
          for (const v of esDesigns) { const o = document.createElement("option"); o.value = v.id; o.textContent = v.name; input.appendChild(o); }
          input.value = s.value || "";
        } else if (s.type === "select") {
          input = document.createElement("select");
          for (const o of (s.options || [])) {
            const opt = document.createElement("option");
            opt.value = o; opt.textContent = o;
            input.appendChild(opt);
          }
          input.value = (s.value !== null && s.value !== undefined) ? s.value : (s.options ? s.options[0] : "");
        } else {
          input = document.createElement("input");
          input.type = (s.type === "int" || s.type === "float") ? "number" : "text";
          if (s.min !== undefined) input.min = s.min;
          if (s.max !== undefined) input.max = s.max;
          if (s.type === "float") input.step = "0.1";
          input.value = (s.value !== null && s.value !== undefined) ? s.value : "";
        }
        input.dataset.engine = eng.name;
        input.dataset.key = s.key;
        input.dataset.type = s.type;
        field.appendChild(input);
        card.appendChild(field);
      }
      box.appendChild(card);
    }
  } catch (e) {}
}

document.getElementById("engines-save").onclick = async () => {
  const updates = {};
  document.querySelectorAll("#engine-settings [data-key]").forEach(el => {
    const eng = el.dataset.engine, key = el.dataset.key, t = el.dataset.type;
    updates[eng] = updates[eng] || {};
    if (el.type === "checkbox") updates[eng][key] = el.checked;
    else if (t === "int") updates[eng][key] = parseInt(el.value, 10);
    else if (t === "float") updates[eng][key] = parseFloat(el.value);
    else updates[eng][key] = el.value;
  });
  const fd = new FormData();
  fd.append("data", JSON.stringify(updates));
  const st = document.getElementById("engines-status");
  st.textContent = "Saving...";
  try {
    const r = await fetch("/api/engines/settings", { method: "POST", body: fd });
    st.textContent = r.ok ? "Saved" : "Save failed";
  } catch (e) { st.textContent = "Save failed"; }
};

loadEngineSettings();

// --- general settings ---
async function loadGeneralSettings() {
  try {
    const r = await fetch("/api/general");
    const d = await r.json();
    const el = document.getElementById("unload-idle");
    if (el) el.value = d.unload_idle_minutes;
    const uo = document.getElementById("unload-others");
    if (uo) uo.checked = !!d.unload_others_before_load;
  } catch (e) {}
}

document.getElementById("general-save").onclick = async () => {
  const fd = new FormData();
  fd.append("unload_idle_minutes", document.getElementById("unload-idle").value);
  fd.append("unload_others_before_load", document.getElementById("unload-others").checked ? "1" : "0");
  const st = document.getElementById("general-status");
  st.textContent = "Saving...";
  try {
    const r = await fetch("/api/general", { method: "POST", body: fd });
    st.textContent = r.ok ? "Saved" : "Save failed";
  } catch (e) { st.textContent = "Save failed"; }
};

loadGeneralSettings();

// --- Avatars tab ---
let editingAvatarId = null;

function avatarShow(view) {
  document.querySelectorAll("[data-avatar-view]").forEach((b) => b.classList.toggle("active", b.dataset.avatarView === view));
  document.getElementById("avatar-defaults").classList.toggle("active", view === "defaults");
  document.getElementById("avatar-list").classList.toggle("active", view === "avatars");
  document.getElementById("avatar-general").classList.toggle("active", view === "general");
}
document.querySelectorAll("[data-avatar-view]").forEach((b) => b.addEventListener("click", () => avatarShow(b.dataset.avatarView)));

async function loadAvatarDefaults() {
  try {
    const r = await fetch("/api/avatar/defaults");
    const d = await r.json();
    document.getElementById("avatar-head-alpha").value = d.head_motion_alpha;
    document.getElementById("avatar-idle-alpha").value = d.idle_motion_alpha;
    document.getElementById("avatar-idle-length").value = d.idle_length;
  } catch (e) {}
}

document.getElementById("avatar-defaults-save").onclick = async () => {
  const fd = new FormData();
  fd.append("head_motion_alpha", document.getElementById("avatar-head-alpha").value);
  fd.append("idle_motion_alpha", document.getElementById("avatar-idle-alpha").value);
  fd.append("idle_length", document.getElementById("avatar-idle-length").value);
  const st = document.getElementById("avatar-defaults-status");
  st.textContent = "Saving...";
  try {
    const r = await fetch("/api/avatar/defaults", { method: "POST", body: fd });
    st.textContent = r.ok ? "Saved" : "Save failed";
  } catch (e) { st.textContent = "Save failed"; }
};

async function loadAvatars() {
  try {
    const r = await fetch("/api/avatars");
    const d = await r.json();
    const grid = document.getElementById("avatar-admin-grid");
    // DITTO builds one idle video at a time. While any build is queued or running, keep the
    // Add avatar button disabled and keep polling until it clears, so uploads cannot stack up.
    const generating = d.generating || [];
    const addBtn = document.getElementById("avatar-add");
    if (addBtn) addBtn.disabled = generating.length > 0;
    const listStatus = document.getElementById("avatar-list-status");
    if (listStatus) listStatus.textContent = generating.length
      ? ("generating idle video... (" + generating.length + " in progress)")
      : "";
    clearTimeout(loadAvatars._poll);
    if (generating.length) loadAvatars._poll = setTimeout(loadAvatars, 4000);
    grid.innerHTML = "";
    for (const a of (d.avatars || [])) {
      const card = document.createElement("div");
      card.className = "avatar-thumb";
      const img = document.createElement("img");
      img.src = "/static/avatars/" + a.image + (a.v ? ("?v=" + a.v) : "");
      img.alt = "Avatar " + a.id;
      img.title = a.idle_ready ? "Click to preview idle" : "Idle video not ready";
      img.onclick = () => { if (a.idle_ready) previewIdle(a.id); };
      const name = document.createElement("span");
      name.className = "avatar-name";
      name.textContent = (a.name || ("Avatar " + a.id)) + (a.idle_ready ? "" : " (no idle)");
      const actions = document.createElement("div");
      actions.className = "profile-actions";
      const preview = document.createElement("button");
      preview.className = "secondary"; preview.textContent = "Preview idle";
      preview.disabled = !a.idle_ready;
      preview.onclick = () => previewIdle(a.id);
      const regen = document.createElement("button");
      // an idle build is already queued or running - don't let another one stack up
      regen.disabled = generating.length > 0;
      if (generating.length) regen.title = "Waiting for the current idle build to finish";
      regen.className = "secondary"; regen.textContent = "Regenerate";
      regen.onclick = async () => { regen.disabled = true; regen.textContent = "Regenerating..."; await fetch("/api/avatars/" + a.id + "/regen", { method: "POST" }); setTimeout(loadAvatars, 2000); };
      const edit = document.createElement("button");
      edit.className = "secondary"; edit.textContent = "Edit";
      edit.onclick = () => openAvatarEdit(a);
      const del = document.createElement("button");
      del.className = "danger"; del.textContent = "Delete";
      del.onclick = async () => {
        if (!confirm("Delete avatar " + a.id + "?")) return;
        await fetch("/api/avatars/remove", { method: "POST", body: new URLSearchParams({id: a.id}) });
        loadAvatars();
      };
      actions.appendChild(preview); actions.appendChild(regen); actions.appendChild(edit); actions.appendChild(del);
      card.appendChild(img); card.appendChild(name); card.appendChild(actions);
      grid.appendChild(card);
    }
  } catch (e) {}
}

function openAvatarEdit(a) {
  editingAvatarId = a.id;
  document.getElementById("avatar-modal-title").textContent = "Edit avatar " + a.id;
  document.getElementById("avatar-modal-name").value = a.name || "";
  document.getElementById("avatar-file").value = "";
  document.getElementById("avatar-file-hint").textContent = "leave empty to keep current image";
  document.getElementById("avatar-modal-head-alpha").value = a.overrides ? a.head_motion_alpha : "";
  document.getElementById("avatar-modal-idle-alpha").value = a.overrides ? a.idle_motion_alpha : "";
  document.getElementById("avatar-modal-idle-length").value = a.overrides ? a.idle_length : "";
  document.getElementById("avatar-modal").style.display = "flex";
}

document.getElementById("avatar-add").onclick = () => {
  editingAvatarId = null;
  document.getElementById("avatar-modal-title").textContent = "Add avatar";
  document.getElementById("avatar-modal-name").value = "";
  document.getElementById("avatar-file").value = "";
  document.getElementById("avatar-file-hint").textContent = "required - square image works best";
  document.getElementById("avatar-modal-head-alpha").value = "";
  document.getElementById("avatar-modal-idle-alpha").value = "";
  document.getElementById("avatar-modal-idle-length").value = "";
  document.getElementById("avatar-modal").style.display = "flex";
};

document.getElementById("avatar-modal-cancel").onclick = () => { document.getElementById("avatar-modal").style.display = "none"; };

document.getElementById("avatar-modal-save").onclick = async () => {
  const st = document.getElementById("avatar-modal-status");
  if (editingAvatarId) {
    const fd = new FormData();
    fd.append("name", document.getElementById("avatar-modal-name").value);
    fd.append("head_motion_alpha", document.getElementById("avatar-modal-head-alpha").value);
    fd.append("idle_motion_alpha", document.getElementById("avatar-modal-idle-alpha").value);
    fd.append("idle_length", document.getElementById("avatar-modal-idle-length").value);
    const file = document.getElementById("avatar-file").files[0];
    if (file) fd.append("image", file);
    st.textContent = "Saving...";
    try {
      const r = await fetch("/api/avatars/" + editingAvatarId, { method: "PUT", body: fd });
      if (!r.ok) throw new Error("failed");
      document.getElementById("avatar-modal").style.display = "none";
      loadAvatars();
    } catch (e) { st.textContent = "Save failed"; }
    return;
  }
  const file = document.getElementById("avatar-file").files[0];
  if (!file) { st.textContent = "choose an image"; return; }
  const fd = new FormData();
  fd.append("image", file);
  fd.append("name", document.getElementById("avatar-modal-name").value);
  fd.append("head_motion_alpha", document.getElementById("avatar-modal-head-alpha").value);
  fd.append("idle_motion_alpha", document.getElementById("avatar-modal-idle-alpha").value);
  fd.append("idle_length", document.getElementById("avatar-modal-idle-length").value);
  st.textContent = "Adding... (idle video generates in background)";
  try {
    const r = await fetch("/api/avatars/add", { method: "POST", body: fd });
    if (!r.ok) throw new Error("failed");
    document.getElementById("avatar-modal").style.display = "none";
    loadAvatars();
  } catch (e) { st.textContent = "Add failed"; }
};

loadAvatarDefaults();
loadAvatars();

function previewIdle(id) {
  document.getElementById("preview-video").src = "/static/avatars/" + id + ".mp4";
  document.getElementById("preview-modal").style.display = "flex";
}
document.getElementById("preview-close").onclick = () => {
  document.getElementById("preview-modal").style.display = "none";
  document.getElementById("preview-video").src = "";
};

// --- Personalities tab ---
let editingPersId = null;
let persDefaultPrompt = "";

function persShow(view) {
  document.querySelectorAll("[data-pers-view]").forEach((b) => b.classList.toggle("active", b.dataset.persView === view));
  document.getElementById("pers-defaults").classList.toggle("active", view === "defaults");
  document.getElementById("pers-list").classList.toggle("active", view === "personalities");
}
document.querySelectorAll("[data-pers-view]").forEach((b) => b.addEventListener("click", () => persShow(b.dataset.persView)));

async function loadPersDefault() {
  try {
    const r = await fetch("/api/personality/default");
    const d = await r.json();
    persDefaultPrompt = d.prompt || "";
    document.getElementById("pers-default-prompt").value = persDefaultPrompt;
  } catch (e) {}
}

document.getElementById("pers-default-save").onclick = async () => {
  const st = document.getElementById("pers-default-status");
  const fd = new FormData();
  fd.append("prompt", document.getElementById("pers-default-prompt").value);
  st.textContent = "Saving...";
  try {
    const r = await fetch("/api/personality/default", { method: "POST", body: fd });
    st.textContent = r.ok ? "Saved" : "Save failed";
    if (r.ok) persDefaultPrompt = document.getElementById("pers-default-prompt").value;
  } catch (e) { st.textContent = "Save failed"; }
};

async function loadPersonalities() {
  try {
    const r = await fetch("/api/personalities");
    const d = await r.json();
    const grid = document.getElementById("pers-grid");
    grid.innerHTML = "";
    const list = (d.personalities || []).filter((p) => p.id !== "default");
    if (!list.length) {
      grid.innerHTML = '<div class="side-empty">No personalities yet. Add one to get started.</div>';
      return;
    }
    for (const p of list) {
      const row = document.createElement("div");
      row.className = "profile";
      const info = document.createElement("div");
      info.className = "profile-info";
      const name = document.createElement("strong");
      name.textContent = p.name;
      const snippet = document.createElement("span");
      snippet.className = "prompt-snippet";
      snippet.textContent = (p.prompt || "").replace(/\s+/g, " ").trim();
      info.appendChild(name); info.appendChild(snippet);
      const actions = document.createElement("div");
      actions.className = "profile-actions";
      const edit = document.createElement("button");
      edit.className = "secondary"; edit.textContent = "Edit";
      edit.onclick = () => openPersEdit(p);
      const del = document.createElement("button");
      del.className = "danger"; del.textContent = "Delete";
      del.onclick = async () => {
        if (!confirm("Delete personality \"" + p.name + "\"?")) return;
        await fetch("/api/personalities/remove", { method: "POST", body: new URLSearchParams({id: p.id}) });
        loadPersonalities();
      };
      actions.appendChild(edit); actions.appendChild(del);
      row.appendChild(info); row.appendChild(actions);
      grid.appendChild(row);
    }
  } catch (e) {}
}

function openPersEdit(p) {
  editingPersId = p.id;
  document.getElementById("pers-modal-title").textContent = "Edit personality";
  document.getElementById("pers-modal-name").value = p.name || "";
  document.getElementById("pers-modal-prompt").value = p.prompt || "";
  document.getElementById("pers-modal").style.display = "flex";
}

document.getElementById("pers-add").onclick = async () => {
  editingPersId = null;
  document.getElementById("pers-modal-title").textContent = "Add personality";
  document.getElementById("pers-modal-name").value = "";
  if (!persDefaultPrompt) {
    try { const d = await (await fetch("/api/personality/default")).json(); persDefaultPrompt = d.prompt || ""; } catch (e) {}
  }
  document.getElementById("pers-modal-prompt").value = persDefaultPrompt;
  document.getElementById("pers-modal").style.display = "flex";
};

document.getElementById("pers-modal-cancel").onclick = () => { document.getElementById("pers-modal").style.display = "none"; };

document.getElementById("pers-modal-save").onclick = async () => {
  const st = document.getElementById("pers-modal-status");
  const name = document.getElementById("pers-modal-name").value;
  const prompt = document.getElementById("pers-modal-prompt").value;
  st.textContent = "Saving...";
  const fd = new FormData();
  fd.append("name", name);
  fd.append("prompt", prompt);
  try {
    let r;
    if (editingPersId) {
      r = await fetch("/api/personalities/" + encodeURIComponent(editingPersId), { method: "PUT", body: fd });
    } else {
      r = await fetch("/api/personalities/add", { method: "POST", body: fd });
    }
    if (!r.ok) {
      let msg = "Save failed";
      try { msg = (await r.json()).detail || msg; } catch (e) {}
      st.textContent = msg;
      return;
    }
    document.getElementById("pers-modal").style.display = "none";
    st.textContent = "";
    loadPersonalities();
  } catch (e) { st.textContent = "Save failed"; }
};

loadPersDefault();
loadPersonalities();

// --- LLMs tab ---
let editingLlmName = null;
let llmDefaults = null;

function llmShow(view) {
  document.querySelectorAll("[data-llm-view]").forEach((b) => b.classList.toggle("active", b.dataset.llmView === view));
  document.getElementById("llm-defaults").classList.toggle("active", view === "defaults");
  document.getElementById("llm-models").classList.toggle("active", view === "models");
  document.getElementById("llm-add").classList.toggle("active", view === "add");
}
document.querySelectorAll("[data-llm-view]").forEach((b) => b.addEventListener("click", () => llmShow(b.dataset.llmView)));

async function loadLlmDefaults() {
  try {
    const r = await fetch("/api/llm/defaults");
    const d = await r.json();
    llmDefaults = d;
    document.getElementById("llm-context").value = d.context_tokens;
    document.getElementById("llm-history").value = d.max_history_tokens;
    document.getElementById("llm-temp").value = d.temperature;
    document.getElementById("llm-maxtokens").value = d.max_tokens;
    document.getElementById("llm-thinking").checked = !!d.thinking;
    document.getElementById("llm-add-context").value = d.context_tokens;
    document.getElementById("llm-add-history").value = d.max_history_tokens;
    document.getElementById("llm-add-temp").value = d.temperature;
    document.getElementById("llm-add-maxtokens").value = d.max_tokens;
    document.getElementById("llm-add-thinking").checked = !!d.thinking;
  } catch (e) {}
}

document.getElementById("llm-defaults-save").onclick = async () => {
  const st = document.getElementById("llm-defaults-status");
  const fd = new FormData();
  fd.append("context_tokens", document.getElementById("llm-context").value);
  fd.append("max_history_tokens", document.getElementById("llm-history").value);
  fd.append("temperature", document.getElementById("llm-temp").value);
  fd.append("max_tokens", document.getElementById("llm-maxtokens").value);
  fd.append("thinking", document.getElementById("llm-thinking").checked ? "1" : "0");
  st.textContent = "Saving...";
  try {
    const r = await fetch("/api/llm/defaults", { method: "POST", body: fd });
    st.textContent = r.ok ? "Saved" : "Save failed";
    if (r.ok) loadLlmDefaults();
  } catch (e) { st.textContent = "Save failed"; }
};

async function loadLlmModels() {
  try {
    const r = await fetch("/api/llm/models");
    const d = await r.json();
    const list = document.getElementById("llm-models-list");
    list.innerHTML = "";
    const models = d.models || [];
    if (!models.length) {
      list.innerHTML = '<div class="side-empty">No models yet. Add one in the "Add model" view.</div>';
      return;
    }
    for (const m of models) {
      const row = document.createElement("div");
      row.className = "profile";
      const info = document.createElement("div");
      info.className = "profile-info";
      const name = document.createElement("strong");
      name.textContent = m.name;
      const detail = document.createElement("span");
      detail.className = "prompt-snippet";
      detail.textContent = m.model + "  |  " + m.base_url + "  |  ctx " + m.context_tokens + "  |  hist " + m.max_history_tokens + "  |  temp " + m.temperature + "  |  max " + m.max_tokens + "  |  think " + (m.thinking ? "on" : "off");
      info.appendChild(name); info.appendChild(detail);
      const actions = document.createElement("div");
      actions.className = "profile-actions";
      const edit = document.createElement("button");
      edit.className = "secondary"; edit.textContent = "Edit";
      edit.onclick = () => openLlmEdit(m);
      const del = document.createElement("button");
      del.className = "danger"; del.textContent = "Delete";
      del.onclick = async () => {
        if (!confirm("Delete model \"" + m.name + "\"?")) return;
        await fetch("/api/llm/models/remove", { method: "POST", body: new URLSearchParams({id: m.name}) });
        loadLlmModels();
      };
      actions.appendChild(edit); actions.appendChild(del);
      row.appendChild(info); row.appendChild(actions);
      list.appendChild(row);
    }
  } catch (e) {}
}

function openLlmEdit(m) {
  editingLlmName = m.name;
  document.getElementById("llm-modal-title").textContent = "Edit model \"" + m.name + "\"";
  document.getElementById("llm-modal-name").value = m.name;
  document.getElementById("llm-modal-url").value = m.base_url;
  document.getElementById("llm-modal-model").value = m.model;
  document.getElementById("llm-modal-context").value = m.context_tokens;
  document.getElementById("llm-modal-history").value = m.max_history_tokens;
  document.getElementById("llm-modal-temp").value = m.temperature;
  document.getElementById("llm-modal-maxtokens").value = m.max_tokens;
  document.getElementById("llm-modal-thinking").checked = !!m.thinking;
  document.getElementById("llm-modal-token").value = m.api_token || "";
  document.getElementById("llm-modal").style.display = "flex";
}

document.getElementById("llm-modal-cancel").onclick = () => { document.getElementById("llm-modal").style.display = "none"; };

document.getElementById("llm-modal-save").onclick = async () => {
  const st = document.getElementById("llm-modal-status");
  const fd = new FormData();
  fd.append("new_name", document.getElementById("llm-modal-name").value);
  fd.append("base_url", document.getElementById("llm-modal-url").value);
  fd.append("model", document.getElementById("llm-modal-model").value);
  fd.append("context_tokens", document.getElementById("llm-modal-context").value);
  fd.append("max_history_tokens", document.getElementById("llm-modal-history").value);
  fd.append("temperature", document.getElementById("llm-modal-temp").value);
  fd.append("max_tokens", document.getElementById("llm-modal-maxtokens").value);
  fd.append("thinking", document.getElementById("llm-modal-thinking").checked ? "1" : "0");
  fd.append("api_token", document.getElementById("llm-modal-token").value);
  st.textContent = "Saving...";
  try {
    const r = await fetch("/api/llm/models/" + encodeURIComponent(editingLlmName), { method: "PUT", body: fd });
    if (!r.ok) {
      let msg = "Save failed";
      try { msg = (await r.json()).detail || msg; } catch (e) {}
      st.textContent = msg;
      return;
    }
    document.getElementById("llm-modal").style.display = "none";
    loadLlmModels();
  } catch (e) { st.textContent = "Save failed"; }
};

document.getElementById("llm-fetch").onclick = async () => {
  const st = document.getElementById("llm-fetch-status");
  const url = document.getElementById("llm-add-url").value.trim();
  if (!url) { st.textContent = "enter a URL"; return; }
  st.textContent = "Fetching...";
  try {
    const r = await fetch("/api/llm/fetch-models", { method: "POST", body: new URLSearchParams({base_url: url}) });
    const d = await r.json();
    if (!r.ok) { st.textContent = d.detail || "fetch failed"; return; }
    const sel = document.getElementById("llm-add-model-select");
    sel.innerHTML = "";
    const models = d.models || [];
    if (!models.length) { st.textContent = "no models returned"; return; }
    for (const m of models) {
      const opt = document.createElement("option");
      opt.value = m; opt.textContent = m;
      sel.appendChild(opt);
    }
    st.textContent = models.length + " models found";
  } catch (e) { st.textContent = "fetch failed"; }
};

document.getElementById("llm-add-save").onclick = async () => {
  const st = document.getElementById("llm-add-status");
  const name = document.getElementById("llm-add-name").value.trim();
  const url = document.getElementById("llm-add-url").value.trim();
  const model = document.getElementById("llm-add-model-select").value;
  if (!name) { st.textContent = "name required"; return; }
  if (!url) { st.textContent = "base URL required"; return; }
  if (!model) { st.textContent = "select a model"; return; }
  const fd = new FormData();
  fd.append("name", name);
  fd.append("base_url", url);
  fd.append("model", model);
  fd.append("context_tokens", document.getElementById("llm-add-context").value);
  fd.append("max_history_tokens", document.getElementById("llm-add-history").value);
  fd.append("temperature", document.getElementById("llm-add-temp").value);
  fd.append("max_tokens", document.getElementById("llm-add-maxtokens").value);
  fd.append("thinking", document.getElementById("llm-add-thinking").checked ? "1" : "0");
  fd.append("api_token", document.getElementById("llm-add-token").value);
  st.textContent = "Saving...";
  try {
    const r = await fetch("/api/llm/models/add", { method: "POST", body: fd });
    if (!r.ok) {
      let msg = "Save failed";
      try { msg = (await r.json()).detail || msg; } catch (e) {}
      st.textContent = msg;
      return;
    }
    st.textContent = "Saved";
    document.getElementById("llm-add-name").value = "";
    loadLlmModels();
  } catch (e) { st.textContent = "Save failed"; }
};

loadLlmDefaults();
loadLlmModels();

// --- General tab (memory / server / auth / toolcalling) ---
function genShow(view) {
  document.querySelectorAll("[data-gen-view]").forEach((b) => b.classList.toggle("active", b.dataset.genView === view));
  ["memory", "server", "auth", "toolcalling", "skills"].forEach((v) => {
    document.getElementById("gen-" + v).classList.toggle("active", view === v);
  });
}
document.querySelectorAll("[data-gen-view]").forEach((b) => b.addEventListener("click", () => genShow(b.dataset.genView)));

let editingSkillName = null;

async function loadSkills() {
  try {
    const d = await (await fetch("/api/skills")).json();
    renderSkillGrid(d.skills || []);
  } catch (e) {}
}

function renderSkillGrid(list) {
  const grid = document.getElementById("skill-grid");
  if (!grid) return;
  grid.innerHTML = "";
  if (!list.length) {
    grid.innerHTML = '<div class="side-empty">No skills yet. Add one to get started.</div>';
    return;
  }
  for (const s of list) {
    const row = document.createElement("div");
    row.className = "profile";
    const info = document.createElement("div");
    info.className = "profile-info";
    const name = document.createElement("strong");
    name.textContent = s.name;
    const title = document.createElement("span");
    title.className = "prompt-snippet";
    title.textContent = s.title || "";
    info.appendChild(name); info.appendChild(title);
    const actions = document.createElement("div");
    actions.className = "profile-actions";
    const edit = document.createElement("button");
    edit.className = "secondary"; edit.textContent = "Edit";
    edit.onclick = () => openSkillEdit(s.name);
    const del = document.createElement("button");
    del.className = "danger"; del.textContent = "Delete";
    del.onclick = async () => {
      if (!confirm('Delete skill "' + s.name + '"?')) return;
      await fetch("/api/skills/" + encodeURIComponent(s.name), { method: "DELETE" });
      loadSkills();
    };
    actions.appendChild(edit); actions.appendChild(del);
    row.appendChild(info); row.appendChild(actions);
    grid.appendChild(row);
  }
}

function openSkillAdd() {
  editingSkillName = null;
  document.getElementById("skill-modal-title").textContent = "Add skill";
  document.getElementById("skill-name").value = "";
  document.getElementById("skill-name").disabled = false;
  document.getElementById("skill-content").value = "";
  document.getElementById("skill-modal").style.display = "flex";
}

async function openSkillEdit(name) {
  editingSkillName = name;
  document.getElementById("skill-modal-title").textContent = "Edit skill";
  document.getElementById("skill-name").value = name;
  document.getElementById("skill-name").disabled = true;
  document.getElementById("skill-content").value = "";
  try {
    const d = await (await fetch("/api/skills/" + encodeURIComponent(name))).json();
    document.getElementById("skill-content").value = d.content || "";
  } catch (e) {}
  document.getElementById("skill-modal").style.display = "flex";
}

document.getElementById("skill-add").onclick = openSkillAdd;
document.getElementById("skill-cancel").onclick = () => { document.getElementById("skill-modal").style.display = "none"; };
document.getElementById("skill-save").onclick = async () => {
  const st = document.getElementById("skill-modal-status");
  const name = document.getElementById("skill-name").value.trim().toLowerCase();
  const content = document.getElementById("skill-content").value;
  if (!name) { st.textContent = "name required"; return; }
  if (!content.trim()) { st.textContent = "content required"; return; }
  st.textContent = "Saving...";
  const fd = new FormData();
  fd.append("content", content);
  try {
    let r;
    if (editingSkillName) {
      r = await fetch("/api/skills/" + encodeURIComponent(editingSkillName), { method: "PUT", body: fd });
    } else {
      fd.append("name", name);
      r = await fetch("/api/skills", { method: "POST", body: fd });
    }
    if (!r.ok) { let m = "Save failed"; try { m = (await r.json()).detail || m; } catch (e) {} st.textContent = m; return; }
    document.getElementById("skill-modal").style.display = "none";
    loadSkills();
  } catch (e) { st.textContent = "Save failed"; }
};

loadSkills();

async function loadMemorySettings() {
  try {
    const d = await (await fetch("/api/memory/settings")).json();
    document.getElementById("mem-brain-dir").value = d.brain_dir;
    document.getElementById("mem-db-path").value = d.db_path;
    document.getElementById("mem-embed-model").value = d.embedding_model;
    document.getElementById("mem-embed-device").value = d.embedding_device;
    document.getElementById("mem-top-k").value = d.top_k;
    document.getElementById("mem-min-score").value = d.min_score;
    document.getElementById("mem-max-chars").value = d.max_chars;
    document.getElementById("mem-body-chars").value = d.body_chars;
    document.getElementById("mem-curate-every").value = d.curate_every;
    document.getElementById("mem-history-max").value = d.history_max_turns;
  } catch (e) {}
}

document.getElementById("mem-save").onclick = async () => {
  const st = document.getElementById("mem-status");
  const fd = new FormData();
  fd.append("brain_dir", document.getElementById("mem-brain-dir").value);
  fd.append("db_path", document.getElementById("mem-db-path").value);
  fd.append("embedding_model", document.getElementById("mem-embed-model").value);
  fd.append("embedding_device", document.getElementById("mem-embed-device").value);
  fd.append("top_k", document.getElementById("mem-top-k").value);
  fd.append("min_score", document.getElementById("mem-min-score").value);
  fd.append("max_chars", document.getElementById("mem-max-chars").value);
  fd.append("body_chars", document.getElementById("mem-body-chars").value);
  fd.append("curate_every", document.getElementById("mem-curate-every").value);
  fd.append("history_max_turns", document.getElementById("mem-history-max").value);
  st.textContent = "Saving...";
  try {
    const r = await fetch("/api/memory/settings", { method: "POST", body: fd });
    st.textContent = r.ok ? "Saved" : "Save failed";
  } catch (e) { st.textContent = "Save failed"; }
};

async function loadServerSettings() {
  try {
    const d = await (await fetch("/api/server/settings")).json();
    document.getElementById("srv-host").value = d.host;
    document.getElementById("srv-port").value = d.port;
    document.getElementById("srv-https").checked = !!d.https;
    document.getElementById("srv-token").value = d.api_token;
    document.getElementById("srv-cert-status").textContent = d.ssl_ready ? "cert present" : "no cert yet";
  } catch (e) {}
}

document.getElementById("srv-save").onclick = async () => {
  const st = document.getElementById("srv-status");
  const fd = new FormData();
  fd.append("host", document.getElementById("srv-host").value);
  fd.append("port", document.getElementById("srv-port").value);
  fd.append("https", document.getElementById("srv-https").checked ? "1" : "0");
  st.textContent = "Saving...";
  try {
    const r = await fetch("/api/server/settings", { method: "POST", body: fd });
    st.textContent = r.ok ? "Saved (restart to apply)" : "Save failed";
  } catch (e) { st.textContent = "Save failed"; }
};

document.getElementById("srv-token-gen").onclick = async () => {
  try {
    const d = await (await fetch("/api/server/generate-token", { method: "POST" })).json();
    document.getElementById("srv-token").value = d.api_token;
  } catch (e) {}
};

document.getElementById("srv-cert").onclick = async () => {
  const st = document.getElementById("srv-cert-status");
  st.textContent = "Generating...";
  try {
    const r = await fetch("/api/server/generate-cert", { method: "POST" });
    const d = await r.json();
    st.textContent = r.ok ? "cert generated (restart to apply)" : (d.detail || "failed");
  } catch (e) { st.textContent = "failed"; }
};

async function loadAuthSettings() {
  try {
    const d = await (await fetch("/api/auth/settings")).json();
    document.getElementById("auth-username").value = d.username;
    document.getElementById("auth-password").value = d.password;
    document.getElementById("auth-hf-token").value = d.hf_token;
  } catch (e) {}
}

document.getElementById("auth-save").onclick = async () => {
  const st = document.getElementById("auth-status");
  const fd = new FormData();
  fd.append("username", document.getElementById("auth-username").value);
  fd.append("password", document.getElementById("auth-password").value);
  fd.append("hf_token", document.getElementById("auth-hf-token").value);
  st.textContent = "Saving...";
  try {
    const r = await fetch("/api/auth/settings", { method: "POST", body: fd });
    st.textContent = r.ok ? "Saved" : "Save failed";
  } catch (e) { st.textContent = "Save failed"; }
};

document.getElementById("auth-pass-gen").onclick = async () => {
  try {
    const d = await (await fetch("/api/auth/generate-password", { method: "POST" })).json();
    document.getElementById("auth-password").value = d.password;
  } catch (e) {}
};

async function loadToolcalling() {
  try {
    const d = await (await fetch("/api/toolcalling")).json();
    document.getElementById("tc-enabled").checked = !!d.enabled;
  } catch (e) {}
}

document.getElementById("tc-save").onclick = async () => {
  const st = document.getElementById("tc-status");
  const fd = new FormData();
  fd.append("enabled", document.getElementById("tc-enabled").checked ? "1" : "0");
  st.textContent = "Saving...";
  try {
    const r = await fetch("/api/toolcalling", { method: "POST", body: fd });
    st.textContent = r.ok ? "Saved" : "Save failed";
  } catch (e) { st.textContent = "Save failed"; }
};

loadMemorySettings();
loadServerSettings();
loadAuthSettings();
loadToolcalling();

// --- TTS general ---
async function loadTtsGeneral() {
  try {
    const d = await (await fetch("/api/tts/general")).json();
    document.getElementById("ttsg-min-chunk").value = d.min_chunk_chars;
    document.getElementById("ttsg-max-chunk").value = d.max_chunk_chars;
    document.getElementById("ttsg-mode").value = d.default_mode;
    document.getElementById("ttsg-format").value = d.output_format;
    document.getElementById("ttsg-gain").value = d.gain_db;
    document.getElementById("ttsg-sample-rate").value = d.sample_rate;
    document.getElementById("ttsg-audio-ttl").value = d.audio_ttl_hours;
    document.getElementById("ttsg-enabled").checked = !!d.enabled;
    document.getElementById("ttsg-engine").value = d.default_model;
    document.getElementById("ttsg-instruction").value = d.default_instruction;
    document.getElementById("ttsg-cfg-scale").value = d.default_cfg_scale;
    // populate voice design + clone dropdowns
    const vd = await (await fetch("/api/voices")).json();
    const all = vd.voices || [];
    const clones = all.filter((v) => v.kind === "clone");
    const designs = all.filter((v) => v.kind === "design");
    const voiceSel = document.getElementById("ttsg-voice");
    const designSel = document.getElementById("ttsg-design");
    voiceSel.innerHTML = "";
    designSel.innerHTML = "";
    const noneV = document.createElement("option"); noneV.value = ""; noneV.textContent = "(none)"; voiceSel.appendChild(noneV);
    for (const v of clones) { const o = document.createElement("option"); o.value = v.id; o.textContent = v.name; voiceSel.appendChild(o); }
    const noneD = document.createElement("option"); noneD.value = ""; noneD.textContent = "(none)"; designSel.appendChild(noneD);
    for (const v of designs) { const o = document.createElement("option"); o.value = v.id; o.textContent = v.name; designSel.appendChild(o); }
    voiceSel.value = d.default_voice_id || "";
    designSel.value = d.default_instruction_id || "";
  } catch (e) {}
}

document.getElementById("ttsg-save").onclick = async () => {
  const st = document.getElementById("ttsg-status");
  const fd = new FormData();
  fd.append("min_chunk_chars", document.getElementById("ttsg-min-chunk").value);
  fd.append("max_chunk_chars", document.getElementById("ttsg-max-chunk").value);
  fd.append("default_mode", document.getElementById("ttsg-mode").value);
  fd.append("output_format", document.getElementById("ttsg-format").value);
  fd.append("gain_db", document.getElementById("ttsg-gain").value);
  fd.append("sample_rate", document.getElementById("ttsg-sample-rate").value);
  fd.append("audio_ttl_hours", document.getElementById("ttsg-audio-ttl").value);
  fd.append("enabled", document.getElementById("ttsg-enabled").checked ? "1" : "0");
  fd.append("default_model", document.getElementById("ttsg-engine").value);
  fd.append("default_voice_id", document.getElementById("ttsg-voice").value);
  fd.append("default_instruction_id", document.getElementById("ttsg-design").value);
  fd.append("default_instruction", document.getElementById("ttsg-instruction").value);
  fd.append("default_cfg_scale", document.getElementById("ttsg-cfg-scale").value);
  st.textContent = "Saving...";
  try {
    const r = await fetch("/api/tts/general", { method: "POST", body: fd });
    st.textContent = r.ok ? "Saved" : "Save failed";
  } catch (e) { st.textContent = "Save failed"; }
};

// --- Avatar general ---
async function loadAvatarGeneral() {
  try {
    const d = await (await fetch("/api/avatar/general")).json();
    document.getElementById("avg-video-ttl").value = d.video_ttl_hours;
  } catch (e) {}
}

document.getElementById("avg-save").onclick = async () => {
  const st = document.getElementById("avg-status");
  const fd = new FormData();
  fd.append("video_ttl_hours", document.getElementById("avg-video-ttl").value);
  st.textContent = "Saving...";
  try {
    const r = await fetch("/api/avatar/general", { method: "POST", body: fd });
    st.textContent = r.ok ? "Saved" : "Save failed";
  } catch (e) { st.textContent = "Save failed"; }
};

loadTtsGeneral();
loadAvatarGeneral();

// --- Status tab ---
function statusShow(view) {
  document.querySelectorAll("[data-status-view]").forEach((b) => b.classList.toggle("active", b.dataset.statusView === view));
  document.getElementById("status-health").classList.toggle("active", view === "health");
  document.getElementById("status-logs").classList.toggle("active", view === "logs");
}
document.querySelectorAll("[data-status-view]").forEach((b) => b.addEventListener("click", () => statusShow(b.dataset.statusView)));

async function loadStatus() {
  try {
    const d = await (await fetch("/api/status")).json();
    renderStatus(d);
  } catch (e) {
    document.getElementById("status-cards").innerHTML = '<div class="side-empty">Failed to load status</div>';
  }
}

function renderStatus(d) {
  const gpu = d.gpu || {};
  const gpuText = gpu.used_gb != null
    ? "GPU: " + gpu.used_gb + " GB / " + gpu.total_gb + " GB used · models ~" + gpu.models_used_gb + " GB · system ~" + gpu.system_used_gb + " GB"
    : "GPU: nvidia-smi unavailable";
  document.getElementById("status-gpu").textContent = gpuText;

  const cards = [];

  const stt = d.stt || {};
  const sttBadge = stt.loaded ? '<span class="badge ok">LOADED</span>' : (stt.available ? '<span class="badge off">UNLOADED</span>' : '<span class="badge err">MISSING</span>');
  cards.push('<section class="card">' +
    '<div class="status-row"><strong>Speech to Text (Whisper)</strong> ' + sttBadge + '</div>' +
    '<div class="status-row"><span class="status">model: ' + (stt.model || "?") + ' · installed: ' + (stt.available ? "yes" : "no") + ' · enabled: ' + (stt.enabled ? "yes" : "no") + ' · vram ~' + stt.vram_est_gb + ' GB</span></div>' +
    (!stt.available ? '<div class="status-row"><button class="danger stt-dl" data-model="' + stt.model + '">Download model</button> <a class="download" href="' + (stt.repo || "#") + '" target="_blank" rel="noopener">HuggingFace page</a></div>' : '') +
  '</section>');

  for (const t of (d.tts || [])) {
    let b, state;
    if (t.loaded) { b = "ok"; state = "LOADED"; }
    else if (!t.available) { b = "err"; state = "UNAVAILABLE"; }
    else if (!t.enabled) { b = "off"; state = "DISABLED"; }
    else { b = "off"; state = "UNLOADED"; }
    cards.push('<section class="card">' +
      '<div class="status-row"><strong>TTS · ' + t.name + '</strong> <span class="badge ' + b + '">' + state + '</span></div>' +
      '<div class="status-row"><span class="status">installed: ' + (t.available ? "yes" : "no") + ' · vram ~' + t.vram_est_gb + ' GB</span></div>' +
      (t.load_error ? '<div class="status-row"><span class="status err">error: ' + t.load_error + '</span></div>' : '') +
    '</section>');
  }

  const di = d.ditto || {};
  cards.push('<section class="card">' +
    '<div class="status-row"><strong>DITTO (avatar video)</strong> ' + (di.running ? '<span class="badge ok">RUNNING</span>' : '<span class="badge off">STOPPED</span>') + '</div>' +
    '<div class="status-row"><span class="status">enabled: ' + (di.enabled ? "yes" : "no") + ' · vram ~' + di.vram_est_gb + ' GB</span></div>' +
  '</section>');

  const emb = d.embedding || {};
  const embBadge = emb.loaded ? '<span class="badge ok">LOADED</span>' : (emb.installed ? '<span class="badge off">INSTALLED</span>' : '<span class="badge err">MISSING</span>');
  cards.push('<section class="card">' +
    '<div class="status-row"><strong>Embedding (memory)</strong> ' + embBadge + '</div>' +
    '<div class="status-row"><span class="status">model: ' + (emb.model || "?") + ' · device: ' + (emb.device || "cpu") + ' · installed: ' + (emb.installed ? "yes" : "no") + ' · size ~' + emb.vram_est_gb + ' GB</span></div>' +
    (!emb.installed ? '<div class="status-row"><button class="danger emb-dl">Download model</button> <a class="download" href="' + (emb.repo || "#") + '" target="_blank" rel="noopener">HuggingFace page</a></div>' : '') +
  '</section>');

  document.getElementById("status-cards").innerHTML = cards.join("");
  document.querySelectorAll(".stt-dl").forEach((btn) => {
    btn.onclick = async () => {
      btn.disabled = true; btn.textContent = "Downloading...";
      try {
        await fetch("/api/stt/download", { method: "POST", body: new URLSearchParams({ model: btn.dataset.model }) });
        btn.textContent = "Downloading (background)...";
      } catch (e) { btn.textContent = "Download failed"; btn.disabled = false; }
    };
  });
  document.querySelectorAll(".emb-dl").forEach((btn) => {
    btn.onclick = async () => {
      btn.disabled = true; btn.textContent = "Downloading...";
      try {
        await fetch("/api/embedding/download", { method: "POST" });
        btn.textContent = "Downloading (background)...";
      } catch (e) { btn.textContent = "Download failed"; btn.disabled = false; }
    };
  });
}

document.getElementById("status-refresh").onclick = loadStatus;

async function loadLogs() {
  try {
    const d = await (await fetch("/api/logs")).json();
    document.getElementById("logs-output").textContent = d.log || "(no log entries yet)";
  } catch (e) {}
}

async function loadLogSettings() {
  try {
    const d = await (await fetch("/api/logs/settings")).json();
    document.getElementById("logs-retention").value = d.retention_hours;
  } catch (e) {}
}

document.getElementById("logs-reload").onclick = loadLogs;

document.getElementById("logs-clear").onclick = async () => {
  if (!confirm("Delete the entire log file?")) return;
  await fetch("/api/logs/clear", { method: "POST" });
  loadLogs();
};

document.getElementById("logs-save").onclick = async () => {
  const st = document.getElementById("logs-save-status");
  const fd = new FormData();
  fd.append("retention_hours", document.getElementById("logs-retention").value);
  st.textContent = "Saving...";
  try {
    const r = await fetch("/api/logs/settings", { method: "POST", body: fd });
    st.textContent = r.ok ? "Saved" : "Save failed";
  } catch (e) { st.textContent = "Save failed"; }
};

loadStatus();
loadLogs();
loadLogSettings();

// --- Profiles tab ---
let editingProfName = null;
let profVoices = [];
let profEngines = [];

function profVoiceName(id) {
  const v = profVoices.find((x) => x.id === id);
  return v ? v.name : "";
}

async function loadProfDropdowns() {
  try {
    const [pd, vd, ed, md] = await Promise.all([
      (await fetch("/api/personalities")).json(),
      (await fetch("/api/voices")).json(),
      (await fetch("/api/engines")).json(),
      (await fetch("/api/llm/models")).json(),
    ]);
    profVoices = vd.voices || [];
    profEngines = ed.engines || [];

    const psel = document.getElementById("prof-personality");
    psel.innerHTML = "";
    const pdOpt = document.createElement("option"); pdOpt.value = "default"; pdOpt.textContent = "default"; psel.appendChild(pdOpt);
    for (const p of (pd.personalities || [])) {
      if (p.id === "default") continue;
      const o = document.createElement("option"); o.value = p.id; o.textContent = p.name; psel.appendChild(o);
    }

    const tsel = document.getElementById("prof-tts");
    tsel.innerHTML = "";
    for (const e of profEngines) {
      const o = document.createElement("option"); o.value = e.name; o.textContent = e.name; tsel.appendChild(o);
    }

    const msel = document.getElementById("prof-model");
    msel.innerHTML = "";
    for (const m of (md.models || [])) {
      const o = document.createElement("option"); o.value = m.name; o.textContent = m.name; msel.appendChild(o);
    }
  } catch (e) {}
}

function _noneOption() {
  const o = document.createElement("option"); o.value = ""; o.textContent = "(none)"; return o;
}

function voiceMarkedFor(v, engineName) {
  if (v.engines && Array.isArray(v.engines) && v.engines.length) {
    return v.engines.includes(engineName);
  }
  return v.model === engineName;
}

function updateProfVoiceDropdowns(engineName) {
  const engine = profEngines.find((e) => e.name === engineName) || {};
  const supportsClone = !!engine.supports_cloning;
  const supportsDesign = !!engine.supports_design;
  const isKokoro = engineName === "kokoro";

  const psel = document.getElementById("prof-voice-preset");
  const spd = document.getElementById("prof-speed");
  psel.innerHTML = "";
  for (const v of (engine.voices || [])) {
    const o = document.createElement("option"); o.value = v.id; o.textContent = v.name;
    if (v.default === true) o.selected = true;
    psel.appendChild(o);
  }
  const speedSetting = (engine.settings || []).find((s) => s.name === "speed");
  if (speedSetting && speedSetting.default !== undefined) spd.value = speedSetting.default;

  const vsel = document.getElementById("prof-voice");
  const dsel = document.getElementById("prof-design");
  vsel.innerHTML = "";
  dsel.innerHTML = "";

  const clones = supportsClone ? profVoices.filter((v) => v.kind === "clone" && voiceMarkedFor(v, engineName)) : [];
  vsel.appendChild(_noneOption());
  for (const v of clones) {
    const o = document.createElement("option"); o.value = v.id; o.textContent = v.name;
    if (v.id === (engine.default_voice_id || "")) o.selected = true;
    vsel.appendChild(o);
  }
  vsel.disabled = clones.length === 0;

  const designs = supportsDesign ? profVoices.filter((v) => v.kind === "design" && voiceMarkedFor(v, engineName)) : [];
  dsel.appendChild(_noneOption());
  for (const v of designs) {
    const o = document.createElement("option"); o.value = v.id; o.textContent = v.name;
    if (v.id === (engine.default_instruction_id || "")) o.selected = true;
    dsel.appendChild(o);
  }
  dsel.disabled = designs.length === 0;

  document.getElementById("prof-row-voice-preset").style.display = isKokoro ? "" : "none";
  document.getElementById("prof-row-speed").style.display = isKokoro ? "" : "none";
  document.getElementById("prof-row-voice").style.display = !isKokoro ? "" : "none";
  document.getElementById("prof-row-design").style.display = (!isKokoro && supportsDesign) ? "" : "none";
}

document.getElementById("prof-tts").onchange = () => updateProfVoiceDropdowns(document.getElementById("prof-tts").value);

async function loadTalkProfiles() {
  try {
    const d = await (await fetch("/api/profiles")).json();
    renderTalkProfiles(d);
  } catch (e) {}
}

function renderTalkProfiles(d) {
  const grid = document.getElementById("prof-grid");
  grid.innerHTML = "";
  const profiles = d.profiles || {};
  const names = Object.keys(profiles);
  if (!names.length) {
    grid.innerHTML = '<div class="side-empty">No profiles yet. Add one to get started.</div>';
    return;
  }
  for (const name of names) {
    const p = profiles[name];
    const isDefault = d.default === name;
    const row = document.createElement("div");
    row.className = "profile";
    const info = document.createElement("div");
    info.className = "profile-info";
    const nm = document.createElement("strong");
    nm.textContent = name + (isDefault ? " (default)" : "");
    const det = document.createElement("span");
    det.className = "prompt-snippet";
    det.textContent = "personality: " + (p.personality || "-") + "  |  tts: " + (p.tts_model || "-") + "  |  voice: " + (profVoiceName(p.voice_id) || "none") + "  |  design: " + (profVoiceName(p.instruction_id) || "none") + "  |  llm: " + (p.model || "-") + "  |  mode: " + (p.mode || "chunked");
    info.appendChild(nm); info.appendChild(det);
    const actions = document.createElement("div");
    actions.className = "profile-actions";
    const defBtn = document.createElement("button");
    defBtn.className = "secondary"; defBtn.textContent = "Set default";
    defBtn.disabled = isDefault;
    defBtn.onclick = async () => { await fetch("/api/profiles/set-default", { method: "POST", body: new URLSearchParams({ name }) }); loadTalkProfiles(); };
    const edit = document.createElement("button");
    edit.className = "secondary"; edit.textContent = "Edit";
    edit.onclick = () => openTalkProfileEdit(name, p);
    const del = document.createElement("button");
    del.className = "danger"; del.textContent = "Delete";
    del.onclick = async () => {
      if (!confirm("Delete profile \"" + name + "\"?")) return;
      await fetch("/api/profiles/delete", { method: "POST", body: new URLSearchParams({ name }) });
      loadTalkProfiles();
    };
    actions.appendChild(defBtn); actions.appendChild(edit); actions.appendChild(del);
    row.appendChild(info); row.appendChild(actions);
    grid.appendChild(row);
  }
}

function openTalkProfileEdit(name, p) {
  editingProfName = name;
  document.getElementById("prof-modal-title").textContent = "Edit profile \"" + name + "\"";
  document.getElementById("prof-name").value = name;
  document.getElementById("prof-personality").value = p.personality || "default";
  document.getElementById("prof-tts").value = p.tts_model || "";
  document.getElementById("prof-model").value = p.model || "";
  document.getElementById("prof-mode").value = p.mode || "chunked";
  updateProfVoiceDropdowns(p.tts_model || "");
  document.getElementById("prof-voice").value = p.voice_id || "";
  document.getElementById("prof-design").value = p.instruction_id || "";
  if (p.voice) document.getElementById("prof-voice-preset").value = p.voice;
  if (p.speed !== undefined && p.speed !== null && p.speed !== "") { document.getElementById("prof-speed").value = p.speed; }
  document.getElementById("prof-modal").style.display = "flex";
}

document.getElementById("prof-add").onclick = async () => {
  if (!profVoices.length && !profEngines.length) { await loadProfDropdowns(); }
  editingProfName = null;
  document.getElementById("prof-modal-title").textContent = "Add profile";
  document.getElementById("prof-name").value = "";
  document.getElementById("prof-personality").value = "default";
  document.getElementById("prof-model").value = "";
  document.getElementById("prof-mode").value = "chunked";
  const tsel = document.getElementById("prof-tts");
  if (tsel.options.length) { tsel.value = tsel.options[0].value; updateProfVoiceDropdowns(tsel.value); }
  document.getElementById("prof-modal").style.display = "flex";
};

document.getElementById("prof-cancel").onclick = () => { document.getElementById("prof-modal").style.display = "none"; };

document.getElementById("prof-save").onclick = async () => {
  const st = document.getElementById("prof-status");
  const name = document.getElementById("prof-name").value.trim();
  if (!name) { st.textContent = "name required"; return; }
  const fd = new FormData();
  fd.append("name", name);
  fd.append("personality", document.getElementById("prof-personality").value);
  fd.append("tts_model", document.getElementById("prof-tts").value);
  fd.append("voice_id", document.getElementById("prof-voice").value);
  fd.append("instruction_id", document.getElementById("prof-design").value);
  fd.append("voice", document.getElementById("prof-voice-preset").value);
  fd.append("speed", document.getElementById("prof-speed").value);
  fd.append("model", document.getElementById("prof-model").value);
  fd.append("mode", document.getElementById("prof-mode").value);
  st.textContent = "Saving...";
  try {
    const r = await fetch("/api/profiles", { method: "POST", body: fd });
    if (!r.ok) { let m = "Save failed"; try { m = (await r.json()).detail || m; } catch (e) {} st.textContent = m; return; }
    document.getElementById("prof-modal").style.display = "none";
    loadTalkProfiles();
  } catch (e) { st.textContent = "Save failed"; }
};

document.getElementById("prof-test-play").onclick = async () => {
  const st = document.getElementById("prof-test-status");
  const text = document.getElementById("prof-test-text").value.trim();
  if (!text) { st.textContent = "enter some text"; return; }
  const fd = new FormData();
  fd.append("text", text);
  fd.append("model", document.getElementById("prof-tts").value);
  fd.append("voice_id", document.getElementById("prof-voice").value);
  fd.append("instruction_id", document.getElementById("prof-design").value);
  st.textContent = "Generating...";
  try {
    const r = await fetch("/api/tts", { method: "POST", body: fd });
    const d = await r.json();
    if (!r.ok) { st.textContent = d.detail || "test failed"; return; }
    const audio = document.getElementById("prof-test-audio");
    audio.src = d.audio_url;
    audio.style.display = "";
    audio.play().catch(() => {});
    st.textContent = "";
  } catch (e) { st.textContent = "Test failed"; }
};

loadProfDropdowns().then(() => loadTalkProfiles());

// --- DITTO (video generation) management ---
async function loadDittoStatus() {
  try {
    const d = await (await fetch("/api/ditto/status")).json();
    document.getElementById("ditto-enabled").checked = !!d.enabled;
    renderDittoStatus(d);
  } catch (e) {}
}

function renderDittoStatus(d) {
  const st = document.getElementById("ditto-status");
  if (d.running) {
    st.textContent = d.model_loaded ? "running (model loaded)" : "running (loading model)";
    st.className = "status ok";
  } else {
    st.textContent = "stopped";
    st.className = "status";
  }
}

document.getElementById("ditto-enabled").onchange = async () => {
  const fd = new FormData();
  fd.append("enabled", document.getElementById("ditto-enabled").checked ? "1" : "0");
  try {
    const r = await fetch("/api/ditto/toggle", { method: "POST", body: fd });
    if (r.ok) loadDittoStatus();
  } catch (e) {}
};

document.getElementById("ditto-load").onclick = async () => {
  try {
    await fetch("/api/ditto/load", { method: "POST" });
    setTimeout(loadDittoStatus, 1500);
  } catch (e) {}
};

document.getElementById("ditto-unload").onclick = async () => {
  try {
    await fetch("/api/ditto/unload", { method: "POST" });
    loadDittoStatus();
  } catch (e) {}
};

async function loadDittoSettings() {
  try {
    const d = await (await fetch("/api/ditto/settings")).json();
    document.getElementById("ditto-sampling-steps").value = d.sampling_timesteps;
    document.getElementById("ditto-stream-buffer").value = d.stream_playback_buffer;
    document.getElementById("ditto-min-fps").value = d.min_generation_fps;
    document.getElementById("ditto-fps-samples").value = d.fps_sample_count;
  } catch (e) {}
}

document.getElementById("ditto-settings-save").onclick = async () => {
  const fd = new FormData();
  fd.append("sampling_timesteps", document.getElementById("ditto-sampling-steps").value);
  fd.append("stream_playback_buffer", document.getElementById("ditto-stream-buffer").value);
  fd.append("min_generation_fps", document.getElementById("ditto-min-fps").value);
  fd.append("fps_sample_count", document.getElementById("ditto-fps-samples").value);
  const st = document.getElementById("ditto-settings-status");
  st.textContent = "Saving...";
  try {
    const r = await fetch("/api/ditto/settings", { method: "POST", body: fd });
    st.textContent = r.ok ? "Saved" : "Save failed";
  } catch (e) { st.textContent = "Save failed"; }
};

loadDittoStatus();

// ==================== Clone wizard ====================
let cw = { step: 1, file: null, audioBuffer: null, trimStart: 0, trimEnd: 0, trimmedBlob: null, engines: [], transcript: "", sampleText: "Hello, this is a preview of my voice.", samples: {} };
let wizAudioCtx = null;
let wizDrag = null;

function openCloneWizard() {
  cw = { step: 1, file: null, audioBuffer: null, trimStart: 0, trimEnd: 0, trimmedBlob: null, engines: [], transcript: "", sampleText: "Hello, this is a preview of my voice.", samples: {} };
  document.getElementById("wiz-file").value = "";
  document.getElementById("wiz-transcript").value = "";
  document.getElementById("wiz-sample-text").value = cw.sampleText;
  document.getElementById("wiz-name").value = "";
  document.getElementById("wiz-samples").innerHTML = "";
  document.getElementById("wiz-wave-wrap").style.display = "none";
  document.getElementById("wiz-status").textContent = "";
  goWizStep(1);
  document.getElementById("clone-wizard").style.display = "flex";
}

function goWizStep(n) {
  cw.step = n;
  document.querySelectorAll(".wiz-step").forEach(s => {
    const sn = parseInt(s.dataset.step, 10);
    s.classList.toggle("active", sn === n);
    s.classList.toggle("done", sn < n);
  });
  document.querySelectorAll(".wiz-panel").forEach(p => p.classList.remove("active"));
  const panel = document.getElementById("wiz-panel-" + n);
  if (panel) panel.classList.add("active");
  document.getElementById("wiz-back").style.display = n > 1 ? "" : "none";
  const nb = document.getElementById("wiz-next");
  nb.textContent = { 1: "Next", 2: "Save trim", 3: "Process (Whisper)", 4: "Next", 5: "Next", 6: "Save voice clone" }[n] || "Next";
  if (n === 3) renderWizEngines();
  if (n === 6) renderWizSamples();
}

function wizNext() {
  const st = document.getElementById("wiz-status");
  st.textContent = "";
  if (cw.step === 1) goWizStep(2);
  else if (cw.step === 2) {
    if (!cw.audioBuffer) { st.textContent = "Upload an audio file first."; return; }
    cw.trimmedBlob = makeTrimmedWav();
    cw.samples = {};
    renderWizEngines();
    goWizStep(3);
  } else if (cw.step === 3) {
    if (!cw.engines.length) { st.textContent = "Select at least one engine."; return; }
    wizTranscribe();
  } else if (cw.step === 4) {
    cw.transcript = document.getElementById("wiz-transcript").value.trim();
    goWizStep(5);
  } else if (cw.step === 5) {
    cw.sampleText = document.getElementById("wiz-sample-text").value.trim();
    if (!cw.sampleText) { st.textContent = "Sample text is empty."; return; }
    goWizStep(6);
  } else if (cw.step === 6) {
    wizSave();
  }
}

function wizBack() { if (cw.step > 1) goWizStep(cw.step - 1); }

// ---- step 2: upload + waveform + trim ----
document.getElementById("wiz-file").addEventListener("change", async (e) => {
  const f = e.target.files[0];
  if (!f) return;
  try {
    if (!wizAudioCtx) wizAudioCtx = new (window.AudioContext || window.webkitAudioContext)();
    const buf = await f.arrayBuffer();
    cw.audioBuffer = await wizAudioCtx.decodeAudioData(buf);
    cw.file = f;
    cw.trimStart = 0;
    cw.trimEnd = cw.audioBuffer.duration;
    document.getElementById("wiz-wave-wrap").style.display = "";
    document.getElementById("wiz-duration").textContent = cw.audioBuffer.duration.toFixed(1) + "s";
    drawWaveform();
  } catch (err) {
    document.getElementById("wiz-status").textContent = "Couldn't decode audio: " + err.message;
  }
});

function drawWaveform() {
  const canvas = document.getElementById("wiz-wave");
  if (!cw.audioBuffer) return;
  const ctx = canvas.getContext("2d");
  const w = canvas.width, h = canvas.height;
  const data = cw.audioBuffer.getChannelData(0);
  const n = data.length;
  const dur = cw.audioBuffer.duration;
  ctx.clearRect(0, 0, w, h);
  const step = Math.max(1, Math.floor(n / w));
  ctx.fillStyle = "#5b8def";
  for (let x = 0; x < w; x++) {
    let mn = 1, mx = -1;
    const s0 = x * step;
    for (let i = s0; i < Math.min(s0 + step, n); i++) {
      if (data[i] < mn) mn = data[i];
      if (data[i] > mx) mx = data[i];
    }
    const y1 = (1 - mx) * h / 2, y2 = (1 - mn) * h / 2;
    ctx.fillRect(x, y1, 1, Math.max(1, y2 - y1));
  }
  const sx = (cw.trimStart / dur) * w;
  const ex = ((cw.trimEnd || dur) / dur) * w;
  ctx.fillStyle = "rgba(0,0,0,0.4)";
  ctx.fillRect(0, 0, sx, h);
  ctx.fillRect(ex, 0, w - ex, h);
  ctx.lineWidth = 2;
  ctx.strokeStyle = "#2ecc71"; ctx.beginPath(); ctx.moveTo(sx, 0); ctx.lineTo(sx, h); ctx.stroke();
  ctx.strokeStyle = "#e74c3c"; ctx.beginPath(); ctx.moveTo(ex, 0); ctx.lineTo(ex, h); ctx.stroke();
  updateTrimLabels();
}

function updateTrimLabels() {
  const start = cw.trimStart || 0;
  const end = cw.trimEnd || (cw.audioBuffer ? cw.audioBuffer.duration : 0);
  document.getElementById("wiz-trim-start").textContent = start.toFixed(1) + "s";
  document.getElementById("wiz-trim-end").textContent = end.toFixed(1) + "s";
  document.getElementById("wiz-trim-sel").textContent = (end - start).toFixed(1) + "s";
}

(function initWaveDrag() {
  const canvas = document.getElementById("wiz-wave");
  const pos = (e) => { const r = canvas.getBoundingClientRect(); return ((e.clientX - r.left) / r.width) * canvas.width; };
  canvas.addEventListener("mousedown", (e) => {
    if (!cw.audioBuffer) return;
    const w = canvas.width, dur = cw.audioBuffer.duration;
    const x = pos(e);
    const sx = (cw.trimStart / dur) * w, ex = ((cw.trimEnd || dur) / dur) * w;
    if (Math.abs(x - sx) < 14) wizDrag = "start";
    else if (Math.abs(x - ex) < 14) wizDrag = "end";
    else wizDrag = null;
  });
  canvas.addEventListener("mousemove", (e) => {
    if (!wizDrag || !cw.audioBuffer) return;
    const dur = cw.audioBuffer.duration;
    const t = Math.max(0, Math.min(dur, (pos(e) / canvas.width) * dur));
    if (wizDrag === "start") cw.trimStart = Math.min(t, cw.trimEnd || dur);
    else if (wizDrag === "end") cw.trimEnd = Math.max(t, cw.trimStart);
    drawWaveform();
  });
  window.addEventListener("mouseup", () => { wizDrag = null; });
})();

let wizPlaySrc = null;
document.getElementById("wiz-play").onclick = () => {
  if (!cw.audioBuffer || !wizAudioCtx) return;
  if (wizPlaySrc) { try { wizPlaySrc.stop(); } catch (e) {} wizPlaySrc = null; document.getElementById("wiz-play").textContent = "Play selection"; return; }
  const src = wizAudioCtx.createBufferSource();
  src.buffer = cw.audioBuffer;
  src.connect(wizAudioCtx.destination);
  const dur = (cw.trimEnd || cw.audioBuffer.duration) - cw.trimStart;
  src.start(0, cw.trimStart, Math.max(0.05, dur));
  wizPlaySrc = src;
  document.getElementById("wiz-play").textContent = "Stop";
  src.onended = () => { wizPlaySrc = null; document.getElementById("wiz-play").textContent = "Play selection"; };
};

function makeTrimmedWav() {
  const buf = cw.audioBuffer;
  const sr = buf.sampleRate;
  const s0 = Math.floor(cw.trimStart * sr);
  const s1 = Math.floor((cw.trimEnd || buf.duration) * sr);
  const len = Math.max(1, s1 - s0);
  const trimmed = new Float32Array(len);
  trimmed.set(buf.getChannelData(0).subarray(s0, s1));
  return floatToWav(trimmed, sr);
}

function floatToWav(samples, sr) {
  const numCh = 1, len = samples.length * numCh * 2;
  const ab = new ArrayBuffer(44 + len), v = new DataView(ab);
  const ws = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
  ws(0, "RIFF"); v.setUint32(4, 36 + len, true); ws(8, "WAVE");
  ws(12, "fmt "); v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, numCh, true);
  v.setUint32(24, sr, true); v.setUint32(28, sr * numCh * 2, true); v.setUint16(32, numCh * 2, true); v.setUint16(34, 16, true);
  ws(36, "data"); v.setUint32(40, len, true);
  let off = 44;
  for (let i = 0; i < samples.length; i++) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    v.setInt16(off, s < 0 ? s * 0x8000 : s * 0x7fff, true);
    off += 2;
  }
  return new Blob([ab], { type: "audio/wav" });
}

// ---- step 3: engines ----
function renderWizEngines() {
  const box = document.getElementById("wiz-engines");
  box.innerHTML = "";
  const dur = cw.audioBuffer ? (cw.trimEnd || cw.audioBuffer.duration) - cw.trimStart : 0;
  const engs = [
    { name: "breeze", max: 60, note: "up to 60s (5-10s advised)" },
    { name: "omnivoice", max: 9, note: "trims to 9s" },
    { name: "lux", max: 5, note: "uses ~5s" },
  ];
  const compatible = [];
  for (const e of engs) {
    const ok = dur <= e.max;
    const label = document.createElement("label");
    label.className = "checkbox-label";
    const cb = document.createElement("input");
    cb.type = "checkbox"; cb.value = e.name; cb.checked = ok && cw.engines.includes(e.name);
    cb.addEventListener("change", () => {
      const idx = cw.engines.indexOf(e.name);
      if (cb.checked && idx < 0) cw.engines.push(e.name);
      else if (!cb.checked && idx >= 0) cw.engines.splice(idx, 1);
    });
    label.appendChild(cb);
    label.appendChild(document.createTextNode(" " + e.name + (ok ? "" : "  (clip too long - " + e.note + ")")));
    box.appendChild(label);
    if (ok) compatible.push(e.name);
  }
  if (!cw.engines.length) cw.engines = compatible.slice();
}

// ---- step 3->4: transcribe ----
async function wizTranscribe() {
  const st = document.getElementById("wiz-status");
  const nb = document.getElementById("wiz-next");
  if (!cw.trimmedBlob) { st.textContent = "No trimmed audio."; return; }
  nb.disabled = true;
  st.textContent = "Transcribing with Whisper…";
  const fd = new FormData();
  fd.append("audio", cw.trimmedBlob, "ref.wav");
  try {
    const r = await fetch("/api/stt", { method: "POST", body: fd });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || d.error || JSON.stringify(d));
    cw.transcript = d.text || "";
    document.getElementById("wiz-transcript").value = cw.transcript;
    st.textContent = "Transcribed.";
    goWizStep(4);
  } catch (e) {
    st.textContent = "Transcribe error: " + e.message;
  } finally {
    nb.disabled = false;
  }
}

// ---- step 6: samples + save ----
function renderWizSamples() {
  const box = document.getElementById("wiz-samples");
  box.innerHTML = '<div class="row actions"><button id="wiz-gen" class="primary">Generate samples</button></div>';
  document.getElementById("wiz-gen").onclick = wizGenerateSamples;
  for (const [eng, s] of Object.entries(cw.samples)) {
    addWizSample(eng, s.url, s.error);
  }
}

async function wizGenerateSamples() {
  const st = document.getElementById("wiz-status");
  const name = document.getElementById("wiz-name").value.trim();
  if (!name) { st.textContent = "Give the voice a name first."; return; }
  const genBtn = document.getElementById("wiz-gen");
  if (genBtn) { genBtn.disabled = true; genBtn.textContent = "Generating…"; }
  const text = cw.sampleText || "Hello, this is a preview of my voice.";
  st.textContent = "Generating samples…";
  cw.samples = {};
  for (const eng of cw.engines) {
    const fd = new FormData();
    fd.append("text", text);
    fd.append("model", eng);
    fd.append("ref_audio", cw.trimmedBlob, "ref.wav");
    fd.append("ref_text", cw.transcript || "");
    try {
      const r = await fetch("/api/tts/sample", { method: "POST", body: fd });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || d.error || JSON.stringify(d));
      cw.samples[eng] = { url: d.audio_url, error: null };
    } catch (e) {
      cw.samples[eng] = { url: null, error: e.message };
    }
  }
  renderWizSamples();
  st.textContent = "Done. Listen and pick the good ones.";
}

function addWizSample(eng, url, err) {
  const box = document.getElementById("wiz-samples");
  const row = document.createElement("div");
  row.className = "wiz-sample";
  const head = document.createElement("div");
  head.className = "wiz-sample-head";
  const lbl = document.createElement("label");
  lbl.className = "checkbox-label";
  const cb = document.createElement("input");
  cb.type = "checkbox"; cb.className = "wiz-pick"; cb.dataset.eng = eng; cb.checked = true;
  lbl.appendChild(cb);
  lbl.appendChild(document.createTextNode(" " + eng));
  head.appendChild(lbl);
  if (url) {
    const a = document.createElement("audio");
    a.controls = true; a.src = url;
    head.appendChild(a);
  } else {
    const sp = document.createElement("span");
    sp.className = "hint"; sp.textContent = "failed: " + (err || "error");
    head.appendChild(sp);
  }
  row.appendChild(head);
  box.appendChild(row);
}

async function wizSave() {
  const st = document.getElementById("wiz-status");
  const name = document.getElementById("wiz-name").value.trim();
  if (!name) { st.textContent = "Give the voice a name."; return; }
  const picked = [...document.querySelectorAll("#wiz-samples .wiz-pick:checked")].map(cb => cb.dataset.eng);
  if (!picked.length) { st.textContent = "Select at least one engine to save."; return; }
  st.textContent = "Saving…";
  const fd = new FormData();
  fd.append("name", name);
  fd.append("kind", "clone");
  fd.append("model", picked[0]);
  fd.append("transcript", cw.transcript || "");
  fd.append("ref_audio", cw.trimmedBlob, "ref.wav");
  fd.append("auto_transcribe", "false");
  fd.append("engines", picked.join(","));
  try {
    const r = await fetch("/api/voices", { method: "POST", body: fd });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || d.error || JSON.stringify(d));
    st.textContent = "Saved.";
    document.getElementById("clone-wizard").style.display = "none";
    refreshVoices($("model").value);
  } catch (e) {
    st.textContent = "Save error: " + e.message;
  }
}

document.getElementById("clone-add").onclick = openCloneWizard;
document.getElementById("wiz-next").onclick = wizNext;
document.getElementById("wiz-back").onclick = wizBack;
document.getElementById("wiz-cancel").onclick = () => { document.getElementById("clone-wizard").style.display = "none"; };

// ==================== Design wizard ====================
const OMNIVOICE_ATTRS = [
  { cat: "Gender", opts: [["Male","Male"],["Female","Female"]] },
  { cat: "Age", opts: [["Child","Child"],["Teenager","Teenager"],["Young Adult","Young Adult"],["Middle-aged","Middle-aged"],["Elderly","Elderly"]] },
  { cat: "Pitch", opts: [["Very Low","Very Low Pitch"],["Low","Low Pitch"],["Moderate","Moderate Pitch"],["High","High Pitch"],["Very High","Very High Pitch"]] },
  { cat: "Style", opts: [["Whisper","Whisper"]] },
  { cat: "English Accent", opts: [["American","American Accent"],["Australian","Australian Accent"],["British","British Accent"],["Chinese","Chinese Accent"],["Canadian","Canadian Accent"],["Indian","Indian Accent"],["Korean","Korean Accent"],["Portuguese","Portuguese Accent"],["Russian","Russian Accent"],["Japanese","Japanese Accent"]] },
  { cat: "Chinese Dialect", opts: [["Henan","河南话"],["Shaanxi","陕西话"],["Sichuan","四川话"],["Guizhou","贵州话"],["Yunnan","云南话"],["Guilin","桂林话"],["Jinan","济南话"],["Shijiazhuang","石家庄话"],["Gansu","甘肃话"],["Ningxia","宁夏话"],["Qingdao","青岛话"],["Northeast","东北话"]] },
];

let dw = { step: 1, engine: "breeze", instruction: "", sampleText: "Hello, this is a preview of my designed voice.", sampleUrl: null, name: "", editingId: null };

function openDesignWizard(editing) {
  dw = { step: 1, engine: "breeze", instruction: "", sampleText: "Hello, this is a preview of my designed voice.", sampleUrl: null, name: "", editingId: editing ? editing.id : null };
  document.getElementById("dwiz-free").value = "";
  document.getElementById("dwiz-name").value = "";
  document.getElementById("dwiz-sample-text").value = dw.sampleText;
  document.getElementById("dwiz-audio").innerHTML = "";
  document.getElementById("dwiz-status").textContent = "";
  renderDwizAttrs();
  if (editing) {
    dw.engine = (editing.model === "omnivoice") ? "omnivoice" : "breeze";
    dw.instruction = editing.instruction || "";
    dw.name = editing.name || "";
    document.getElementById("dwiz-name").value = dw.name;
    if (dw.engine === "breeze") {
      document.getElementById("dwiz-free").value = dw.instruction;
    } else {
      const parts = dw.instruction.split(",").map(s => s.trim());
      document.querySelectorAll("#dwiz-attrs input[type=checkbox]").forEach(cb => { cb.checked = parts.includes(cb.value); });
    }
  }
  const radio = document.querySelector('input[name="dwiz-engine"][value="' + dw.engine + '"]');
  if (radio) radio.checked = true;
  toggleDwizInput();
  goDwizStep(1);
  document.getElementById("design-wizard").style.display = "flex";
}

function goDwizStep(n) {
  dw.step = n;
  document.querySelectorAll("#dwiz-steps .wiz-step").forEach(s => {
    const sn = parseInt(s.dataset.step, 10);
    s.classList.toggle("active", sn === n);
    s.classList.toggle("done", sn < n);
  });
  document.querySelectorAll("#design-wizard .wiz-panel").forEach(p => p.classList.remove("active"));
  const panel = document.getElementById("dwiz-panel-" + n);
  if (panel) panel.classList.add("active");
  document.getElementById("dwiz-back").style.display = n > 1 ? "" : "none";
  document.getElementById("dwiz-next").textContent = { 1: "Next", 2: "Next", 3: "Next", 4: "Save voice design" }[n] || "Next";
}

function dwizNext() {
  const st = document.getElementById("dwiz-status");
  st.textContent = "";
  if (dw.step === 1) goDwizStep(2);
  else if (dw.step === 2) {
    dw.instruction = buildDwizInstruct();
    if (!dw.instruction) { st.textContent = "Enter a description or select attributes."; return; }
    goDwizStep(3);
  }
  else if (dw.step === 3) {
    dw.sampleText = document.getElementById("dwiz-sample-text").value.trim();
    if (!dw.sampleText) { st.textContent = "Sample text is empty."; return; }
    goDwizStep(4);
  }
  else if (dw.step === 4) dwizSave();
}

function dwizBack() { if (dw.step > 1) goDwizStep(dw.step - 1); }

document.querySelectorAll('input[name="dwiz-engine"]').forEach(r => r.addEventListener("change", () => {
  dw.engine = document.querySelector('input[name="dwiz-engine"]:checked').value;
  toggleDwizInput();
}));

function toggleDwizInput() {
  const isBreeze = dw.engine === "breeze";
  document.getElementById("dwiz-free-field").style.display = isBreeze ? "" : "none";
  document.getElementById("dwiz-attr-field").style.display = isBreeze ? "none" : "";
}

function renderDwizAttrs() {
  const box = document.getElementById("dwiz-attrs");
  box.innerHTML = "";
  for (const g of OMNIVOICE_ATTRS) {
    const h = document.createElement("div");
    h.className = "hint"; h.textContent = g.cat; h.style.marginTop = "8px"; h.style.fontWeight = "600";
    box.appendChild(h);
    const row = document.createElement("div");
    row.className = "btn-group";
    for (const [label, val] of g.opts) {
      const l = document.createElement("label");
      l.className = "checkbox-label";
      const cb = document.createElement("input");
      cb.type = "checkbox"; cb.value = val;
      l.appendChild(cb);
      l.appendChild(document.createTextNode(" " + label));
      row.appendChild(l);
    }
    box.appendChild(row);
  }
}

function buildDwizInstruct() {
  if (dw.engine === "breeze") return document.getElementById("dwiz-free").value.trim();
  const sel = [...document.querySelectorAll("#dwiz-attrs input[type=checkbox]:checked")].map(cb => cb.value);
  return sel.join(", ");
}

async function dwizGenerate() {
  const st = document.getElementById("dwiz-sample-status");
  const inst = buildDwizInstruct();
  const text = document.getElementById("dwiz-sample-text").value.trim();
  if (!inst) { st.textContent = "No design input."; return; }
  if (!text) { st.textContent = "Sample text empty."; return; }
  const btn = document.getElementById("dwiz-gen");
  btn.disabled = true; btn.textContent = "Generating…";
  st.textContent = "Generating…";
  const fd = new FormData();
  fd.append("text", text);
  fd.append("model", dw.engine);
  fd.append("instruction", inst);
  try {
    const r = await fetch("/api/tts/sample", { method: "POST", body: fd });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || d.error || JSON.stringify(d));
    dw.sampleUrl = d.audio_url;
    const box = document.getElementById("dwiz-audio");
    box.innerHTML = "";
    const a = document.createElement("audio");
    a.controls = true; a.src = d.audio_url;
    box.appendChild(a);
    st.textContent = "Done.";
  } catch (e) {
    st.textContent = "Generate error: " + e.message;
  } finally {
    btn.disabled = false; btn.textContent = "Generate sample";
  }
}

async function dwizSave() {
  const st = document.getElementById("dwiz-status");
  const name = document.getElementById("dwiz-name").value.trim();
  if (!name) { st.textContent = "Give the voice a name."; return; }
  const inst = buildDwizInstruct();
  if (!inst) { st.textContent = "No design input."; return; }
  st.textContent = "Saving…";
  const fd = new FormData();
  fd.append("name", name);
  fd.append("instruction", inst);
  fd.append("engines", dw.engine);
  try {
    let r;
    if (dw.editingId) {
      r = await fetch("/api/voices/" + dw.editingId, { method: "PUT", body: fd });
    } else {
      fd.append("kind", "design");
      fd.append("model", dw.engine);
      r = await fetch("/api/voices", { method: "POST", body: fd });
    }
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || d.error || JSON.stringify(d));
    st.textContent = "Saved.";
    document.getElementById("design-wizard").style.display = "none";
    refreshVoices($("model").value);
  } catch (e) {
    st.textContent = "Save error: " + e.message;
  }
}

document.getElementById("design-add").onclick = () => openDesignWizard(null);
document.getElementById("dwiz-next").onclick = dwizNext;
document.getElementById("dwiz-back").onclick = dwizBack;
document.getElementById("dwiz-cancel").onclick = () => { document.getElementById("design-wizard").style.display = "none"; };
document.getElementById("dwiz-gen").onclick = dwizGenerate;
