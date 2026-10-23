// Template editor modal: a small vector editor for a flat shape template (stencil) of the Generate task.
//   import { openTemplateEditor } from "/template.js";
//   const res = await openTemplateEditor({ width, height, doc });   // { blob (PNG), doc } on "Use template", null on cancel
// Needs template.css. All DOM and listeners are removed again when the editor closes.
//
// The document lives in image pixels (width x height): a background colour, an optional imported image and a list of
// shapes. Every shape is rendered from its parameters into SVG path data (`d`); the export draws the same data with
// Path2D onto a canvas (an SVG element rasterised through an <img> would taint the canvas in Safari / WKWebView).
//   shape = { id, type: "rect" | "ellipse" | "triangle" | "star", x, y, w, h, r (rect radius), fill, d }
//         | { id, type: "pen", nodes: [{ x, y, i, o, smooth }], closed, fill, d }     (i / o = absolute handle points or null)

const MIN_SIZE = 2;          // smallest shape side in px
const HIT_PX = 9;            // screen px around anchors / handles that grab them
const HISTORY_MAX = 80;
const SVG_NS = "http://www.w3.org/2000/svg";
const ACCENT = "#2f6fed";

const ICONS = {
  select: '<path d="m4 4 7.07 16.97 2.51-7.39 7.39-2.51L4 4z"/>',
  rect: '<rect x="3" y="5" width="18" height="14" rx="3"/>',
  ellipse: '<circle cx="12" cy="12" r="9"/>',
  triangle: '<path d="M12 4 21 20H3z"/>',
  star: '<path d="m12 3 2.7 5.8 6.3.8-4.6 4.4 1.2 6.3L12 17.2 6.4 20.3l1.2-6.3L3 9.6l6.3-.8z"/>',
  pen: '<path d="m12 19 7-7 3 3-7 7z"/><path d="m18 13-1.5-7.5L2 2l3.5 14.5L13 18z"/><path d="m2 2 7.6 7.6"/><circle cx="11" cy="11" r="2"/>',
  undo: '<path d="M9 14 4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11"/>',
  redo: '<path d="m15 14 5-5-5-5"/><path d="M20 9H9.5a5.5 5.5 0 0 0 0 11H13"/>',
  copy: '<rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
  trash: '<path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M19 6l-1 14H6L5 6"/>',
  image: '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-3.1-3.1a2 2 0 0 0-2.8 0L6 21"/>',
};

const TOOLS = [
  { id: "select", label: "Select", key: "V", hint: "Click a shape to select it, drag to move, drag the handles to resize. Alt-click an anchor of a pen shape to switch it between smooth and sharp." },
  { id: "rect", label: "Rectangle", key: "R", hint: "Drag to draw (Shift = square). Set the corner radius next to the colour." },
  { id: "ellipse", label: "Ellipse", key: "O", hint: "Drag to draw (Shift = circle)." },
  { id: "triangle", label: "Triangle", key: "T", hint: "Drag to draw (Shift = proportional)." },
  { id: "star", label: "Star", key: "S", hint: "Drag to draw a five-point star (Shift = proportional)." },
  { id: "pen", label: "Pen", key: "P", hint: "Click = sharp anchor, click and drag = smooth anchor. Click the first anchor or press Enter to close the shape, Esc to cancel it." },
];

function el(tag, cls, props, kids) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (props) for (const [k, v] of Object.entries(props)) {
    if (k === "text") n.textContent = v;
    else if (k === "html") n.innerHTML = v;
    else n.setAttribute(k, v);
  }
  if (kids) for (const c of kids) if (c) n.append(c);
  return n;
}
const icon = (name) => el("span", "te-icon", { html: `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name]}</svg>` });
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const f = (n) => Math.round(n * 100) / 100;
const clone = (v) => JSON.parse(JSON.stringify(v));

// ---------------------------------------------------------------------------------------------- geometry (pure)

/** SVG path data of a pen shape from its anchors; handles are absolute points (null = none). */
export function penPath(nodes, closed = true) {
  if (!nodes.length) return "";
  let d = `M${f(nodes[0].x)} ${f(nodes[0].y)}`;
  const seg = (a, b) => {
    const c1 = a.o || a, c2 = b.i || b;
    d += a.o || b.i ? `C${f(c1.x)} ${f(c1.y)} ${f(c2.x)} ${f(c2.y)} ${f(b.x)} ${f(b.y)}` : `L${f(b.x)} ${f(b.y)}`;
  };
  for (let k = 1; k < nodes.length; k++) seg(nodes[k - 1], nodes[k]);
  if (closed && nodes.length > 1) { seg(nodes[nodes.length - 1], nodes[0]); d += "Z"; }
  return d;
}

/** The five-point star scaled to fill the box exactly (the raw star does not reach every side of its circle). */
function starPoints(x, y, w, h) {
  const raw = [];
  for (let k = 0; k < 10; k++) {
    const a = -Math.PI / 2 + (k * Math.PI) / 5, r = k % 2 ? 0.382 : 1;
    raw.push([Math.cos(a) * r, Math.sin(a) * r]);
  }
  const xs = raw.map((p) => p[0]), ys = raw.map((p) => p[1]);
  const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys);
  return raw.map(([px, py]) => [x + ((px - x0) / (x1 - x0)) * w, y + ((py - y0) / (y1 - y0)) * h]);
}

/** SVG path data of a shape in image pixels. */
export function shapePath(sh) {
  if (sh.type === "pen") return penPath(sh.nodes, sh.closed);
  const { x, y, w, h } = sh;
  if (sh.type === "ellipse") {
    const rx = w / 2, ry = h / 2, cy = y + ry;
    return `M${f(x)} ${f(cy)}A${f(rx)} ${f(ry)} 0 1 0 ${f(x + w)} ${f(cy)}A${f(rx)} ${f(ry)} 0 1 0 ${f(x)} ${f(cy)}Z`;
  }
  if (sh.type === "triangle") return `M${f(x + w / 2)} ${f(y)}L${f(x + w)} ${f(y + h)}L${f(x)} ${f(y + h)}Z`;
  if (sh.type === "star") return "M" + starPoints(x, y, w, h).map(([px, py]) => `${f(px)} ${f(py)}`).join("L") + "Z";
  const r = clamp(sh.r || 0, 0, Math.min(w, h) / 2);
  if (!r) return `M${f(x)} ${f(y)}H${f(x + w)}V${f(y + h)}H${f(x)}Z`;
  return `M${f(x + r)} ${f(y)}H${f(x + w - r)}A${f(r)} ${f(r)} 0 0 1 ${f(x + w)} ${f(y + r)}V${f(y + h - r)}`
    + `A${f(r)} ${f(r)} 0 0 1 ${f(x + w - r)} ${f(y + h)}H${f(x + r)}A${f(r)} ${f(r)} 0 0 1 ${f(x)} ${f(y + h - r)}`
    + `V${f(y + r)}A${f(r)} ${f(r)} 0 0 1 ${f(x + r)} ${f(y)}Z`;
}

/** Bounding box {x, y, w, h} of a shape (a pen shape is sampled along its curves). */
export function shapeBox(sh) {
  if (sh.type !== "pen") return { x: sh.x, y: sh.y, w: sh.w, h: sh.h };
  const pts = [];
  const n = sh.nodes, count = sh.closed ? n.length : n.length - 1;
  for (let k = 0; k < count; k++) {
    const a = n[k], b = n[(k + 1) % n.length];
    if (!a.o && !b.i) { pts.push([a.x, a.y], [b.x, b.y]); continue; }
    const c1 = a.o || a, c2 = b.i || b;
    for (let s = 0; s <= 24; s++) {
      const t = s / 24, u = 1 - t;
      pts.push([u * u * u * a.x + 3 * u * u * t * c1.x + 3 * u * t * t * c2.x + t * t * t * b.x,
        u * u * u * a.y + 3 * u * u * t * c1.y + 3 * u * t * t * c2.y + t * t * t * b.y]);
    }
  }
  if (!pts.length && n.length) pts.push([n[0].x, n[0].y]);
  const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
  const x = Math.min(...xs), y = Math.min(...ys);
  return { x, y, w: Math.max(...xs) - x, h: Math.max(...ys) - y };
}

/** Moves a shape by (dx, dy), anchors and handles included. */
export function moveShape(sh, dx, dy) {
  if (sh.type === "pen") {
    for (const n of sh.nodes) for (const p of [n, n.i, n.o]) if (p) { p.x += dx; p.y += dy; }
  } else { sh.x += dx; sh.y += dy; }
}

/** Fits a shape into box `to`, given its original shape `from` and that shape's box `box0`. */
export function fitShape(sh, from, box0, to) {
  if (sh.type !== "pen") { Object.assign(sh, { x: to.x, y: to.y, w: to.w, h: to.h }); return; }
  const kx = box0.w > 0 ? to.w / box0.w : 1, ky = box0.h > 0 ? to.h / box0.h : 1;
  const map = (p) => ({ x: to.x + (p.x - box0.x) * kx, y: to.y + (p.y - box0.y) * ky });
  sh.nodes = from.nodes.map((n) => ({ ...n, ...map(n), i: n.i && map(n.i), o: n.o && map(n.o) }));
}

/** The box a drag from (x0, y0) to (x1, y1) draws; `square` makes it as wide as tall. */
export function dragBox(x0, y0, x1, y1, square) {
  let dx = x1 - x0, dy = y1 - y0;
  if (square) { const m = Math.max(Math.abs(dx), Math.abs(dy)); dx = Math.sign(dx || 1) * m; dy = Math.sign(dy || 1) * m; }
  return { x: Math.min(x0, x0 + dx), y: Math.min(y0, y0 + dy), w: Math.abs(dx), h: Math.abs(dy) };
}

/** Where an imported image is drawn: contain = whole image visible, cover = fills the canvas (cropped). */
export function imageRect(iw, ih, W, H, fit) {
  const k = (fit === "cover" ? Math.max : Math.min)(W / iw, H / ih);
  return { x: (W - iw * k) / 2, y: (H - ih * k) / 2, w: iw * k, h: ih * k };
}

// ---------------------------------------------------------------------------------------------- editor

export function openTemplateEditor({ width, height, doc } = {}) {
  return new Promise((resolve) => {
    const W = Math.max(16, Math.round(width || 1024)), H = Math.max(16, Math.round(height || 1024));
    const prevFocus = document.activeElement;
    let closed = false, nextId = 1;

    // ---- state ---------------------------------------------------------------------------------------
    const s = {
      shapes: [], bg: "#ffffff", fill: "#000000", radius: 0,
      images: [],            // [{ src, el }], history refers to them by index
      img: -1, fit: "contain",
      sel: null, tool: "select",
      pen: null,             // path being drawn: { nodes }
      drag: null,
    };
    const undo = [], redo = [];
    if (doc) {
      s.bg = doc.bg || s.bg; s.fill = doc.fill || s.fill; s.radius = doc.radius || 0;
      s.shapes = clone(doc.shapes || []);
      for (const sh of s.shapes) nextId = Math.max(nextId, (sh.id || 0) + 1);
      if (doc.image?.src) {
        s.fit = doc.image.fit === "cover" ? "cover" : "contain";
        const im = new Image();
        im.onload = () => { s.images.push({ src: doc.image.src, el: im }); s.img = 0; render(); sync(); };
        im.src = doc.image.src;
      }
    }

    // ---- DOM -----------------------------------------------------------------------------------------
    const overlay = el("div", "te-overlay", { role: "dialog", "aria-modal": "true", "aria-label": "Template editor", tabindex: "-1" });
    const box = el("div", "te-box");
    const head = el("div", "te-head", null, [
      el("h3", "", { text: "Template" }),
      el("span", "hint", { text: `${W} × ${H} px. Every picture keeps the shapes you draw here.` }),
    ]);

    const toolBtns = {};
    const toolRow = el("div", "te-group", { role: "group", "aria-label": "Tools" });
    for (const t of TOOLS) {
      const b = el("button", "small te-tool", { type: "button", "aria-label": t.label, "aria-pressed": "false", title: `${t.label} (${t.key})` }, [icon(t.id)]);
      b.addEventListener("click", () => setTool(t.id));
      toolBtns[t.id] = b;
      toolRow.append(b);
    }
    const fillIn = el("input", "te-color", { type: "color", value: s.fill, "aria-label": "Shape colour", title: "Colour of the selected shape and of new shapes" });
    const radiusIn = el("input", "te-num", { type: "number", min: "0", max: "4000", step: "1", value: "0", "aria-label": "Corner radius", title: "Corner radius of rectangles (px)" });
    const radiusBox = el("label", "te-radius", { title: "Corner radius of rectangles (px)" }, [el("span", "", { text: "Radius" }), radiusIn]);
    const bgIn = el("input", "te-color", { type: "color", value: s.bg, "aria-label": "Background colour", title: "Background colour" });
    const mkBtn = (ic, label, text) => el("button", "small te-act", { type: "button", "aria-label": label, title: label }, [ic ? icon(ic) : null, text ? el("span", "", { text }) : null]);
    const bUndo = mkBtn("undo", "Undo (Cmd/Ctrl+Z)"), bRedo = mkBtn("redo", "Redo (Shift+Cmd/Ctrl+Z)");
    const bDup = mkBtn("copy", "Duplicate (Cmd/Ctrl+D)"), bDel = mkBtn("trash", "Delete (Backspace)");
    const bImg = mkBtn("image", "Import an image as the background layer", "Image…");
    const bFit = el("button", "small te-act", { type: "button", "aria-pressed": "false", title: "Imported image: contain (whole image) or cover (fills the canvas)" });
    const bRemoveImg = el("button", "small te-act", { type: "button", text: "Remove image" });
    const fileIn = el("input", "", { type: "file", accept: "image/*", hidden: "" });
    const bar = el("div", "te-bar", null, [
      toolRow,
      el("span", "te-sep"),
      el("label", "te-lab", { title: "Colour of the selected shape and of new shapes" }, [el("span", "", { text: "Fill" }), fillIn]),
      radiusBox,
      el("label", "te-lab", { title: "Background colour" }, [el("span", "", { text: "Background" }), bgIn]),
      el("span", "te-sep"),
      bUndo, bRedo, bDup, bDel,
      el("span", "te-sep"),
      bImg, bFit, bRemoveImg, fileIn,
    ]);

    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    svg.setAttribute("class", "te-svg");
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", "Template canvas");
    const stage = el("div", "te-stage", null, []);
    stage.append(svg);
    const status = el("div", "hint te-status");
    const btnCancel = el("button", "", { type: "button", text: "Cancel" });
    const btnUse = el("button", "primary", { type: "button", text: "Use template" });
    const actions = el("div", "te-actions", null, [status, el("span", "te-spacer"), btnCancel, btnUse]);
    box.append(head, bar, stage, actions);
    overlay.append(box);

    // ---- helpers -------------------------------------------------------------------------------------
    const selShape = () => s.shapes.find((sh) => sh.id === s.sel) || null;
    const scale = () => svg.getScreenCTM()?.a || 1;   // screen px per image px
    const pointOf = (e) => {
      const p = new DOMPoint(e.clientX, e.clientY).matrixTransform(svg.getScreenCTM().inverse());
      return { x: p.x, y: p.y };
    };
    const snap = () => JSON.stringify({ shapes: s.shapes, bg: s.bg, img: s.img, fit: s.fit, sel: s.sel });
    const restore = (str) => {
      const v = JSON.parse(str);
      Object.assign(s, { shapes: v.shapes, bg: v.bg, img: v.img, fit: v.fit, sel: v.shapes.some((x) => x.id === v.sel) ? v.sel : null });
    };
    let before = null;
    const begin = () => { before = snap(); };
    const commit = () => {
      if (before != null && before !== snap()) {
        undo.push(before); if (undo.length > HISTORY_MAX) undo.shift();
        redo.length = 0;
      }
      before = null;
    };
    const change = (fn) => { begin(); fn(); commit(); render(); sync(); };

    const hitCtx = document.createElement("canvas").getContext("2d");
    const shapeAt = (p) => {
      for (let k = s.shapes.length - 1; k >= 0; k--) if (hitCtx.isPointInPath(new Path2D(shapePath(s.shapes[k])), p.x, p.y)) return s.shapes[k];
      return null;
    };

    // ---- render --------------------------------------------------------------------------------------
    function render() {
      const k = scale(), hs = 9 / k, line = 1.5 / k;
      const out = [`<rect x="0" y="0" width="${W}" height="${H}" fill="${s.bg}"/>`];
      const im = s.images[s.img];
      if (im) {
        const r = imageRect(im.el.naturalWidth, im.el.naturalHeight, W, H, s.fit);
        out.push(`<svg x="0" y="0" width="${W}" height="${H}" overflow="hidden"><image href="${im.src}" x="${f(r.x)}" y="${f(r.y)}" width="${f(r.w)}" height="${f(r.h)}" preserveAspectRatio="none"/></svg>`);
      }
      for (const sh of s.shapes) { sh.d = shapePath(sh); out.push(`<path d="${sh.d}" fill="${sh.fill}"/>`); }
      out.push(`<rect x="0" y="0" width="${W}" height="${H}" fill="none" stroke="rgba(128,128,128,.6)" stroke-width="${line}"/>`);
      const sel = selShape();
      if (sel && !s.pen) {
        const b = shapeBox(sel);
        out.push(`<rect x="${f(b.x)}" y="${f(b.y)}" width="${f(b.w)}" height="${f(b.h)}" fill="none" stroke="${ACCENT}" stroke-width="${line}" stroke-dasharray="${4 / k} ${3 / k}"/>`);
        for (const [, hx, hy] of handlePoints(b)) out.push(`<rect x="${f(hx - hs / 2)}" y="${f(hy - hs / 2)}" width="${f(hs)}" height="${f(hs)}" fill="#fff" stroke="${ACCENT}" stroke-width="${line}"/>`);
        if (sel.type === "pen") out.push(...nodeOverlay(sel.nodes, k));
      }
      if (s.pen) {
        if (s.pen.nodes.length > 1) out.push(`<path d="${penPath(s.pen.nodes, false)}" fill="none" stroke="${ACCENT}" stroke-width="${2 / k}"/>`);
        out.push(...nodeOverlay(s.pen.nodes, k));
      }
      svg.innerHTML = out.join("");
      svg.style.cursor = s.tool === "select" ? "default" : "crosshair";
    }
    function nodeOverlay(nodes, k) {
      const out = [], r = 4 / k, a = 7 / k, line = 1.25 / k;
      for (const n of nodes) {
        for (const h of [n.i, n.o]) if (h) {
          out.push(`<line x1="${f(n.x)}" y1="${f(n.y)}" x2="${f(h.x)}" y2="${f(h.y)}" stroke="${ACCENT}" stroke-width="${line}"/>`);
          out.push(`<circle cx="${f(h.x)}" cy="${f(h.y)}" r="${f(r)}" fill="#fff" stroke="${ACCENT}" stroke-width="${line}"/>`);
        }
      }
      for (const n of nodes) out.push(`<rect x="${f(n.x - a / 2)}" y="${f(n.y - a / 2)}" width="${f(a)}" height="${f(a)}" fill="${ACCENT}" stroke="#fff" stroke-width="${line}"/>`);
      return out;
    }
    const handlePoints = (b) => [
      ["nw", b.x, b.y], ["n", b.x + b.w / 2, b.y], ["ne", b.x + b.w, b.y], ["e", b.x + b.w, b.y + b.h / 2],
      ["se", b.x + b.w, b.y + b.h], ["s", b.x + b.w / 2, b.y + b.h], ["sw", b.x, b.y + b.h], ["w", b.x, b.y + b.h / 2],
    ];

    /** Updates the toolbar to the state (pressed tools, enabled buttons, colour of the selection). */
    function sync() {
      for (const [id, b] of Object.entries(toolBtns)) b.setAttribute("aria-pressed", String(s.tool === id));
      const sel = selShape();
      if (sel) fillIn.value = sel.fill;
      const rectLike = s.tool === "rect" || sel?.type === "rect";
      radiusBox.hidden = !rectLike;
      if (sel?.type === "rect") radiusIn.value = String(Math.round(sel.r || 0));
      else if (document.activeElement !== radiusIn) radiusIn.value = String(Math.round(s.radius));
      bUndo.disabled = !undo.length; bRedo.disabled = !redo.length;
      bDup.disabled = bDel.disabled = !sel;
      const hasImg = s.img >= 0;
      bFit.hidden = bRemoveImg.hidden = !hasImg;
      bFit.textContent = `Fit: ${s.fit}`;
      bFit.setAttribute("aria-pressed", String(s.fit === "cover"));
      bImg.querySelector("span:last-child").textContent = hasImg ? "Replace image…" : "Image…";
      btnUse.disabled = !s.shapes.length && !hasImg;
      status.textContent = TOOLS.find((t) => t.id === s.tool).hint;
    }
    function setTool(id) {
      if (s.pen) finishPen(false);
      s.tool = id;
      if (id !== "select") s.sel = null;
      render(); sync();
    }
    function select(id) { s.sel = id; render(); sync(); }

    // ---- pen -----------------------------------------------------------------------------------------
    function finishPen(close) {
      const nodes = s.pen?.nodes || [];
      s.pen = null;
      if (close && nodes.length >= 2) {
        begin();
        const sh = { id: nextId++, type: "pen", nodes, closed: true, fill: s.fill };
        s.shapes.push(sh); s.sel = sh.id; s.tool = "select";
        commit();
      }
      render(); sync();
    }

    // ---- pointer -------------------------------------------------------------------------------------
    function onDown(e) {
      if (e.button !== 0 && e.pointerType === "mouse") return;
      overlay.focus({ preventScroll: true });
      const p = pointOf(e), k = scale(), grab = HIT_PX / k;
      svg.setPointerCapture(e.pointerId);
      if (s.tool === "pen") {
        const nodes = (s.pen ||= { nodes: [] }).nodes;
        if (nodes.length >= 2 && Math.hypot(p.x - nodes[0].x, p.y - nodes[0].y) <= grab) { finishPen(true); s.drag = null; return; }
        nodes.push({ x: p.x, y: p.y, i: null, o: null, smooth: false });
        s.drag = { kind: "pennode", node: nodes[nodes.length - 1] };
        render(); return;
      }
      if (s.tool !== "select") {
        const sh = { id: nextId++, type: s.tool, x: p.x, y: p.y, w: 0, h: 0, r: s.tool === "rect" ? s.radius : 0, fill: s.fill };
        begin();
        s.shapes.push(sh); s.sel = sh.id;
        s.drag = { kind: "create", x0: p.x, y0: p.y, sh, moved: false };
        render(); return;
      }
      // select tool: anchors and handles of a selected pen shape, then the resize handles, then shapes
      const sel = selShape();
      if (sel?.type === "pen") {
        for (let idx = 0; idx < sel.nodes.length; idx++) {
          const n = sel.nodes[idx];
          for (const which of ["i", "o"]) {
            if (n[which] && Math.hypot(p.x - n[which].x, p.y - n[which].y) <= grab) {
              begin(); s.drag = { kind: "handle", n, which }; return;
            }
          }
        }
        for (let idx = 0; idx < sel.nodes.length; idx++) {
          const n = sel.nodes[idx];
          if (Math.hypot(p.x - n.x, p.y - n.y) <= grab) {
            begin();
            if (e.altKey) { toggleSmooth(sel, idx); commit(); s.drag = null; render(); sync(); return; }
            s.drag = { kind: "anchor", n, last: p }; return;
          }
        }
      }
      if (sel) {
        for (const [h, hx, hy] of handlePoints(shapeBox(sel))) {
          if (Math.hypot(p.x - hx, p.y - hy) <= grab) {
            begin();
            s.drag = { kind: "resize", h, from: clone(sel), box0: shapeBox(sel), sh: sel };
            return;
          }
        }
      }
      const hit = shapeAt(p);
      if (hit) {
        s.sel = hit.id; begin();
        s.drag = { kind: "move", last: p, sh: hit };
      } else { s.sel = null; s.drag = null; }
      render(); sync();
    }

    function toggleSmooth(sh, idx) {
      const n = sh.nodes[idx];
      if (n.i || n.o) { n.i = n.o = null; n.smooth = false; return; }
      const cnt = sh.nodes.length;
      const prev = sh.nodes[idx - 1] || (sh.closed ? sh.nodes[cnt - 1] : null);
      const next = sh.nodes[idx + 1] || (sh.closed ? sh.nodes[0] : null);
      const a = prev || n, b = next || n;
      let dx = b.x - a.x, dy = b.y - a.y;
      const len = Math.hypot(dx, dy);
      if (!len) return;
      dx /= len; dy /= len;
      const d = Math.min(prev ? Math.hypot(n.x - prev.x, n.y - prev.y) : Infinity, next ? Math.hypot(next.x - n.x, next.y - n.y) : Infinity) / 3;
      n.o = { x: n.x + dx * d, y: n.y + dy * d };
      n.i = { x: n.x - dx * d, y: n.y - dy * d };
      n.smooth = true;
    }

    function onMove(e) {
      const d = s.drag;
      if (!d) return;
      const p = pointOf(e), k = scale();
      if (d.kind === "pennode") {
        const n = d.node;
        if (Math.hypot(p.x - n.x, p.y - n.y) * k > 3) { n.o = { x: p.x, y: p.y }; n.i = { x: 2 * n.x - p.x, y: 2 * n.y - p.y }; n.smooth = true; }
      } else if (d.kind === "create") {
        const b = dragBox(d.x0, d.y0, p.x, p.y, e.shiftKey);
        Object.assign(d.sh, b);
        d.moved = d.moved || Math.max(b.w, b.h) * k > 4;
      } else if (d.kind === "move") {
        moveShape(d.sh, p.x - d.last.x, p.y - d.last.y); d.last = p;
      } else if (d.kind === "anchor") {
        const dx = p.x - d.last.x, dy = p.y - d.last.y;
        for (const q of [d.n, d.n.i, d.n.o]) if (q) { q.x += dx; q.y += dy; }
        d.last = p;
      } else if (d.kind === "handle") {
        const { n, which } = d, other = which === "i" ? "o" : "i";
        n[which] = { x: p.x, y: p.y };
        if (n.smooth) n[other] = { x: 2 * n.x - p.x, y: 2 * n.y - p.y };
      } else if (d.kind === "resize") {
        const b = d.box0, h = d.h;
        let x0 = b.x, y0 = b.y, x1 = b.x + b.w, y1 = b.y + b.h;
        if (h.includes("w")) x0 = p.x; if (h.includes("e")) x1 = p.x;
        if (h.includes("n")) y0 = p.y; if (h.includes("s")) y1 = p.y;
        if (e.shiftKey && b.w > 0 && b.h > 0) {   // proportional: the larger relative change wins
          const kx = Math.abs(x1 - x0) / b.w, ky = Math.abs(y1 - y0) / b.h;
          const kk = h.length === 2 ? Math.max(kx, ky) : h === "n" || h === "s" ? ky : kx;
          const nw = b.w * kk, nh = b.h * kk;
          if (h.includes("w")) x0 = x1 - nw; else if (h.includes("e")) x1 = x0 + nw; else { const cx = b.x + b.w / 2; x0 = cx - nw / 2; x1 = cx + nw / 2; }
          if (h.includes("n")) y0 = y1 - nh; else if (h.includes("s")) y1 = y0 + nh; else { const cy = b.y + b.h / 2; y0 = cy - nh / 2; y1 = cy + nh / 2; }
        }
        const to = { x: Math.min(x0, x1), y: Math.min(y0, y1), w: Math.max(MIN_SIZE, Math.abs(x1 - x0)), h: Math.max(MIN_SIZE, Math.abs(y1 - y0)) };
        fitShape(d.sh, d.from, b, to);
      }
      render();
    }

    function onUp() {
      const d = s.drag;
      s.drag = null;
      if (!d) return;
      if (d.kind === "pennode") { render(); return; }   // the path goes on until it is closed
      if (d.kind === "create") {
        const sh = d.sh;
        if (!d.moved) {   // a click places a default-sized shape centred on the click
          const side = Math.round(Math.min(W, H) * 0.25);
          Object.assign(sh, { x: d.x0 - side / 2, y: d.y0 - side / 2, w: side, h: side });
        }
        s.tool = "select";
        commit(); render(); sync(); return;
      }
      commit(); render(); sync();
    }

    // ---- actions -------------------------------------------------------------------------------------
    function doUndo() {
      if (s.pen) { s.pen.nodes.pop(); if (!s.pen.nodes.length) s.pen = null; render(); return; }
      if (!undo.length) return;
      redo.push(snap()); restore(undo.pop()); render(); sync();
    }
    function doRedo() {
      if (!redo.length) return;
      undo.push(snap()); restore(redo.pop()); render(); sync();
    }
    function remove() {
      const sel = selShape();
      if (!sel) return;
      change(() => { s.shapes = s.shapes.filter((x) => x !== sel); s.sel = null; });
    }
    function duplicate() {
      const sel = selShape();
      if (!sel) return;
      change(() => {
        const c = clone(sel); c.id = nextId++;
        moveShape(c, 20, 20);
        s.shapes.push(c); s.sel = c.id;
      });
    }
    function nudge(dx, dy) {
      const sel = selShape();
      if (!sel) return;
      change(() => moveShape(sel, dx, dy));
    }

    fillIn.addEventListener("input", () => {
      s.fill = fillIn.value;
      const sel = selShape();
      if (sel) { if (sel.fill !== fillIn.value) { begin(); sel.fill = fillIn.value; commit(); } render(); }
    });
    radiusIn.addEventListener("input", () => {
      const r = Math.max(0, parseFloat(radiusIn.value) || 0);
      s.radius = r;
      const sel = selShape();
      if (sel?.type === "rect") { begin(); sel.r = r; commit(); render(); sync(); }
    });
    bgIn.addEventListener("input", () => { if (s.bg !== bgIn.value) { begin(); s.bg = bgIn.value; commit(); render(); sync(); } });
    bUndo.addEventListener("click", doUndo);
    bRedo.addEventListener("click", doRedo);
    bDup.addEventListener("click", duplicate);
    bDel.addEventListener("click", remove);
    bImg.addEventListener("click", () => fileIn.click());
    bFit.addEventListener("click", () => change(() => { s.fit = s.fit === "cover" ? "contain" : "cover"; }));
    bRemoveImg.addEventListener("click", () => change(() => { s.img = -1; }));
    fileIn.addEventListener("change", () => {
      const file = fileIn.files?.[0];
      fileIn.value = "";
      if (!file) return;
      const reader = new FileReader();
      reader.onload = () => {
        const im = new Image();
        im.onload = () => { change(() => { s.images.push({ src: reader.result, el: im }); s.img = s.images.length - 1; }); };
        im.onerror = () => { status.textContent = `${file.name} is not an image this browser can read.`; };
        im.src = reader.result;
      };
      reader.readAsDataURL(file);
    });
    svg.addEventListener("pointerdown", onDown);
    svg.addEventListener("pointermove", onMove);
    svg.addEventListener("pointerup", onUp);
    svg.addEventListener("pointercancel", onUp);

    // ---- keyboard ------------------------------------------------------------------------------------
    function onKey(e) {
      if (closed) return;
      const typing = e.target instanceof HTMLInputElement && ["number", "text"].includes(e.target.type);
      const mod = e.metaKey || e.ctrlKey, key = e.key.toLowerCase();
      if (e.key === "Escape") {
        e.preventDefault(); e.stopPropagation();
        if (s.pen) { s.pen = null; s.drag = null; render(); sync(); } else close(null);
        return;
      }
      if (e.key === "Enter" && s.pen) { e.preventDefault(); finishPen(true); return; }
      if (mod && key === "z") { e.preventDefault(); if (e.shiftKey) doRedo(); else doUndo(); return; }
      if (mod && key === "y") { e.preventDefault(); doRedo(); return; }
      if (typing) return;
      if (mod && key === "d") { e.preventDefault(); duplicate(); return; }
      if (mod) return;
      if (e.key === "Delete" || e.key === "Backspace") {
        if (s.pen) { e.preventDefault(); doUndo(); } else if (selShape()) { e.preventDefault(); remove(); }
        return;
      }
      const step = e.shiftKey ? 10 : 1;
      const arrows = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] };
      if (arrows[e.key] && selShape() && !(e.target instanceof HTMLInputElement)) { e.preventDefault(); nudge(...arrows[e.key]); return; }
      const tool = TOOLS.find((t) => t.key.toLowerCase() === key);
      if (tool && !e.altKey && !(e.target instanceof HTMLInputElement)) { e.preventDefault(); setTool(tool.id); }
    }
    document.addEventListener("keydown", onKey, true);
    const onResize = () => render();
    window.addEventListener("resize", onResize);

    // ---- export / close ------------------------------------------------------------------------------
    function exportBlob() {
      const c = document.createElement("canvas");
      c.width = W; c.height = H;
      const ctx = c.getContext("2d");
      ctx.fillStyle = s.bg; ctx.fillRect(0, 0, W, H);
      const im = s.images[s.img];
      if (im) {
        const r = imageRect(im.el.naturalWidth, im.el.naturalHeight, W, H, s.fit);
        ctx.imageSmoothingQuality = "high";
        ctx.drawImage(im.el, r.x, r.y, r.w, r.h);
      }
      for (const sh of s.shapes) { ctx.fillStyle = sh.fill; ctx.fill(new Path2D(shapePath(sh))); }
      return new Promise((res) => c.toBlob(res, "image/png"));
    }
    function docOut() {
      const im = s.images[s.img];
      return {
        bg: s.bg, fill: s.fill, radius: s.radius,
        image: im ? { src: im.src, fit: s.fit } : null,
        shapes: s.shapes.map((sh) => ({ ...clone(sh), d: shapePath(sh) })),
      };
    }
    function close(result) {
      if (closed) return;
      closed = true;
      document.removeEventListener("keydown", onKey, true);
      window.removeEventListener("resize", onResize);
      overlay.remove();
      try { prevFocus?.focus?.({ preventScroll: true }); } catch { /* the opener may be gone */ }
      resolve(result);
    }
    btnCancel.addEventListener("click", () => close(null));
    btnUse.addEventListener("click", async () => {
      if (s.pen) finishPen(true);
      btnUse.disabled = true;
      const blob = await exportBlob();
      if (!blob) { status.textContent = "Could not render the template."; btnUse.disabled = false; return; }
      close({ blob, doc: docOut() });
    });
    overlay.addEventListener("pointerdown", (e) => { if (e.target === overlay) overlay.focus(); });

    document.body.append(overlay);
    overlay.focus({ preventScroll: true });
    render(); sync();
    requestAnimationFrame(render);   // handle sizes need the laid-out scale
  });
}
