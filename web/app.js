// Inpaint Studio frontend: Create view (mask + edit settings) and Runs view (queue, viewer, results).
// Vanilla ES module, no build step.

import { createSetup } from "/setup.js";
import { initPromptPresets } from "/promptpresets.js";

const $ = (id) => document.getElementById(id);

// ------------------------------------------------------------------ persisted form fields
const PERSIST = [
  "megapixels", "resolution", "autofix", "matchRef", "maskText", "threshold", "refine", "expand", "invert",
  "brushSize", "opacity", "prompt", "negative", "steps", "denoise", "feather", "mode", "keepNote", "postColors", "postWarp", "postPoisson", "saveEvery", "saveLast", "upscale", "upscaler", "seed",
  "randomSeed", "cfg", "sampler", "scheduler", "task", "preset", "quant", "aspect", "refNote",
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
  view: "create",             // "create" | "runs" (from the location hash)
  run: null,                  // current/last run object
  runs: [],
  task: "edit",               // "edit" | "generate"
  setup: null,                // last /api/setup report (presets, components)
  models: null,               // last /api/models report
  maskAvailable: true,        // false when SAM3 is not installed: the UI hides everything about masks
};

const display = $("display");
const dctx = display.getContext("2d");
const tint = document.createElement("canvas");
const tctx = tint.getContext("2d");
// mask outline: the eroded mask (ero) is cut out of the mask, the ring (edge) gets a dash pattern
const ero = document.createElement("canvas");
const ectx = ero.getContext("2d");
const edge = document.createElement("canvas");
const edgectx = edge.getContext("2d");
let dash = { k: 0, pattern: null };

// ------------------------------------------------------------------ helpers
let toastTimer = 0;
// Info toasts ("Queued as Run 3") slide in at the top centre, errors stay at the bottom right
function showToast(msg, { error = false, runsLink = false, ms = 6000 } = {}) {
  const t = $("toast");
  $("toastText").textContent = String(msg);
  t.classList.toggle("error", error);
  t.classList.toggle("top", !error);
  $("toastLink").hidden = !runsLink;
  t.classList.remove("enter", "leave");
  t.hidden = false;
  void t.offsetWidth;   // restart the animation when a toast replaces another one
  t.classList.add("enter");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(hideToast, ms);
}
function hideToast() {
  const t = $("toast");
  clearTimeout(toastTimer);
  if (t.hidden) return;
  t.classList.remove("enter");
  t.classList.add("leave");
  toastTimer = setTimeout(() => { t.hidden = true; t.classList.remove("leave"); }, 200);
}
const showError = (msg) => showToast(msg, { error: true, ms: 15000 });
$("toastLink").onclick = hideToast;
$("toastClose").onclick = hideToast;

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
      $("statusText").textContent = "ComfyUI running";
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
    state.models = m;
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
    fill("sampler", m.samplers, "euler");
    fill("scheduler", m.schedulers, "simple");
    if (!$("sampler").value && m.samplers.includes("euler")) $("sampler").value = "euler";
    // file overrides: the first option ("From preset") is the default and sends nothing
    for (const [id, list] of [["unet", m.unets], ["clip", m.clips], ["vae", m.vaes]]) {
      const sel = $(id);
      const keep = sel.value;
      sel.innerHTML = "";
      sel.add(new Option("From preset", ""));
      for (const v of list) sel.add(new Option(v, v));
      sel.value = list.includes(keep) ? keep : "";
    }
    updateOverrideLabels();
    saveForm();
  } catch (e) {
    showError(`Could not load models: ${e.message}`);
  }
}

// ------------------------------------------------------------------ size & safety
const num = (id) => parseFloat($(id).value);

// working size for a generate run: the aspect ratio stands in for the image size
const aspectDims = () => { const [w, h] = $("aspect").value.split(":").map(Number); return { w: w * 100, h: h * 100 }; };

async function refreshSize() {
  if (state.task === "generate") {
    const { w, h } = aspectDims();
    const body = { width: w, height: h, megapixels: num("megapixels"), resolution: parseInt($("resolution").value, 10) || 1024 };
    if (!(body.megapixels > 0)) return null;
    try {
      const rep = await postJson("/api/size", body);
      // no reference image: only the target size counts
      const ok = rep.target_tokens <= TOKEN_LIMIT;
      $("sizeInfo").innerHTML = `${rep.work_w} × ${rep.work_h}, ${rep.target_tokens} of ${TOKEN_LIMIT} tokens`
        + (ok ? "" : '<br><span class="badge bad">Gray-noise risk</span> Lower the megapixels.');
      setTokenBar(rep.target_tokens);
      state.genSize = rep;
      syncGenFrame();
      return rep;
    } catch (e) {
      showError(e.message);
      return null;
    }
  }
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
    if ($("matchRef").checked && rep.match_res && rep.match_res !== parseInt($("resolution").value, 10)) {
      $("resolution").value = rep.match_res;
      saveForm();
      rep = await postJson("/api/size", { ...body, megapixels: num("megapixels"), resolution: rep.match_res });
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

// "1184 × 784, 3626 of 4096 tokens" + a bar; the larger of target and reference counts
const TOKEN_LIMIT = 4096;
function renderSizeInfo(rep, applied) {
  const sg = rep.suggested || {};
  const tokens = Math.max(rep.target_tokens, rep.ref_tokens);
  let html = `${rep.work_w} × ${rep.work_h}, ${tokens} of ${TOKEN_LIMIT} tokens`;
  if (rep.ref_tokens !== rep.target_tokens) html += ` <span class="hint">(target ${rep.target_tokens}, reference ${rep.ref_tokens})</span>`;
  if (rep.safe && applied) html += `<br><span class="hint">Auto-fixed to ${sg.megapixels} MP, reference ${sg.resolution} px.</span>`;
  if (!rep.safe) {
    html += `<br><span class="badge bad">Gray-noise risk</span> ` +
      `<span class="hint">Suggested: ${sg.megapixels} MP, reference ${sg.resolution} px (${sg.work_w} × ${sg.work_h}).</span>`;
  }
  $("sizeInfo").innerHTML = html;
  setTokenBar(tokens);
  $("stageInfo").textContent = `${state.imageLabel || "Image"}, ${rep.work_w} × ${rep.work_h}`;
}
function setTokenBar(tokens) {
  const bar = $("tokenBar");
  bar.hidden = tokens == null;
  if (tokens == null) return;
  bar.classList.toggle("over", tokens > TOKEN_LIMIT);
  bar.firstElementChild.style.width = `${Math.min(100, (tokens / TOKEN_LIMIT) * 100)}%`;
}
function clearSizeInfo() {
  $("sizeInfo").textContent = "Load an image to see the working size.";
  setTokenBar(null);
  $("stageInfo").textContent = "";
}
// Generate: a dashed frame in the chosen aspect ratio stands in for the image
function syncGenFrame() {
  const [a, b] = $("aspect").value.split(":").map(Number);
  $("genShape").style.aspectRatio = `${a} / ${b}`;
  $("genShape").classList.toggle("tall", b > a);
  const g = state.genSize;
  if (state.task === "generate") $("stageInfo").textContent = g ? `New image, ${g.work_w} × ${g.work_h}` : "New image";
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
  if (state.mask && maskOn()) {
    if (tint.width !== state.mask.width || tint.height !== state.mask.height) {
      tint.width = state.mask.width; tint.height = state.mask.height;
    }
    tctx.globalCompositeOperation = "source-over";
    tctx.clearRect(0, 0, tint.width, tint.height);
    tctx.drawImage(state.mask, 0, 0);
    tctx.globalCompositeOperation = "source-in";
    tctx.fillStyle = "#000";
    tctx.fillRect(0, 0, tint.width, tint.height);
    dctx.globalAlpha = num("opacity");
    dctx.drawImage(tint, 0, 0, w, h);
    dctx.globalAlpha = 1;
    dctx.drawImage(maskOutline(), 0, 0, w, h);
  }
}

// White dashed outline of the mask, about 1.5 screen pixels wide
function maskOutline() {
  const m = state.mask, mw = m.width, mh = m.height;
  const k = Math.max(1, Math.round((1.5 * mw) / (display.clientWidth || mw)));
  for (const c of [ero, edge]) if (c.width !== mw || c.height !== mh) { c.width = mw; c.height = mh; }
  ectx.globalCompositeOperation = "copy";
  ectx.drawImage(m, 0, 0);
  ectx.globalCompositeOperation = "destination-in";   // erode: keep pixels whose neighbours are in the mask too
  for (const [dx, dy] of [[k, 0], [-k, 0], [0, k], [0, -k]]) ectx.drawImage(m, dx, dy);
  edgectx.globalCompositeOperation = "copy";
  edgectx.drawImage(m, 0, 0);
  edgectx.globalCompositeOperation = "destination-out";
  edgectx.drawImage(ero, 0, 0);
  if (dash.k !== k) {   // diagonal stripes read as dashes along any outline direction
    const p = document.createElement("canvas");
    p.width = p.height = 8 * k;
    const pc = p.getContext("2d");
    pc.fillStyle = "#fff";
    pc.beginPath(); pc.moveTo(0, 0); pc.lineTo(4 * k, 0); pc.lineTo(0, 4 * k); pc.closePath(); pc.fill();
    pc.beginPath(); pc.moveTo(8 * k, 4 * k); pc.lineTo(8 * k, 8 * k); pc.lineTo(4 * k, 8 * k); pc.closePath(); pc.fill();
    dash = { k, pattern: edgectx.createPattern(p, "repeat") };
  }
  edgectx.globalCompositeOperation = "source-in";
  edgectx.fillStyle = dash.pattern;
  edgectx.fillRect(0, 0, mw, mh);
  return edge;
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
  if (!maskOn() || state.mode === "off" || !state.imgEl || e.button !== 0) return;
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
const endStroke = () => { if (stroking) scheduleMaskSave(); stroking = false; last = null; };
display.addEventListener("pointerup", endStroke);
display.addEventListener("pointercancel", endStroke);
display.addEventListener("pointerleave", () => { $("brushCursor").hidden = true; });
display.addEventListener("pointerenter", moveCursor);

function moveCursor(e) {
  const cur = $("brushCursor");
  if (!maskOn() || state.mode === "off" || !state.imgEl) { cur.hidden = true; return; }
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
  display.classList.toggle("brush", mode !== "off" && maskOn());
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
  if (maskOn() && (e.metaKey || e.ctrlKey) && e.key === "z" && !typing) {
    e.preventDefault(); undo();
    return;
  }
  // in the Mask view, arrow keys switch between batch images
  if ((e.key === "ArrowLeft" || e.key === "ArrowRight") && !typing && state.view === "create" && state.batch.length) {
    e.preventDefault();
    const n = state.batch.length;
    const i = state.batchIdx < 0 ? 0 : (state.batchIdx + (e.key === "ArrowRight" ? 1 : -1) + n) % n;
    openBatchItem(i);
    return;
  }
  // arrow keys step through the visible filmstrip; past the last frame shows the final comparison
  if ((e.key === "ArrowLeft" || e.key === "ArrowRight") && !typing && state.view === "runs") {
    const run = state.run;
    const frames = run ? visibleFrames(run) : [];
    if (!frames.length) return;
    e.preventDefault();
    const last = frames.length - 1;
    const final = run.done && run.resultUrl;   // the finished result sits after the last frame
    let i = run.shown ?? (final ? last + 1 : last);
    i += e.key === "ArrowRight" ? 1 : -1;
    if (i > last) { if (final) showFinal(); else showFrame(last); return; }
    showFrame(Math.max(0, i));
    $("filmstrip").children[Math.max(0, i)]?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }
});

// ------------------------------------------------------------------ image upload
async function setImageFile(file) {
  if (!file || !file.type.startsWith("image/")) { showError("Please choose an image file."); return; }
  if (!ensureEditTask()) return;
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
    state.imageLabel = file.name || "image";
    $("imageInfo").textContent = `${file.name || "image"} – original ${up.width}×${up.height}`;
    saveSession({ imageName: up.name, srcW: up.width, srcH: up.height, label: file.name || "image", maskName: null, maskMeta: null });
    resetMask();
    state.size = null;
    renderBatch();
    await refreshSize();
    setView("create");
    render();
  } catch (e) {
    $("imageInfo").textContent = state.imageName ? $("imageInfo").textContent : "No image loaded.";
    showError(e.message);
  }
}
$("fileInput").addEventListener("change", (e) => { openFiles([...e.target.files]); e.target.value = ""; });
$("folderInput").addEventListener("change", (e) => { openFiles([...e.target.files]); e.target.value = ""; });
let dragDepth = 0;
window.addEventListener("dragenter", (e) => { e.preventDefault(); dragDepth++; document.body.classList.add("dragging"); });
window.addEventListener("dragleave", () => { if (--dragDepth <= 0) { dragDepth = 0; document.body.classList.remove("dragging"); } });
window.addEventListener("dragover", (e) => e.preventDefault());
window.addEventListener("drop", (e) => {
  e.preventDefault(); dragDepth = 0; document.body.classList.remove("dragging");
  const files = e.dataTransfer ? [...e.dataTransfer.files] : [];
  if (files.length) openFiles(files);
});

// ------------------------------------------------------------------ step 1: compute mask
async function computeMask() {
  if (!state.maskAvailable) return;
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
    applyMaskImage(im);
    $("maskTime").textContent = `Last: ${res.seconds}s`;
    scheduleMaskSave();
    updateStale();
    render();
  } catch (e) {
    showError(e.message);
  } finally {
    btn.disabled = false;
    $("maskSpinner").hidden = true;
  }
}

// black/white mask image (white = replace) -> mask canvas
function applyMaskImage(im, mp = num("megapixels")) {
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
    state.maskMeta = { w, h, mp };
}
$("computeMask").onclick = computeMask;

// Exports the mask as a strict black/white PNG at working size (white = replace).
function exportMaskBlob(src = state.mask) {
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
// Submitting never blocks for the whole run anymore: jobs go into the server queue.
function setSubmitting(on) {
  state.submitting = on;
  $("runEdit").disabled = on;
  $("runEdit").textContent = on ? "Adding to queue..." : runLabel();
}

const runLabel = () => (state.task === "generate" ? "Add image to queue" : "Add edit to queue");

// "Run N": runs are numbered by creation time over everything this page knows (saved + queued)
function runNumber(run) {
  const all = [...state.runs, ...state.jobs.values()].sort((a, b) => (a.created || 0) - (b.created || 0));
  return all.indexOf(run) + 1;
}
const currentFamily = () => presetById($("preset").value)?.family || "";

// edit parameters from the form for one image (seed is drawn per job when "Random" is on)
function editParams({ image, srcW, srcH, maskName, useMask, megapixels, resolution }) {
  if (!maskOn()) { useMask = false; maskName = null; }
  let seed = parseInt($("seed").value, 10) || 0;
  if ($("randomSeed").checked) { seed = Math.floor(Math.random() * 2 ** 32); $("seed").value = seed; }
  return {
    image, mask: maskName, use_mask: useMask, src_w: srcW, src_h: srcH,
    megapixels: megapixels ?? num("megapixels"), resolution: resolution ?? parseInt($("resolution").value, 10),
    prompt: $("prompt").value, negative: $("negative").value,
    steps: parseInt($("steps").value, 10), denoise: num("denoise"), seed, cfg: num("cfg"),
    sampler: $("sampler").value, scheduler: $("scheduler").value, feather: num("feather"), mode: maskOn() && currentFamily() !== "zimage" ? $("mode").value : "inpaint",
    // only an edited instruction is sent; otherwise the server adds its default (graphs.KEEP_IDENTICAL)
    keep_note: $("keepNote").value === KEEP_NOTE ? undefined : $("keepNote").value, save_every: parseInt($("saveEvery").value, 10) || 0,
    save_last: parseInt($("saveLast").value, 10) || 0,
    post_colors: $("postColors").checked, post_warp: $("postWarp").checked, post_poisson: $("postPoisson").checked,
    upscale: $("upscaler").value ? parseInt($("upscale").value, 10) || 0 : 0, upscaler: $("upscaler").value || null,
    preset: $("preset").value, quant: $("quant").value, task: state.task, preview_method: "auto",
    refs: state.refs.slice(0, maxRefs()).map((r) => r.name),
    ref_takes: state.refs.slice(0, maxRefs()).map((r) => (r.take || "").trim()),
    ref_crops: state.refs.slice(0, maxRefs()).map((r) => r.crop || null),
    // only an edited instruction is sent; otherwise the server uses its default (graphs.REF_NOTE)
    ref_note: isDefaultRefNote($("refNote").value) ? undefined : $("refNote").value,
    // explicit file overrides only; empty = taken from the preset by the server
    ...Object.fromEntries(["unet", "clip", "vae"].filter((id) => $(id).value).map((id) => [id, $(id).value])),
  };
}

async function uploadMaskBlob(blob) {
  const fd = new FormData();
  fd.append("file", blob, "mask.png");
  return (await api("/api/upload-mask", { method: "POST", body: fd })).name;
}

async function runEdit({ thenNext = false } = {}) {
  if (state.submitting) return;
  const generate = state.task === "generate";
  if (!generate && !state.imageName) { showError("Load an image first."); return; }
  if (!presetById($("preset").value)) { showError("No model installed. Open Downloads to get one."); return; }
  const useMask = !generate && maskOn();
  setSubmitting(true);
  try {
    const rep = await refreshSize();
    if (!rep) return;
    if (generate) {
      saveForm();
      const { w, h } = aspectDims();
      await submitJob(editParams({ image: null, srcW: w, srcH: h, maskName: null, useMask: false }));
      return;
    }
    let maskName = null;
    if (useMask) {
      if (!state.hasMask) throw new Error("No mask yet. Compute or paint a mask, or turn off \"Use mask\".");
      if (isStale()) throw new Error("Size changed – recompute the mask first.");
      const { blob, white } = await exportMaskBlob();
      if (!white) throw new Error("The mask is empty. Paint or compute a mask, or turn off \"Use mask\".");
      maskName = await uploadMaskBlob(blob);
    }
    saveForm();
    await submitJob(editParams({ image: state.imageName, srcW: state.srcW, srcH: state.srcH, maskName, useMask }));
    const item = currentBatchItem();
    if (item) { item.status = "queued"; renderBatch(); if (thenNext) openNextBatchItem(); }
  } catch (e) {
    showError(e.message);
  } finally {
    setSubmitting(false);
  }
}

// ------------------------------------------------------------------ job queue (server-side)
state.jobs = new Map();   // job_id -> run-like object while queued/running
state.follow = true;      // viewer follows the running job until the user picks something else
state.resultFilter = "all";

function jobFromSummary(sum) {
  let job = state.jobs.get(sum.job_id);
  if (!job) {
    job = { id: sum.job_id, serverId: sum.job_id, prompt: sum.prompt, seed: sum.seed, steps: sum.steps,
      max: sum.steps, value: 0, frames: [], done: false, status: "queued", t0: null, created: sum.created };
    state.jobs.set(sum.job_id, job);
  }
  job.status = sum.status || job.status;
  job.value = sum.value || job.value;
  if (sum.phase) job.phase = sum.phase;
  if (sum.decode_steps) job.decodeSteps = sum.decode_steps;
  if (sum.size) job.size = sum.size;
  if (sum.frames) job.frames = sum.frames.map((f) => ({ ...f }));
  if (job.status === "running" && !job.t0) job.t0 = performance.now();
  return job;
}

async function submitJob(params) {
  const sum = await postJson("/api/jobs", params);
  const job = jobFromSummary(sum);
  job.params = params;   // known only to the page that queued it ("Load settings in Create")
  renderQueue();
  showToast(`Queued as Run ${runNumber(job)}`, { runsLink: true });
  // show it right away if nothing else is running, otherwise it just waits in the queue
  if (state.follow && ![...state.jobs.values()].some((j) => j !== job && j.status === "running")) viewJob(job);
}

// Viewer on a queued or running job; a progress timer keeps the time estimate fresh
function viewJob(job) {
  if (state.run !== job) freeRunFrames(state.run);
  state.run = job;
  job.shown = null;
  clearInterval(state.progressTimer);
  state.progressTimer = setInterval(updateProgressText, 500);
  renderViewer();
}

// Viewer on a saved (finished or failed) run
function showRun(run) {
  if (state.run !== run) freeRunFrames(state.run);
  clearInterval(state.progressTimer);
  state.run = run;
  run.shown = null;
  $("alignPanel").hidden = true;
  renderViewer();
  if (run.rawUrl && run.maskUrl && !run.match) measureMatch(run);
}

function addJobFrame(job, frame) {
  if (job.frames.some((f) => f.url === frame.url)) return;
  if (frame.kind === "saved") job.frames = job.frames.filter((f) => !(f.kind === "live" && f.step === frame.step));
  frameAdded(job, frame);
}

function jobFinished(m) {
  const job = state.jobs.get(m.job_id);
  state.jobs.delete(m.job_id);
  const viewing = job && state.run === job;
  if ((m.type === "done" || m.type === "error") && m.run) {
    // failed runs stay in the results for this session (the server lists only finished ones)
    const run = runFromStored(m.run);
    if (job?.params) run.params = { ...job.params, ...run.params };
    if (!state.runs.some((r) => r.serverId === run.serverId)) state.runs.unshift(run);
    if (viewing) { if (job.t0) run.took = (performance.now() - job.t0) / 1000; showRun(run); }
  } else if (viewing) {
    job.status = "cancelled"; job.done = true;
    clearInterval(state.progressTimer);
  }
  if (m.type === "error") showError(m.run?.error || "Run failed");
  renderQueue();
  renderHistory();
  if (viewing && m.type === "cancelled") renderViewer();
  // keep following: switch to the next running job
  const next = [...state.jobs.values()].find((j) => j.status === "running");
  if (next && state.follow && viewing) viewJob(next);
}

function connectJobs() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/jobs`);
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    switch (m.type) {
      case "snapshot": {
        state.jobs.clear();
        for (const sum of m.jobs) jobFromSummary(sum);
        renderQueue();
        const running = [...state.jobs.values()].find((j) => j.status === "running");
        const wanted = state.wantRun && findRun(state.wantRun);
        if (wanted) { state.wantRun = null; selectRun(wanted); }
        else if (running && state.follow) viewJob(running);
        else if (state.run && !state.run.done && !state.jobs.has(state.run.id)) showLatest();  // finished while offline
        break;
      }
      case "queued": jobFromSummary(m.job); renderQueue(); break;
      case "running": {
        const job = state.jobs.get(m.job_id);
        if (!job) break;
        job.status = "running"; job.t0 = performance.now();
        renderQueue();
        const viewingFinished = !state.run || state.run.done || !state.jobs.has(state.run.id);
        if (state.follow && (state.run === job || viewingFinished)) viewJob(job);
        else if (state.run === job) renderViewer();
        break;
      }
      case "progress": {
        const job = state.jobs.get(m.job_id);
        if (!job) break;
        noteProgress(job, m.value);
        job.value = m.value; job.max = m.max;
        if (state.run === job) { renderPipeline(job); updateProgressText(); }
        renderQueue();
        break;
      }
      case "node": {   // the ComfyUI node that runs now (graphs.node_phase)
        const job = state.jobs.get(m.job_id);
        if (!job) break;
        notePhase(job, m.phase);
        job.phase = { phase: m.phase, detail: m.detail };
        if (state.run === job) { renderPipeline(job); updateProgressText(); }
        renderQueue();
        break;
      }
      case "frame": { const job = state.jobs.get(m.job_id); if (job) addJobFrame(job, m.frame); break; }
      case "done": case "error": case "cancelled": jobFinished(m); break;
    }
  };
  ws.onclose = () => setTimeout(connectJobs, 2000); // server restart / sleep -> reconnect
}

// "Step 12 of 20, about 40 s left". ComfyUI reports a step when it is done, so while sampling
// the step being computed is value + 1; the first sampler node also loads the diffusion model.
function stepText(job) {
  if (job.status !== "running") return "Waiting";
  const ph = job.phase;
  if (!ph) return "Starting…";
  if (ph.phase !== "sample") return PHASE_NAMES[ph.phase] + (ph.detail ? ` · ${ph.detail}` : "");
  if (!job.value) return `Loading the model, then step 1 of ${job.max}`;
  let txt = `Step ${Math.min(job.value + 1, job.max)} of ${job.max}`;
  const eta = stepEta(job);
  if (eta != null) txt += eta > 0 ? `, about ${fmtTime(eta)} left` : ", finishing…";
  return txt;
}

const jobThumb = (job) => followFrame(job)?.url || (job.params?.image ? inputViewUrl(job.params.image) : null);

function renderQueue() {
  const box = $("queue");
  const jobs = [...state.jobs.values()].sort((a, b) => a.created - b.created);
  $("queueEmpty").hidden = !!jobs.length;
  box.innerHTML = "";
  let pos = 0;
  for (const job of jobs) {
    const card = document.createElement("div");
    card.className = "qcard" + (job === state.run ? " active" : "") + (job.status === "running" ? " running" : "")
      + (job.cancelling ? " cancelling" : "");
    const main = document.createElement("button");
    main.className = "qmain";
    const thumb = document.createElement("span"); thumb.className = "qthumb";
    const src = jobThumb(job);
    if (src) { const img = document.createElement("img"); img.src = src; img.alt = ""; thumb.append(img); }
    const txt = document.createElement("span"); txt.className = "qtext";
    const t = document.createElement("b"); t.textContent = `Run ${runNumber(job)}`;
    const pr = document.createElement("span"); pr.className = "qprompt"; pr.textContent = job.prompt;
    txt.append(t, pr);
    main.append(thumb, txt);
    main.onclick = () => selectRun(job);
    card.append(main);
    if (job.status === "running") {
      const bar = document.createElement("div"); bar.className = "progress";
      const fill = document.createElement("div"); fill.className = "bar";
      fill.style.width = `${job.max ? (job.value / job.max) * 100 : 0}%`;
      bar.append(fill);
      const st = document.createElement("span"); st.className = "qstep"; st.textContent = job.cancelling ? "Cancelling…" : stepText(job);
      card.append(bar, st);
    } else {
      const row = document.createElement("div"); row.className = "row";
      const p = document.createElement("span"); p.className = "hint grow"; p.textContent = `Position ${++pos}`;
      const rm = document.createElement("button"); rm.className = "small" + (job.cancelling ? " busy" : "");
      rm.textContent = job.cancelling ? "Removing…" : "Remove"; rm.disabled = !!job.cancelling;
      rm.setAttribute("aria-label", `Remove Run ${runNumber(job)} from queue`);
      rm.onclick = () => cancelJob(job);
      row.append(p, rm);
      card.append(row);
    }
    box.appendChild(card);
  }
  renderQueueLabel();
}

// black badge on the Runs switch: running + waiting jobs
function renderQueueLabel() {
  const n = state.jobs ? state.jobs.size : 0;
  $("runsBadge").hidden = !n;
  $("runsBadge").textContent = `${n} active`;
  $("cancelEdit").hidden = !n;
}

// Cancelling takes a moment (ComfyUI stops at the end of the current step): until the job is gone
// the card, the viewer and the button show it with an animation (job.cancelling).
async function cancelJob(job) {
  job.cancelling = true;
  renderQueue();
  if (state.run === job) renderViewer();
  try { await postJson(`/api/jobs/${encodeURIComponent(job.id)}/cancel`, {}); } catch (e) {
    job.cancelling = false;
    renderQueue();
    if (state.run === job) renderViewer();
    showError(e.message);
  }
}

// Workflow strip above the image: the current phase is marked, earlier ones are done
const PHASES = ["load", "encode", "sample", "decode", "save"];
const PHASE_NAMES = { load: "Load", encode: "Text encoder", sample: "Sampling", decode: "VAE decode", save: "Save" };
// "Step 3 of 7" under the image. ComfyUI reports a step when it is done, so while sampling the step
// being computed is value + 1; the first sampler node also loads the diffusion model.
function stepCount(run) {
  if (run.phase?.phase === "sample") {
    return run.value ? `Step ${Math.min(run.value + 1, run.max)} of ${run.max}` : `Loading the model, then step 1 of ${run.max}`;
  }
  return run.value ? `Step ${run.value} of ${run.max}` : "";
}
function renderPipeline(run) {
  const live = !!(run && !run.done && state.jobs.has(run.id) && run.status === "running");
  $("pipeline").hidden = !live;
  if (!live) return;
  const cur = PHASES.indexOf(run.phase?.phase);
  for (const li of $("pipeline").children) {
    const i = PHASES.indexOf(li.dataset.phase);
    li.className = i === cur ? "now" : i < cur ? "done" : "";
  }
}

function updateProgressText() {
  const run = state.run;
  if (!run || run.done || !state.jobs.has(run.id)) return;
  if (run.status === "queued") {
    const ahead = [...state.jobs.values()].filter((j) => j.created < run.created).length;
    $("progressBar").style.width = "0%";
    $("progressText").textContent = `Waiting. ${ahead} run${ahead === 1 ? "" : "s"} ahead of this one.`;
    return;
  }
  const pct = run.max ? (run.value / run.max) * 100 : 0;
  $("progressBar").style.width = `${pct}%`;
  const el = (performance.now() - (run.t0 || performance.now())) / 1000;
  // the strip above the image says which phase runs; here the step and the times
  const eta = stepEta(run);
  const steps = stepCount(run);
  $("progressText").textContent = (steps ? `${steps} · ` : "") + `${fmtTime(el)} elapsed`
    + (eta > 0 ? ` · about ${fmtTime(eta)} left` : "");
  $("detSettings").querySelector("[data-k=time]")?.replaceChildren(fmtTime(el));
}

// Remaining time from measured step durations (median of the last few steps), counting down
// within the current step. Model loading and text encoding before step 1 are not part of it.
// Time estimate from measured durations: pure sampling steps (from the sampler node start or the last
// step report; the first chunk is skipped, it includes loading the model) and VAE decodes with saving
// (from the decode node to the next sampler node). Remaining = steps left × median step + decodes left ×
// median decode, minus what the current step or decode already took.
function noteProgress(job, value) {
  const now = performance.now();
  if (value > job.value && job.markT != null) (job.stepDur ||= []).push((now - job.markT) / (value - job.value));
  if (value > job.value) job.markT = now;
}
function notePhase(job, phase) {
  const now = performance.now(), prev = job.phase?.phase;
  if (phase === "decode" && prev !== "decode" && prev !== "save") job.decT0 = now;
  if (phase === "sample" && prev !== "sample") {
    if (job.decT0 != null) { (job.decDur ||= []).push(now - job.decT0); job.decT0 = null; }
    job.markT = job.value ? now : null;   // value 0: this chunk loads the model first, not a step time
  }
}
const median = (a) => { const s = [...a].slice(-6).sort((x, y) => x - y); return s[Math.floor(s.length / 2)]; };
function stepEta(run) {
  if (!run.stepDur?.length) return null;
  const now = performance.now(), step = median(run.stepDur);
  const decodes = (run.decodeSteps || [run.max]).filter((s) => s > run.value).length
    + (run.phase?.phase === "decode" || run.phase?.phase === "save" ? 1 : 0);
  const dec = run.decDur?.length ? median(run.decDur) : step / 2;   // until the first decode is measured
  let ms = (run.max - run.value) * step + decodes * dec;
  if (run.phase?.phase === "sample" && run.markT != null) ms -= Math.min(step, now - run.markT);
  if (run.decT0 != null) ms -= Math.min(dec, now - run.decT0);
  return Math.max(0, ms / 1000);
}

function frameAdded(run, frame) {
  run.frames.push(frame);
  if (state.run !== run) { renderQueue(); return; }
  const vis = visibleFrames(run);
  const idx = vis.indexOf(frame);
  renderSteps();
  if (!run.done && frame === followFrame(run)) showFollowFrame(run);
}

// What a running job shows: live previews only until the first saved step exists, then the newest
// saved step until the next one arrives (live previews in between are only in the steps strip)
function followFrame(run) {
  const wantRaw = $("viewRaw").checked;
  const saved = run.frames.filter((f) => f.kind === "saved" && (f.variant === "raw") === (wantRaw && hasRaw(run)));
  const pool = saved.length ? saved : run.frames.some((f) => f.kind === "saved") ? run.frames.filter((f) => f.kind === "saved")
    : run.frames.filter((f) => f.kind === "live");
  return pool.reduce((a, f) => (!a || f.step >= a.step ? f : a), null);
}
function showFollowFrame(run) {
  const f = followFrame(run);
  if (!f) return false;
  const i = visibleFrames(run).indexOf(f);
  if (i >= 0) { showFrame(i); return true; }
  $("liveImg").src = f.url;
  $("liveImg").hidden = false;
  $("compare").hidden = true;
  $("resultEmpty").hidden = true;
  run.shown = null;
  for (const img of $("filmstrip").children) img.classList.remove("active");
  return true;
}

// The steps strip: saved steps only (raw ones with "Raw" on). Live previews never appear here,
// the viewer shows them only until the first saved step exists (see followFrame).
function visibleFrames(run) {
  const wantRaw = $("viewRaw").checked;
  const byStep = new Map();
  for (const f of run.frames) {
    const slot = byStep.get(f.step) || {};
    if (f.kind === "live") slot.live = f;
    else if (f.variant === "raw") slot.raw = f;
    else slot.result = f;
    byStep.set(f.step, slot);
  }
  const out = [];
  for (const step of [...byStep.keys()].sort((a, b) => a - b)) {
    const s = byStep.get(step);
    const pick = (wantRaw && s.raw) || s.result || null;
    if (pick) out.push(pick);
  }
  return out;
}

const hasRaw = (run) => !!(run && (run.rawUrl || run.frames.some((f) => f.variant === "raw")));

// one step frame in the viewer
function showFrame(i) {
  const run = state.run;
  const f = run && visibleFrames(run)[i];
  if (!f) return;
  $("liveImg").src = f.url;
  $("liveImg").hidden = false;
  $("compare").hidden = true;
  $("resultEmpty").hidden = true;
  run.shown = i;
  for (const [j, img] of [...$("filmstrip").children].entries()) img.classList.toggle("active", j === i);
}

// the finished result: compare slider (edits, "Compare with original" on) or the image alone
function showFinal() {
  const run = state.run;
  if (!run || !run.resultUrl) return;
  run.shown = null;
  for (const img of $("filmstrip").children) img.classList.remove("active");
  $("resultEmpty").hidden = true;
  const after = $("viewRaw").checked && run.rawUrl ? run.rawUrl : (run.aligned?.url || run.resultUrl);
  if (!run.beforeUrl || !$("compareToggle").checked) {
    $("liveImg").src = after;
    $("liveImg").hidden = false;
    $("compare").hidden = true;
    return;
  }
  $("cmpBefore").src = run.beforeUrl;
  $("cmpAfter").src = after;
  $("liveImg").hidden = true;
  $("compare").hidden = false;
  setDivider(50);
}

function renderSteps() {
  const fs = $("filmstrip");
  fs.innerHTML = "";
  const run = state.run;
  const frames = run ? visibleFrames(run) : [];
  $("stepsBar").hidden = !frames.length;
  frames.forEach((f, i) => {
    const img = document.createElement("img");
    img.src = f.url;
    img.alt = `Step ${f.step}`;
    img.title = f.variant === "raw" ? `Step ${f.step}, raw full image` : `Step ${f.step}`;
    if (f.kind === "saved") img.classList.add("saved");
    if (f.variant === "raw") img.classList.add("raw");
    img.onclick = () => showFrame(i);
    if (i === run.shown) img.classList.add("active");
    fs.appendChild(img);
  });
  if (run && !run.done) fs.scrollLeft = fs.scrollWidth;
}

$("viewRaw").addEventListener("change", () => {
  const run = state.run;
  if (!run) return;
  renderSteps();
  syncRawSeg();
  if (run.resultUrl) showFinal(); else showFollowFrame(run);
});
// "Pasted result / Raw" is a view of the hidden #viewRaw checkbox
function syncRawSeg() {
  for (const b of $("rawSeg").children) b.setAttribute("aria-pressed", String((b.dataset.raw === "1") === $("viewRaw").checked));
}
$("rawSeg").addEventListener("click", (e) => {
  const b = e.target.closest("[data-raw]");
  if (!b || (b.dataset.raw === "1") === $("viewRaw").checked) return;
  $("viewRaw").checked = b.dataset.raw === "1";
  $("viewRaw").dispatchEvent(new Event("change"));
});
$("compareToggle").addEventListener("change", () => { if (state.run?.done) showFinal(); });

function freeRunFrames(run) {
  if (!run || state.runs.some((r) => r === run)) return;
  for (const f of run.frames) if (f.url.startsWith("blob:")) URL.revokeObjectURL(f.url);
}

// ------------------------------------------------------------------ viewer + details for the selected run
const runStatus = (run) => (state.jobs.has(run.id) ? run.status : run.status || "done");
const runTask = (run) => run.task || run.params?.task || "edit";

function relTime(sec) {
  if (!sec) return "";
  const d = Date.now() / 1000 - sec;
  if (d < 60) return "just now";
  if (d < 3600) return `${Math.floor(d / 60)} min ago`;
  if (d < 86400) return `${Math.floor(d / 3600)} h ago`;
  return new Date(sec * 1000).toLocaleDateString();
}

function errorAdvice(msg) {
  const m = String(msg || "");
  if (/4096|token|gray/i.test(m)) return "Lower the megapixels or turn on Auto-fix size, then run again.";
  if (/restarted/i.test(m)) return "Load the settings in Create and queue it again.";
  return "Check the settings, then use Load settings in Create to try again.";
}

function renderViewer() {
  const run = state.run;
  const status = run ? runStatus(run) : "none";
  const done = status === "done";
  const n = run ? runNumber(run) : 0;
  $("runTitle").textContent = run ? (n ? `Run ${n}` : "Run") : (state.runs.length ? "No run selected" : "No runs yet");
  $("runStatus").textContent = { queued: "Waiting", running: "Running", done: `Finished ${relTime(run?.finished || run?.created)}`,
    error: "Failed", cancelled: "Cancelled", none: "" }[status] ?? status;
  $("runStatus").className = status === "running" || status === "error" ? "strong" : "hint";
  const cancelling = !!(run?.cancelling && (status === "running" || status === "queued"));
  if (cancelling) { $("runStatus").textContent = "Cancelling…"; $("runStatus").className = "strong cancelling-text"; }
  $("bigArea").classList.toggle("cancelling", cancelling);
  $("runFile").textContent = run?.filename || "";
  // toolbar above the image
  $("rawSeg").hidden = !(run && hasRaw(run));
  syncRawSeg();
  $("compareRow").hidden = !(done && run.beforeUrl);
  $("viewerTools").hidden = $("rawSeg").hidden && $("compareRow").hidden;
  // image area by status
  const msg = (text) => {
    $("liveImg").hidden = true; $("compare").hidden = true;
    $("resultEmpty").hidden = false; $("resultEmpty").textContent = text;
  };
  $("progressWrap").hidden = !(status === "queued" || status === "running");
  renderPipeline(run);
  renderSteps();
  if (!run) {
    $("resultEmpty").hidden = false;
    $("resultEmpty").innerHTML = 'No runs yet. Add one in <a href="#create">Create</a>.';
    $("liveImg").hidden = true; $("compare").hidden = true;
  } else if (status === "queued") {
    const ahead = [...state.jobs.values()].filter((j) => j.created < run.created).length;
    msg(`Waiting. ${ahead ? `${ahead} run${ahead === 1 ? "" : "s"} ahead of this one.` : "Starts next."}`);
  } else if (status === "running") {
    if (run.shown != null && visibleFrames(run)[run.shown]) showFrame(run.shown);
    else if (!showFollowFrame(run)) msg("Starting. The first preview appears after the first step.");
  } else if (done) {
    if (run.resultUrl) showFinal(); else msg("This run has no result image.");
  } else if (status === "error") {
    msg(`${run.error || "The run failed."} ${errorAdvice(run.error)}`);
  } else {
    msg("Cancelled.");
  }
  updateProgressText();
  renderDetails(run, status);
  renderQueue();
  renderHistory();
  syncRunHash();
}

function settingsRows(run) {
  const p = run.params || {};
  const task = p.task || run.task;   // unknown for jobs queued elsewhere
  const area = !p.use_mask ? "whole image" : p.mode === "paste" ? "free edit + paste" : "inpaint";
  const preset = presetById(p.preset);
  const size = run.size || {};
  const rows = [
    ["Run ID", run.serverId || run.id],   // the folder name in the data dir, e.g. to point Claude at a run
    ["Task", !task ? "" : task === "generate" ? "Generate" : p.mode || p.use_mask != null ? `Edit, ${area}` : "Edit"],
    ["Model", preset ? `${preset.title}${p.quant ? ` · ${p.quant}` : ""}` : p.unet || ""],
    ["Size", size.work_w ? `${size.work_w} × ${size.work_h}` : ""],
    ["Steps", p.steps ?? run.steps ?? ""],
    ["Seed", p.seed ?? run.seed ?? ""],
    ["CFG", p.cfg ?? ""],
  ];
  if (task !== "generate" && p.denoise != null && p.denoise !== 1) rows.push(["Denoise", p.denoise]);
  if (p.upscale > 1) rows.push(["Upscale", `${p.upscale}×`]);
  const took = run.took || (run.finished && run.started ? run.finished - run.started : run.finished && run.created ? run.finished - run.created : null);
  rows.push(["Time", took ? fmtTime(took) : runStatus(run) === "running" ? "…" : ""]);
  return rows.filter(([, v]) => v !== "" && v != null);
}

function renderDetails(run, status) {
  $("detEmpty").hidden = !!run;
  $("detBody").hidden = !run;
  if (!run) return;
  const done = status === "done";
  $("detPrompt").textContent = run.prompt || run.params?.prompt || "";
  const refs = run.params?.refs || [];
  $("detRefsSec").hidden = !refs.length;
  $("detRefs").replaceChildren(...refs.map((name, i) => {
    const row = document.createElement("div"); row.className = "ref-row";
    const crop = run.params?.ref_crops?.[i] || null;
    const t = document.createElement("span"); t.className = "batch-item" + (crop ? " cropped" : "");
    const img = document.createElement("img"); img.alt = ""; refThumb(img, name, crop);
    const tag = document.createElement("span"); tag.className = "tag"; tag.textContent = String(runTask(run) === "generate" ? i + 1 : i + 2);
    t.append(img, tag);
    const take = document.createElement("span"); take.className = "ref-take";
    take.textContent = run.params?.ref_takes?.[i] ? `Take: ${run.params.ref_takes[i]}` : "";
    row.append(t, take);
    return row;
  }));
  const dl = $("detSettings");
  dl.innerHTML = "";
  for (const [k, v] of settingsRows(run)) {
    const dt = document.createElement("dt"); dt.textContent = k;
    const dd = document.createElement("dd"); dd.textContent = String(v);
    if (k === "Time") dd.dataset.k = "time";
    if (k === "Run ID") dd.className = "runid";
    dl.append(dt, dd);
  }
  $("loadSettings").disabled = !run.params;
  $("loadSettings").title = run.params ? "Switch to Create with this run's settings (the image stays)"
    : "The settings of this run are not known to this page (it was queued elsewhere)";
  $("useResult").hidden = !(done && run.resultUrl);
  $("alignBtn").hidden = !(done && run.serverId && run.rawUrl && run.maskUrl);
  $("downloadBtn").hidden = !(done && run.resultUrl);
  if (done && run.resultUrl) {
    $("downloadBtn").href = run.aligned?.url || run.resultUrl;
    $("downloadBtn").download = run.filename || "result.png";
  }
  $("downloadUpscaled").hidden = !(done && run.upscaledUrl);
  if (done && run.upscaledUrl) {
    $("downloadUpscaled").href = run.upscaledUrl;
    $("downloadUpscaled").textContent = `Download upscaled (${run.upscale}×)`;
    $("downloadUpscaled").download = (run.filename || "result.png").replace(/\.png$/, `_x${run.upscale}.png`);
  }
  $("downloadSteps").hidden = !visibleFrames(run).length;
  $("cancelRun").hidden = status !== "running";
  $("removeRun").hidden = status !== "queued";
  for (const id of ["cancelRun", "removeRun"]) {
    $(id).disabled = !!run.cancelling;
    $(id).classList.toggle("busy", !!run.cancelling);
  }
  $("cancelRun").textContent = run.cancelling ? "Cancelling…" : "Cancel run";
  $("removeRun").textContent = run.cancelling ? "Removing…" : "Remove from queue";
  $("deleteRun").hidden = !(done || status === "error");
  $("matchInfo").textContent = run.match || "";
}

$("cancelRun").onclick = () => { if (state.run && state.jobs.has(state.run.id)) cancelJob(state.run); };
$("removeRun").onclick = $("cancelRun").onclick;
$("deleteRun").onclick = () => { if (state.run) deleteRun(state.run); };
$("loadSettings").onclick = () => { if (state.run?.params) loadRunSettings(state.run); };

// ------------------------------------------------------------------ persisted run history
function runFromStored(r) {
  return {
    id: r.id, serverId: r.id, prompt: r.params?.prompt || "", seed: r.params?.seed, steps: r.params?.steps,
    frames: (r.frames || []).map((f) => ({ ...f })), resultUrl: r.result_url, beforeUrl: r.before_url,
    rawUrl: r.raw_url || null, maskUrl: r.mask_url || null, filename: r.filename, done: true,
    upscaledUrl: r.upscaled_url || null, upscale: r.params?.upscale || 0,
    aligned: r.aligned || null, task: r.params?.task || "edit", preset: r.params?.preset || null,
    value: r.params?.steps, max: r.params?.steps, created: r.created, finished: r.finished || null,
    status: r.status || "done", error: r.error || null, params: r.params || null, size: r.size || null,
  };
}

async function loadStoredRuns() {
  try {
    const runs = await api("/api/runs");
    state.runs = runs.map(runFromStored);
    renderHistory();
  } catch (e) { showError(`Could not load run history: ${e.message}`); }
}

// the viewer falls back to the latest result (or the empty state)
function showLatest() {
  const run = state.runs.find((r) => r.status === "done") || state.runs[0];
  if (run) showRun(run);
  else { clearInterval(state.progressTimer); state.run = null; renderViewer(); }
}

async function deleteRun(run) {
  if (!confirm(`Delete Run ${runNumber(run)} and its files?`)) return;
  try {
    if (run.serverId) await api(`/api/runs/${encodeURIComponent(run.serverId)}`, { method: "DELETE" });
    state.runs = state.runs.filter((r) => r !== run);
    if (state.run === run) { state.follow = false; showLatest(); } else renderHistory();
  } catch (e) { showError(e.message); }
}

$("cancelEdit").onclick = () => {
  const job = (state.run && state.jobs.get(state.run.id)) || [...state.jobs.values()].find((j) => j.status === "running");
  if (job) cancelJob(job); else showError("No queued or running run.");
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
    const verdict = diff < 8 ? "very close" : diff < 16 ? "close" : diff < 28 ? "noticeably different" : "different, paste may not line up";
    run.match = `Outside-mask difference: ${diff.toFixed(1)} / 255 (${verdict})`;
    if (state.run === run) $("matchInfo").textContent = run.match;
  } catch { /* measurement is optional */ }
}

function setDivider(pct) {
  pct = Math.max(0, Math.min(100, pct));
  $("cmpDivider").style.left = `${pct}%`;
  $("cmpAfter").style.clipPath = `inset(0 0 0 ${pct}%)`;
}
{
  const cmp = $("compare");
  let drag = false;
  const move = (e) => { const r = cmp.getBoundingClientRect(); setDivider((e.clientX - r.left) / r.width * 100); };
  // preventDefault: no text/image selection highlight while dragging the divider
  cmp.addEventListener("pointerdown", (e) => { e.preventDefault(); drag = true; cmp.setPointerCapture(e.pointerId); move(e); });
  cmp.addEventListener("dragstart", (e) => e.preventDefault());
  cmp.addEventListener("pointermove", (e) => { if (drag) move(e); });
  cmp.addEventListener("pointerup", () => { drag = false; });
  cmp.addEventListener("pointercancel", () => { drag = false; });
}

// Results: finished and failed runs as tiles, filtered by task
function renderHistory() {
  const box = $("history");
  box.innerHTML = "";
  const f = state.resultFilter;
  const list = state.runs.filter((r) => f === "all" || runTask(r) === f);
  $("resultCount").textContent = `${list.length} result${list.length === 1 ? "" : "s"}`;
  if (!list.length) {
    box.innerHTML = `<div class="hint">${state.runs.length ? "No results for this filter." : "No results yet."}</div>`;
    return;
  }
  for (const run of list) {
    const b = document.createElement("button");
    b.className = "rtile" + (run === state.run ? " active" : "") + (run.status === "error" ? " failed" : "");
    const n = runNumber(run);
    b.setAttribute("aria-label", `Open Run ${n}`);
    const pic = document.createElement("span"); pic.className = "rpic";
    if (run.size?.work_w) pic.style.aspectRatio = `${run.size.work_w} / ${run.size.work_h}`;
    if (run.resultUrl) { const img = document.createElement("img"); img.src = run.resultUrl; img.alt = ""; img.loading = "lazy"; pic.append(img); }
    const meta = document.createElement("span"); meta.className = "rmeta";
    const l = document.createElement("span"); l.textContent = `Run ${n}`;
    const st = document.createElement("span"); st.className = run.status === "error" ? "strong" : "hint";
    st.textContent = run.status === "error" ? "Failed" : relTime(run.finished || run.created);
    meta.append(l, st);
    b.append(pic, meta);
    b.title = run.prompt;
    b.onclick = () => selectRun(run);
    // × removes the run from the history only; its files stay (a sibling: buttons cannot nest)
    const x = document.createElement("button");
    x.className = "rtile-x"; x.textContent = "×";
    x.title = "Remove from history (files are kept)";
    x.setAttribute("aria-label", `Remove Run ${n} from history`);
    x.onclick = () => hideRun(run);
    const wrap = document.createElement("div"); wrap.className = "rwrap";
    wrap.append(b, x);
    box.appendChild(wrap);
  }
}
async function hideRun(run) {
  try {
    await postJson(`/api/runs/${encodeURIComponent(run.serverId)}/hide`, {});
    state.runs = state.runs.filter((r) => r !== run);
    if (state.run === run) { state.follow = false; showLatest(); } else renderHistory();
  } catch (e) { showError(e.message); }
}

$("resultFilter").addEventListener("click", (e) => {
  const b = e.target.closest("[data-filter]");
  if (!b) return;
  state.resultFilter = b.dataset.filter;
  for (const x of $("resultFilter").children) x.setAttribute("aria-pressed", String(x === b));
  renderHistory();
});

$("downloadSteps").onclick = async () => {
  const run = state.run;
  if (!run || !visibleFrames(run).length) { showError("No step frames to download."); return; }
  const list = visibleFrames(run); // exactly what the viewer currently shows
  for (const [i, f] of list.entries()) {
    const a = document.createElement("a");
    a.href = f.url;
    a.download = f.kind === "saved" ? `${f.variant === "raw" ? "raw-" : ""}step-${String(f.step).padStart(3, "0")}.png`
      : `preview-${String(i + 1).padStart(2, "0")}.${f.mime.includes("png") ? "png" : "jpg"}`;
    document.body.appendChild(a); a.click(); a.remove();
    await new Promise((r) => setTimeout(r, 150));
  }
};

$("useResult").onclick = async () => {
  const run = state.run;
  if (!run || !run.resultUrl) return;
  try {
    const blob = await (await fetch(run.aligned?.url || run.resultUrl)).blob();
    if (!ensureEditTask()) return;
    await setImageFile(new File([blob], run.filename || "result.png", { type: blob.type || "image/png" }));
    setView("create");
  } catch (e) { showError(e.message); }
};

// "Load settings in Create": the run's parameters back into the form (the current image stays)
function loadRunSettings(run) {
  const p = run.params;
  const task = p.task === "generate" ? "generate" : "edit";
  if (task !== state.task) setTask(task);
  const pick = `${p.preset}|${p.quant}`;
  if (p.preset && [...$("modelSel").options].some((o) => o.value === pick)) {
    $("modelSel").value = pick;
    $("modelSel").dispatchEvent(new Event("change"));   // applies the preset defaults first
  }
  const fields = { prompt: p.prompt, negative: p.negative, steps: p.steps, denoise: p.denoise, cfg: p.cfg, feather: p.feather,
    megapixels: p.megapixels, resolution: p.resolution, saveEvery: p.save_every, saveLast: p.save_last, seed: p.seed,
    upscale: p.upscale != null ? String(p.upscale) : null };
  for (const [id, v] of Object.entries(fields)) if (v != null) $(id).value = v;
  for (const [id, v] of [["sampler", p.sampler], ["scheduler", p.scheduler]]) if (v) setSelectValue(id, v);
  if (p.upscaler && [...$("upscaler").options].some((o) => o.value === p.upscaler)) $("upscaler").value = p.upscaler;
  $("keepNote").value = p.keep_note ?? (p.keep_identical === false ? "" : KEEP_NOTE);
  const checks = { postColors: p.post_colors, postWarp: p.post_warp, postPoisson: p.post_poisson };
  for (const [id, v] of Object.entries(checks)) if (v != null) $(id).checked = !!v;
  $("randomSeed").checked = false;   // reproduce the run
  $("refNote").value = p.ref_note ?? defaultRefNote();
  state.refs = (p.refs || []).map((name, i) => ({ name, label: name.split("/").pop().replace(/^[0-9a-f]{8}_/, ""),
    take: p.ref_takes?.[i] || "", crop: p.ref_crops?.[i] || null }));
  saveSession({ refs: state.refs });
  if (task === "edit" && p.mode) {
    $("mode").value = p.use_mask === false ? "none" : p.mode;
    $("mode").dispatchEvent(new Event("change"));
  }
  if (task === "generate" && run.size?.work_w) {   // nearest aspect ratio tile
    const r = run.size.work_w / run.size.work_h;
    const off = (v) => { const [a, b] = v.split(":").map(Number); return Math.abs(Math.log(a / b / r)); };
    const best = [...$("aspect").options].map((o) => o.value).sort((a, b) => off(a) - off(b))[0];
    $("aspect").value = best;
    $("aspect").dispatchEvent(new Event("change"));
  }
  for (const id of ["prompt", "upscale"]) $(id).dispatchEvent(new Event("change"));
  $("prompt").dispatchEvent(new Event("input"));
  syncTaskUi();
  saveForm();
  refreshSize();
  setView("create");
  showToast(`Settings of Run ${runNumber(run)} loaded.`);
}

// ------------------------------------------------------------------ view switching (hash router)
// #create, #runs, #runs/<run id>: reloads keep the view and the selected run
function setView(v) {
  state.view = v === "runs" ? "runs" : "create";
  const runs = state.view === "runs";
  $("createView").hidden = runs;
  $("runsView").hidden = !runs;
  $("navCreate").setAttribute("aria-current", runs ? "false" : "page");
  $("navRuns").setAttribute("aria-current", runs ? "page" : "false");
  if (!location.hash.startsWith(`#${state.view}`)) location.hash = state.view;
  applyCols();
  if (!runs) render();
}
// keeps the selected run in the hash without adding history entries
function syncRunHash() {
  if (state.view !== "runs") return;
  const want = state.run?.serverId ? `#runs/${state.run.serverId}` : "#runs";
  if (location.hash !== want) history.replaceState(null, "", want);
}
function route() {
  const [v, id] = location.hash.slice(1).split("/");
  setView(v);
  if (state.view === "runs") {
    state.wantRun = id ? decodeURIComponent(id) : null;
    const run = state.wantRun && findRun(state.wantRun);
    if (run) { state.wantRun = null; if (run !== state.run) selectRun(run); }
    else if (!state.wantRun) syncRunHash();
  }
}
window.addEventListener("hashchange", route);
const findRun = (id) => state.jobs.get(id) || state.runs.find((r) => r.serverId === id) || null;
function selectRun(run) {
  if (state.jobs.has(run.id)) { state.follow = true; viewJob(run); return; }
  state.follow = false;
  showRun(run);
}

// ------------------------------------------------------------------ resizable columns
// Drag the handle between columns; widths are kept per view. Double-click resets.
const COLS_KEY = "inpaint-studio-cols-v1";
const COL_MIN = { left: 260, right: 280 }, COL_MAX = 640, CENTER_MIN = 420;
const COL_DEFAULT = { createView: { left: 300, right: 340 }, runsView: { left: 300, right: 320 } };

function readCols() { try { return JSON.parse(localStorage.getItem(COLS_KEY) || "{}"); } catch { return {}; } }
function colLimit(view, side, w) {
  const other = side === "left" ? "right" : "left";
  const otherW = parseFloat(getComputedStyle(view).getPropertyValue(`--${other}-w`)) || COL_DEFAULT[view.id][other];
  const max = Math.min(COL_MAX, view.clientWidth - otherW - CENTER_MIN);
  return Math.round(Math.max(COL_MIN[side], Math.min(max, w)));
}
function setColWidth(view, side, w, save = true) {
  view.style.setProperty(`--${side}-w`, `${colLimit(view, side, w)}px`);
  if (!save) return;
  const all = readCols();
  all[view.id] = { ...all[view.id], [side]: parseFloat(view.style.getPropertyValue(`--${side}-w`)) };
  try { localStorage.setItem(COLS_KEY, JSON.stringify(all)); } catch { /* widths are a convenience */ }
}
function applyCols() {
  const all = readCols();
  for (const view of [$("createView"), $("runsView")]) {
    if (view.hidden || !view.clientWidth) continue;   // re-applied when the view is shown
    for (const side of ["left", "right"]) {
      const w = all[view.id]?.[side] ?? COL_DEFAULT[view.id][side];
      setColWidth(view, side, w, false);
    }
  }
}
for (const h of document.querySelectorAll(".col-resizer")) {
  const view = h.parentElement, side = h.dataset.side;
  h.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    try { h.setPointerCapture(e.pointerId); } catch { /* moves still arrive while over the handle */ }
    h.classList.add("dragging");
    document.body.classList.add("resizing");
  });
  h.addEventListener("pointermove", (e) => {
    if (!h.classList.contains("dragging")) return;
    const r = view.getBoundingClientRect();
    setColWidth(view, side, side === "left" ? e.clientX - r.left : r.right - e.clientX, false);
  });
  const end = (e) => {
    if (!h.classList.contains("dragging")) return;
    h.classList.remove("dragging");
    document.body.classList.remove("resizing");
    try { h.releasePointerCapture(e.pointerId); } catch { /* not captured */ }
    setColWidth(view, side, parseFloat(view.style.getPropertyValue(`--${side}-w`)));
    render();   // the stage changed size: redraw the mask outline at the new scale
  };
  h.addEventListener("pointerup", end);
  h.addEventListener("pointercancel", end);
  h.addEventListener("dblclick", () => { setColWidth(view, side, COL_DEFAULT[view.id][side]); render(); });
}
window.addEventListener("resize", debounce(applyCols, 150));

// ------------------------------------------------------------------ wiring
function bindOutput(id, outId, fmt = (v) => v) {
  const upd = () => { $(outId).textContent = fmt($(id).value); };
  $(id).addEventListener("input", upd);
  upd();
}

function initApp() {
  setView(location.hash.slice(1).split("/")[0]);
  state.refs = readSession()?.refs || [];
  loadForm();
  syncModeUi();   // the stored mode decides which paste-only fields show
  $("aspect").dispatchEvent(new Event("change"));
  syncUpscaler();
  state.task = $("task").value === "generate" ? "generate" : "edit";
  initModelPicker();
  promptPresets = initPromptPresets({ getTask: () => state.task });
  syncTaskUi();
  if (state.task === "generate") refreshSize();
  bindOutput("threshold", "thresholdOut", (v) => (+v).toFixed(2));
  bindOutput("brushSize", "brushSizeOut", (v) => `${v} px`);
  bindOutput("opacity", "opacityOut", (v) => `${Math.round(v * 100)}%`);
  for (const id of PERSIST) $(id).addEventListener("change", saveForm);
  $("aspect").addEventListener("change", refreshSizeDebounced);
  for (const id of ["unet", "clip", "vae"]) $(id).addEventListener("change", updateOverrideHint);
  for (const id of ["megapixels", "resolution"]) {
    $(id).addEventListener("input", () => { updateStale(); refreshSizeDebounced(); });
  }
  $("autofix").addEventListener("change", refreshSizeDebounced);
  $("moreModels").onclick = () => showSetup({ section: "models" });
  $("taskTabs").addEventListener("click", (e) => { if (e.target.dataset.task) setTask(e.target.dataset.task); });
  $("opacity").addEventListener("input", render);
  $("runEdit").onclick = () => runEdit();
  $("maskText").addEventListener("keydown", (e) => { if (e.key === "Enter") computeMask(); });
  setMode("paint");
  renderHistory();
  loadModels();
}

function syncModeUi() {
  const notPaste = $("mode").value !== "paste";
  $("keepNoteRow").hidden = notPaste;
  $("postFixRow").hidden = notPaste;
  syncAreaCards();
  if (state.maskUiReady) applyMaskTexts();  // not during module init (applyMaskMode runs it later)
}
$("mode").addEventListener("change", syncModeUi);

// "Area to change" cards are a view of the hidden #mode select
function syncAreaCards() {
  for (const b of $("areaCards").children) {
    const opt = $("mode").querySelector(`[value="${b.dataset.mode}"]`);
    b.setAttribute("aria-checked", String($("mode").value === b.dataset.mode));
    b.disabled = !!opt?.disabled;
  }
}
$("areaCards").addEventListener("click", (e) => {
  const b = e.target.closest("[data-mode]");
  if (!b || b.disabled || $("mode").value === b.dataset.mode) return;
  $("mode").value = b.dataset.mode;
  $("mode").dispatchEvent(new Event("change"));
});

// aspect ratio tiles are a view of the hidden #aspect select
function renderAspectTiles() {
  const box = $("aspectTiles");
  box.innerHTML = "";
  for (const o of $("aspect").options) {
    const [a, b] = o.value.split(":").map(Number);
    const t = document.createElement("button");
    t.setAttribute("role", "radio");
    t.dataset.aspect = o.value;
    t.setAttribute("aria-checked", String(o.value === $("aspect").value));
    const icon = document.createElement("i");
    icon.style.width = `${a >= b ? 22 : Math.round((22 * a) / b)}px`;
    icon.style.height = `${b >= a ? 22 : Math.round((22 * b) / a)}px`;
    const l = document.createElement("span"); l.textContent = o.value;
    t.append(icon, l);
    box.appendChild(t);
  }
}
$("aspectTiles").addEventListener("click", (e) => {
  const t = e.target.closest("[data-aspect]");
  if (!t) return;
  $("aspect").value = t.dataset.aspect;
  $("aspect").dispatchEvent(new Event("change"));
});
$("aspect").addEventListener("change", () => {
  for (const t of $("aspectTiles").children) t.setAttribute("aria-checked", String(t.dataset.aspect === $("aspect").value));
  syncGenFrame();
});
renderAspectTiles();
syncModeUi();

// ------------------------------------------------------------------ extra reference images
// The text encoder of some models takes more images than the edited one (see graphs.MAX_REFS).
// The edited image is image 1, references follow; when generating they start at image 1.
const MAX_REFS = { qwen21: 3, qwen21_turbo: 3, qwen_edit: 2 };
const maxRefs = () => MAX_REFS[currentFamily()] || 0;
state.refs = [];   // [{name, label, take, crop}] uploaded to ComfyUI's input folder; take = what to take from it,
                   // crop = {x, y, w, h} in the image's pixels or null for the whole image

// Free edit + paste instruction after the prompt, editable in Advanced. Mirrors graphs.KEEP_IDENTICAL.
const KEEP_NOTE = "Keep everything else in the image exactly identical to the original: same framing, "
  + "perspective, positions, people, objects, colors, lighting and fine details. "
  + "Only change what is described above.";
$("keepNote").value = KEEP_NOTE;   // before loadForm: a stored text wins
$("keepNoteReset").onclick = () => { $("keepNote").value = KEEP_NOTE; $("keepNote").dispatchEvent(new Event("change")); };

// Hidden instruction for edits with references, editable in Advanced. Mirrors graphs.REF_NOTE / ref_labels;
// as long as it is unchanged it follows the model's names for the images (Qwen 2.1 <image1>, Edit 2511 "Picture 1").
// The "Take from it" text per reference is added by the server after the prompt, as an order:
// "Replace the face in <image1> with the face from <image3>." (graphs.reference_takes)
const REF_NOTE = "{main} is the image to edit. Keep its framing, composition, camera angle, perspective and "
  + "everything the instruction does not change. Take from {refs} only what the instruction asks for. "
  + "Add no body parts, people or objects that the instruction does not ask for.";
const refNoteFor = (family, n, template = REF_NOTE) => {
  const name = (i) => family === "qwen_edit" ? `Picture ${i}` : `<image${i}>`;
  const refs = Array.from({ length: Math.max(1, n) }, (_, i) => name(i + 2));
  return template.replaceAll("{main}", name(1))
    .replaceAll("{refs}", refs.length === 1 ? refs[0] : refs.slice(0, -1).join(", ") + " and " + refs.at(-1));
};
const defaultRefNote = () => refNoteFor(currentFamily(), Math.min(state.refs.length, maxRefs()));
// earlier defaults count as unchanged too, so a stored copy follows the current one
const OLD_REF_NOTE = (m) => `Edit ${m}. The result keeps the framing, composition, camera angle and perspective of ${m} `
  + "and everything in it that the instruction does not change. The other images are only references "
  + "for what the instruction takes from them.";
const OLD_REF_NOTE_3 = "{main} is the image to edit. Keep its framing, composition, camera angle, perspective and "
  + "everything the instruction does not change. Take from {refs} only what the instruction asks for.";
const OLD_REF_NOTE_2 = "{main} is the image to edit. Keep its framing, composition, camera angle, perspective, poses and "
  + "everything the instruction does not change. Use {refs} only as reference for how the changed parts "
  + "should look, never for pose or composition.";
const isDefaultRefNote = (v) => ["qwen21", "qwen_edit"].some((f) => [1, 2, 3].some((n) =>
  [REF_NOTE, OLD_REF_NOTE_2, OLD_REF_NOTE_3].some((t) => v === refNoteFor(f, n, t))))
  || ["image 1", "Picture 1"].some((m) => v === OLD_REF_NOTE(m));
$("refNote").value = refNoteFor("qwen21", 1);   // before loadForm: a stored text wins
$("refNoteReset").onclick = () => { $("refNote").value = defaultRefNote(); $("refNote").dispatchEvent(new Event("change")); };

function renderRefs() {
  const max = maxRefs();
  $("refNoteRow").hidden = !(max && state.task === "edit" && state.refs.length);   // only with a reference image
  if (isDefaultRefNote($("refNote").value)) $("refNote").value = defaultRefNote();
  $("refsSec").hidden = !max;
  const first = state.task === "generate" ? 1 : 2;
  $("refsHint").textContent = (state.task === "generate"
    ? "Optional. Refer to them as image 1, 2 … in the prompt."
    : "Optional. Your image is image 1 and stays the one that is edited (the model is told so). Say what to take from each reference, e.g. \u201cface\u201d, and click it to crop that part.")
    + ` This model takes up to ${max}; each one makes the run a lot slower.`;
  const grid = $("refGrid");
  grid.innerHTML = "";
  state.refs.forEach((r, i) => {
    const row = document.createElement("div");
    const unused = i >= max;
    row.className = "ref-row" + (unused ? " skipped" : "");
    const t = document.createElement("div");
    t.className = "batch-item ref" + (r.crop ? " cropped" : "");
    t.title = unused ? `${r.label}: not used by this model` : `${r.label}: image ${first + i}. Click to crop.`;
    t.tabIndex = 0; t.setAttribute("role", "button");
    t.setAttribute("aria-label", `Crop reference ${first + i}`);
    t.onclick = () => openCrop(r);
    t.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openCrop(r); } };
    const img = document.createElement("img"); img.alt = ""; refThumb(img, r.name, r.crop);
    const tag = document.createElement("span"); tag.className = "tag"; tag.textContent = unused ? "unused" : String(first + i);
    t.append(img, tag);
    const take = document.createElement("input");
    take.type = "text"; take.value = r.take || ""; take.disabled = unused || state.task === "generate";
    take.placeholder = state.task === "generate" ? "" : "Take from it, e.g. muscular torso";
    take.setAttribute("aria-label", `What to take from reference ${first + i}`);
    take.oninput = () => { r.take = take.value; saveSession({ refs: state.refs }); };
    const x = document.createElement("button");
    x.className = "ref-del"; x.textContent = "×"; x.setAttribute("aria-label", `Remove reference ${r.label}`);
    x.onclick = () => { state.refs.splice(i, 1); saveSession({ refs: state.refs }); renderRefs(); };
    row.append(t, take, x);
    grid.appendChild(row);
  });
  if (state.refs.length < max) {
    const add = document.createElement("label");
    add.className = "batch-item add";
    add.htmlFor = "refInput";
    add.title = "Add reference images";
    add.setAttribute("aria-label", "Add reference images");
    add.textContent = "+";
    grid.appendChild(add);
  }
}

// Thumbnail of a reference: the crop when there is one (drawn on a canvas, works in every browser)
function refThumb(img, name, crop) {
  if (!crop) { img.src = inputViewUrl(name); return; }
  const src = new Image();
  src.onload = () => {
    const c = document.createElement("canvas");
    const s = Math.min(1, 240 / Math.max(crop.w, crop.h));
    c.width = Math.max(1, Math.round(crop.w * s)); c.height = Math.max(1, Math.round(crop.h * s));
    c.getContext("2d").drawImage(src, crop.x, crop.y, crop.w, crop.h, 0, 0, c.width, c.height);
    img.src = c.toDataURL("image/jpeg", 0.85);
  };
  src.src = inputViewUrl(name);
}

// Crop dialog: drag a rectangle on the reference; stored in image pixels
const cropUi = { ref: null, box: null, start: null };
function openCrop(r) {
  cropUi.ref = r; cropUi.box = r.crop ? { ...r.crop } : null;
  $("cropImg").onload = drawCropRect;
  $("cropImg").src = inputViewUrl(r.name);
  $("cropModal").hidden = false;
  drawCropRect();
}
function closeCrop() { $("cropModal").hidden = true; cropUi.ref = null; }
function drawCropRect() {
  const img = $("cropImg"), b = cropUi.box;
  $("cropRect").hidden = !(b && img.naturalWidth);
  $("cropSave").disabled = !b;
  if (!b || !img.naturalWidth) return;
  const W = img.naturalWidth, H = img.naturalHeight;
  Object.assign($("cropRect").style, { left: `${(b.x / W) * 100}%`, top: `${(b.y / H) * 100}%`,
    width: `${(b.w / W) * 100}%`, height: `${(b.h / H) * 100}%` });
}
function cropPoint(e) {   // pointer position in image pixels, clamped to the image
  const img = $("cropImg"), r = img.getBoundingClientRect();
  const fx = Math.min(1, Math.max(0, (e.clientX - r.left) / r.width));
  const fy = Math.min(1, Math.max(0, (e.clientY - r.top) / r.height));
  return { x: fx * img.naturalWidth, y: fy * img.naturalHeight };
}
$("cropStage").addEventListener("pointerdown", (e) => {
  if (!$("cropImg").naturalWidth) return;
  e.preventDefault();
  cropUi.start = cropPoint(e);
  try { $("cropStage").setPointerCapture(e.pointerId); } catch {}
});
$("cropStage").addEventListener("pointermove", (e) => {
  if (!cropUi.start) return;
  const p = cropPoint(e), s = cropUi.start;
  const box = { x: Math.round(Math.min(s.x, p.x)), y: Math.round(Math.min(s.y, p.y)),
    w: Math.round(Math.abs(p.x - s.x)), h: Math.round(Math.abs(p.y - s.y)) };
  cropUi.box = box.w >= 16 && box.h >= 16 ? box : null;   // the server ignores smaller crops (graphs.crop_box)
  drawCropRect();
});
const endCropDrag = () => { cropUi.start = null; };
$("cropStage").addEventListener("pointerup", endCropDrag);
$("cropStage").addEventListener("pointercancel", endCropDrag);
$("cropSave").onclick = () => { if (cropUi.ref && cropUi.box) cropUi.ref.crop = cropUi.box; saveSession({ refs: state.refs }); closeCrop(); renderRefs(); };
$("cropClear").onclick = () => { if (cropUi.ref) cropUi.ref.crop = null; saveSession({ refs: state.refs }); closeCrop(); renderRefs(); };
$("cropCancel").onclick = closeCrop;
$("cropModal").addEventListener("keydown", (e) => { if (e.key === "Escape") closeCrop(); });
$("cropModal").addEventListener("click", (e) => { if (e.target === $("cropModal")) closeCrop(); });

$("refInput").addEventListener("change", async (e) => {
  const files = [...e.target.files].filter((f) => f.type.startsWith("image/")).slice(0, Math.max(0, maxRefs() - state.refs.length));
  e.target.value = "";
  for (const file of files) {
    try {
      const fd = new FormData();
      fd.append("file", file, file.name || "reference.png");
      const up = await api("/api/upload", { method: "POST", body: fd });
      state.refs.push({ name: up.name, label: file.name || "reference" });
    } catch (err) { showError(err.message); }
  }
  saveSession({ refs: state.refs });
  renderRefs();
});

// ------------------------------------------------------------------ session restore (image + mask survive reloads)
const SESSION_KEY = "inpaint-studio-session-v1";
function readSession() { try { return JSON.parse(localStorage.getItem(SESSION_KEY) || "null"); } catch { return null; } }
function saveSession(patch) {
  try { localStorage.setItem(SESSION_KEY, JSON.stringify({ ...(readSession() || {}), ...patch })); } catch { /* storage optional */ }
}

let maskSaveTimer = 0;
// uploads the current mask (incl. brush edits) shortly after it changes, so a reload can restore it
function scheduleMaskSave() {
  clearTimeout(maskSaveTimer);
  maskSaveTimer = setTimeout(async () => {
    if (!state.maskAvailable || !state.mask || !state.hasMask || !state.imageName) return;
    try {
      const { blob } = await exportMaskBlob();
      const fd = new FormData();
      fd.append("file", blob, "mask.png");
      const up = await api("/api/upload-mask", { method: "POST", body: fd });
      saveSession({ imageName: state.imageName, maskName: up.name, maskMeta: state.maskMeta });
    } catch { /* restoring the mask is a convenience */ }
  }, 1200);
}
for (const id of ["maskClear", "maskFill", "maskUndo"]) $(id).addEventListener("click", scheduleMaskSave);

function inputViewUrl(name) {
  const i = name.lastIndexOf("/");
  const q = new URLSearchParams({ filename: name.slice(i + 1), subfolder: i >= 0 ? name.slice(0, i) : "", type: "input" });
  return `/api/view?${q}`;
}

async function restoreSession() {
  const sess = readSession();
  if (!sess || !sess.imageName) return false;
  try {
    const im = await loadImage(inputViewUrl(sess.imageName));
    state.imgEl = im; state.imgUrl = null;
    state.imageName = sess.imageName; state.srcW = sess.srcW; state.srcH = sess.srcH;
    state.imageLabel = sess.label || "image";
    $("imageInfo").textContent = `${sess.label || "image"} – original ${sess.srcW}×${sess.srcH} (restored)`;
    resetMask();
    state.size = null;
    renderBatch();
    await refreshSize();
    if (sess.maskName && state.maskAvailable) {
      try {
        applyMaskImage(await loadImage(inputViewUrl(sess.maskName)), sess.maskMeta?.mp);
        if (sess.maskMeta) state.maskMeta = sess.maskMeta;
        updateStale();
      } catch { /* mask file gone */ }
    }
    render();
    return true;
  } catch {
    return false; // image no longer in ComfyUI's input folder
  }
}

async function startSession() {
  await Promise.all([restoreSession(), loadStoredRuns()]);
  route();
  connectJobs();
  // the viewer starts on the latest result
  if (!state.run && !state.wantRun) showLatest();
}

// ------------------------------------------------------------------ advanced: post-hoc alignment
let alignTimer = 0;
function alignValues() {
  return { dx: num("alignDx") || 0, dy: num("alignDy") || 0, scale: (num("alignScale") || 100) / 100,
           colors: $("fixColors").checked, warp: $("fixWarp").checked, poisson: $("fixPoisson").checked };
}
function setAlignValues(v) {
  $("alignDx").value = Math.round(v.dx * 10) / 10;
  $("alignDy").value = Math.round(v.dy * 10) / 10;
  $("alignScale").value = Math.round(v.scale * 10000) / 100;
  for (const [id, k] of [["fixColors", "colors"], ["fixWarp", "warp"], ["fixPoisson", "poisson"]]) {
    if (k in v) $(id).checked = !!v[k];
  }
}
async function alignRequest(body) {
  const run = state.run;
  if (!run?.serverId) return;
  $("alignInfo").textContent = "Working...";
  try {
    const res = await postJson(`/api/runs/${encodeURIComponent(run.serverId)}/align`, body);
    setAlignValues(res);
    $("cmpBefore").src = run.beforeUrl;
    $("cmpAfter").src = res.url;
    $("liveImg").hidden = true; $("compare").hidden = false;
    $("alignInfo").textContent = `Outside-mask difference: ${res.unaligned_diff} → ${res.outside_diff} / 255`
      + (res.confidence ? ` · match confidence ${res.confidence}` : "") + (res.saved ? " · saved" : "");
    if (res.saved) {
      run.aligned = { dx: res.dx, dy: res.dy, scale: res.scale, colors: res.colors, warp: res.warp,
                      poisson: res.poisson, outside_diff: res.outside_diff, url: res.url };
      $("downloadBtn").href = res.url;
    }
  } catch (e) { $("alignInfo").textContent = ""; showError(e.message); }
}
function schedulePreview() {
  clearTimeout(alignTimer);
  alignTimer = setTimeout(() => alignRequest({ ...alignValues(), save: false }), 250);
}
$("alignBtn").onclick = () => {
  const run = state.run;
  $("alignPanel").hidden = false;
  setAlignValues(run.aligned || { dx: 0, dy: 0, scale: 1, colors: true, warp: false, poisson: false });
  $("alignInfo").textContent = "Try Auto-align and the fixes, then fine-tune with the arrows (Shift = 5 px).";
  schedulePreview();
};
$("alignClose").onclick = () => { $("alignPanel").hidden = true; showFinal(); };
$("alignAuto").onclick = () => alignRequest({ auto: true, save: false });
$("alignReset").onclick = () => { setAlignValues({ dx: 0, dy: 0, scale: 1, colors: false, warp: false, poisson: false }); schedulePreview(); };
$("alignSave").onclick = () => alignRequest({ ...alignValues(), save: true });
for (const id of ["alignDx", "alignDy", "alignScale"]) $(id).addEventListener("input", schedulePreview);
for (const id of ["fixColors", "fixWarp", "fixPoisson"]) $(id).addEventListener("change", schedulePreview);
for (const b of document.querySelectorAll("#alignPanel [data-nudge]")) {
  b.onclick = (e) => {
    const [x, y] = b.dataset.nudge.split(",").map(Number);
    const step = e.shiftKey ? 5 : 1;
    const v = alignValues();
    setAlignValues({ ...v, dx: v.dx + x * step, dy: v.dy + y * step });
    schedulePreview();
  };
}

// ------------------------------------------------------------------ batch (folder / several images)
state.batch = [];
state.batchIdx = -1;

function openFiles(files) {
  const imgs = files.filter((f) => f.type.startsWith("image/")).sort((a, b) => (a.webkitRelativePath || a.name).localeCompare(b.webkitRelativePath || b.name));
  if (!imgs.length) { showError("No images found."); return; }
  if (!ensureEditTask()) return;
  if (imgs.length === 1 && !state.batch.length) { setImageFile(imgs[0]); return; }
  const items = imgs.map((file) => ({ file, label: file.name, thumbUrl: URL.createObjectURL(file),
    name: null, srcW: 0, srcH: 0, status: "open", mask: null, maskMeta: null }));
  const first = state.batch.length;   // an open batch grows
  state.batch.push(...items);
  if (!first) state.batchIdx = -1;
  renderBatch();
  openBatchItem(first);
}

function currentBatchItem() {
  const it = state.batch[state.batchIdx];
  return it && it.name === state.imageName ? it : null;
}

// "+" tile at the end of the image grid: opens the file picker
function addTile() {
  const add = document.createElement("label");
  add.className = "batch-item add";
  add.htmlFor = "fileInput";
  // a batch grows, a single image is replaced (choosing several starts a batch)
  const label = state.batch.length ? "Add images" : "Choose other images";
  add.title = label;
  add.setAttribute("aria-label", label);
  add.textContent = "+";
  return add;
}

function renderBatch() {
  $("batchWrap").hidden = !state.batch.length;
  $("dropzone").hidden = !!(state.imgEl || state.batch.length);
  const grid = $("batchGrid");
  grid.hidden = !$("dropzone").hidden;
  grid.innerHTML = "";
  if (!state.batch.length) {   // a single image: one tile
    if (state.imgEl) {
      const b = document.createElement("button");
      b.className = "batch-item active";
      b.title = $("imageInfo").textContent;
      const img = document.createElement("img"); img.src = state.imgEl.src; img.alt = "";
      b.append(img);
      grid.append(b, addTile());
    }
    return;
  }
  const counts = { open: 0, masked: 0, queued: 0, nomask: 0, error: 0, skipped: 0 };
  state.batch.forEach((it, i) => {
    counts[it.status] = (counts[it.status] || 0) + 1;
    const b = document.createElement("button");
    b.className = `batch-item ${it.status}` + (i === state.batchIdx ? " active" : "");
    b.title = `${it.label} – ${{ nomask: "nothing found to mask", skipped: "skipped (click to open, Submit still works)" }[it.status] || it.status}`;
    const img = document.createElement("img"); img.src = it.thumbUrl; img.alt = "";
    const tag = document.createElement("span"); tag.className = "tag";
    tag.textContent = { open: "", masked: "mask", queued: "✓", nomask: "∅", error: "!", skipped: "skip" }[it.status] || "";
    b.append(img, tag);
    b.onclick = () => openBatchItem(i);
    grid.appendChild(b);
  });
  grid.appendChild(addTile());
  $("batchInfo").textContent = `${state.batch.length} images · ${counts.queued} queued · ${counts.open + counts.masked} open`
    + (counts.nomask ? ` · ${counts.nomask} without mask found` : "") + (counts.skipped ? ` · ${counts.skipped} skipped` : "")
    + (counts.error ? ` · ${counts.error} failed` : "");
}

function stashCurrentMask() {
  const it = currentBatchItem();
  if (!state.maskAvailable || !it || !state.hasMask || !state.mask) return;
  const c = document.createElement("canvas");
  c.width = state.mask.width; c.height = state.mask.height;
  c.getContext("2d").drawImage(state.mask, 0, 0);
  it.mask = c; it.maskMeta = state.maskMeta;
  if (it.status === "open") it.status = "masked";
}

async function ensureUploaded(it) {
  if (it.name) return it;
  const fd = new FormData();
  fd.append("file", it.file, it.file.name || "image.png");
  const up = await api("/api/upload", { method: "POST", body: fd });
  Object.assign(it, { name: up.name, srcW: up.width, srcH: up.height });
  return it;
}

async function openBatchItem(i) {
  const it = state.batch[i];
  if (!it) return;
  stashCurrentMask();
  try {
    await ensureUploaded(it);
    const im = await loadImage(it.thumbUrl);
    state.imgEl = im; state.imgUrl = null;
    state.imageName = it.name; state.srcW = it.srcW; state.srcH = it.srcH;
    state.imageLabel = it.label;
    state.batchIdx = i;
    $("imageInfo").textContent = `${it.label} – original ${it.srcW}×${it.srcH} (${i + 1} / ${state.batch.length})`;
    saveSession({ imageName: it.name, srcW: it.srcW, srcH: it.srcH, label: it.label, maskName: null, maskMeta: null });
    resetMask();
    state.size = null;
    await refreshSize();
    if (it.mask) {
      newMask(it.mask.width, it.mask.height);
      state.mask.getContext("2d").drawImage(it.mask, 0, 0);
      state.hasMask = true; state.maskMeta = it.maskMeta;
      updateStale();
    }
    setView("create");
    render();
  } catch (e) {
    it.status = "error";
    showError(`${it.label}: ${e.message}`);
  }
  renderBatch();
}

function openNextBatchItem() {
  const n = state.batch.length;
  for (let k = 1; k <= n; k++) {
    const j = (state.batchIdx + k) % n;
    if (state.batch[j].status === "open" || state.batch[j].status === "masked") { openBatchItem(j); return; }
  }
  showError("No open images left in the batch.");
}

// size settings for one image: current form values, auto-fixed below the gray-noise limit
async function sizeFor(it) {
  const body = { width: it.srcW, height: it.srcH, megapixels: num("megapixels"), resolution: parseInt($("resolution").value, 10) };
  const rep = await postJson("/api/size", body);
  const out = !rep.safe && $("autofix").checked && rep.suggested
    ? { ...rep.suggested } : { ...rep, megapixels: body.megapixels, resolution: body.resolution };
  if ($("matchRef").checked) {  // per image: its own working size decides the matching resolution
    const fixed = await postJson("/api/size", { ...body, megapixels: out.megapixels });
    out.resolution = fixed.match_res;
  }
  return out;
}

function maskHasWhite(im) {
  const c = document.createElement("canvas");
  c.width = im.naturalWidth; c.height = im.naturalHeight;
  const x = c.getContext("2d", { willReadFrequently: true });
  x.drawImage(im, 0, 0);
  const d = x.getImageData(0, 0, c.width, c.height).data;
  for (let i = 0; i < d.length; i += 4) if (d[i] > 127) return true;
  return false;
}

async function batchSubmitAll(withMask) {
  if (!maskOn()) withMask = false;
  if (state.batchBusy) return;
  stashCurrentMask();
  // "without mask" also takes images where auto-masking found nothing
  const todo = state.batch.filter((it) => it.status === "open" || it.status === "masked" || (!withMask && it.status === "nomask"));
  if (!todo.length) { showError("No open images in the batch."); return; }
  if (withMask && !$("maskText").value.trim()) { showError("Enter what to mask first."); return; }
  setBatchBusy(true);
  let done = 0;
  try {
    for (const it of todo) {
      $("batchInfo").textContent = `${withMask ? "Masking and queueing" : "Queueing"} ${++done} / ${todo.length}: ${it.label}`;
      try {
        await ensureUploaded(it);
        const size = await sizeFor(it);
        let maskName = null;
        if (withMask) {
          let blob;
          if (it.mask && it.maskMeta && it.maskMeta.w === size.work_w && it.maskMeta.h === size.work_h) {
            ({ blob } = await exportMaskBlob(it.mask)); // keep a mask you already made/painted
          } else {
            const res = await postJson("/api/mask", {
              image: it.name, megapixels: size.megapixels, text: $("maskText").value.trim(),
              threshold: num("threshold"), refine: parseInt($("refine").value, 10) || 0,
              expand: parseInt($("expand").value, 10) || 0, invert: $("invert").checked,
            });
            const im = await loadImage(res.mask_url);
            if (!maskHasWhite(im)) { it.status = "nomask"; renderBatch(); continue; }
            blob = await (await fetch(res.mask_url)).blob();
          }
          maskName = await uploadMaskBlob(blob);
        }
        await submitJob(editParams({ image: it.name, srcW: it.srcW, srcH: it.srcH, maskName, useMask: withMask,
          megapixels: size.megapixels, resolution: size.resolution }));
        it.status = "queued";
      } catch (e) {
        it.status = "error";
        showError(`${it.label}: ${e.message}`);
      }
      renderBatch();
    }
  } finally {
    setBatchBusy(false);
    renderBatch();
  }
}

// black/white mask image -> white-on-transparent canvas (same format as the painted mask)
function maskCanvasFromImage(im) {
  const c = document.createElement("canvas");
  c.width = im.naturalWidth; c.height = im.naturalHeight;
  const x = c.getContext("2d", { willReadFrequently: true });
  x.drawImage(im, 0, 0);
  const data = x.getImageData(0, 0, c.width, c.height);
  const px = data.data;
  let white = 0;
  for (let i = 0; i < px.length; i += 4) {
    const on = 0.299 * px[i] + 0.587 * px[i + 1] + 0.114 * px[i + 2] > 127;
    px[i] = px[i + 1] = px[i + 2] = 255; px[i + 3] = on ? 255 : 0;
    if (on) white++;
  }
  x.putImageData(data, 0, 0);
  return { canvas: c, white };
}

function setBatchBusy(on) {
  state.batchBusy = on;
  for (const id of ["batchSubmitNext", "batchSkip", "batchMaskAll", "batchSubmitMasked", "batchAutoAll", "batchNoMaskAll"]) $(id).disabled = on;
}

// step 1 for the whole batch: masks only, nothing is queued
async function batchMaskAll() {
  if (!state.maskAvailable) return;
  if (state.batchBusy) return;
  if (!$("maskText").value.trim()) { showError("Enter what to mask first."); return; }
  stashCurrentMask();
  const todo = state.batch.filter((it) => it.status === "open" || it.status === "nomask");
  if (!todo.length) { showError("No open images without a mask."); return; }
  setBatchBusy(true);
  let n = 0;
  try {
    for (const it of todo) {
      $("batchInfo").textContent = `Masking ${++n} / ${todo.length}: ${it.label}`;
      try {
        await ensureUploaded(it);
        const size = await sizeFor(it);
        const res = await postJson("/api/mask", {
          image: it.name, megapixels: size.megapixels, text: $("maskText").value.trim(),
          threshold: num("threshold"), refine: parseInt($("refine").value, 10) || 0,
          expand: parseInt($("expand").value, 10) || 0, invert: $("invert").checked,
        });
        const { canvas, white } = maskCanvasFromImage(await loadImage(res.mask_url));
        if (!white) { it.status = "nomask"; it.mask = null; }
        else { it.mask = canvas; it.maskMeta = { w: canvas.width, h: canvas.height, mp: size.megapixels }; it.status = "masked"; }
        if (state.batch[state.batchIdx] === it) await openBatchItem(state.batchIdx); // refresh the open image
      } catch (e) {
        it.status = "error";
        showError(`${it.label}: ${e.message}`);
      }
      renderBatch();
    }
  } finally {
    setBatchBusy(false);
    renderBatch();
  }
}

// step 2 for the whole batch: queue everything that has a mask
async function batchSubmitMasked() {
  if (!state.maskAvailable) return;
  if (state.batchBusy) return;
  stashCurrentMask();
  const todo = state.batch.filter((it) => it.status === "masked" && it.mask);
  if (!todo.length) { showError("No masked images to submit."); return; }
  setBatchBusy(true);
  let n = 0;
  try {
    for (const it of todo) {
      $("batchInfo").textContent = `Queueing ${++n} / ${todo.length}: ${it.label}`;
      try {
        const size = await sizeFor(it);
        if (it.maskMeta && (it.maskMeta.w !== size.work_w || it.maskMeta.h !== size.work_h)) {
          throw new Error("size changed since the mask was made – open it and recompute the mask");
        }
        const { blob, white } = await exportMaskBlob(it.mask);
        if (!white) throw new Error("mask is empty");
        const maskName = await uploadMaskBlob(blob);
        await submitJob(editParams({ image: it.name, srcW: it.srcW, srcH: it.srcH, maskName, useMask: true,
          megapixels: size.megapixels, resolution: size.resolution }));
        it.status = "queued";
      } catch (e) {
        it.status = "error";
        showError(`${it.label}: ${e.message}`);
      }
      renderBatch();
    }
  } finally {
    setBatchBusy(false);
    renderBatch();
  }
}

$("batchMaskAll").onclick = batchMaskAll;
$("batchSubmitMasked").onclick = batchSubmitMasked;
$("batchSubmitNext").onclick = () => runEdit({ thenNext: true });
$("batchSkip").onclick = () => {
  const it = currentBatchItem();
  if (!it) { showError("No batch image open."); return; }
  stashCurrentMask();   // keep a mask you already made, in case you come back
  it.status = "skipped";
  renderBatch();
  openNextBatchItem();
};
$("batchAutoAll").onclick = () => batchSubmitAll(true);
$("batchNoMaskAll").onclick = () => batchSubmitAll(false);
$("batchClear").onclick = () => {
  for (const it of state.batch) URL.revokeObjectURL(it.thumbUrl);
  state.batch = []; state.batchIdx = -1;
  renderBatch();
};
$("batchMore").onclick = () => {
  const open = $("batchMenu").hidden;
  $("batchMenu").hidden = !open;
  $("batchMore").setAttribute("aria-expanded", String(open));
};

// ------------------------------------------------------------------ setup page & no-mask mode
const MASK_TEXTS = {
  next: ["Queue the current image with its mask and open the next open one", "Queue the current image and open the next open one"],
  all: ["Submit all without mask", "Submit all"],
  allTitle: ["Queue every open image without a mask (whole image is edited)", "Queue every open image"],
};

// masks are used when SAM3 is installed and the mode is not "No mask"
function maskOn() { return !!state.maskAvailable && $("mode").value !== "none"; }


// Masking needs SAM3. Without it the UI hides everything about masks and never sends one.
function applyMaskMode(available) {
  state.maskAvailable = available;
  state.maskUiReady = true;
  document.body.classList.toggle("no-sam", !available);
  applyMaskTexts();
}

// hides / renames everything about masks when SAM3 is missing or the mode is "No mask"
function applyMaskTexts() {
  const i = maskOn() ? 0 : 1;
  document.body.classList.toggle("no-mask", !maskOn());
  $("batchSubmitNext").title = MASK_TEXTS.next[i];
  $("batchNoMaskAll").textContent = MASK_TEXTS.all[i];
  $("batchNoMaskAll").title = MASK_TEXTS.allTitle[i];
  setMode(state.mode);
  render();
}

let appStarted = false;
function showApp() {
  setup.close();
  $("appLayout").hidden = false;
  $("viewSwitch").hidden = false;
  if (appStarted) return;
  appStarted = true;
  initApp();
  startSession();
}
function showSetup(opts = {}) {
  $("appLayout").hidden = true;
  $("viewSwitch").hidden = true;
  setup.open(opts instanceof Event ? {} : opts).catch((e) => showError(e.message));
}

const setup = createSetup({
  api, postJson, root: $("setupView"),
  onBack: showApp,
  onReady: (data) => {
    state.setup = data;
    // SAM3 was installed or removed: reload so the whole UI switches mode
    if (appStarted && data.mask_available !== state.maskAvailable) { location.reload(); return; }
    applyMaskMode(data.mask_available);
    if (appStarted) { loadModels(); renderModelPicker(); renderUpscalers(); }   // ComfyUI was (re)started, model lists may have changed
    showApp();
  },
  // downloads or deletions while the page stays open: refresh the model picker
  onChanged: (data) => {
    state.setup = data;
    if (appStarted && data.mask_available !== state.maskAvailable) { location.reload(); return; }
    if (appStarted) { renderModelPicker(); renderUpscalers(); }
  },
});
$("setupBtn").onclick = showSetup;

// optional upscaler: lists the installed upscale models, links to the downloads page otherwise
function renderUpscalers() {
  const sel = $("upscaler");
  const ups = (state.setup?.components || []).filter((c) => c.kind === "upscaler" && c.installed);
  const keep = sel.value || storedForm().upscaler;
  sel.innerHTML = "";
  for (const u of ups) sel.add(new Option(`${u.title}`, u.key));
  if (ups.some((u) => u.key === keep)) sel.value = keep;
  sel.hidden = !ups.length;
  $("getUpscalers").hidden = !!ups.length;
}
$("getUpscalers").onclick = showSetup;
// the upscaler only matters with Upscale on
function syncUpscaler() {
  const off = $("upscale").value === "0";
  $("upscaler").disabled = off;
  $("upscalerRow").classList.toggle("dim", off);
}
$("upscale").addEventListener("change", syncUpscaler);

(async () => {
  pollStatus();
  setInterval(pollStatus, 5000);
  let info = null;
  try { info = await api("/api/setup"); } catch { /* old server without setup: just start the app */ }
  if (info) { state.setup = info; applyMaskMode(info.mask_available); renderUpscalers(); }
  if (info && !info.ready) showSetup();
  else showApp();
})();

// ------------------------------------------------------------------ model picker, edit | generate
let promptPresets = null;
const presetById = (id) => state.setup?.presets.find((p) => p.id === id);
const completePresets = () => (state.setup?.presets || []).filter((p) => p.complete);
const storedForm = () => { try { return JSON.parse(localStorage.getItem(STORE_KEY) || "{}"); } catch { return {}; } };

function setSelectValue(id, v) {
  const sel = $(id);
  if (![...sel.options].some((o) => o.value === String(v))) sel.add(new Option(v, v));
  sel.value = v;
}

// the file names a preset + quant resolves to, shown in the "From preset" override options
function updateOverrideLabels() {
  const p = presetById($("preset").value);
  const q = p?.quants.find((x) => x.quant === $("quant").value);
  const file = (key) => state.setup?.components.find((c) => c.key === key)?.file;
  for (const [id, f] of [["unet", q?.file], ["clip", file(p?.text_encoder)], ["vae", file(p?.vae)]]) {
    const o = $(id).options[0];
    if (o && o.value === "") o.textContent = f ? `From preset (${f})` : "From preset";
  }
  updateOverrideHint();
}
function updateOverrideHint() {
  $("overrideHint").hidden = !["unet", "clip", "vae"].some((id) => $(id).value);
}
function resetOverrides() {
  for (const id of ["unet", "clip", "vae"]) $(id).value = "";
  updateOverrideLabels();
}

function applyPresetDefaults(p) {
  const d = p.defaults || {};
  if (d.steps != null) $("steps").value = d.steps;
  if (d.cfg != null) $("cfg").value = d.cfg;
  if (d.sampler) setSelectValue("sampler", d.sampler);
  if (d.scheduler) setSelectValue("scheduler", d.scheduler);
  saveForm();
}

function fillQuant(p, keep, stored) {
  const sel = $("quant");
  const prev = sel.value || stored;
  sel.innerHTML = "";
  if (!p) return;
  const inst = p.quants.filter((q) => q.installed);
  const FIT = { tight: " · tight", no: " · too large" };
  for (const q of inst) sel.add(new Option(q.quant + (FIT[q.fit] || ""), q.quant));
  const rec = p.recommended_quant || p.default_quant;
  const want = (keep && inst.some((q) => q.quant === prev) && prev)
    || (inst.some((q) => q.quant === rec) && rec) || (inst.some((q) => q.quant === p.default_quant) && p.default_quant) || inst[0]?.quant;
  if (want) sel.value = want;
}

// Rebuilds task toggle, model and quant selects from state.setup. applyDefaults: take the preset's defaults.
function renderModelPicker({ applyDefaults = false } = {}) {
  const done = completePresets();
  const avail = ["edit", "generate"].filter((t) => done.some((p) => p.modes.includes(t)));
  if (avail.length && !avail.includes(state.task)) state.task = avail[0];
  for (const b of $("taskTabs").children) b.hidden = !avail.includes(b.dataset.task);
  $("taskTabs").hidden = avail.length < 2;
  const list = done.filter((p) => p.modes.includes(state.task)).sort((a, b) => (b.recommended ? 1 : 0) - (a.recommended ? 1 : 0));
  const sel = $("preset");
  const prev = sel.value || storedForm().preset;
  sel.innerHTML = "";
  for (const p of list) sel.add(new Option(p.title + (p.experimental ? " (experimental)" : ""), p.id));
  const want = list.find((p) => p.id === prev) || list.find((p) => p.id === state.setup.default_preset) || list[0];
  if (want) sel.value = want.id;
  sel.disabled = !want;
  $("quant").disabled = !want;
  fillQuant(want, true, storedForm().quant);
  if (applyDefaults && want) applyPresetDefaults(want);
  updateOverrideLabels();
  syncTaskUi();
  renderModelSel(list);
}

// single picker: one entry per installed preset + quant, grouped by model
function renderModelSel(list) {
  const sel = $("modelSel");
  sel.innerHTML = "";
  const FIT = { tight: " · tight", no: " · too large" };
  for (const p of list) {
    const grp = document.createElement("optgroup");
    grp.label = p.title + (p.experimental ? " (experimental)" : "");
    for (const q of p.quants.filter((x) => x.installed)) {
      grp.appendChild(new Option(`${p.title} · ${q.quant}${FIT[q.fit] || ""}`, `${p.id}|${q.quant}`));
    }
    sel.appendChild(grp);
  }
  sel.value = `${$("preset").value}|${$("quant").value}`;
  sel.disabled = !list.length;
  sel.title = sel.selectedOptions[0]?.textContent || "";
  $("modelListHint").textContent = state.task === "generate" ? "Text-to-image models only" : "Edit models only";
}
function initModelPicker() {
  renderModelPicker({ applyDefaults: !storedForm().preset });
  $("preset").addEventListener("change", () => {
    const p = presetById($("preset").value);
    fillQuant(p, false);
    if (p) applyPresetDefaults(p);
    resetOverrides();
    syncTaskUi();
    saveForm();
  });
  $("quant").addEventListener("change", () => { updateOverrideLabels(); saveForm(); });
  $("modelSel").addEventListener("change", () => {
    const [pid, q] = $("modelSel").value.split("|");
    if (pid !== $("preset").value) { $("preset").value = pid; $("preset").dispatchEvent(new Event("change")); }
    $("quant").value = q;
    $("quant").dispatchEvent(new Event("change"));
    $("modelSel").title = $("modelSel").selectedOptions[0]?.textContent || "";
  });
}

// UI parts that depend on the task and the selected model family
function syncTaskUi() {
  const gen = state.task === "generate";
  $("task").value = state.task;
  document.body.classList.toggle("task-generate", gen);
  for (const b of $("taskTabs").children) b.classList.toggle("active", b.dataset.task === state.task);
  const fam = currentFamily();
  const zedit = fam === "zimage" && !gen;
  document.body.classList.toggle("fam-zimage", zedit);
  // Z-Image cannot do free edit + paste (it does not follow instructions)
  $("mode").querySelector('[value="paste"]').disabled = zedit;
  if (zedit && $("mode").value === "paste") { $("mode").value = "inpaint"; syncModeUi(); }
  syncAreaCards();
  // turbo models: fixed 5-7 steps, no CFG; the server clamps and forces, the form just follows
  const turbo = fam === "qwen21_turbo";
  document.body.classList.toggle("fam-turbo", turbo);
  $("steps").min = turbo ? 5 : 1;
  $("steps").max = turbo ? 7 : 100;
  if (turbo) {
    $("steps").value = Math.min(7, Math.max(5, parseInt($("steps").value, 10) || 6));
    $("cfg").value = 1;
  }
  const hint = $("modelHint");
  const text = turbo ? "Turbo: 6 steps, no CFG" : zedit ? "Z-Image edits = img2img: lower Denoise (e.g. 0.6) keeps more of the original. It does not follow instructions."
    : fam === "qwen_edit" ? "Experimental: 20B model, slow on 32 GB." : "";
  hint.textContent = text;
  hint.hidden = !text;
  if (!state.submitting) $("runEdit").textContent = runLabel();
  renderRefs();
  promptPresets?.refresh();
}

// A new image always belongs to Edit: switch there (false if no installed model can edit)
function ensureEditTask() {
  if (state.task === "edit") return true;
  if (!completePresets().some((p) => p.modes.includes("edit"))) { showError("None of the installed models can edit images."); return false; }
  setTask("edit");
  return true;
}

function setTask(task) {
  if (state.task === task) return;
  state.task = task;
  renderModelPicker();
  if (task === "generate") {
    refreshSize();
  } else {
    if (state.imageName) refreshSize();
    else clearSizeInfo();
    render();
  }
  saveForm();
}
