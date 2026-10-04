// Guide page: a drawn scene that "denoises" with scroll, the node graph that lights up per step,
// and the quantisation bars. No dependencies.
"use strict";

// ------------------------------------------------------------------ scene + noise frames
const W = 360, H = 240, STEPS = 20;

function drawScene(c) {
  const sky = c.createLinearGradient(0, 0, 0, H);
  sky.addColorStop(0, "#86BDEB"); sky.addColorStop(1, "#EAF3FA");
  c.fillStyle = sky; c.fillRect(0, 0, W, H);
  c.fillStyle = "#FFD66B"; c.beginPath(); c.arc(282, 58, 22, 0, Math.PI * 2); c.fill();
  c.fillStyle = "#9DB2C6"; c.beginPath();
  c.moveTo(0, 150); c.lineTo(60, 96); c.lineTo(110, 130); c.lineTo(175, 82); c.lineTo(250, 138); c.lineTo(310, 104); c.lineTo(360, 140);
  c.lineTo(360, 240); c.lineTo(0, 240); c.fill();
  c.fillStyle = "#6FA85A"; c.beginPath(); c.moveTo(0, 168); c.quadraticCurveTo(180, 132, 360, 160); c.lineTo(360, 240); c.lineTo(0, 240); c.fill();
  c.fillStyle = "#4F8C3E"; c.beginPath(); c.moveTo(0, 206); c.quadraticCurveTo(200, 186, 360, 214); c.lineTo(360, 240); c.lineTo(0, 240); c.fill();
  // turf house
  c.fillStyle = "#F3EEE4"; c.fillRect(118, 142, 76, 36);
  c.fillStyle = "#4A3A2A"; c.fillRect(146, 156, 15, 22);
  c.fillStyle = "#6E8FAE"; c.fillRect(172, 151, 12, 11); c.fillRect(126, 151, 12, 11);
  c.fillStyle = "#5C9A47"; c.strokeStyle = "#3F6E30"; c.lineWidth = 3;
  c.beginPath(); c.moveTo(106, 146); c.quadraticCurveTo(156, 92, 206, 146); c.closePath(); c.fill(); c.stroke();
  c.fillStyle = "#8A8A8A"; c.fillRect(178, 108, 8, 18);
  // lake
  c.fillStyle = "#7FB2DA"; c.beginPath(); c.ellipse(268, 196, 54, 10, 0, 0, Math.PI * 2); c.fill();
}

const frames = (() => {
  const mk = (w, h) => { const cv = document.createElement("canvas"); cv.width = w; cv.height = h; return cv; };
  const sharpCv = mk(W, H), sc = sharpCv.getContext("2d");
  drawScene(sc);
  const sharp = sc.getImageData(0, 0, W, H).data;
  // a blob version: what the layout looks like in the first steps
  const tiny = mk(12, 8); tiny.getContext("2d").drawImage(sharpCv, 0, 0, 12, 8);
  const blurCv = mk(W, H), bc = blurCv.getContext("2d");
  bc.imageSmoothingEnabled = true; bc.imageSmoothingQuality = "high"; bc.drawImage(tiny, 0, 0, W, H);
  const blur = bc.getImageData(0, 0, W, H).data;
  // fixed seed: the same noise on every load, like a fixed seed in the app
  let s = 7;
  const rnd = () => { s |= 0; s = (s + 0x6D2B79F5) | 0; let t = Math.imul(s ^ (s >>> 15), 1 | s); t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t; return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
  const noise = new Float32Array(W * H * 3);
  for (let i = 0; i < noise.length; i++) noise[i] = 128 + (rnd() + rnd() + rnd() - 1.5) * 150;
  const cache = new Map();
  return (step) => {
    if (cache.has(step)) return cache.get(step);
    const t = step / STEPS, a = Math.pow(t, 1.25), d = Math.min(1, Math.max(0, (t - 0.2) / 0.6));
    const cv = mk(W, H), cx = cv.getContext("2d"), img = cx.createImageData(W, H), o = img.data;
    for (let p = 0, n = 0; p < o.length; p += 4, n += 3) {
      for (let k = 0; k < 3; k++) {
        const base = blur[p + k] * (1 - d) + sharp[p + k] * d;
        o[p + k] = a * base + (1 - a) * noise[n + k];
      }
      o[p + 3] = 255;
    }
    cx.putImageData(img, 0, 0);
    cache.set(step, cv);
    return cv;
  };
})();

for (const cv of document.querySelectorAll(".strip canvas")) {
  cv.getContext("2d").drawImage(frames(+cv.dataset.step), 0, 0, cv.width, cv.height);
}

// ------------------------------------------------------------------ the loop, driven by scroll
const loopSec = document.getElementById("loop");
const loopCv = document.getElementById("loopCanvas"), loopCx = loopCv.getContext("2d");
const stepNum = document.getElementById("stepNum"), stepBar = document.getElementById("stepBar");
const caption = document.getElementById("loopCaption");
const cycle = [...document.querySelectorAll(".cycle li")];
const CAPTIONS = [
  [0, "Step 0: only noise. The prompt decides what the noise will turn into."],
  [1, "First the rough layout: light sky on top, dark ground below, something in the middle."],
  [6, "Then shapes and colours come in: a hill, a house, a sun."],
  [13, "The last steps add fine detail: edges, texture, small lights."],
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
  loopCx.drawImage(frames(step), 0, 0);
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
