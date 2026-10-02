// Inpaint Studio frontend: two-step mask + edit UI. Vanilla ES module, no build step.

const $ = (id) => document.getElementById(id);

// ------------------------------------------------------------------ persisted form fields
const PERSIST = [
  "megapixels", "resolution", "autofix", "maskText", "threshold", "refine", "expand", "invert",
  "brushSize", "opacity", "useMask", "prompt", "negative", "steps", "denoise", "feather", "mode", "keepIdentical", "saveEvery", "saveLast", "seed",
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
const endStroke = () => { if (stroking) scheduleMaskSave(); stroking = false; last = null; };
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
  // in the Mask view, arrow keys switch between batch images
  if ((e.key === "ArrowLeft" || e.key === "ArrowRight") && !typing && $("resultView").hidden && state.batch.length) {
    e.preventDefault();
    const n = state.batch.length;
    const i = state.batchIdx < 0 ? 0 : (state.batchIdx + (e.key === "ArrowRight" ? 1 : -1) + n) % n;
    openBatchItem(i);
    return;
  }
  // arrow keys step through the visible filmstrip; past the last frame shows the final comparison
  if ((e.key === "ArrowLeft" || e.key === "ArrowRight") && !typing && !$("resultView").hidden) {
    const run = state.run;
    const frames = run ? visibleFrames(run) : [];
    if (!frames.length) return;
    e.preventDefault();
    const onCompare = !$("compare").hidden;
    const last = frames.length - 1;
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
    saveSession({ imageName: up.name, srcW: up.width, srcH: up.height, label: file.name || "image", maskName: null, maskMeta: null });
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
  $("runEdit").textContent = on ? "Adding to queue..." : "Run edit (add to queue)";
}

// edit parameters from the form for one image (seed is drawn per job when "Random" is on)
function editParams({ image, srcW, srcH, maskName, useMask, megapixels, resolution }) {
  let seed = parseInt($("seed").value, 10) || 0;
  if ($("randomSeed").checked) { seed = Math.floor(Math.random() * 2 ** 32); $("seed").value = seed; }
  return {
    image, mask: maskName, use_mask: useMask, src_w: srcW, src_h: srcH,
    megapixels: megapixels ?? num("megapixels"), resolution: resolution ?? parseInt($("resolution").value, 10),
    prompt: $("prompt").value, negative: $("negative").value,
    steps: parseInt($("steps").value, 10), denoise: num("denoise"), seed, cfg: num("cfg"),
    sampler: $("sampler").value, scheduler: $("scheduler").value, feather: num("feather"), mode: $("mode").value,
    keep_identical: $("keepIdentical").checked, save_every: parseInt($("saveEvery").value, 10) || 0,
    save_last: parseInt($("saveLast").value, 10) || 0,
    unet: $("unet").value, clip: $("clip").value, vae: $("vae").value, preview_method: "auto",
  };
}

async function uploadMaskBlob(blob) {
  const fd = new FormData();
  fd.append("file", blob, "mask.png");
  return (await api("/api/upload-mask", { method: "POST", body: fd })).name;
}

async function runEdit({ thenNext = false } = {}) {
  if (state.submitting) return;
  if (!state.imageName) { showError("Load an image first."); return; }
  const useMask = $("useMask").checked;
  setSubmitting(true);
  try {
    const rep = await refreshSize();
    if (!rep) return;
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

function jobFromSummary(sum) {
  let job = state.jobs.get(sum.job_id);
  if (!job) {
    job = { id: sum.job_id, serverId: sum.job_id, prompt: sum.prompt, seed: sum.seed, steps: sum.steps,
      max: sum.steps, value: 0, frames: [], done: false, status: "queued", t0: null, created: sum.created };
    state.jobs.set(sum.job_id, job);
  }
  job.status = sum.status || job.status;
  job.value = sum.value || job.value;
  if (sum.frames) job.frames = sum.frames.map((f) => ({ ...f }));
  if (job.status === "running" && !job.t0) job.t0 = performance.now();
  return job;
}

async function submitJob(params) {
  const sum = await postJson("/api/jobs", params);
  const job = jobFromSummary(sum);
  renderQueue();
  // show it right away if nothing else is running, otherwise it just waits in the queue
  if (state.follow && ![...state.jobs.values()].some((j) => j !== job && j.status === "running")) viewJob(job);
  setView("result");
}

function viewJob(job) {
  freeRunFrames(state.run);
  state.run = job;
  $("resultEmpty").hidden = true;
  $("compare").hidden = true;
  $("liveImg").hidden = true;
  $("resultActions").hidden = true;
  $("showCompare").hidden = true;
  $("progressWrap").hidden = false;
  $("matchInfo").textContent = "";
  clearInterval(state.progressTimer);
  state.progressTimer = setInterval(updateProgressText, 500);
  updateProgressText();
  renderFilmstrip();
  syncViewerOpts();
  const vis = visibleFrames(job);
  if (vis.length) showFrame(vis.length - 1);
  renderQueue();
  renderHistory();
}

function addJobFrame(job, frame) {
  if (job.frames.some((f) => f.url === frame.url)) return;
  if (frame.kind === "saved") job.frames = job.frames.filter((f) => !(f.kind === "live" && f.step === frame.step));
  frameAdded(job, frame);
}

function jobFinished(m) {
  const job = state.jobs.get(m.job_id);
  state.jobs.delete(m.job_id);
  renderQueue();
  const viewing = job && state.run === job;
  if (m.type === "done" && m.run) {
    const run = runFromStored(m.run);
    if (!state.runs.some((r) => r.serverId === run.serverId)) state.runs.unshift(run);
    renderHistory();
    if (viewing) {
      clearInterval(state.progressTimer);
      showRun(run);
      $("progressWrap").hidden = false;
      $("progressBar").style.width = "100%";
      $("progressText").textContent = job.t0 ? `Done in ${fmtTime((performance.now() - job.t0) / 1000)}` : "Done";
    }
  } else if (viewing) {
    clearInterval(state.progressTimer);
    $("progressText").textContent = m.type === "cancelled" ? "Cancelled" : `Failed: ${m.run?.error || "unknown error"}`;
  }
  if (m.type === "error") showError(m.run?.error || "Job failed");
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
        if (running && state.follow) { setView("result"); viewJob(running); }
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
        else if (state.run === job) updateProgressText();
        break;
      }
      case "progress": {
        const job = state.jobs.get(m.job_id);
        if (!job) break;
        job.value = m.value; job.max = m.max;
        if (state.run === job) updateProgressText();
        renderQueue();
        break;
      }
      case "frame": { const job = state.jobs.get(m.job_id); if (job) addJobFrame(job, m.frame); break; }
      case "done": case "error": case "cancelled": jobFinished(m); break;
    }
  };
  ws.onclose = () => setTimeout(connectJobs, 2000); // server restart / sleep -> reconnect
}

function renderQueue() {
  const box = $("queue");
  const jobs = [...state.jobs.values()].sort((a, b) => a.created - b.created);
  $("queueWrap").hidden = !jobs.length;
  box.innerHTML = "";
  let pos = 0;
  for (const job of jobs) {
    const b = document.createElement("button");
    b.className = "queue-item" + (job === state.run ? " active" : "");
    const status = job.status === "running" ? `running · step ${job.value} / ${job.max}` : `queued #${++pos}`;
    const t = document.createElement("div"); t.className = "t"; t.textContent = job.prompt;
    const st = document.createElement("div"); st.className = "s"; st.textContent = `${status} · seed ${job.seed}`;
    const d = document.createElement("div"); d.append(t, st);
    const x = document.createElement("span"); x.className = "hist-del"; x.textContent = "×";
    x.title = job.status === "running" ? "Cancel this job" : "Remove from queue";
    x.onclick = (ev) => { ev.stopPropagation(); cancelJob(job); };
    b.append(d, x);
    b.onclick = () => { state.follow = true; setView("result"); viewJob(job); };
    box.appendChild(b);
  }
  renderQueueLabel();
}

function renderQueueLabel() {
  const tab = document.querySelector('#viewTabs [data-view="result"]');
  const n = state.jobs ? state.jobs.size : 0;
  if (tab) tab.textContent = `Result${state.runs.length ? ` · ${state.runs.length} saved` : ""}${n ? ` · ${n} queued` : ""}`;
}

async function cancelJob(job) {
  try { await postJson(`/api/jobs/${encodeURIComponent(job.id)}/cancel`, {}); } catch (e) { showError(e.message); }
}

function updateProgressText() {
  const run = state.run;
  if (!run || run.done) return;
  if (run.status === "queued") {
    const ahead = [...state.jobs.values()].filter((j) => j.created < run.created).length;
    $("progressBar").style.width = "0%";
    $("progressText").textContent = `In queue – ${ahead} job${ahead === 1 ? "" : "s"} ahead`;
    return;
  }
  const pct = run.max ? (run.value / run.max) * 100 : 0;
  $("progressBar").style.width = `${pct}%`;
  const el = (performance.now() - (run.t0 || performance.now())) / 1000;
  let txt = run.value ? `step ${run.value} / ${run.max}` : "Waiting for sampler...";
  txt += ` · ${fmtTime(el)} elapsed`;
  if (run.value > 0 && run.value < run.max) txt += ` · ~${fmtTime(el / run.value * (run.max - run.value))} left`;
  $("progressText").textContent = txt;
}

function frameAdded(run, frame) {
  run.frames.push(frame);
  if (state.run !== run) return;
  syncViewerOpts();
  const vis = visibleFrames(run);
  const idx = vis.indexOf(frame);
  renderFilmstrip();
  if (idx >= 0 && !run.done) showFrame(idx);
}

// viewer filters (default off): live previews and raw full images are only shown on demand
function visibleFrames(run) {
  const wantLive = $("viewLive").checked, wantRaw = $("viewRaw").checked;
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
    const pick = (wantRaw && s.raw) || s.result || (wantLive && s.live) || null;
    if (pick) out.push(pick);
  }
  return out;
}

function syncViewerOpts() {
  const run = state.run;
  $("viewerOpts").hidden = !run;
  $("viewRawRow").hidden = !(run && (run.rawUrl || run.frames.some((f) => f.variant === "raw")));
}

function showFrame(i) {
  const run = state.run;
  const f = run && visibleFrames(run)[i];
  if (!f) return;
  $("liveImg").src = f.url;
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
  visibleFrames(run).forEach((f, i) => {
    const img = document.createElement("img");
    img.src = f.url;
    img.title = f.kind === "live" ? `Step ${f.step} – live preview`
      : f.variant === "raw" ? `Step ${f.step} – raw full image (saved)` : `Step ${f.step} – full quality (saved)`;
    if (f.kind === "saved") img.classList.add("saved");
    if (f.variant === "raw") img.classList.add("raw");
    img.onclick = () => showFrame(i);
    if (i === run.shown) img.classList.add("active");
    fs.appendChild(img);
  });
  if (!run.done) fs.scrollLeft = fs.scrollWidth;
}

for (const id of ["viewLive", "viewRaw"]) {
  $(id).addEventListener("change", () => {
    const run = state.run;
    if (!run) return;
    run.shown = undefined;
    renderFilmstrip();
    if (run.done) showCompare(); else { const n = visibleFrames(run).length; if (n) showFrame(n - 1); }
  });
}

function freeRunFrames(run) {
  if (!run || state.runs.some((r) => r === run)) return;
  for (const f of run.frames) if (f.url.startsWith("blob:")) URL.revokeObjectURL(f.url);
}

// ------------------------------------------------------------------ persisted run history
function runFromStored(r) {
  return {
    id: r.id, serverId: r.id, prompt: r.params?.prompt || "", seed: r.params?.seed, steps: r.params?.steps,
    frames: (r.frames || []).map((f) => ({ ...f })), resultUrl: r.result_url, beforeUrl: r.before_url,
    rawUrl: r.raw_url || null, maskUrl: r.mask_url || null, filename: r.filename, done: true,
    aligned: r.aligned || null,
    value: r.params?.steps, max: r.params?.steps, created: r.created,
  };
}

async function loadStoredRuns() {
  try {
    const runs = await api("/api/runs");
    state.runs = runs.map(runFromStored);
    renderHistory();
  } catch (e) { showError(`Could not load run history: ${e.message}`); }
}

async function deleteRun(run) {
  try {
    if (run.serverId) await api(`/api/runs/${encodeURIComponent(run.serverId)}`, { method: "DELETE" });
    state.runs = state.runs.filter((r) => r !== run);
    renderHistory();
  } catch (e) { showError(e.message); }
}

$("cancelEdit").onclick = () => {
  const job = (state.run && state.jobs.get(state.run.id)) || [...state.jobs.values()].find((j) => j.status === "running");
  if (job) cancelJob(job); else showError("No queued or running job.");
};

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
  syncViewerOpts();
  $("alignBtn").hidden = !(run.serverId && run.rawUrl && run.maskUrl);
  $("alignPanel").hidden = true;
  if (run.aligned) $("downloadBtn").href = run.aligned.url;
  $("matchInfo").textContent = run.match || "";
  if (run.rawUrl && run.maskUrl && !run.match) measureMatch(run);
}

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
  $("cmpAfter").src = $("viewRaw").checked && run.rawUrl ? run.rawUrl : (run.aligned?.url || run.resultUrl);
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
  renderQueueLabel();
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
    const when = run.created ? new Date(run.created * 1000).toLocaleString() : "";
    const s = document.createElement("div"); s.className = "s";
    s.textContent = [when, `seed ${run.seed}`, `${run.steps ?? run.max} steps`].filter(Boolean).join(" · ");
    d.append(t, s);
    const del = document.createElement("span"); del.className = "hist-del"; del.textContent = "×"; del.title = "Delete from history";
    del.onclick = (ev) => { ev.stopPropagation(); deleteRun(run); };
    b.append(img, d, del);
    b.onclick = () => { state.follow = false; clearInterval(state.progressTimer); setView("result"); showRun(run); $("progressWrap").hidden = true; };
    box.appendChild(b);
  }
}

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
  $("runEdit").onclick = () => runEdit();
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
    if (!state.mask || !state.hasMask || !state.imageName) return;
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
    $("imageInfo").textContent = `${sess.label || "image"} – original ${sess.srcW}×${sess.srcH} (restored)`;
    $("dropText").textContent = "Drop another image or click to replace";
    resetMask();
    state.size = null;
    await refreshSize();
    if (sess.maskName) {
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

(async () => {
  const [restored] = await Promise.all([restoreSession(), loadStoredRuns()]);
  connectJobs();
  // nothing to work on yet -> show the latest result instead of an empty page
  if (!restored && state.runs.length) { setView("result"); showRun(state.runs[0]); $("progressWrap").hidden = true; }
})();

// ------------------------------------------------------------------ advanced: post-hoc alignment
let alignTimer = 0;
function alignValues() {
  return { dx: num("alignDx") || 0, dy: num("alignDy") || 0, scale: (num("alignScale") || 100) / 100 };
}
function setAlignValues(v) {
  $("alignDx").value = Math.round(v.dx * 10) / 10;
  $("alignDy").value = Math.round(v.dy * 10) / 10;
  $("alignScale").value = Math.round(v.scale * 10000) / 100;
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
      run.aligned = { dx: res.dx, dy: res.dy, scale: res.scale, outside_diff: res.outside_diff, url: res.url };
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
  setAlignValues(run.aligned || { dx: 0, dy: 0, scale: 1 });
  $("alignInfo").textContent = "Try Auto-align, then fine-tune with the arrows (Shift = 5 px).";
};
$("alignClose").onclick = () => { $("alignPanel").hidden = true; showCompare(); };
$("alignAuto").onclick = () => alignRequest({ auto: true, save: false });
$("alignReset").onclick = () => { setAlignValues({ dx: 0, dy: 0, scale: 1 }); schedulePreview(); };
$("alignSave").onclick = () => alignRequest({ ...alignValues(), save: true });
for (const id of ["alignDx", "alignDy", "alignScale"]) $(id).addEventListener("input", schedulePreview);
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
  if (imgs.length === 1 && !state.batch.length) { setImageFile(imgs[0]); return; }
  for (const it of state.batch) URL.revokeObjectURL(it.thumbUrl);
  state.batch = imgs.map((file, i) => ({ id: i, file, label: file.name, thumbUrl: URL.createObjectURL(file),
    name: null, srcW: 0, srcH: 0, status: "open", mask: null, maskMeta: null }));
  state.batchIdx = -1;
  renderBatch();
  openBatchItem(0);
}

function currentBatchItem() {
  const it = state.batch[state.batchIdx];
  return it && it.name === state.imageName ? it : null;
}

function renderBatch() {
  $("batchWrap").hidden = !state.batch.length;
  const grid = $("batchGrid");
  grid.innerHTML = "";
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
  $("batchInfo").textContent = `${state.batch.length} images · ${counts.queued} queued · ${counts.open + counts.masked} open`
    + (counts.nomask ? ` · ${counts.nomask} without mask found` : "") + (counts.skipped ? ` · ${counts.skipped} skipped` : "")
    + (counts.error ? ` · ${counts.error} failed` : "");
}

function stashCurrentMask() {
  const it = currentBatchItem();
  if (!it || !state.hasMask || !state.mask) return;
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
    setView("mask");
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
  if (!rep.safe && $("autofix").checked && rep.suggested) return { ...rep.suggested };
  return { ...rep, megapixels: body.megapixels, resolution: body.resolution };
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
