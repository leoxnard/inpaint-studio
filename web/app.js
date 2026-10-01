// Inpaint Studio frontend: two-step mask + edit UI. Vanilla ES module, no build step.

const $ = (id) => document.getElementById(id);

// ------------------------------------------------------------------ persisted form fields
const PERSIST = [
  "megapixels", "resolution", "autofix", "maskText", "threshold", "refine", "expand", "invert",
  "brushSize", "opacity", "useMask", "prompt", "negative", "steps", "denoise", "feather", "mode", "keepIdentical", "livePreview", "saveEvery", "seed",
  "randomSeed", "cfg", "sampler", "scheduler", "unet", "clip", "vae",
];
const STORE_KEY = "inpaint-studio-form-v1";

function loadForm() {
  try {
    const saved = JSON.parse(localStorage.getItem(STORE_KEY) || "{}");
    for (const id of PERSIST) {
      if (!(id in saved)) continue;
      const el = $(id);
      if (el.type === "checkbox") el.checked = !!saved[id];
      else el.value = saved[id];
    }
  } catch { /* storage unavailable or corrupt */ }
}
function saveForm() {
  try {
    const out = {};
    for (const id of PERSIST) out[id] = $(id).type === "checkbox" ? $(id).checked : $(id).value;
    localStorage.setItem(STORE_KEY, JSON.stringify(out));
  } catch { /* ignore */ }
}

// ------------------------------------------------------------------ state
const state = {
  imageName: null, srcW: 0, srcH: 0, imgEl: null, imgUrl: null,
  size: null,                 // last /api/size report (incl. suggested)
  mask: null,                 // offscreen canvas: white = replace, transparent = keep
  maskMeta: null,             // {w, h, mp} the mask was created for
  hasMask: false,
  history: [],                // ImageData snapshots for undo
  mode: "paint",
  running: false,
  ws: null,
  view: "mask",
  run: null,                  // current/last run object
  runs: [],
};

const display = $("display");
const dctx = display.getContext("2d");
const tint = document.createElement("canvas");
const tctx = tint.getContext("2d");

// ------------------------------------------------------------------ helpers
let toastTimer = 0;
function showError(msg) {
  $("toastText").textContent = String(msg);
  $("toast").hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { $("toast").hidden = true; }, 15000);
}
$("toastClose").onclick = () => { $("toast").hidden = true; };

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let detail = "";
    try { const j = await r.json(); detail = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); }
    catch { detail = await r.text().catch(() => ""); }
    throw new Error(`${path}: ${r.status} ${detail}`.slice(0, 600));
  }
  return r.json();
}
const postJson = (path, body) => api(path, {
  method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
});
function debounce(fn, ms) {
  let t = 0;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}
function loadImage(url) {
  return new Promise((res, rej) => {
    const im = new Image();
    im.onload = () => res(im);
    im.onerror = () => rej(new Error("Could not load image"));
    im.src = url;
  });
}
const fmtTime = (s) => s >= 60 ? `${Math.floor(s / 60)}m ${Math.round(s % 60)}s` : `${Math.round(s)}s`;

// ------------------------------------------------------------------ status pill
async function pollStatus() {
  const pill = $("statusPill");
  try {
    const s = await api("/api/status");
    if (s.comfy) {
      pill.className = "pill online";
      $("statusText").textContent = `ComfyUI online · running ${s.running} · pending ${s.pending}`;
    } else {
      pill.className = "pill offline";
      $("statusText").textContent = "ComfyUI offline";
    }
  } catch {
    pill.className = "pill offline";
    $("statusText").textContent = "Server unreachable";
  }
}

// ------------------------------------------------------------------ models
async function loadModels() {
  try {
    const m = await api("/api/models");
    const fill = (id, list, pref) => {
      const sel = $(id);
      const saved = sel.value;
      sel.innerHTML = "";
      for (const v of list) sel.add(new Option(v, v));
      const stored = (() => { try { return JSON.parse(localStorage.getItem(STORE_KEY) || "{}")[id]; } catch { return null; } })();
      const want = list.includes(stored) ? stored
        : list.includes(saved) ? saved
        : (pref && list.find((v) => v.includes(pref))) || list[0];
      if (want) sel.value = want;
    };
    fill("unet", m.unets, "Q4_K_M");
    fill("clip", m.clips, "qwen3vl_8b");
    fill("vae", m.vaes, "qwen_image_2.1");
    fill("sampler", m.samplers, "euler");
    fill("scheduler", m.schedulers, "simple");
    if (!$("sampler").value && m.samplers.includes("euler")) $("sampler").value = "euler";
    saveForm();
  } catch (e) {
    showError(`Could not load models: ${e.message}`);
  }
}

// ------------------------------------------------------------------ size & safety
const num = (id) => parseFloat($(id).value);

async function refreshSize() {
  if (!state.imageName) return null;
  const body = { width: state.srcW, height: state.srcH, megapixels: num("megapixels"), resolution: parseInt($("resolution").value, 10) };
  if (!(body.megapixels > 0) || !(body.resolution > 0)) return null;
  let applied = false;
  try {
    let rep = await postJson("/api/size", body);
    if (!rep.safe && $("autofix").checked && rep.suggested) {
      $("megapixels").value = rep.suggested.megapixels;
      $("resolution").value = rep.suggested.resolution;
      saveForm();
      rep = await postJson("/api/size", { ...body, megapixels: rep.suggested.megapixels, resolution: rep.suggested.resolution });
      applied = true;
    }
    state.size = rep;
    renderSizeInfo(rep, applied);
    syncMaskToSize();
    return rep;
  } catch (e) {
    showError(e.message);
    return null;
  }
}
const refreshSizeDebounced = debounce(refreshSize, 300);

function renderSizeInfo(rep, applied) {
  const sg = rep.suggested || {};
  let html = `Working size: <b>${rep.work_w}×${rep.work_h}</b><br>` +
    `Target tokens: <b>${rep.target_tokens}</b> · ref tokens: <b>${rep.ref_tokens}</b><br>`;
  if (rep.safe) {
    html += `<span class="badge ok">OK</span>`;
    if (applied) html += ` <span class="hint">auto-adjusted to ${sg.megapixels} MP / ${sg.resolution}</span>`;
  } else {
    html += `<span class="badge bad">Gray-noise risk (limit 4096 tokens)</span><br>` +
      `<span class="hint">Suggested: ${sg.megapixels} MP, resolution ${sg.resolution} (${sg.work_w}×${sg.work_h})</span>`;
  }
  $("sizeInfo").innerHTML = html;
}

// Makes sure the offscreen mask matches the working size; flags it as stale otherwise.
function syncMaskToSize() {
  const s = state.size;
  if (!s) return;
  if (!state.hasMask) {
    if (!state.mask || state.mask.width !== s.work_w || state.mask.height !== s.work_h) {
      newMask(s.work_w, s.work_h);
    }
  }
  updateStale();
  render();
}

function isStale() {
  const s = state.size, m = state.maskMeta;
  if (!state.hasMask || !s || !m) return false;
  return m.w !== s.work_w || m.h !== s.work_h || m.mp !== num("megapixels");
}
function updateStale() {
  const stale = isStale();
  $("maskStale").hidden = !stale;
}

// ------------------------------------------------------------------ mask canvas
function newMask(w, h) {
  const c = document.createElement("canvas");
  c.width = w; c.height = h;
  state.mask = c;
  state.history = [];
  tint.width = w; tint.height = h;
}
function resetMask() {
  state.hasMask = false;
  state.maskMeta = null;
  state.mask = null;
  state.history = [];
  $("maskTime").textContent = "";
  if (state.size) newMask(state.size.work_w, state.size.work_h);
  updateStale();
}
function ensureMask() {
  if (!state.size) return false;
  if (!state.mask) newMask(state.size.work_w, state.size.work_h);
  if (!state.hasMask) {
    state.hasMask = true;
    state.maskMeta = { w: state.mask.width, h: state.mask.height, mp: num("megapixels") };
  }
  return true;
}
function pushHistory() {
  const c = state.mask;
  if (!c) return;
  state.history.push(c.getContext("2d").getImageData(0, 0, c.width, c.height));
  if (state.history.length > 20) state.history.shift();
}
function undo() {
  const snap = state.history.pop();
  if (!snap || !state.mask) return;
  state.mask.getContext("2d").putImageData(snap, 0, 0);
  render();
}

// ------------------------------------------------------------------ rendering
let raf = 0;
function render() {
  if (raf) return;
  raf = requestAnimationFrame(() => { raf = 0; draw(); });
}
function draw() {
  const empty = $("stageEmpty");
  const w = state.size ? state.size.work_w : (state.mask ? state.mask.width : 0);
  const h = state.size ? state.size.work_h : (state.mask ? state.mask.height : 0);
  empty.hidden = !!state.imgEl;
  display.hidden = !state.imgEl;
  if (!state.imgEl || !w) return;
  if (display.width !== w || display.height !== h) { display.width = w; display.height = h; }
  dctx.clearRect(0, 0, w, h);
  dctx.drawImage(state.imgEl, 0, 0, w, h);
  if (state.mask) {
    if (tint.width !== state.mask.width || tint.height !== state.mask.height) {
      tint.width = state.mask.width; tint.height = state.mask.height;
    }
    tctx.globalCompositeOperation = "source-over";
    tctx.clearRect(0, 0, tint.width, tint.height);
    tctx.drawImage(state.mask, 0, 0);
    tctx.globalCompositeOperation = "source-in";
    tctx.fillStyle = "#ff2a2a";
    tctx.fillRect(0, 0, tint.width, tint.height);
    dctx.globalAlpha = num("opacity");
    dctx.drawImage(tint, 0, 0, w, h);
    dctx.globalAlpha = 1;
  }
}

// ------------------------------------------------------------------ brush painting
function canvasPoint(e) {
  const r = display.getBoundingClientRect();
  return { x: (e.clientX - r.left) * display.width / r.width, y: (e.clientY - r.top) * display.height / r.height };
}
let stroking = false, last = null;

function strokeTo(p) {
  const ctx = state.mask.getContext("2d");
  // The mask is in working-size coordinates; the display canvas has the same size when not stale.
  const sx = state.mask.width / display.width, sy = state.mask.height / display.height;
  const x = p.x * sx, y = p.y * sy;
  ctx.lineCap = ctx.lineJoin = "round";
  ctx.lineWidth = parseFloat($("brushSize").value) * sx;
  ctx.fillStyle = ctx.strokeStyle = "#fff";
  ctx.globalCompositeOperation = state.mode === "erase" ? "destination-out" : "source-over";
  ctx.beginPath();
  if (last) { ctx.moveTo(last.x * sx, last.y * sy); ctx.lineTo(x, y); ctx.stroke(); }
  else { ctx.arc(x, y, ctx.lineWidth / 2, 0, Math.PI * 2); ctx.fill(); }
  ctx.globalCompositeOperation = "source-over";
  last = p;
  render();
}
display.addEventListener("pointerdown", (e) => {
  if (state.mode === "off" || !state.imgEl || e.button !== 0) return;
  if (!ensureMask()) return;
  pushHistory();
  stroking = true; last = null;
  display.setPointerCapture(e.pointerId);
  strokeTo(canvasPoint(e));
  updateStale();
});
display.addEventListener("pointermove", (e) => {
  moveCursor(e);
  if (stroking) strokeTo(canvasPoint(e));
});
const endStroke = () => { stroking = false; last = null; };
display.addEventListener("pointerup", endStroke);
display.addEventListener("pointercancel", endStroke);
display.addEventListener("pointerleave", () => { $("brushCursor").hidden = true; });
display.addEventListener("pointerenter", moveCursor);

function moveCursor(e) {
  const cur = $("brushCursor");
  if (state.mode === "off" || !state.imgEl) { cur.hidden = true; return; }
  const r = display.getBoundingClientRect();
  const d = parseFloat($("brushSize").value) * r.width / display.width;
  cur.hidden = false;
  cur.className = "cursor" + (state.mode === "erase" ? " erase" : "");
  cur.style.width = cur.style.height = `${d}px`;
  cur.style.left = `${e.clientX}px`;
  cur.style.top = `${e.clientY}px`;
}

function setMode(mode) {
  state.mode = mode;
  for (const b of $("brushMode").children) b.classList.toggle("active", b.dataset.mode === mode);
  display.classList.toggle("brush", mode !== "off");
  if (mode === "off") $("brushCursor").hidden = true;
}
$("brushMode").addEventListener("click", (e) => { if (e.target.dataset.mode) setMode(e.target.dataset.mode); });

$("maskClear").onclick = () => {
  if (!state.mask || !state.hasMask) return;
  pushHistory();
  state.mask.getContext("2d").clearRect(0, 0, state.mask.width, state.mask.height);
  render();
};
$("maskFill").onclick = () => {
  if (!ensureMask()) return;
  pushHistory();
  const ctx = state.mask.getContext("2d");
  ctx.fillStyle = "#fff";
  ctx.fillRect(0, 0, state.mask.width, state.mask.height);
  render();
};
$("maskUndo").onclick = undo;
document.addEventListener("keydown", (e) => {
  const typing = /INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName);
  if ((e.metaKey || e.ctrlKey) && e.key === "z" && !typing) {
    e.preventDefault(); undo();
    return;
  }
  // arrow keys step through the filmstrip; past the last frame shows the final comparison
  if ((e.key === "ArrowLeft" || e.key === "ArrowRight") && !typing && !$("resultView").hidden) {
    const run = state.run;
    if (!run || !run.frames.length) return;
    e.preventDefault();
    const onCompare = !$("compare").hidden;
    const last = run.frames.length - 1;
    let i = onCompare ? last + 1 : (run.shown ?? last);
    i += e.key === "ArrowRight" ? 1 : -1;
    if (i > last) { if (run.resultUrl) showCompare(); else showFrame(last); return; }
    showFrame(Math.max(0, i));
    $("filmstrip").children[Math.max(0, i)]?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }
});

// ------------------------------------------------------------------ image upload
async function setImageFile(file) {
  if (!file || !file.type.startsWith("image/")) { showError("Please choose an image file."); return; }
  $("imageInfo").textContent = "Uploading...";
  try {
    const fd = new FormData();
    fd.append("file", file, file.name || "image.png");
    const up = await api("/api/upload", { method: "POST", body: fd });
    const url = URL.createObjectURL(file);
    const im = await loadImage(url);
    if (state.imgUrl) URL.revokeObjectURL(state.imgUrl);
    state.imgUrl = url; state.imgEl = im;
    state.imageName = up.name; state.srcW = up.width; state.srcH = up.height;
    $("imageInfo").textContent = `${file.name || "image"} – original ${up.width}×${up.height}`;
    $("dropText").textContent = "Drop another image or click to replace";
    resetMask();
    state.size = null;
    await refreshSize();
    setView("mask");
    render();
  } catch (e) {
    $("imageInfo").textContent = state.imageName ? $("imageInfo").textContent : "No image loaded.";
    showError(e.message);
  }
}
$("fileInput").addEventListener("change", (e) => { setImageFile(e.target.files[0]); e.target.value = ""; });
let dragDepth = 0;
window.addEventListener("dragenter", (e) => { e.preventDefault(); dragDepth++; document.body.classList.add("dragging"); });
window.addEventListener("dragleave", () => { if (--dragDepth <= 0) { dragDepth = 0; document.body.classList.remove("dragging"); } });
window.addEventListener("dragover", (e) => e.preventDefault());
window.addEventListener("drop", (e) => {
  e.preventDefault(); dragDepth = 0; document.body.classList.remove("dragging");
  const f = e.dataTransfer && e.dataTransfer.files[0];
  if (f) setImageFile(f);
});

// ------------------------------------------------------------------ step 1: compute mask
async function computeMask() {
  if (!state.imageName) { showError("Load an image first."); return; }
  const text = $("maskText").value.trim();
  if (!text) { showError("Enter what to mask."); return; }
  const btn = $("computeMask");
  btn.disabled = true;
  $("maskSpinner").hidden = false;
  try {
    const rep = await refreshSize();
    if (!rep) return;
    const res = await postJson("/api/mask", {
      image: state.imageName, megapixels: num("megapixels"), text,
      threshold: num("threshold"), refine: parseInt($("refine").value, 10) || 0,
      expand: parseInt($("expand").value, 10) || 0, invert: $("invert").checked,
    });
    const im = await loadImage(res.mask_url);
    const w = im.naturalWidth, h = im.naturalHeight;
    const tmp = document.createElement("canvas");
    tmp.width = w; tmp.height = h;
    const tc = tmp.getContext("2d", { willReadFrequently: true });
    tc.drawImage(im, 0, 0);
    const data = tc.getImageData(0, 0, w, h);
    const px = data.data;
    for (let i = 0; i < px.length; i += 4) {
      const lum = 0.299 * px[i] + 0.587 * px[i + 1] + 0.114 * px[i + 2];
      px[i] = px[i + 1] = px[i + 2] = 255;
      px[i + 3] = lum > 127 ? 255 : 0;
    }
    newMask(w, h);
    state.mask.getContext("2d").putImageData(data, 0, 0);
    state.hasMask = true;
    state.maskMeta = { w, h, mp: num("megapixels") };
    $("maskTime").textContent = `Last: ${res.seconds}s`;
    updateStale();
    render();
  } catch (e) {
    showError(e.message);
  } finally {
    btn.disabled = false;
    $("maskSpinner").hidden = true;
  }
}
$("computeMask").onclick = computeMask;

// Exports the mask as a strict black/white PNG at working size (white = replace).
function exportMaskBlob() {
  const src = state.mask;
  const c = document.createElement("canvas");
  c.width = src.width; c.height = src.height;
  const ctx = c.getContext("2d", { willReadFrequently: true });
  ctx.fillStyle = "#000";
  ctx.fillRect(0, 0, c.width, c.height);
  ctx.drawImage(src, 0, 0);
  const data = ctx.getImageData(0, 0, c.width, c.height);
  const px = data.data;
  let white = 0;
  for (let i = 0; i < px.length; i += 4) {
    const v = px[i] > 127 ? 255 : 0;
    px[i] = px[i + 1] = px[i + 2] = v; px[i + 3] = 255;
    if (v) white++;
  }
  ctx.putImageData(data, 0, 0);
  return new Promise((res) => c.toBlob((b) => res({ blob: b, white }), "image/png"));
}

// ------------------------------------------------------------------ step 2: run edit
function setRunning(on) {
  state.running = on;
  $("runEdit").disabled = on;
  $("runEdit").textContent = on ? "Running..." : "Run edit";
}

async function runEdit() {
  if (state.running) return;
  if (!state.imageName) { showError("Load an image first."); return; }
  const useMask = $("useMask").checked;
  setRunning(true);
  try {
    const rep = await refreshSize();
    if (!rep) { setRunning(false); return; }
    let maskName = null;
    if (useMask) {
      if (!state.hasMask) throw new Error("No mask yet. Compute or paint a mask, or turn off \"Use mask\".");
      if (isStale()) throw new Error("Size changed – recompute the mask first.");
      const { blob, white } = await exportMaskBlob();
      if (!white) throw new Error("The mask is empty. Paint or compute a mask, or turn off \"Use mask\".");
      const fd = new FormData();
      fd.append("file", blob, "mask.png");
      maskName = (await api("/api/upload-mask", { method: "POST", body: fd })).name;
    }
    let seed = parseInt($("seed").value, 10) || 0;
    if ($("randomSeed").checked) { seed = Math.floor(Math.random() * 2 ** 32); $("seed").value = seed; }
    saveForm();
    const params = {
      image: state.imageName, mask: maskName, use_mask: useMask,
      src_w: state.srcW, src_h: state.srcH,
      megapixels: num("megapixels"), resolution: parseInt($("resolution").value, 10),
      prompt: $("prompt").value, negative: $("negative").value,
      steps: parseInt($("steps").value, 10), denoise: num("denoise"), seed, cfg: num("cfg"),
      sampler: $("sampler").value, scheduler: $("scheduler").value, feather: num("feather"), mode: $("mode").value, keep_identical: $("keepIdentical").checked, save_every: parseInt($("saveEvery").value, 10) || 0,
      unet: $("unet").value, clip: $("clip").value, vae: $("vae").value, preview_method: $("livePreview").checked ? "auto" : "none",
    };
    startRun(params);
  } catch (e) {
    setRunning(false);
    showError(e.message);
  }
}

function startRun(params) {
  freeRunFrames(state.run);
  const run = {
    id: Date.now(), prompt: params.prompt, seed: params.seed, frames: [], t0: performance.now(),
    value: 0, max: params.steps, resultUrl: null, beforeUrl: null, filename: null, done: false, timer: 0,
  };
  state.run = run;
  setView("result");
  $("resultEmpty").hidden = true;
  $("compare").hidden = true;
  $("liveImg").hidden = true;
  $("resultActions").hidden = true;
  $("showCompare").hidden = true;
  $("progressWrap").hidden = false;
  $("progressBar").style.width = "0%";
  $("progressText").textContent = "Queued...";
  renderFilmstrip();
  run.timer = setInterval(updateProgressText, 500);

  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/edit`);
  state.ws = ws;
  let finished = false;
  ws.onopen = () => ws.send(JSON.stringify(params));
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    switch (m.type) {
      case "queued": $("progressText").textContent = `Queued – working size ${m.size.work_w}×${m.size.work_h}`; break;
      case "node": $("progressText").textContent = `Running node: ${m.node}`; break;
      case "progress": run.value = m.value; run.max = m.max; updateProgressText(); break;
      case "preview": addFrame(run, m); break;
      case "step_image": addSavedFrame(run, m); break;
      case "done": finished = true; finishRun(run, m); break;
      case "error": finished = true; failRun(run, m.message); break;
    }
  };
  ws.onerror = () => { if (!finished) { finished = true; failRun(run, "WebSocket connection failed."); } };
  ws.onclose = () => { if (!finished) { finished = true; failRun(run, "Connection closed before the edit finished."); } };
}

function updateProgressText() {
  const run = state.run;
  if (!run || run.done) return;
  const pct = run.max ? (run.value / run.max) * 100 : 0;
  $("progressBar").style.width = `${pct}%`;
  const el = (performance.now() - run.t0) / 1000;
  let txt = run.value ? `step ${run.value} / ${run.max}` : "Waiting for sampler...";
  txt += ` · ${fmtTime(el)} elapsed`;
  if (run.value > 0 && run.value < run.max) txt += ` · ~${fmtTime(el / run.value * (run.max - run.value))} left`;
  $("progressText").textContent = txt;
}

function addFrame(run, m) {
  // a saved full-quality render for this step already exists -> no extra live preview
  if (run.frames.some((f) => f.kind === "saved" && f.step === m.step)) return;
  const bin = atob(m.data);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  const url = URL.createObjectURL(new Blob([bytes], { type: m.mime }));
  run.frames.push({ url, step: m.step, mime: m.mime, kind: "live" });
  if (state.run === run && !run.done) showFrame(run.frames.length - 1);
  renderFilmstrip();
}

// full-quality intermediate image (real VAE decode), saved by ComfyUI every N steps
function addSavedFrame(run, m) {
  // the saved render replaces the live preview(s) of the same step
  run.frames = run.frames.filter((f) => {
    const drop = f.kind === "live" && f.step === m.step;
    if (drop) URL.revokeObjectURL(f.url);
    return !drop;
  });
  run.frames.push({ url: m.url, step: m.step, mime: "image/png", kind: "saved", filename: m.filename });
  if (state.run === run && !run.done) showFrame(run.frames.length - 1);
  renderFilmstrip();
}

function showFrame(i) {
  const run = state.run;
  if (!run || !run.frames[i]) return;
  $("liveImg").src = run.frames[i].url;
  $("liveImg").hidden = false;
  $("compare").hidden = true;
  $("resultEmpty").hidden = true;
  $("showCompare").hidden = !run.resultUrl;
  run.shown = i;
  for (const [j, img] of [...$("filmstrip").children].entries()) img.classList.toggle("active", j === i);
}

function renderFilmstrip() {
  const fs = $("filmstrip");
  fs.innerHTML = "";
  const run = state.run;
  if (!run) return;
  run.frames.forEach((f, i) => {
    const img = document.createElement("img");
    img.src = f.url;
    img.title = f.kind === "saved" ? `Step ${f.step} – full quality (saved)` : `Step ${f.step} – live preview`;
    if (f.kind === "saved") img.classList.add("saved");
    img.onclick = () => showFrame(i);
    if (i === run.shown) img.classList.add("active");
    fs.appendChild(img);
  });
  const active = fs.querySelector(".active");
  if (active && !run.done) fs.scrollLeft = fs.scrollWidth;
}

function freeRunFrames(run) {
  if (!run || state.runs.some((r) => r === run)) return;
  for (const f of run.frames) if (f.kind !== "saved") URL.revokeObjectURL(f.url);
}

function endSocket() {
  setRunning(false);
  if (state.ws) { try { state.ws.close(); } catch { /* ignore */ } state.ws = null; }
}

function finishRun(run, m) {
  clearInterval(run.timer);
  run.done = true;
  run.resultUrl = m.result_url;
  run.beforeUrl = m.before_url;
  run.rawUrl = m.raw_url || null;
  run.maskUrl = m.mask_url || null;
  run.filename = m.filename;
  $("progressBar").style.width = "100%";
  $("progressText").textContent = `Done in ${fmtTime((performance.now() - run.t0) / 1000)}`;
  endSocket();
  if (!m.result_url) { showError("The run finished without a result image."); return; }
  state.runs.unshift(run);
  renderHistory();
  showRun(run);
}

function failRun(run, message) {
  clearInterval(run.timer);
  run.done = true;
  $("progressText").textContent = `Failed: ${message}`;
  endSocket();
  showError(message);
}

$("cancelEdit").onclick = async () => {
  try { await postJson("/api/cancel", {}); } catch (e) { showError(e.message); }
};

// ------------------------------------------------------------------ result display
function showRun(run) {
  state.run = run;
  $("resultEmpty").hidden = true;
  $("resultActions").hidden = false;
  $("downloadBtn").href = run.resultUrl;
  $("downloadBtn").download = run.filename || "result.png";
  $("filmstrip").innerHTML = "";
  renderFilmstrip();
  showCompare();
  renderHistory();
  $("showRaw").hidden = !run.rawUrl;
  $("matchInfo").textContent = run.match || "";
  if (run.rawUrl && run.maskUrl && !run.match) measureMatch(run);
}

$("showRaw").onclick = () => {
  const run = state.run;
  if (!run || !run.rawUrl) return;
  $("liveImg").src = run.rawUrl;
  $("liveImg").hidden = false;
  $("compare").hidden = true;
  $("showCompare").hidden = false;
};

function loadImg(url) {
  return new Promise((res, rej) => { const i = new Image(); i.onload = () => res(i); i.onerror = rej; i.src = url; });
}

// How closely the free edit matches the original outside the mask (paste only works if it lines up)
async function measureMatch(run) {
  try {
    const [b, r, m] = await Promise.all([loadImg(run.beforeUrl), loadImg(run.rawUrl), loadImg(run.maskUrl)]);
    const w = b.naturalWidth, h = b.naturalHeight;
    const px = (img) => { const c = document.createElement("canvas"); c.width = w; c.height = h; const x = c.getContext("2d"); x.drawImage(img, 0, 0, w, h); return x.getImageData(0, 0, w, h).data; };
    const B = px(b), R = px(r), M = px(m);
    let sum = 0, n = 0;
    for (let i = 0; i < B.length; i += 4) {
      if (M[i] > 20) continue; // only pixels clearly outside the mask
      sum += (Math.abs(B[i] - R[i]) + Math.abs(B[i + 1] - R[i + 1]) + Math.abs(B[i + 2] - R[i + 2])) / 3;
      n++;
    }
    const diff = n ? sum / n : 0;
    const verdict = diff < 8 ? "very close" : diff < 16 ? "close" : diff < 28 ? "noticeably different" : "different – paste may not line up";
    run.match = `Outside-mask difference: ${diff.toFixed(1)} / 255 (${verdict})`;
    if (state.run === run) $("matchInfo").textContent = run.match;
  } catch { /* measurement is optional */ }
}

function showCompare() {
  const run = state.run;
  if (!run || !run.resultUrl) return;
  $("cmpBefore").src = run.beforeUrl || run.resultUrl;
  $("cmpAfter").src = run.resultUrl;
  $("liveImg").hidden = true;
  $("compare").hidden = false;
  $("showCompare").hidden = true;
  setDivider(50);
  for (const img of $("filmstrip").children) img.classList.remove("active");
}
$("showCompare").onclick = showCompare;

function setDivider(pct) {
  pct = Math.max(0, Math.min(100, pct));
  $("cmpDivider").style.left = `${pct}%`;
  $("cmpAfter").style.clipPath = `inset(0 0 0 ${pct}%)`;
}
{
  const cmp = $("compare");
  let drag = false;
  const move = (e) => { const r = cmp.getBoundingClientRect(); setDivider((e.clientX - r.left) / r.width * 100); };
  cmp.addEventListener("pointerdown", (e) => { drag = true; cmp.setPointerCapture(e.pointerId); move(e); });
  cmp.addEventListener("pointermove", (e) => { if (drag) move(e); });
  cmp.addEventListener("pointerup", () => { drag = false; });
  cmp.addEventListener("pointercancel", () => { drag = false; });
}

function renderHistory() {
  const box = $("history");
  box.innerHTML = "";
  if (!state.runs.length) { box.innerHTML = '<div class="hint">No runs yet.</div>'; return; }
  for (const run of state.runs) {
    const b = document.createElement("button");
    b.className = "hist-item" + (run === state.run ? " active" : "");
    const img = document.createElement("img");
    img.src = run.resultUrl; img.alt = "";
    const d = document.createElement("div");
    const t = document.createElement("div"); t.className = "t"; t.textContent = run.prompt;
    const s = document.createElement("div"); s.className = "s"; s.textContent = `seed ${run.seed} · ${run.frames.length} steps`;
    d.append(t, s);
    b.append(img, d);
    b.onclick = () => { if (state.running) return; showRun(run); $("progressWrap").hidden = true; };
    box.appendChild(b);
  }
}

$("downloadSteps").onclick = async () => {
  const run = state.run;
  if (!run || !run.frames.length) { showError("No step frames to download."); return; }
  // prefer the full-quality saved steps; fall back to the live previews
  const saved = run.frames.filter((f) => f.kind === "saved");
  const list = saved.length ? saved : run.frames;
  for (const [i, f] of list.entries()) {
    const a = document.createElement("a");
    a.href = f.url;
    a.download = f.kind === "saved" ? `step-${String(f.step).padStart(3, "0")}.png`
      : `preview-${String(i + 1).padStart(2, "0")}.${f.mime.includes("png") ? "png" : "jpg"}`;
    document.body.appendChild(a); a.click(); a.remove();
    await new Promise((r) => setTimeout(r, 150));
  }
};

$("useResult").onclick = async () => {
  const run = state.run;
  if (!run || !run.resultUrl) return;
  try {
    const blob = await (await fetch(run.resultUrl)).blob();
    await setImageFile(new File([blob], run.filename || "result.png", { type: blob.type || "image/png" }));
  } catch (e) { showError(e.message); }
};

// ------------------------------------------------------------------ view switching
function setView(v) {
  state.view = v;
  for (const b of $("viewTabs").children) b.classList.toggle("active", b.dataset.view === v);
  $("maskView").hidden = v !== "mask";
  $("resultView").hidden = v !== "result";
  if (v === "mask") render();
}
$("viewTabs").addEventListener("click", (e) => { if (e.target.dataset.view) setView(e.target.dataset.view); });

// ------------------------------------------------------------------ wiring
function bindOutput(id, outId, fmt = (v) => v) {
  const upd = () => { $(outId).textContent = fmt($(id).value); };
  $(id).addEventListener("input", upd);
  upd();
}

function init() {
  loadForm();
  bindOutput("threshold", "thresholdOut");
  bindOutput("brushSize", "brushSizeOut", (v) => `${v}px`);
  bindOutput("opacity", "opacityOut", (v) => (+v).toFixed(2));
  for (const id of PERSIST) $(id).addEventListener("change", saveForm);
  for (const id of ["megapixels", "resolution"]) {
    $(id).addEventListener("input", () => { updateStale(); refreshSizeDebounced(); });
  }
  $("autofix").addEventListener("change", refreshSizeDebounced);
  $("opacity").addEventListener("input", render);
  $("runEdit").onclick = runEdit;
  $("maskText").addEventListener("keydown", (e) => { if (e.key === "Enter") computeMask(); });
  setMode("paint");
  renderHistory();
  loadModels();
  pollStatus();
  setInterval(pollStatus, 5000);
}
init();

function syncModeUi() { $("keepIdenticalRow").hidden = $("mode").value !== "paste"; }
$("mode").addEventListener("change", syncModeUi);
syncModeUi();
