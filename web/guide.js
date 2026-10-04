// Guide page: real sampler frames that "denoise" with scroll, the node graph that lights up per step,
// the quantisation bars and the one-click download of the recommended model. No dependencies.
"use strict";

const STEPS = 20;
// real frames (web/guide/): noisy_NN = the sampler's latent after step NN, decoded; guess_NN = the model's prediction
const noisySrc = (k) => `/guide/noisy_${String(k).padStart(2, "0")}.jpg`;
const guessSrc = (k) => `/guide/guess_${String(k).padStart(2, "0")}.jpg`;
for (let k = 0; k <= STEPS; k++) { new Image().src = noisySrc(k); if (k) new Image().src = guessSrc(k); }

// ------------------------------------------------------------------ the loop, driven by scroll
const loopSec = document.getElementById("loop");
const loopImg = document.getElementById("loopImg"), guess = document.getElementById("guess"), guessImg = document.getElementById("guessImg");
const stepNum = document.getElementById("stepNum"), stepBar = document.getElementById("stepBar");
const caption = document.getElementById("loopCaption");
const cycle = [...document.querySelectorAll(".cycle li")];
const CAPTIONS = [
  [0, "Step 0: only noise. The prompt decides what the noise will turn into."],
  [1, "First only light and dark: bright sky on top, dark ground below."],
  [6, "Then the shapes appear: hills, a lake, a small turf house."],
  [13, "The last steps add fine detail: grass on the roof, wooden boards, clouds."],
  [20, "Done. The translator turns the latent into the finished picture."],
];
let shownStep = -1;
function onScroll() {
  const r = loopSec.getBoundingClientRect();
  const p = Math.min(1, Math.max(0, -r.top / (r.height - innerHeight)));
  const exact = p * STEPS, step = Math.round(exact);
  const k = step >= STEPS ? -1 : Math.floor((exact % 1) * 4);
  cycle.forEach((li, i) => li.classList.toggle("on", i === k));
  if (step === shownStep) return;
  shownStep = step;
  loopImg.src = noisySrc(step);
  guess.classList.toggle("off", step === 0 || step === STEPS);
  if (step > 0) guessImg.src = guessSrc(step);
  stepNum.textContent = step;
  stepBar.style.width = `${(100 * step) / STEPS}%`;
  caption.textContent = CAPTIONS.filter(([s]) => step >= s).pop()[1];
}
addEventListener("scroll", () => requestAnimationFrame(onScroll), { passive: true });
addEventListener("resize", onScroll);
onScroll();

// ------------------------------------------------------------------ node graph
// the real graph for Qwen-Image 2.1 (graphs.build_edit_graph), simplified to the nodes that matter
const NODES = {
  unet:    { x: 20,  y: 30,  t: "Load diffusion model", cls: "UnetLoaderGGUF", c: "model" },
  lora:    { x: 250, y: 30,  t: "LoRA (optional)", cls: "LoraLoaderModelOnly", c: "addon" },
  cache:   { x: 480, y: 30,  t: "Prepare model", cls: "QwenImage21Cache", c: "model" },
  clip:    { x: 20,  y: 170, t: "Load text encoder", cls: "CLIPLoader", c: "text" },
  vae:     { x: 20,  y: 310, t: "Load VAE", cls: "VAELoader", c: "vae" },
  load:    { x: 20,  y: 430, t: "Your image", cls: "LoadImage", c: "io", only: "edit" },
  scale:   { x: 250, y: 430, t: "Scale to ~1 MP", cls: "ImageScaleToTotalPixels", c: "io", only: "edit" },
  encode:  { x: 480, y: 200, w: 220, t: "Read prompt + image", tGen: "Read prompt", cls: "TextEncodeQwenImage21", c: "text" },
  empty:   { x: 480, y: 380, t: "Empty latent", cls: "EmptyLatentImage", c: "io", only: "generate" },
  sampler: { x: 760, y: 150, w: 220, h: 100, t: "Sampler", cls: "KSampler", c: "model", sampler: true },
  decode:  { x: 760, y: 320, w: 220, t: "Latent → pixels", cls: "VAEDecode", c: "vae" },
  save:    { x: 760, y: 430, w: 220, t: "Save result", cls: "SaveImage", c: "io" },
};
const EDGES = [
  ["unet", "lora"], ["lora", "cache"], ["cache", "sampler"], ["clip", "encode"], ["vae", "encode"],
  ["load", "scale"], ["scale", "encode"], ["encode", "sampler"], ["empty", "sampler"], ["vae", "decode"],
  ["sampler", "decode", "v"], ["decode", "save", "v"],
];
const svg = document.getElementById("graphSvg"), NS = "http://www.w3.org/2000/svg";
const sv = (tag, attrs, parent) => { const e = document.createElementNS(NS, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); parent?.appendChild(e); return e; };
for (const n of Object.values(NODES)) { n.w ??= 200; n.h ??= 64; }

const edgeEls = EDGES.map(([a, b, v]) => {
  const A = NODES[a], B = NODES[b];
  let d;
  if (v) {
    const x = A.x + A.w / 2, y1 = A.y + A.h, y2 = B.y;
    d = `M${x} ${y1} C${x} ${y1 + 30} ${x} ${y2 - 30} ${x} ${y2}`;
  } else {
    const x1 = A.x + A.w, y1 = A.y + A.h / 2, x2 = B.x, y2 = B.y + B.h / 2, dx = Math.max(30, (x2 - x1) / 2);
    d = `M${x1} ${y1} C${x1 + dx} ${y1} ${x2 - dx} ${y2} ${x2} ${y2}`;
  }
  const only = A.only || B.only;
  return Object.assign(sv("path", { class: "edge", d }, svg), { from: a, to: b, only });
});
const nodeEls = Object.entries(NODES).map(([id, n]) => {
  const g = sv("g", { class: `node c-${n.c}${n.sampler ? " sampler" : ""}`, transform: `translate(${n.x} ${n.y})` }, svg);
  sv("rect", { width: n.w, height: n.h, rx: 12 }, g);
  sv("rect", { class: "bar", width: 6, height: n.h - 20, x: 8, y: 10, rx: 3 }, g);
  const title = sv("text", { x: 24, y: n.sampler ? 38 : 29 }, g);
  title.textContent = n.t;
  sv("text", { class: "cls", x: 24, y: n.sampler ? 58 : 48 }, g).textContent = n.cls;
  if (n.sampler) {
    const loop = sv("g", { transform: `translate(${n.w - 46} ${n.h / 2})` }, g);
    sv("path", { class: "spin", d: "M0 -16 A16 16 0 1 1 -14 8", fill: "none", stroke: "var(--c)", "stroke-width": 3, "stroke-linecap": "round" }, loop);
    sv("text", { class: "loop", x: 0, y: 5, "text-anchor": "middle" }, loop).textContent = "×20";
    sv("text", { class: "cls", x: 24, y: 82 }, g).textContent = "runs the loop";
  }
  return Object.assign(g, { id_: id, only: n.only, title, n });
});

const graphSec = document.getElementById("graph");
let graphMode = "edit", activeHl = null;
function paintGraph() {
  graphSec.dataset.graph = graphMode;
  const hide = (o) => !!o && o !== graphMode;
  for (const g of nodeEls) {
    g.classList.toggle("hidden", hide(g.only));
    g.classList.toggle("on", !!activeHl?.has(g.id_));
    g.classList.toggle("dim", !!activeHl && !activeHl.has(g.id_));
    g.title.textContent = graphMode === "generate" && g.n.tGen ? g.n.tGen : g.n.t;
  }
  for (const e of edgeEls) {
    const on = !!activeHl?.has(e.to);
    e.classList.toggle("hidden", hide(e.only));
    e.classList.toggle("on", on);
    e.classList.toggle("dim", !!activeHl && !on);
  }
  // narrow screens scroll the graph sideways: bring the lit nodes into view
  const wrap = svg.parentElement;
  const lit = activeHl ? [...activeHl].map((id) => NODES[id]).filter((n) => n && !hide(n.only)) : [];
  if (lit.length && wrap.scrollWidth > wrap.clientWidth) {
    const cx = lit.reduce((a, n) => a + n.x + n.w / 2, 0) / lit.length;
    wrap.scrollTo({ left: (cx / 1000) * svg.clientWidth - wrap.clientWidth / 2, behavior: "smooth" });
  }
}
for (const b of document.querySelectorAll(".seg button")) {
  b.onclick = () => {
    graphMode = b.dataset.graph;
    for (const o of document.querySelectorAll(".seg button")) o.setAttribute("aria-selected", String(o === b));
    paintGraph();
  };
}
const stepEls = [...document.querySelectorAll(".steps li")];
const stepObs = new IntersectionObserver((entries) => {
  for (const en of entries) {
    if (!en.isIntersecting) continue;
    for (const li of stepEls) li.classList.toggle("on", li === en.target);
    activeHl = new Set(en.target.dataset.hl.split(" "));
    paintGraph();
  }
}, { rootMargin: "-72% 0px -18% 0px" });
stepEls.forEach((li) => stepObs.observe(li));
paintGraph();

// ------------------------------------------------------------------ quantisation bars (Qwen-Image 2.1 UC, GB)
const QUANTS = [["Q4_0", 4.15], ["Q4_K_M", 4.60], ["Q5_K_M", 5.22], ["Q6_K", 5.88], ["Q8_0", 7.59], ["BF16", 14.23]];
const bars = document.getElementById("quantBars");
bars.innerHTML = QUANTS.map(([q, gb]) =>
  `<div class="qb${q === "Q4_K_M" ? " pick" : ""}"><b>${q}</b><div class="track"><div class="fill" data-w="${(100 * gb) / 14.23}"></div></div><em>${gb.toFixed(1)} GB</em></div>`).join("");
new IntersectionObserver((entries, obs) => {
  if (!entries[0].isIntersecting) return;
  for (const f of bars.querySelectorAll(".fill")) f.style.width = `${f.dataset.w}%`;
  obs.disconnect();
}, { threshold: 0.3 }).observe(bars);

// ------------------------------------------------------------------ section links in the top bar
const tocLinks = [...document.querySelectorAll(".toc a")];
const tocObs = new IntersectionObserver((entries) => {
  for (const en of entries) {
    if (en.isIntersecting) tocLinks.forEach((a) => a.classList.toggle("on", a.hash === `#${en.target.id}`));
  }
}, { rootMargin: "-40% 0px -55% 0px" });
tocLinks.forEach((a) => { const s = document.querySelector(a.hash); if (s) tocObs.observe(s); });

// ------------------------------------------------------------------ direct download of the recommended setup
// same queue as the Download Center (POST /api/setup/install); base steps are added on a fresh install
const REC = { preset: "qwen21_uc", quant: "Q4_K_M" };
const dlBtn = document.getElementById("dlBtn"), dlBar = document.getElementById("dlBar"), dlNote = document.getElementById("dlNote");
const gb = (b) => `${(b / 1e9).toFixed(1)} GB`;
let dlItems = [], dlTimer = null;

async function getJson(url, body) {
  const r = await fetch(url, body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {});
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
}
function recState(d) {
  const p = d.presets.find((x) => x.id === REC.preset), q = p.quants.find((x) => x.quant === REC.quant);
  const items = d.base.filter((b) => !b.installed).map((b) => b.id);
  let bytes = 0;
  if (!q.installed) { items.push(q.id); bytes += q.size; }
  for (const key of [p.text_encoder, p.vae]) {
    const c = d.components.find((x) => x.key === key);
    if (c && !c.installed) { items.push(c.id); bytes += c.size; }
  }
  return { items, bytes, base: d.base.some((b) => !b.installed), done: !items.length };
}
async function refreshDl() {
  let d, st;
  try { [d, st] = await Promise.all([getJson("/api/setup"), getJson("/api/status")]); }
  catch { dlNote.textContent = "The app is not running. Open Inpaint Studio to download."; return; }
  const r = recState(d), running = !!d.install?.running;
  dlItems = r.items;
  dlNote.classList.toggle("err", !!d.install?.error && !running);
  if (r.done) {
    dlBtn.textContent = "Installed"; dlBtn.classList.add("done"); dlBtn.disabled = true; dlBar.hidden = true;
    dlNote.innerHTML = 'Ready to go. <a href="/#create">Start editing</a>';
  } else if (running) {
    dlBtn.textContent = "Downloading…"; dlBtn.disabled = true; dlBar.hidden = false;
    const p = st.download;
    if (p?.total) {
      dlBar.firstElementChild.style.width = `${(100 * p.done) / p.total}%`;
      dlNote.textContent = `${gb(p.done)} of ${gb(p.total)}. You can close this page, it keeps going.`;
    } else dlNote.textContent = "Installing…";
  } else {
    dlBtn.textContent = `Download · ${gb(r.bytes)}`; dlBtn.disabled = false; dlBar.hidden = true;
    dlNote.textContent = d.install?.error ? `Stopped: ${d.install.error}. Press Download to try again.`
      : r.base ? "Also installs ComfyUI, the engine (about 1.5 GB more)." : "Qwen-Image 2.1 UC Q4_K_M with its text encoder and VAE.";
  }
  clearTimeout(dlTimer);
  if (running) dlTimer = setTimeout(refreshDl, 1500);
}
dlBtn.onclick = async () => {
  dlBtn.disabled = true;
  try { await getJson("/api/setup/install", { items: dlItems }); }
  catch (e) { dlNote.textContent = e.message; dlNote.classList.add("err"); }
  refreshDl();
};
refreshDl();
// opening the guide once is enough: from now on "/" opens the app
fetch("/api/guide/seen", { method: "POST" }).catch(() => {});
