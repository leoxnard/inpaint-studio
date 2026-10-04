// Compare several runs side by side (Runs view). Two modes:
//  - "detail" (default): a grid where every tile shows the same part of its image; wheel / drag / pinch in any
//    tile zoom and pan all tiles together.
//  - "split": one frame cut into equal fixed vertical strips, strip k shows run k.
// In both modes the labels can be dragged onto another one to change the order.
// The view is kept relative to the first image (centre and width as fractions), so images of different sizes
// show the same spot; images with another aspect ratio are fitted around the same centre, never stretched.

const GAP = 6;

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

export function createMultiCompare(root, { onExit }) {
  let items = [];          // [{key, url, label, title}] in display order
  let mode = "detail";
  let note = "";
  const nat = new Map();   // url -> {w, h} once loaded
  const view = { cx: 0.5, cy: 0.5, w: 1 };   // relative to the first image; w = part width / image width
  let tileAspect = 1;
  let tiles = [];          // {item, box, img} of the detail grid
  let dragKey = null;
  let fresh = true;        // next detail layout starts with the largest part

  root.innerHTML = "";
  const bar = el("div", "mc-bar");
  const title = el("b", "mc-title");
  const seg = el("div", "seg");
  seg.setAttribute("role", "group");
  seg.setAttribute("aria-label", "Compare mode");
  const modeBtns = {};
  for (const [m, label, tip] of [["detail", "Detail", "Every tile shows the same part of its image"],
                                 ["split", "Split", "One frame cut into fixed strips, one per run"]]) {
    const b = el("button", null, label);
    b.type = "button";
    b.title = tip;
    b.onclick = () => { mode = m; render(); };
    modeBtns[m] = b;
    seg.append(b);
  }
  const hint = el("span", "hint mc-hint");
  const spacer = el("span", "spacer");
  const exit = el("button", null, "Exit compare");
  exit.type = "button";
  exit.onclick = () => onExit();
  bar.append(title, seg, hint, spacer, exit);
  const stage = el("div", "mc-stage");
  root.append(bar, stage);
  new ResizeObserver(() => { if (items.length) layout(); }).observe(stage);

  const ref = () => nat.get(items[0]?.url) || { w: 1, h: 1 };
  const refAspect = () => ref().w / ref().h;
  // height of the part as a fraction of the first image's height
  const viewH = () => (view.w * ref().w / tileAspect) / ref().h;
  const maxW = () => Math.min(1, tileAspect * ref().h / ref().w);

  function clampView() {
    view.w = Math.max(0.02, Math.min(view.w, maxW()));
    const hw = view.w / 2, hh = viewH() / 2;
    view.cx = Math.max(hw, Math.min(1 - hw, view.cx));
    view.cy = Math.max(hh, Math.min(1 - hh, view.cy));
  }

  function loadSizes() {
    return Promise.all(items.map((it) => nat.has(it.url) ? null : new Promise((res) => {
      const im = new Image();
      im.onload = () => { nat.set(it.url, { w: im.naturalWidth, h: im.naturalHeight }); res(); };
      im.onerror = () => { nat.set(it.url, { w: 1, h: 1 }); res(); };
      im.src = it.url;
    })));
  }

  // ---------------------------------------------------------------- labels (drag to reorder)
  function label(it) {
    const l = el("span", "mc-label", it.label);
    l.title = `${it.title || it.label}\n\nDrag onto another label to change the order.`;
    l.draggable = true;
    l.addEventListener("dragstart", (e) => { dragKey = it.key; e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/plain", it.key); });
    l.addEventListener("dragend", () => { dragKey = null; root.querySelectorAll(".drop").forEach((x) => x.classList.remove("drop")); });
    l.addEventListener("pointerdown", (e) => e.stopPropagation());   // no pan while grabbing a label
    return l;
  }
  function dropTarget(node, it) {
    node.addEventListener("dragover", (e) => { if (dragKey && dragKey !== it.key) { e.preventDefault(); node.classList.add("drop"); } });
    node.addEventListener("dragleave", () => node.classList.remove("drop"));
    node.addEventListener("drop", (e) => {
      e.preventDefault();
      node.classList.remove("drop");
      const from = items.findIndex((x) => x.key === dragKey), to = items.findIndex((x) => x.key === it.key);
      if (from < 0 || to < 0 || from === to) return;
      const [moved] = items.splice(from, 1);
      items.splice(to, 0, moved);
      render();
    });
  }

  // ---------------------------------------------------------------- detail grid
  // columns: tiles closest to the image's aspect ratio, few empty cells
  function grid(W, H, n) {
    let best = null;
    for (let c = 1; c <= n; c++) {
      const r = Math.ceil(n / c);
      const tw = (W - (c - 1) * GAP) / c, th = (H - (r - 1) * GAP) / r;
      if (tw <= 0 || th <= 0) continue;
      const score = Math.abs(Math.log((tw / th) / refAspect())) + 0.35 * (c * r - n);
      if (!best || score < best.score) best = { c, r, tw, th, score };
    }
    return best;
  }

  function placeImage(t) {
    const n = nat.get(t.item.url);
    if (!n) return;
    const tw = t.box.clientWidth, th = t.box.clientHeight;
    const s = tw / (view.w * n.w);
    t.img.style.width = `${n.w * s}px`;
    t.img.style.height = `${n.h * s}px`;
    t.img.style.transform = `translate(${tw / 2 - view.cx * n.w * s}px, ${th / 2 - view.cy * n.h * s}px)`;
  }

  function updateView() {
    clampView();
    for (const t of tiles) placeImage(t);
  }

  function buildDetail() {
    const W = stage.clientWidth, H = stage.clientHeight;
    const g = grid(W, H, items.length);
    if (!g) return;
    const wasFull = view.w >= maxW() - 1e-6;
    tileAspect = g.tw / g.th;
    if (fresh || wasFull) view.w = maxW();   // default: as large as possible
    fresh = false;
    tiles = [];
    items.forEach((it, i) => {
      const row = Math.floor(i / g.c), col = i % g.c;
      const inRow = row === g.r - 1 ? items.length - row * g.c : g.c;
      const offset = (g.c - inRow) * (g.tw + GAP) / 2;   // centre the last row
      const box = el("div", "mc-tile");
      Object.assign(box.style, { left: `${offset + col * (g.tw + GAP)}px`, top: `${row * (g.th + GAP)}px`, width: `${g.tw}px`, height: `${g.th}px` });
      const img = el("img");
      img.src = it.url;
      img.alt = it.label;
      img.draggable = false;
      box.append(img, label(it));
      dropTarget(box, it);
      stage.append(box);
      tiles.push({ item: it, box, img });
      panZoom(box);
    });
    updateView();
  }

  // wheel zooms around the cursor, drag pans, two fingers pinch; all tiles follow
  function panZoom(box) {
    const zoomAt = (mx, my, f) => {
      const tw = box.clientWidth, th = box.clientHeight;
      const dx = (mx / tw - 0.5) * view.w, dy = (my / th - 0.5) * viewH();
      const px = view.cx + dx, py = view.cy + dy;
      const w0 = view.w;
      view.w = Math.max(0.02, Math.min(view.w * f, maxW()));
      const k = view.w / w0;
      view.cx = px - dx * k;
      view.cy = py - dy * k;
      updateView();
    };
    box.addEventListener("wheel", (e) => {
      e.preventDefault();
      const r = box.getBoundingClientRect();
      zoomAt(e.clientX - r.left, e.clientY - r.top, Math.exp(e.deltaY * 0.0015));
    }, { passive: false });
    const pts = new Map();
    let pinch = null;
    box.addEventListener("pointerdown", (e) => {
      box.setPointerCapture(e.pointerId);
      pts.set(e.pointerId, { x: e.clientX, y: e.clientY });
      if (pts.size === 2) {
        const [a, b] = [...pts.values()];
        pinch = Math.hypot(a.x - b.x, a.y - b.y);
      }
    });
    box.addEventListener("pointermove", (e) => {
      const p = pts.get(e.pointerId);
      if (!p) return;
      if (pts.size === 1) {
        view.cx -= (e.clientX - p.x) / box.clientWidth * view.w;
        view.cy -= (e.clientY - p.y) / box.clientHeight * viewH();
        p.x = e.clientX; p.y = e.clientY;
        updateView();
        return;
      }
      p.x = e.clientX; p.y = e.clientY;
      const [a, b] = [...pts.values()];
      const d = Math.hypot(a.x - b.x, a.y - b.y);
      if (pinch && d) {
        const r = box.getBoundingClientRect();
        zoomAt((a.x + b.x) / 2 - r.left, (a.y + b.y) / 2 - r.top, pinch / d);
      }
      pinch = d;
    });
    const up = (e) => { pts.delete(e.pointerId); if (pts.size < 2) pinch = null; };
    box.addEventListener("pointerup", up);
    box.addEventListener("pointercancel", up);
  }

  // ---------------------------------------------------------------- split
  function buildSplit() {
    const W = stage.clientWidth, H = stage.clientHeight;
    const a = refAspect();
    const fw = Math.min(W, H * a), fh = fw / a;
    const frame = el("div", "mc-split");
    Object.assign(frame.style, { width: `${fw}px`, height: `${fh}px`, left: `${(W - fw) / 2}px`, top: `${(H - fh) / 2}px` });
    const n = items.length;
    items.forEach((it, i) => {
      const img = el("img");
      img.src = it.url;
      img.alt = it.label;
      img.draggable = false;
      img.style.clipPath = `inset(0 ${100 - (100 * (i + 1)) / n}% 0 ${(100 * i) / n}%)`;
      frame.append(img);
    });
    items.forEach((it, i) => {
      const strip = el("div", "mc-strip");
      Object.assign(strip.style, { left: `${(100 * i) / n}%`, width: `${100 / n}%` });
      strip.append(label(it));
      dropTarget(strip, it);
      frame.append(strip);
    });
    stage.append(frame);
  }

  // ---------------------------------------------------------------- render
  function layout() {
    stage.innerHTML = "";
    tiles = [];
    if (mode === "detail") buildDetail(); else buildSplit();
  }

  function render() {
    for (const [m, b] of Object.entries(modeBtns)) b.setAttribute("aria-pressed", String(m === mode));
    title.textContent = `Comparing ${items.length} runs`;
    hint.textContent = [note, mode === "detail" ? "Scroll or pinch to zoom, drag to pan" : "", "drag a label to reorder"]
      .filter(Boolean).join(" · ");
    layout();
  }

  return {
    async show(list, { note: n = "" } = {}) {
      items = list.slice();
      note = n;
      view.cx = 0.5; view.cy = 0.5; view.w = 1;
      fresh = true;
      await loadSizes();
      render();
    },
  };
}
