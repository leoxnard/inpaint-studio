// Zoom viewer (a "zoom" block in data.json): every tile shows the same part of its image; wheel / drag / pinch in any
// tile moves all of them, like the Compare view (compare.js). Several viewers can live on one page.
//
// Block fields: id, images {key: {label, w, h, ta (tile aspect), detail {cx, cy, w}, detailLabel}},
// tiles [{key, title, src ("img/x_{img}_{fix}.jpg"), noFix, px, sep}], shown [keys], fixes [{v, label}] (optional),
// runs {img: {key: {took}}} (optional), metrics {img: {key: {fix: {psnr, ssim, detail}}}} + table [keys] (optional),
// focus {img, shown (with "$" = the clicked card), view} for card pictures with data-focus.

const VIEWER_COLUMNS = [   // default table columns; a block can set its own "columns"
  { k: "psnr", label: "PSNR ↑", best: "max", digits: 2 },
  { k: "ssim", label: "SSIM ↑", best: "max", digits: 3 },
  { k: "detail", label: "Detail (1 = original)", best: "one", digits: 2 },
];
const viewerColumns = (D) => D.columns || VIEWER_COLUMNS;

// relative picture paths in data.json live in the benchmarks folder (App Support), served under /data/benchmarks/
const benchAsset = (p) => /^(\/|https?:|data:)/.test(p) ? p : `/data/benchmarks/${p}`;

function viewerMarkup(D) {
  const imgs = Object.entries(D.images);
  const seg = (cls, items, on) => `<div class="seg ${cls}">${items.map(([v, label]) =>
    `<button data-v="${v}" aria-pressed="${v === on}">${label}</button>`).join("")}</div>`;
  const firstDetail = imgs[0][1].detail ? `<button data-z="detail">${imgs[0][1].detailLabel || "Detail"}</button>` : "";
  return `
    <div class="uv" id="${D.id}">
      <div class="uv-bar">
        <button class="uv-back" hidden>&larr; Back to the pictures</button>
        ${imgs.length > 1 ? seg("uv-img", imgs.map(([k, v]) => [k, v.label]), imgs[0][0]) : ""}
        ${D.fixes ? seg("uv-fix", D.fixes.map((f) => [f.v, f.label]), D.fixes[0].v) : ""}
        <div class="uv-zoom">Zoom <output>–</output>
          <div class="seg">${firstDetail}<button data-z="fit">Fit</button><button data-z="100">100 %</button><button data-z="200">200 %</button></div>
        </div>
      </div>
      <div class="uv-chips" aria-label="Shown images"></div>
      <div class="uv-stage"></div>
      <p class="uv-hint">Scroll or pinch to zoom, drag to pan, double-click to jump to 200 % and back. All tiles move together. Click a name above to show or hide it.</p>
    </div>` + (D.table ? `
    <div class="mtable-wrap">
      <table class="mtable">
        <thead><tr><th>${D.tableTitle || "Model"}</th><th data-sort="took">Time</th>${viewerColumns(D).map((c) => `<th data-sort="${c.k}">${c.label}</th>`).join("")}</tr></thead>
        <tbody></tbody>
      </table>
    </div>` : "");
}

function initViewer(root, D) {
  const stage = root.querySelector(".uv-stage"), chips = root.querySelector(".uv-chips"), zoomOut = root.querySelector("output");
  const table = D.table ? root.nextElementSibling.querySelector("table") : null, tbody = table?.querySelector("tbody");
  const st = { img: Object.keys(D.images)[0], fix: D.fixes?.[0].v, shown: new Set(D.shown), sort: null };
  const view = { cx: 0.5, cy: 0.5, w: 1 };   // w = visible part of the image width (1 = full width)
  let tiles = [];

  const im = () => D.images[st.img];
  const ia = () => im().w / im().h;             // image aspect
  const ta = () => im().ta || ia();             // tile aspect
  const viewH = () => view.w * ia() / ta();     // visible part of the image height
  const fitW = () => Math.max(1, ta() / ia());  // whole image visible
  const tile = (k) => D.tiles.find((t) => t.key === k);
  const src = (t) => benchAsset(t.src.replaceAll("{img}", st.img).replaceAll("{fix}", st.fix));
  const metric = (k) => {
    const m = D.metrics?.[st.img]?.[k];
    return m && (tile(k).noFix ? Object.values(m)[0] : m[st.fix]);
  };

  function clamp() {
    view.w = Math.min(Math.max(view.w, 0.03), fitW());
    for (const [c, size] of [["cx", view.w], ["cy", viewH()]])
      view[c] = size >= 1 ? 0.5 : Math.min(Math.max(view[c], size / 2), 1 - size / 2);
  }
  function place() {
    clamp();
    for (const t of tiles) {
      const tw = t.box.clientWidth, th = t.box.clientHeight, dw = tw / view.w, dh = dw / ia();
      t.img.style.width = `${dw}px`; t.img.style.height = `${dh}px`;
      t.img.style.transform = `translate(${tw / 2 - view.cx * dw}px, ${th / 2 - view.cy * dh}px)`;
    }
    const tw = tiles[0]?.box.clientWidth || 1;
    zoomOut.value = `${Math.round(tw / view.w / im().w * 100 * devicePixelRatio)}%`;
  }
  function zoomAt(box, mx, my, f) {
    const dx = (mx / box.clientWidth - 0.5) * view.w, dy = (my / box.clientHeight - 0.5) * viewH();
    const px = view.cx + dx, py = view.cy + dy, w0 = view.w;
    view.w = Math.min(Math.max(view.w * f, 0.03), fitW());
    const k = view.w / w0;
    view.cx = px - dx * k; view.cy = py - dy * k;
    place();
  }
  // 100 % = one image pixel per screen pixel
  const setPct = (pct) => { const tw = tiles[0]?.box.clientWidth || 1; view.w = tw * devicePixelRatio / (im().w * pct / 100); place(); };

  function panZoom(box) {
    box.addEventListener("wheel", (e) => {
      e.preventDefault();
      const r = box.getBoundingClientRect();
      zoomAt(box, e.clientX - r.left, e.clientY - r.top, Math.exp(e.deltaY * 0.0015));
    }, { passive: false });
    box.addEventListener("dblclick", (e) => {
      const r = box.getBoundingClientRect();
      if (view.w < fitW() - 1e-6) { view.w = fitW(); place(); }
      else zoomAt(box, e.clientX - r.left, e.clientY - r.top, box.clientWidth * devicePixelRatio / (im().w * 2) / view.w);
    });
    const pts = new Map();
    let pinch = null;
    box.addEventListener("pointerdown", (e) => {
      box.setPointerCapture(e.pointerId);
      pts.set(e.pointerId, { x: e.clientX, y: e.clientY });
      if (pts.size === 2) { const [a, b] = [...pts.values()]; pinch = Math.hypot(a.x - b.x, a.y - b.y); }
    });
    box.addEventListener("pointermove", (e) => {
      const p = pts.get(e.pointerId);
      if (!p) return;
      if (pts.size === 1) {
        view.cx -= (e.clientX - p.x) / box.clientWidth * view.w;
        view.cy -= (e.clientY - p.y) / box.clientHeight * viewH();
        p.x = e.clientX; p.y = e.clientY;
        return place();
      }
      p.x = e.clientX; p.y = e.clientY;
      const [a, b] = [...pts.values()], d = Math.hypot(a.x - b.x, a.y - b.y), r = box.getBoundingClientRect();
      if (pinch && d) zoomAt(box, (a.x + b.x) / 2 - r.left, (a.y + b.y) / 2 - r.top, pinch / d);
      pinch = d;
    });
    const up = (e) => { pts.delete(e.pointerId); if (pts.size < 2) pinch = null; };
    box.addEventListener("pointerup", up);
    box.addEventListener("pointercancel", up);
  }

  function build() {
    const keys = D.tiles.filter((t) => st.shown.has(t.key));
    // fewest columns (largest tiles) whose grid still fits the window height
    const n = keys.length, W = stage.clientWidth, avail = Math.max(320, innerHeight - 190);
    let cols = 1;
    while (cols < n && Math.ceil(n / cols) * ((W - (cols - 1) * 6) / cols / ta()) > avail) cols++;
    stage.style.setProperty("--cols", Math.max(1, cols));
    stage.style.setProperty("--ta", ta());
    stage.replaceChildren();
    tiles = keys.map((t) => {
      const box = document.createElement("div"), img = new Image(), lbl = document.createElement("div");
      box.className = "uv-tile"; lbl.className = "lbl";
      img.src = src(t); img.alt = t.title; img.draggable = false;
      if (t.px) img.className = "px";
      const mt = metric(t.key), run = D.runs?.[st.img]?.[t.key];
      lbl.innerHTML = `<span>${t.title}</span>` + (run?.took != null ? `<span class="m">${run.took.toFixed(1)} s</span>` : "")
        + (mt ? `<span class="m">${D.tileMetric?.label || "PSNR"} ${mt[D.tileMetric?.k || "psnr"].toFixed(1)}</span>` : "");
      box.append(img, lbl);
      stage.append(box);
      panZoom(box);
      return { box, img };
    });
    place();
  }

  function renderChips() {
    chips.replaceChildren();
    D.tiles.forEach((t) => {
      if (t.sep) chips.append(Object.assign(document.createElement("span"), { className: "sep" }));
      const b = document.createElement("button");
      b.textContent = t.title;
      b.setAttribute("aria-pressed", st.shown.has(t.key));
      b.onclick = () => toggle(t.key);
      chips.append(b);
    });
  }
  function toggle(k) {
    st.shown.has(k) ? st.shown.delete(k) : st.shown.add(k);
    if (!st.shown.size) st.shown.add(D.tiles[0].key);
    renderChips(); renderTable(); build();
  }

  function renderTable() {
    if (!tbody) return;
    const rows = D.table.map((k) => ({ k, title: tile(k).title, took: D.runs?.[st.img]?.[k]?.took, ...metric(k) }));
    const cols = viewerColumns(D), best = {};
    const rank = (c, v) => c.best === "one" ? Math.abs(v - 1) : c.best === "min" ? v : -v;   // lower = better
    for (const c of cols) best[c.k] = rows.reduce((a, r) => rank(c, r[c.k]) < rank(c, a) ? r[c.k] : a, rows[0][c.k]);
    const sc = cols.find((c) => c.k === st.sort);
    if (sc) rows.sort((a, b) => rank(sc, a[sc.k]) - rank(sc, b[sc.k]));
    else if (st.sort === "took") rows.sort((a, b) => (a.took ?? 0) - (b.took ?? 0));
    tbody.replaceChildren(...rows.map((r) => {
      const tr = document.createElement("tr");
      tr.className = st.shown.has(r.k) ? "" : "off";
      const td = (v, c) => `<td class="${c && r[c] === best[c] ? "top" : ""}">${v}</td>`;
      tr.innerHTML = `<td>${r.title}</td>${td(r.took == null ? "–" : r.took.toFixed(1) + " s")}`
        + cols.map((c) => td(r[c.k].toFixed(c.digits), c.k)).join("");
      tr.onclick = () => toggle(r.k);
      return tr;
    }));
  }
  table?.querySelectorAll("th[data-sort]").forEach((th) => th.onclick = () => { st.sort = th.dataset.sort; renderTable(); });

  const zDetail = root.querySelector("[data-z=detail]");
  const labelDetail = () => { if (zDetail) zDetail.textContent = im().detailLabel || "Detail"; };
  function seg(sel, key, after) {
    const btns = root.querySelectorAll(`${sel} button`);
    btns.forEach((b) => b.onclick = () => {
      st[key] = b.dataset.v;
      btns.forEach((x) => x.setAttribute("aria-pressed", x === b));
      after();
    });
  }
  seg(".uv-img", "img", () => { view.w = 1e9; labelDetail(); renderTable(); build(); });
  seg(".uv-fix", "fix", () => { renderTable(); build(); });   // the view stays where it is
  if (zDetail) zDetail.onclick = () => { Object.assign(view, im().detail); place(); };
  root.querySelector("[data-z=fit]").onclick = () => { view.w = fitW(); place(); };
  root.querySelector("[data-z='100']").onclick = () => setPct(100);
  root.querySelector("[data-z='200']").onclick = () => setPct(200);
  // card pictures: show that tile next to the reference ones, zoomed into the detail
  // the clicked card is remembered so Back (button or Esc) scrolls to it again
  const back = root.querySelector(".uv-back");
  let from = null;
  const goBack = () => {
    if (!from) return;
    from.scrollIntoView({ behavior: "smooth", block: "center" });
    from = null; back.hidden = true;
  };
  back.onclick = goBack;
  addEventListener("keydown", (e) => { if (e.key === "Escape" && from) goBack(); });
  document.querySelectorAll(`[data-zoom="${D.id}"][data-focus]`).forEach((a) => a.addEventListener("click", (e) => {
    if (document.body.classList.contains("editing")) return;
    e.preventDefault();
    from = a; back.hidden = false;
    const f = D.focus || {};
    st.shown = new Set((f.shown || ["$"]).map((k) => k === "$" ? a.dataset.focus : k));
    if (f.img) {
      st.img = f.img;
      root.querySelectorAll(".uv-img button").forEach((x) => x.setAttribute("aria-pressed", x.dataset.v === f.img));
    }
    labelDetail(); renderChips(); renderTable(); build();
    if (f.view) { Object.assign(view, f.view); place(); }
    root.scrollIntoView({ behavior: "smooth", block: "start" });
  }));
  addEventListener("resize", () => build());
  view.w = 1e9;
  labelDetail(); renderChips(); renderTable(); build();
}
