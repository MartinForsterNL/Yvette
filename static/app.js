// Talk UI - hold to talk, receive streamed voice response
// session expiry: auto-redirect to login on any 401
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

const $ = (id) => document.getElementById(id);

let convId = localStorage.getItem("talk_conv_id") || "";
let mediaRecorder = null;
let stream = null;
let chunks = [];
let mediaSequence = [];  // ordered playback items: {kind: "video"|"audio", url, bubble}
let mediaIndex = -1;     // current playback position, -1 = stopped
let videoPlaying = false; // whether an avatar video is currently playing
let streamPlayed = false; // whether the streaming turn's video has been started
let player = null;       // single hidden audio element
let profiles = {};       // profile name -> {personality, voice_id, instruction_id, mode}
let activeProfile = "";  // currently selected profile name (shown in the page title)
let attachedImage = null; // image File object to send with the next message
let attachedFile = null;  // non-image File object to upload
let muted = localStorage.getItem("talk_muted") === "1"; // sound on/off toggle
let currentModel = localStorage.getItem("talk_model") || ""; // selected LLM model
let chatHidden = localStorage.getItem("talk_chat_hidden") === "1"; // manual chat visibility
let autoHideChat = localStorage.getItem("talk_auto_hide") !== "0"; // auto-hide while thinking
let busy = false; // a turn is in progress (thinking/talking)
let turnStreaming = false; // pollTurn is still active (more media may be coming)

const EYE_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>';
const EYE_OFF_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>';
const VOLUME_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M15.54 8.46a5 5 0 0 1 0 7.07"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14"/></svg>';
const VOLUME_X_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><line x1="23" y1="9" x2="17" y2="15"/><line x1="17" y1="9" x2="23" y2="15"/></svg>';

function applyChatVisibility() {
  const hidden = chatHidden || (autoHideChat && busy);
  document.body.classList.toggle("chat-hidden", hidden);
  const btn = $("chat-toggle");
  if (btn) btn.innerHTML = hidden ? EYE_OFF_SVG : EYE_SVG;
}

function setChatHidden(hidden) {
  chatHidden = hidden;
  localStorage.setItem("talk_chat_hidden", hidden ? "1" : "0");
  applyChatVisibility();
}

function beginTurn() {
  busy = true;
  turnStreaming = true;
  applyChatVisibility();
}

function endTurn() {
  busy = false;
  turnStreaming = false;
  applyChatVisibility();
}

function ensureConvId() {
  if (!convId) {
    convId = (crypto.randomUUID && crypto.randomUUID()) || Date.now().toString(36);
    localStorage.setItem("talk_conv_id", convId);
  }
}

function scrollToBottom() {
  // While audio is playing, keep focus on the playing message instead of
  // jumping to the newest message.
  if (mediaIndex >= 0) return;
  $("conversation").scrollTop = $("conversation").scrollHeight;
}

function escapeHtml(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function linkify(text) {
  const esc = escapeHtml(text);
  return esc.replace(/(https?:\/\/[^\s<]+)/g, (url) => {
    const clean = url.replace(/[.,;:!?]+$/, "");
    return '<a href="' + clean + '" target="_blank" rel="noopener noreferrer">' + clean + '</a>';
  });
}

function addBubble(role, text) {
  const b = document.createElement("div");
  b.className = "bubble " + role;
  const span = document.createElement("span");
  span.innerHTML = linkify(text);
  b.appendChild(span);
  $("conversation").appendChild(b);
  scrollToBottom();
  return b;
}

function addReplayButton(bubble, idx) {
  const btn = document.createElement("button");
  btn.className = "replay-btn";
  btn.textContent = "Replay";
  btn.onclick = () => {
    busy = true;
    turnStreaming = false;
    applyChatVisibility();
    playMediaFrom(idx);
  };
  bubble.appendChild(btn);
}

function addMedia(kind, url, bubble, autoPlay, audio) {
  const idx = mediaSequence.length;
  mediaSequence.push({ kind, url, bubble, audio });
  if (autoPlay !== false && mediaIndex === -1 && !muted) {
    playMediaFrom(idx);
  }
}

function addAudioToBubble(bubble, url, autoPlay) {
  addReplayButton(bubble, mediaSequence.length);
  addMedia("audio", url, bubble, autoPlay);
}

function addImageToBubble(bubble, url) {
  const img = document.createElement("img");
  img.className = "bubble-image";
  img.src = url;
  img.alt = "image";
  img.onclick = () => openImageModal(url);
  bubble.appendChild(img);
}

function addFileToBubble(bubble, url) {
  const a = document.createElement("a");
  a.className = "download-link";
  a.href = url;
  a.target = "_blank";
  a.rel = "noopener noreferrer";
  a.textContent = "Download attached file";
  bubble.appendChild(a);
}

function openImageModal(url) {
  $("image-modal-img").src = url;
  $("image-modal").style.display = "flex";
}

function startVideo(url, onEnd, onError) {
  const v = $("avatar-talk");
  if (!v) return;
  videoPlaying = true;
  v.muted = false;
  v.style.display = "none"; // keep hidden until the first frame is ready
  let ready = false;
  const begin = () => {
    if (ready) return;
    ready = true;
    v.style.display = "";
    v.play().catch(() => { if (onError) onError(); });
  };
  v.oncanplay = begin;
  v.onloadeddata = begin; // fallback: first frame is available
  v.onended = () => {
    videoPlaying = false;
    v.style.display = "none";
    if (onEnd) onEnd();
  };
  v.onerror = () => {
    videoPlaying = false;
    v.style.display = "none";
    if (onError) onError();
  };
  v.src = url;
  v.currentTime = 0;
  v.load();
}

function addVideoReplay(bubble, url, autoPlay, audioUrl) {
  addReplayButton(bubble, mediaSequence.length);
  addMedia("video", url, bubble, autoPlay !== false, audioUrl);
}

function addAssistantSentence(sentence) {
  const b = document.createElement("div");
  b.className = "bubble assistant";
  const span = document.createElement("span");
  span.innerHTML = linkify(sentence.text);
  b.appendChild(span);
  $("conversation").appendChild(b);
  if (sentence.video_url) {
    addVideoReplay(b, sentence.video_url, true, sentence.audio_url);
  } else if (sentence.audio_url) {
    addAudioToBubble(b, sentence.audio_url);
  }
  scrollToBottom();
}

function toolArgsText(tc) {
  const a = tc.args || {};
  return a.query || a.url || JSON.stringify(a);
}

function addToolCall(tc) {
  if (tc.name === "send_file") {
    let info = null;
    try { info = JSON.parse(tc.result || "{}"); } catch (e) {}
    if (info && info.url) {
      const b = document.createElement("div");
      b.className = "bubble tool";
      const a = document.createElement("a");
      a.href = info.url;
      a.textContent = "Download " + (info.filename || "file");
      a.download = info.filename || "";
      a.target = "_blank";
      a.className = "download-link";
      b.appendChild(a);
      $("conversation").appendChild(b);
      scrollToBottom();
      return;
    }
  }
  const b = document.createElement("div");
  b.className = "bubble tool";
  const ts = document.createElement("span");
  ts.textContent = tc.name + ": " + toolArgsText(tc);
  b.appendChild(ts);
  $("conversation").appendChild(b);
  scrollToBottom();
}

function setTurnState(state) {
  // state: "idle" | "processing" | "thinking" | "talking"
  const el = $("talking");
  const label = $("talking-label");
  const stop = $("stop-btn");
  const texts = { processing: "Processing...", thinking: "Thinking...", talking: "Talking..." };
  if (state === "idle") {
    el.style.display = "none";
    $("status").style.display = "";
    if (label) label.style.display = "none";
    if (stop) stop.style.display = "none";
  } else {
    el.style.display = "";
    $("status").style.display = "none";
    if (label) { label.style.display = ""; label.textContent = texts[state] || state; }
    if (stop) stop.style.display = "";
  }
}

function setTalking(t) {
  setTurnState(t ? "talking" : "idle");
}

function setThinking(on, phase) {
  setTurnState(on ? (phase || "thinking") : "idle");
}

function ensurePlayer() {
  if (!player) {
    player = new Audio();
    player.muted = muted;
    player.addEventListener("ended", mediaAdvance);
    player.addEventListener("error", mediaAdvance);
  }
  return player;
}

function playMediaFrom(index) {
  if (index < 0 || index >= mediaSequence.length) return;
  mediaIndex = index;
  playMediaItem(mediaSequence[index]);
}

function playMediaItem(item) {
  setTalking(true);
  if (item.kind === "video") {
    startVideo(item.url, mediaAdvance, () => {
      if (item.audio) {
        ensurePlayer().src = item.audio;
        player.play().catch(() => mediaAdvance());
      } else {
        mediaAdvance();
      }
    });
  } else {
    ensurePlayer().src = item.url;
    player.play().catch(() => mediaAdvance());
  }
  highlightPlaying();
}

function mediaAdvance() {
  mediaIndex++;
  if (mediaIndex < mediaSequence.length) {
    playMediaItem(mediaSequence[mediaIndex]);
  } else {
    stopPlayback();
  }
}

function highlightPlaying() {
  document.querySelectorAll(".bubble.playing").forEach((b) => b.classList.remove("playing"));
  if (mediaIndex < 0 || mediaIndex >= mediaSequence.length) return;
  const bubble = mediaSequence[mediaIndex].bubble;
  if (bubble) {
    bubble.classList.add("playing");
    bubble.scrollIntoView({ behavior: "smooth", block: "center" });
  }
}

function stopPlayback() {
  mediaIndex = -1;
  videoPlaying = false;
  const v = $("avatar-talk");
  if (v) { v.pause(); v.style.display = "none"; }
  if (player) player.pause();
  if (turnStreaming) {
    setThinking(true); // gap between fragments: keep the animation, show "Thinking..."
  } else {
    setTalking(false); // idle
  }
  highlightPlaying();
  if (!turnStreaming) endTurn();
}

async function startRecording() {
  try {
    chunks = [];
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      $("status").textContent = "Mic unavailable - open this page over https:// to enable the microphone";
      return;
    }
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    mediaRecorder = new MediaRecorder(stream);
    mediaRecorder.ondataavailable = (e) => { if (e.data && e.data.size) chunks.push(e.data); };
    mediaRecorder.start();
    $("status").textContent = "Listening...";
    $("talk-btn").classList.add("recording");
  } catch (e) {
    $("status").textContent = "Mic error: " + e.message;
  }
}

function stopRecording() {
  return new Promise((resolve) => {
    if (!mediaRecorder || mediaRecorder.state === "inactive") { resolve(); return; }
    mediaRecorder.onstop = resolve;
    mediaRecorder.stop();
    if (stream) stream.getTracks().forEach((t) => t.stop());
    $("talk-btn").classList.remove("recording");
  });
}

async function finish() {
  if (!mediaRecorder || mediaRecorder.state === "inactive") return;
  await stopRecording();
  const blob = new Blob(chunks, { type: mediaRecorder.mimeType || "audio/webm" });
  if (blob.size < 500) { $("status").textContent = "Too short, try again"; return; }

  setThinking(true, "processing");
  beginTurn();
  ensureConvId();

  // stop any current playback (keep the sequence for replay)
  if (player) player.pause();
  mediaIndex = -1;
  highlightPlaying();

  const userBubble = addBubble("user", "...");
  try {
    const fd = new FormData();
    fd.append("audio", blob, "talk.webm");
    fd.append("conv_id", convId);
    fd.append("voice_id", $("voice").value);
    fd.append("instruction_id", $("instruction").value);
    fd.append("mode", $("mode").value);
    fd.append("personality", $("personality").value);
    fd.append("profile", activeProfile || "default");
    fd.append("skill", selectedSkills.join(","));
    fd.append("model", currentModel);
    fd.append("tts_model", $("tts-model").value);
    fd.append("voice", $("voice-preset").value);
    fd.append("speed", $("kokoro-speed").value);
    fd.append("avatar", localStorage.getItem("avatar") || "");
    fd.append("video", videoOn() ? "1" : "0");
    fd.append("gen_audio", genAudioOn() ? "1" : "0");
    if (attachedImage) { fd.append("image", attachedImage, attachedImage.name); }
    if (attachedFile) { fd.append("file", attachedFile, attachedFile.name); }
    clearAttachedImage();
    const r = await fetch("/api/talk", { method: "POST", body: fd });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || d.error || JSON.stringify(d));
    await pollTurn(d.turn_id, userBubble);
  } catch (e) {
    $("status").textContent = "Error: " + e.message;
  }
}

let allVoices = [];
let engineCaps = {}; // engine name -> {supports_cloning, supports_design}

function voiceMarkedFor(v, engineName) {
  if (v.engines && Array.isArray(v.engines) && v.engines.length) {
    return v.engines.includes(engineName);
  }
  return v.model === engineName;
}

async function loadVoices() {
  try {
    const [vr, er] = await Promise.all([
      fetch("/api/voices").then((r) => r.json()),
      fetch("/api/engines").then((r) => r.json()),
    ]);
    allVoices = vr.voices || [];
    engineCaps = {};
    for (const e of (er.engines || [])) {
      engineCaps[e.name] = { supports_cloning: !!e.supports_cloning, supports_design: !!e.supports_design, voices: e.voices || [], settings: e.settings || [], default_voice_id: e.default_voice_id || "", default_instruction_id: e.default_instruction_id || "" };
    }
    updateVoiceDropdowns($("tts-model").value);
  } catch (e) {
    $("status").textContent = "Couldn't load voices: " + e.message;
  }
}

function updateVoiceDropdowns(engineName) {
  const caps = engineCaps[engineName] || { supports_cloning: false, supports_design: false, voices: [], settings: [] };
  const isKokoro = engineName === "kokoro";

  const presetSel = $("voice-preset");
  presetSel.innerHTML = "";
  for (const v of (caps.voices || [])) {
    const opt = document.createElement("option");
    opt.value = v.id; opt.textContent = v.name;
    if (v.default === true) opt.selected = true;
    presetSel.appendChild(opt);
  }

  const speedSetting = (caps.settings || []).find((s) => s.name === "speed");
  const speedSlider = $("kokoro-speed");
  if (speedSetting) {
    if (speedSetting.min !== undefined) speedSlider.min = speedSetting.min;
    if (speedSetting.max !== undefined) speedSlider.max = speedSetting.max;
    if (!speedSlider.dataset.touched && speedSetting.default !== undefined) speedSlider.value = speedSetting.default;
  }
  updateSpeedLabel();

  const cloneSel = $("voice");
  cloneSel.innerHTML = "";
  const noneClone = document.createElement("option");
  noneClone.value = ""; noneClone.textContent = "(no clone)";
  cloneSel.appendChild(noneClone);
  if (caps.supports_cloning) {
    for (const v of allVoices) {
      if (v.kind !== "clone" || !voiceMarkedFor(v, engineName)) continue;
      const opt = document.createElement("option");
      opt.value = v.id; opt.textContent = v.name;
      if (v.id === (caps.default_voice_id || "")) opt.selected = true;
      cloneSel.appendChild(opt);
    }
  }

  const designSel = $("instruction");
  designSel.innerHTML = "";
  const noneDesign = document.createElement("option");
  noneDesign.value = ""; noneDesign.textContent = "(no design)";
  designSel.appendChild(noneDesign);
  if (caps.supports_design) {
    for (const v of allVoices) {
      if (v.kind !== "design" || !voiceMarkedFor(v, engineName)) continue;
      const opt = document.createElement("option");
      opt.value = v.id; opt.textContent = v.name;
      if (v.id === (caps.default_instruction_id || "")) opt.selected = true;
      designSel.appendChild(opt);
    }
  }

  $("row-voice-preset").style.display = isKokoro ? "" : "none";
  $("row-kokoro-speed").style.display = isKokoro ? "" : "none";
  $("row-voice").style.display = !isKokoro ? "" : "none";
  $("row-instruction").style.display = (!isKokoro && caps.supports_design) ? "" : "none";
}

function updateSpeedLabel() {
  const v = $("kokoro-speed");
  const lbl = $("kokoro-speed-value");
  if (lbl) lbl.textContent = v.value;
}

async function pollTurn(turnId, userBubble) {
  localStorage.setItem("talk_active_turn", turnId);
  let seen = 0;
  let seenTools = 0;
  let userTextShown = false;
  while (true) {
    const r = await fetch("/api/talk/status/" + turnId);
    const st = await r.json();
    if (!r.ok) throw new Error(st.detail || st.error || JSON.stringify(st));

    if (!userTextShown && st.user_text) {
      const _s = document.createElement("span");
      _s.innerHTML = linkify(st.user_text);
      userBubble.innerHTML = "";
      userBubble.appendChild(_s);
      userTextShown = true;
      if (st.user_audio_url) addAudioToBubble(userBubble, st.user_audio_url, false);
      if (st.user_image_url) addImageToBubble(userBubble, st.user_image_url);
      if (st.user_file_url) addFileToBubble(userBubble, st.user_file_url);
    }

    const toolCalls = st.tool_calls || [];
    for (let i = seenTools; i < toolCalls.length; i++) {
      addToolCall(toolCalls[i]);
    }
    seenTools = toolCalls.length;

    const sentences = st.sentences || [];
    for (let i = seen; i < sentences.length; i++) {
      if (st.stream) {
        const b = document.createElement("div");
        b.className = "bubble assistant";
        const sp = document.createElement("span");
        sp.innerHTML = linkify(sentences[i].text);
        b.appendChild(sp);
        $("conversation").appendChild(b);
      } else {
        addAssistantSentence(sentences[i]);
      }
    }
    seen = sentences.length;

    if (st.stream && st.stream.video_url && !streamPlayed) {
      streamPlayed = true;
      startVideo(st.stream.video_url, () => {}, () => {});
      if (st.stream.audio_url) {
        const au = new Audio(st.stream.audio_url);
        au.play().catch(() => {});
      }
      scrollToBottom();
    }

    if (st.status === "done") {
      localStorage.removeItem("talk_active_turn");
      $("status").textContent = "Hold the button to talk";
      turnStreaming = false;
      if (mediaIndex === -1) {
        setTalking(false);
        endTurn();
      }
      break;
    } else if (st.status === "error") {
      localStorage.removeItem("talk_active_turn");
      $("status").textContent = "Error: " + (st.error || "unknown");
      turnStreaming = false;
      if (mediaIndex === -1) {
        setTalking(false);
        endTurn();
      }
      break;
    }
    await new Promise((res) => setTimeout(res, 500));
  }
}

async function resumeActiveTurn() {
  const turnId = localStorage.getItem("talk_active_turn");
  if (!turnId) return;
  const bubble = addBubble("user", "...");
  setThinking(true, "processing");
  beginTurn();
  try {
    await pollTurn(turnId, bubble);
  } catch (e) {
    localStorage.removeItem("talk_active_turn");
    $("status").textContent = "Hold the button to talk";
  }
}

const btn = $("talk-btn");
btn.addEventListener("mousedown", (e) => { e.preventDefault(); startRecording(); });
btn.addEventListener("mouseup", (e) => { e.preventDefault(); finish(); });
btn.addEventListener("mouseleave", () => { finish(); });
btn.addEventListener("touchstart", (e) => { e.preventDefault(); startRecording(); }, { passive: false });
btn.addEventListener("touchend", (e) => { e.preventDefault(); finish(); }, { passive: false });

// Spacebar = push-to-talk (hold to talk) while the window is active.
// Ignored when focus is in a text field so you can still type spaces.
let spaceTalking = false;
window.addEventListener("keydown", (e) => {
  if (e.code !== "Space" || e.repeat || e.ctrlKey || e.altKey || e.metaKey) return;
  const tag = (e.target && e.target.tagName) || "";
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
  e.preventDefault();
  spaceTalking = true;
  startRecording();
});
window.addEventListener("keyup", (e) => {
  if (e.code !== "Space") return;
  if (spaceTalking) {
    spaceTalking = false;
    finish();
  }
});

async function sendText() {
  const text = $("text-input").value.trim();
  if (!text) return;
  $("text-input").value = "";
  setThinking(true, "processing");
  beginTurn();
  ensureConvId();

  if (player) player.pause();
  mediaIndex = -1;
  highlightPlaying();

  const userBubble = addBubble("user", text);
  try {
    const fd = new FormData();
    fd.append("text", text);
    fd.append("conv_id", convId);
    fd.append("voice_id", $("voice").value);
    fd.append("instruction_id", $("instruction").value);
    fd.append("mode", $("mode").value);
    fd.append("personality", $("personality").value);
    fd.append("profile", activeProfile || "default");
    fd.append("skill", selectedSkills.join(","));
    fd.append("model", currentModel);
    fd.append("tts_model", $("tts-model").value);
    fd.append("voice", $("voice-preset").value);
    fd.append("speed", $("kokoro-speed").value);
    fd.append("avatar", localStorage.getItem("avatar") || "");
    fd.append("video", videoOn() ? "1" : "0");
    fd.append("gen_audio", genAudioOn() ? "1" : "0");
    if (attachedImage) { fd.append("image", attachedImage, attachedImage.name); }
    if (attachedFile) { fd.append("file", attachedFile, attachedFile.name); }
    clearAttachedImage();
    const r = await fetch("/api/talk", { method: "POST", body: fd });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || d.error || JSON.stringify(d));
    await pollTurn(d.turn_id, userBubble);
  } catch (e) {
    $("status").textContent = "Error: " + e.message;
  }
}

$("stop-btn").onclick = () => {
  turnStreaming = false;
  stopPlayback();
};

$("settings-btn").onclick = () => { $("settings-modal").style.display = "flex"; };
$("settings-close").onclick = () => { $("settings-modal").style.display = "none"; };
$("settings-modal").addEventListener("click", (e) => {
  if (e.target === $("settings-modal")) $("settings-modal").style.display = "none";
});
$("avatar-btn").onclick = () => { $("avatar-modal").style.display = "flex"; };
$("avatar-modal-close").onclick = () => { $("avatar-modal").style.display = "none"; };
$("avatar-modal").addEventListener("click", (e) => {
  if (e.target === $("avatar-modal")) $("avatar-modal").style.display = "none";
});

function switchProfile(name) {
  activeProfile = name;
  applyProfile(name);
  loadHistory(name);
  const s = $("profile");
  if ([...s.options].some(o => o.value === name)) s.value = name;
  updateTitle();
}

$("profile").onchange = () => {
  const v = $("profile").value;
  if (v && v !== "__new__") {
    switchProfile(v);
  } else {
    activeProfile = "";
    updateTitle();
  }
};
$("profile-quick").onchange = () => {
  const v = $("profile-quick").value;
  if (profiles[v]) {
    switchProfile(v);
  }
};
$("save-profile").onclick = saveProfile;
$("set-default").onclick = setDefaultProfile;

function clearAttachedImage() {
  attachedImage = null;
  attachedFile = null;
  $("image-input").value = "";
  $("image-preview").style.display = "none";
  $("image-preview-img").src = "";
  const nameEl = $("file-preview-name");
  if (nameEl) { nameEl.textContent = ""; nameEl.style.display = "none"; }
}
$("attach-btn").onclick = () => $("image-input").click();
$("image-input").onchange = () => {
  const file = $("image-input").files && $("image-input").files[0];
  if (!file) return;
  const nameEl = $("file-preview-name");
  if (file.type && file.type.startsWith("image/")) {
    attachedImage = file;
    attachedFile = null;
    $("image-preview-img").src = URL.createObjectURL(file);
    $("image-preview-img").style.display = "";
    if (nameEl) { nameEl.textContent = ""; nameEl.style.display = "none"; }
    $("image-preview").style.display = "flex";
  } else {
    attachedImage = null;
    attachedFile = file;
    $("image-preview-img").style.display = "none";
    if (nameEl) {
      nameEl.textContent = "File: " + file.name;
      nameEl.style.display = "";
    }
    $("image-preview").style.display = "flex";
  }
};
$("image-clear").onclick = clearAttachedImage;

function updateMuteButton() {
  const btn = $("mute-btn");
  if (!btn) return;
  btn.innerHTML = muted ? VOLUME_X_SVG : VOLUME_SVG;
  btn.classList.toggle("muted", muted);
}
$("mute-btn").onclick = () => {
  muted = !muted;
  localStorage.setItem("talk_muted", muted ? "1" : "0");
  if (player) player.muted = muted;
  if (muted) stopPlayback();
  updateMuteButton();
};

$("image-modal-close").onclick = () => { $("image-modal").style.display = "none"; };
$("image-modal").addEventListener("click", (e) => {
  if (e.target === $("image-modal")) $("image-modal").style.display = "none";
});

function onManualSettingChange() {
  renderProfileQuick();
  updateTitle();
}
["personality", "voice", "instruction", "mode", "tts-model", "voice-preset", "kokoro-speed"].forEach((id) => {
  $(id).addEventListener("change", onManualSettingChange);
});
$("kokoro-speed").addEventListener("input", () => {
  $("kokoro-speed").dataset.touched = "1";
  updateSpeedLabel();
});
$("tts-model").addEventListener("change", () => updateVoiceDropdowns($("tts-model").value));

$("send-text").onclick = sendText;
$("text-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); sendText(); }
});

function currentProfile() {
  return activeProfile || "default";
}

function renderHistory(turns) {
  $("conversation").innerHTML = "";
  mediaSequence = [];
  mediaIndex = -1;
  if (player) player.pause();
  setTalking(false);
  for (const t of turns) {
    if (t.user_text) {
      const ub = addBubble("user", t.user_text);
      if (t.user_audio_url) addAudioToBubble(ub, t.user_audio_url, false);
      if (t.user_image_url) addImageToBubble(ub, t.user_image_url);
    }
    for (const tc of (t.tool_calls || [])) {
      addToolCall(tc);
    }
    const sentences = t.assistant_sentences || [];
    if (sentences.length) {
      for (const s of sentences) {
        const sb = addBubble("assistant", s.text);
        if (s.video_url) {
          addVideoReplay(sb, s.video_url, false, s.audio_url);
        } else if (s.audio_url) {
          addAudioToBubble(sb, s.audio_url, false);
        }
      }
    } else if (t.assistant_text) {
      addBubble("assistant", t.assistant_text);
    }
  }
}

async function loadHistory(profile) {
  try {
    const r = await fetch("/api/history?profile=" + encodeURIComponent(profile || "default"));
    const d = await r.json();
    renderHistory(d.turns || []);
  } catch (e) {
    $("status").textContent = "Couldn't load history: " + e.message;
  }
}

async function clearHistory() {
  if (!confirm("Erase this profile's history?")) return;
  try {
    const fd = new FormData();
    fd.append("profile", currentProfile());
    const r = await fetch("/api/history/clear", { method: "POST", body: fd });
    if (!r.ok) throw new Error((await r.json()).detail || "failed");
    stopPlayback();
    $("conversation").innerHTML = "";
    mediaSequence = [];
      $("status").textContent = "History cleared";
  } catch (e) {
    $("status").textContent = "Error: " + e.message;
  }
}

async function clearMemory() {
  if (!confirm("Erase this profile's saved memory? This cannot be undone.")) return;
  try {
    const fd = new FormData();
    fd.append("profile", currentProfile());
    const r = await fetch("/api/memory/clear", { method: "POST", body: fd });
    if (!r.ok) throw new Error((await r.json()).detail || "failed");
    $("status").textContent = "Memory cleared";
  } catch (e) {
    $("status").textContent = "Error: " + e.message;
  }
}

$("clear-history").onclick = clearHistory;
$("clear-memory").onclick = clearMemory;
$("logout-btn").onclick = () => { window.location.href = "/logout"; };
$("chat-toggle").onclick = () => setChatHidden(!chatHidden);

async function loadPersonalities() {
  try {
    const r = await fetch("/api/personalities");
    const d = await r.json();
    const sel = $("personality");
    sel.innerHTML = "";
    const saved = localStorage.getItem("talk_personality") || "default";
    for (const p of (d.personalities || [])) {
      const opt = document.createElement("option");
      opt.value = p.id; opt.textContent = p.name;
      sel.appendChild(opt);
    }
    if (saved && [...sel.options].some(o => o.value === saved)) sel.value = saved;
    sel.onchange = () => localStorage.setItem("talk_personality", sel.value);
  } catch (e) {
    $("status").textContent = "Couldn't load personalities: " + e.message;
  }
}

function renderProfileSelect() {
  const sel = $("profile");
  const current = sel.value;
  sel.innerHTML = "";
  for (const name of Object.keys(profiles)) {
    const opt = document.createElement("option");
    opt.value = name; opt.textContent = name;
    sel.appendChild(opt);
  }
  const newOpt = document.createElement("option");
  newOpt.value = "__new__"; newOpt.textContent = "+ New profile";
  sel.appendChild(newOpt);
  if (current && [...sel.options].some(o => o.value === current)) sel.value = current;
}

async function loadProfiles() {
  try {
    const r = await fetch("/api/profiles");
    const d = await r.json();
    profiles = d.profiles || {};
    renderProfileSelect();
    const def = d.default || "";
    if (def && profiles[def]) {
      $("profile").value = def;
      activeProfile = def;
      applyProfile(def);
    }
  } catch (e) {
    $("status").textContent = "Couldn't load profiles: " + e.message;
  }
  updateTitle();
}

function updateTitle() {
  const label = activeProfile ? "Yvette (" + activeProfile + ")" : "Yvette";
  document.title = label;
  const h1 = document.querySelector("h1");
  if (h1) h1.textContent = "Yvette";
  renderProfileQuick();
}

function renderProfileQuick() {
  const sel = $("profile-quick");
  if (!sel) return;
  sel.innerHTML = "";
  for (const name of Object.keys(profiles)) {
    const opt = document.createElement("option");
    opt.value = name; opt.textContent = name;
    sel.appendChild(opt);
  }
  if (activeProfile && [...sel.options].some(o => o.value === activeProfile)) {
    sel.value = activeProfile;
  } else if (sel.options.length) {
    sel.value = sel.options[0].value;
  }
}

function applyProfile(name) {
  const p = profiles[name];
  if (!p) return;
  if (p.personality && [...$("personality").options].some(o => o.value === p.personality)) $("personality").value = p.personality;
  if (p.mode) $("mode").value = p.mode;
  const modelSel = $("model-select");
  if (modelSel.options.length) {
    const desired = (p.model && [...modelSel.options].some(o => o.value === p.model)) ? p.model : modelSel.options[0].value;
    modelSel.value = desired;
    const sm = $("settings-model");
    if (sm && sm.options.length) sm.value = desired;
    currentModel = desired;
  }
  const ttsSel = $("tts-model");
  if (ttsSel) {
    if (p.tts_model && [...ttsSel.options].some(o => o.value === p.tts_model)) ttsSel.value = p.tts_model;
    else ttsSel.value = "breeze";
  }
  updateVoiceDropdowns(ttsSel ? ttsSel.value : "");
  if (p.voice && [...$("voice-preset").options].some(o => o.value === p.voice)) $("voice-preset").value = p.voice;
  if (p.speed && !isNaN(parseFloat(p.speed))) { $("kokoro-speed").value = p.speed; $("kokoro-speed").dataset.touched = "1"; updateSpeedLabel(); }
  if (p.voice_id && [...$("voice").options].some(o => o.value === p.voice_id)) $("voice").value = p.voice_id;
  if (p.instruction_id && [...$("instruction").options].some(o => o.value === p.instruction_id)) $("instruction").value = p.instruction_id;
}

async function saveProfile() {
  let name = $("profile").value;
  if (name === "__new__" || !name) {
    name = prompt("Profile name:");
    if (!name) return;
  }
  const body = new URLSearchParams({
    name: name,
    personality: $("personality").value,
    voice_id: $("voice").value,
    instruction_id: $("instruction").value,
    mode: $("mode").value,
    model: currentModel,
    tts_model: $("tts-model").value,
    voice: $("voice-preset").value,
    speed: $("kokoro-speed").value,
  });
  try {
    await fetch("/api/profiles", { method: "POST", body });
    await loadProfiles();
    $("profile").value = name;
    activeProfile = name;
    updateTitle();
    $("status").textContent = "Profile saved";
  } catch (e) {
    $("status").textContent = "Error: " + e.message;
  }
}

async function setDefaultProfile() {
  const name = $("profile").value;
  if (name === "__new__" || !name) return;
  try {
    await fetch("/api/profiles/set-default", { method: "POST", body: new URLSearchParams({ name }) });
    $("status").textContent = "Default profile: " + name;
  } catch (e) {
    $("status").textContent = "Error: " + e.message;
  }
}

let selectedSkills = [];
try { selectedSkills = JSON.parse(localStorage.getItem("talk_skills") || "[]"); } catch (e) { selectedSkills = []; }
let currentSkillList = [];

async function loadSkills() {
  try {
    const r = await fetch("/api/skills");
    const d = await r.json();
    currentSkillList = d.skills || [];
    renderSkillsList();
    updateSkillBadge();
  } catch (e) {
    // skills are optional, ignore errors
  }
}

function renderSkillsList() {
  const list = $("skills-list");
  if (!list) return;
  list.innerHTML = "";
  if (!currentSkillList.length) {
    list.innerHTML = '<div class="side-empty">No skills available</div>';
    return;
  }
  for (const s of currentSkillList) {
    const label = document.createElement("label");
    label.className = "checkbox-label";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.value = s.name;
    cb.checked = selectedSkills.includes(s.name);
    const span = document.createElement("span");
    span.textContent = s.title || s.name;
    label.appendChild(cb);
    label.appendChild(span);
    list.appendChild(label);
  }
}

function updateSkillBadge() {
  const btn = $("skill-btn");
  if (btn) btn.classList.toggle("active", selectedSkills.length > 0);
}

$("skill-btn").onclick = () => {
  renderSkillsList();
  $("skills-modal").style.display = "flex";
};

$("skills-cancel").onclick = () => { $("skills-modal").style.display = "none"; };

$("skills-ok").onclick = () => {
  const checked = [];
  document.querySelectorAll("#skills-list input[type=checkbox]:checked").forEach((cb) => checked.push(cb.value));
  selectedSkills = checked;
  localStorage.setItem("talk_skills", JSON.stringify(selectedSkills));
  updateSkillBadge();
  $("skills-modal").style.display = "none";
};

async function loadModels() {
  try {
    const r = await fetch("/api/models");
    const d = await r.json();
    const models = d.models || [];
    for (const id of ["model-select", "settings-model"]) {
      const sel = $(id);
      sel.innerHTML = "";
      for (const m of models) {
        const opt = document.createElement("option");
        opt.value = m.name; opt.textContent = m.name;
        sel.appendChild(opt);
      }
    }
    if (!currentModel || !models.some((m) => m.name === currentModel)) {
      currentModel = models.length ? models[0].name : "";
    }
    const sync = (fromSel) => {
      currentModel = fromSel.value;
      localStorage.setItem("talk_model", currentModel);
      const other = fromSel.id === "model-select" ? $("settings-model") : $("model-select");
      if (other) other.value = currentModel;
      onManualSettingChange();
    };
    $("model-select").value = currentModel;
    $("settings-model").value = currentModel;
    $("model-select").onchange = () => sync($("model-select"));
    $("settings-model").onchange = () => sync($("settings-model"));
  } catch (e) {}
}

(async () => {
  await loadVoices();
  await loadPersonalities();
  await loadModels();
  await loadProfiles();
  await loadHistory(currentProfile());
  loadSkills();
  updateMuteButton();
  resumeActiveTurn();
  initBubbleOpacity();
  initTextOpacity();
  initAudioToggle();
  initVideoToggle();
  initIdleAvatarToggle();
  initAutoHideToggle();
  await loadAvatars();
  showEntryScreen();
  applyChatVisibility();
})();

let AVATARS = [];

function avatarImage(id) {
  if (!id) return "/static/avatars/default.jpg";
  const a = AVATARS.find(x => x.id === id);
  return a ? ("/static/avatars/" + a.image + (a.v ? ("?v=" + a.v) : "")) : ("/static/avatars/" + id + ".png");
}

function applyAvatar(id) {
  const name = id || "";
  localStorage.setItem("avatar", name);
  const idle = $("avatar");
  if (idle) {
    const src = name ? ("/static/avatars/" + name + ".mp4") : "/static/idle.mp4";
    if (idle.src !== src) idle.src = src;
  }
  const btnImg = $("avatar-btn-img");
  if (btnImg) btnImg.src = avatarImage(name);
  document.querySelectorAll(".avatar-thumb").forEach(el => {
    el.classList.toggle("selected", el.dataset.id === name);
  });
  applyIdleAvatar();
}

function renderAvatarGrid(avatars) {
  AVATARS = avatars || [];
  const grid = $("avatar-grid");
  if (!grid) return;
  grid.innerHTML = "";
  const make = (id, imageUrl, label, removable, disabled) => {
    const el = document.createElement("div");
    el.className = "avatar-thumb" + (disabled ? " disabled" : "");
    el.dataset.id = id;
    const img = document.createElement("img");
    img.src = imageUrl; img.alt = label; img.loading = "lazy";
    const lbl = document.createElement("span");
    lbl.textContent = label + (disabled ? " (generating)" : "");
    el.appendChild(img); el.appendChild(lbl);
    if (!disabled) el.onclick = () => applyAvatar(id);
    if (removable) {
      const rm = document.createElement("button");
      rm.className = "avatar-remove";
      rm.textContent = "×";
      rm.title = "Remove avatar";
      rm.onclick = (e) => { e.stopPropagation(); removeAvatar(id); };
      el.appendChild(rm);
    }
    grid.appendChild(el);
  };
  make("", "/static/avatars/default.jpg", "Default", false, false);
  for (const a of AVATARS) {
    make(a.id, "/static/avatars/" + a.image + (a.v ? ("?v=" + a.v) : ""), a.name || ("Avatar " + a.id), true, !a.idle_ready);
  }
  applyAvatar(localStorage.getItem("avatar") || "");
}

function removeAvatar(id) {
  if (!confirm("Remove avatar " + id + "?")) return;
  const fd = new FormData();
  fd.append("id", id);
  fetch("/api/avatars/remove", { method: "POST", body: fd })
    .then(r => r.json())
    .then(() => {
      if (localStorage.getItem("avatar") === id) localStorage.removeItem("avatar");
      loadAvatars();
    })
    .catch(e => alert("Remove failed: " + e.message));
}

function loadAvatars() {
  return fetch("/api/avatars")
    .then(r => r.json())
    .then(d => renderAvatarGrid(d.avatars || []))
    .catch(() => renderAvatarGrid([]));
}

$("avatar-add-btn").onclick = () => $("avatar-file").click();
$("avatar-file").onchange = () => {
  const f = $("avatar-file").files[0];
  if (!f) return;
  const fd = new FormData();
  fd.append("image", f, f.name);
  $("status").textContent = "Adding avatar...";
  fetch("/api/avatars/add", { method: "POST", body: fd })
    .then(r => r.json())
    .then(() => {
      $("status").textContent = "";
      $("avatar-file").value = "";
      loadAvatars();
    })
    .catch(e => { $("status").textContent = "Add failed: " + e.message; });
};

function videoOn() {
  const cb = $("video-toggle");
  return !cb || cb.checked;
}

function genAudioOn() {
  const cb = $("audio-toggle");
  return !cb || cb.checked;
}

function initAudioToggle() {
  const cb = $("audio-toggle");
  if (!cb) return;
  const saved = localStorage.getItem("talk_audio");
  cb.checked = saved !== "0";
  cb.addEventListener("change", () => {
    localStorage.setItem("talk_audio", cb.checked ? "1" : "0");
  });
}

function initAutoHideToggle() {
  const cb = $("autohide-toggle");
  if (!cb) return;
  cb.checked = autoHideChat;
  cb.addEventListener("change", () => {
    autoHideChat = cb.checked;
    localStorage.setItem("talk_auto_hide", cb.checked ? "1" : "0");
    applyChatVisibility();
  });
}

function initVideoToggle() {
  const cb = $("video-toggle");
  if (!cb) return;
  const saved = localStorage.getItem("talk_video");
  cb.checked = saved !== "0";
}

function idleAvatarOn() {
  const cb = $("idle-toggle");
  return !cb || cb.checked;
}

let entryDone = false;

function applyIdleAvatar() {
  const idle = $("avatar");
  if (!idle) return;
  const on = idleAvatarOn() && entryDone;
  idle.style.display = on ? "" : "none";
  if (on) {
    idle.play().catch(() => {});
  } else {
    idle.pause();
  }
}

function initIdleAvatarToggle() {
  const cb = $("idle-toggle");
  if (!cb) return;
  const saved = localStorage.getItem("talk_idle");
  cb.checked = saved !== "0";
  cb.addEventListener("change", () => {
    localStorage.setItem("talk_idle", cb.checked ? "1" : "0");
    applyIdleAvatar();
  });
  applyIdleAvatar();
}

function initBubbleOpacity() {
  const slider = $("bubble-opacity");
  const label = $("bubble-opacity-value");
  if (!slider) return;
  const saved = localStorage.getItem("bubbleOpacity");
  if (saved !== null && saved !== undefined) slider.value = saved;
  const apply = () => {
    document.documentElement.style.setProperty("--bubble-opacity", slider.value + "%");
    if (label) label.textContent = slider.value + "%";
    localStorage.setItem("bubbleOpacity", slider.value);
  };
  slider.addEventListener("input", apply);
  apply();
}

function initTextOpacity() {
  const slider = $("text-opacity");
  const label = $("text-opacity-value");
  if (!slider) return;
  const saved = localStorage.getItem("textOpacity");
  if (saved !== null && saved !== undefined) slider.value = saved;
  const apply = () => {
    document.documentElement.style.setProperty("--text-opacity", (slider.value / 100).toString());
    if (label) label.textContent = slider.value + "%";
    localStorage.setItem("textOpacity", slider.value);
  };
  slider.addEventListener("input", apply);
  apply();
}

// --- Entry / setup screen (shown after login) ---
let entryAvatar = localStorage.getItem("avatar") || "";

function renderEntryAvatarGrid(avatars) {
  const grid = $("entry-avatar-grid");
  if (!grid) return;
  grid.innerHTML = "";
  const make = (id, imageUrl, label, disabled) => {
    const el = document.createElement("div");
    el.className = "avatar-thumb" + (disabled ? " disabled" : "");
    el.dataset.id = id;
    const img = document.createElement("img");
    img.src = imageUrl; img.alt = label; img.loading = "lazy";
    const lbl = document.createElement("span");
    lbl.textContent = label + (disabled ? " (generating)" : "");
    el.appendChild(img); el.appendChild(lbl);
    if (!disabled) el.onclick = () => selectEntryAvatar(id);
    grid.appendChild(el);
  };
  make("", "/static/avatars/default.jpg", "Default", false);
  const list = (avatars || []).slice();
  list.sort((a, b) => {
    const as = a.id === entryAvatar ? -1 : 0;
    const bs = b.id === entryAvatar ? -1 : 0;
    return as - bs;
  });
  for (const a of list) {
    make(a.id, "/static/avatars/" + a.image + (a.v ? ("?v=" + a.v) : ""), a.name || ("Avatar " + a.id), !a.idle_ready);
  }
  selectEntryAvatar(entryAvatar);
}

function selectEntryAvatar(id) {
  entryAvatar = id;
  document.querySelectorAll("#entry-avatar-grid .avatar-thumb").forEach((el) => {
    el.classList.toggle("selected", el.dataset.id === id);
  });
}

function showEntryScreen() {
  const psel = $("entry-profile");
  if (psel) {
    psel.innerHTML = "";
    for (const name of Object.keys(profiles)) {
      const opt = document.createElement("option");
      opt.value = name; opt.textContent = name;
      psel.appendChild(opt);
    }
    const want = (activeProfile && profiles[activeProfile]) ? activeProfile : (Object.keys(profiles)[0] || "");
    if (want && [...psel.options].some((o) => o.value === want)) psel.value = want;
  }
  renderEntryAvatarGrid(AVATARS);
  const ea = $("entry-audio"); if (ea) ea.checked = genAudioOn();
  const ev = $("entry-video"); if (ev) ev.checked = videoOn();
  const ei = $("entry-idle"); if (ei) ei.checked = idleAvatarOn();
  const eh = $("entry-autohide"); if (eh) eh.checked = autoHideChat;
  const screen = $("entry-screen");
  if (screen) screen.style.display = "flex";
}

$("entry-start").onclick = () => {
  const p = $("entry-profile").value;
  if (p && profiles[p]) switchProfile(p);
  applyAvatar(entryAvatar);
  const at = $("audio-toggle"); if (at) { at.checked = $("entry-audio").checked; localStorage.setItem("talk_audio", at.checked ? "1" : "0"); }
  const vt = $("video-toggle"); if (vt) { vt.checked = $("entry-video").checked; localStorage.setItem("talk_video", vt.checked ? "1" : "0"); }
  const it = $("idle-toggle"); if (it) { it.checked = $("entry-idle").checked; localStorage.setItem("talk_idle", it.checked ? "1" : "0"); }
  const ht = $("autohide-toggle"); if (ht) { ht.checked = $("entry-autohide").checked; }
  autoHideChat = $("entry-autohide").checked;
  localStorage.setItem("talk_auto_hide", autoHideChat ? "1" : "0");
  entryDone = true;
  applyIdleAvatar();
  applyChatVisibility();
  const screen = $("entry-screen");
  if (screen) screen.style.display = "none";
};
