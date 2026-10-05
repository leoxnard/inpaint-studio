// Self-contained image editor modal: crop (free or fixed aspect), straighten, rotate 90, flip.
//   import { openImageEditor } from "/imageedit.js";
//   const blob = await openImageEditor({ url, label });   // PNG Blob on Apply, null on cancel
// Needs imageedit.css. All DOM and listeners are removed again when the editor closes.
//
// Geometry: the "frame" is the output space, origin at the image centre, 1 unit = 1 source pixel.
//   frame = Rot(angle) * Rot(quarter * 90) * Flip * source        (angle = straighten, -45..45 deg)
// The crop is an axis-aligned rectangle in the frame. It must stay inside the image, so its four corners are
// mapped back with Rot(-angle) and tested against the (quarter-rotated) image rectangle. `want` is the crop the
// user asked for, `crop` is `want` shrunk/nudged until it fits; when the angle shrinks again it grows back.

const MAX_SIDE = 8192;      // longest side of the exported PNG
const PREVIEW_SIDE = 2400;  // the on-screen canvas draws a downscaled copy of big images
const MIN_CROP = 16;        // smallest crop side in source pixels
const EPS = 1e-6;

const RATIOS = [
  { id: "free", label: "Free" },
  { id: "orig", label: "Original" },
  { id: "1:1", label: "1:1", r: 1 },
  { id: "4:3", label: "4:3", r: 4 / 3 },
  { id: "3:2", label: "3:2", r: 3 / 2 },
  { id: "16:9", label: "16:9", r: 16 / 9 },
  { id: "5:4", label: "5:4", r: 5 / 4 },
];

const ICONS = {
  left: '<path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/>',
  right: '<path d="M21 12a9 9 0 1 1-9-9c2.52 0 4.93 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/>',
  flip: '<path d="M8 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h3"/><path d="M16 3h3a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-3"/><path d="M12 2v3"/><path d="M12 9v2"/><path d="M12 13v2"/><path d="M12 19v3"/>',
};

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
const icon = (name) => el("span", "ie-icon", { html: `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name]}</svg>` });
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const rad = (deg) => (deg * Math.PI) / 180;

export function openImageEditor({ url, label } = {}) {
  return new Promise((resolve) => {
    const prevFocus = document.activeElement;
    let img = null, preview = null;
    let closed = false, busy = false, raf = 0;

    // ---- state -------------------------------------------------------------------------------------------
    const s = {
      W: 0, H: 0,
      quarter: 0,          // 0..3, clockwise quarter turns
      flip: false,         // mirror of the source before the rotation
      angle: 0,            // straighten, degrees
      ratioId: "free",
      portrait: false,
      want: { x0: 0, y0: 0, x1: 0, y1: 0 },
      crop: { x0: 0, y0: 0, x1: 0, y1: 0 },
    };
    const view = { sw: 0, sh: 0, k: 1, dpr: 1 };

    // ---- DOM ---------------------------------------------------------------------------------------------
    const overlay = el("div", "ie-overlay", { role: "dialog", "aria-modal": "true", "aria-label": "Edit image", tabindex: "-1" });
    const box = el("div", "ie-box");
    const title = el("div", "ie-head", null, [
      el("h3", "", { text: "Edit image" }),
      label ? el("span", "hint ie-label", { text: label, title: label }) : null,
    ]);
    const canvas = el("canvas", "ie-canvas");
    const status = el("div", "ie-status hint", { text: "Loading image…" });
    const cropBox = el("div", "ie-crop");
    cropBox.hidden = true;
    const gridLines = el("div", "ie-grid");
    cropBox.append(gridLines);
    for (const h of ["nw", "n", "ne", "e", "se", "s", "sw", "w"]) cropBox.append(el("div", `ie-h ie-h-${h}`, { "data-h": h }));
    const stage = el("div", "ie-stage", null, [canvas, cropBox, status]);

    const ratioBtns = {};
    const ratioRow = el("div", "ie-row ie-pills", { role: "group", "aria-label": "Aspect ratio" });
    for (const r of RATIOS) {
      const b = el("button", "small ie-pill", { type: "button", text: r.label, "aria-pressed": "false" });
      b.addEventListener("click", () => setRatio(r.id));
      ratioBtns[r.id] = b;
      ratioRow.append(b);
    }
    const orientBtn = el("button", "small ie-pill ie-orient", { type: "button", "aria-label": "Swap landscape and portrait" });
    orientBtn.addEventListener("click", () => { s.portrait = !s.portrait; refit(); });
    ratioRow.append(orientBtn);

    const slider = el("input", "ie-slider", { type: "range", min: "-45", max: "45", step: "0.1", value: "0", "aria-label": "Straighten" });
    const angleOut = el("span", "ie-angle", { text: "0.0°" });
    const angleReset = el("button", "small", { type: "button", text: "Reset angle" });
    const straighten = el("div", "ie-row ie-straighten", null, [
      el("span", "ie-name", { text: "Straighten" }), slider, angleOut, angleReset,
    ]);
    const mkTool = (ic, text, aria) => {
      const b = el("button", "small ie-tool", { type: "button", "aria-label": aria, title: aria }, [icon(ic), el("span", "ie-lbl", { text })]);
      return b;
    };
    const rotL = mkTool("left", "Rotate left 90°", "Rotate left 90°");
    const rotR = mkTool("right", "Rotate right 90°", "Rotate right 90°");
    const flipB = mkTool("flip", "Flip horizontal", "Flip horizontal");
    const tools = el("div", "ie-row ie-tools", null, [rotL, rotR, flipB]);

    const btnReset = el("button", "ie-reset", { type: "button", text: "Reset" });
    const btnCancel = el("button", "", { type: "button", text: "Cancel" });
    const btnApply = el("button", "primary", { type: "button", text: "Apply" });
    btnApply.disabled = true;
    const actions = el("div", "ie-actions", null, [btnReset, el("span", "ie-spacer"), btnCancel, btnApply]);

    box.append(title, stage, ratioRow, el("div", "ie-controls", null, [straighten, tools]), actions);
    overlay.append(box);

    // ---- geometry ----------------------------------------------------------------------------------------
    const baseDims = () => (s.quarter % 2 ? { w: s.H, h: s.W } : { w: s.W, h: s.H });
    const rw = (r) => r.x1 - r.x0;
    const rh = (r) => r.y1 - r.y0;
    const centered = (w, h) => ({ x0: -w / 2, y0: -h / 2, x1: w / 2, y1: h / 2 });
    const lerpRect = (a, b, t) => ({
      x0: a.x0 + (b.x0 - a.x0) * t, y0: a.y0 + (b.y0 - a.y0) * t,
      x1: a.x1 + (b.x1 - a.x1) * t, y1: a.y1 + (b.y1 - a.y1) * t,
    });

    // is every corner of the rectangle covered by image pixels? (margin keeps the AA edge off the border)
    function feasible(r) {
      const { w: bw, h: bh } = baseDims();
      const a = rad(s.angle), c = Math.cos(a), sn = Math.sin(a);
      const m = s.angle === 0 ? 0 : 1;
      const hw = bw / 2 - m + EPS, hh = bh / 2 - m + EPS;
      for (const [x, y] of [[r.x0, r.y0], [r.x1, r.y0], [r.x1, r.y1], [r.x0, r.y1]]) {
        if (Math.abs(x * c + y * sn) > hw || Math.abs(-x * sn + y * c) > hh) return false;
      }
      return true;
    }
    // largest point on the segment a -> b that is still feasible (a must be feasible)
    function maxToward(a, b) {
      if (feasible(b)) return b;
      let lo = 0, hi = 1;
      for (let i = 0; i < 30; i++) {
        const mid = (lo + hi) / 2;
        if (feasible(lerpRect(a, b, mid))) lo = mid; else hi = mid;
      }
      return lerpRect(a, b, lo);
    }
    // shrink around the centre (and nudge toward the image centre when the centre itself is outside)
    function constrain(want) {
      if (feasible(want)) return { ...want };
      const cx = (want.x0 + want.x1) / 2, cy = (want.y0 + want.y1) / 2;
      const point = { x0: cx, y0: cy, x1: cx, y1: cy };
      if (feasible(point)) return maxToward(point, want);
      const origin = { x0: 0, y0: 0, x1: 0, y1: 0 };
      return maxToward(origin, want);
    }
    function ratioValue() {
      if (s.ratioId === "free") return null;
      if (s.ratioId === "orig") { const b = baseDims(); return b.w / b.h; }
      const r = RATIOS.find((x) => x.id === s.ratioId).r;
      return s.portrait ? 1 / r : r;
    }
    function fullRect() { const b = baseDims(); return centered(b.w, b.h); }
    function fitRatio(r) {
      const b = baseDims();
      const w = Math.min(b.w, b.h * r), h = w / r;
      return maxToward({ x0: 0, y0: 0, x1: 0, y1: 0 }, centered(w, h));
    }
    function setCrop(rect) { s.want = { ...rect }; s.crop = { ...rect }; }

    // ---- state changes -----------------------------------------------------------------------------------
    function refit() {                       // largest centred crop for the current ratio (or the full image)
      const r = ratioValue();
      setCrop(r ? fitRatio(r) : constrain(fullRect()));
      sync();
    }
    function setRatio(id) { s.ratioId = id; refit(); }
    function setAngle(deg) {
      s.angle = clamp(Math.round(deg * 10) / 10, -45, 45);
      s.crop = constrain(s.want);
      sync();
    }
    function rotate90(dir) {
      s.quarter = (s.quarter + dir + 4) % 4;
      s.portrait = !s.portrait;
      refit();
    }
    function flipH() {                       // visual mirror: flip toggles, rotation negates, crop mirrors
      s.flip = !s.flip;
      s.quarter = (4 - s.quarter) % 4;
      s.angle = -s.angle;
      const m = (r) => ({ x0: -r.x1, y0: r.y0, x1: -r.x0, y1: r.y1 });
      s.want = m(s.want);
      s.crop = constrain(s.want);
      sync();
    }
    function resetAll() {
      s.quarter = 0; s.flip = false; s.angle = 0; s.ratioId = "free"; s.portrait = false;
      refit();
    }

    // reflect state into the controls, then redraw
    function sync() {
      slider.value = String(s.angle);
      angleOut.textContent = `${s.angle.toFixed(1)}°`;
      for (const r of RATIOS) ratioBtns[r.id].setAttribute("aria-pressed", String(r.id === s.ratioId));
      const lockable = s.ratioId !== "free" && s.ratioId !== "orig";
      orientBtn.disabled = !lockable;
      orientBtn.textContent = `⇄ ${s.portrait ? "Portrait" : "Landscape"}`;
      schedule();
    }

    // ---- rendering ---------------------------------------------------------------------------------------
    function schedule() { if (!raf && !closed) raf = requestAnimationFrame(() => { raf = 0; render(); }); }

    function measure() {
      const rect = stage.getBoundingClientRect();
      view.sw = Math.max(1, Math.round(rect.width));
      view.sh = Math.max(1, Math.round(rect.height));
      view.dpr = window.devicePixelRatio || 1;
      const w = Math.round(view.sw * view.dpr), h = Math.round(view.sh * view.dpr);
      if (canvas.width !== w || canvas.height !== h) {
        canvas.width = w; canvas.height = h;
        canvas.style.width = `${view.sw}px`; canvas.style.height = `${view.sh}px`;
      }
      if (!img) return;
      const { w: bw, h: bh } = baseDims();
      const a = rad(s.angle), c = Math.abs(Math.cos(a)), sn = Math.abs(Math.sin(a));
      const ew = bw * c + bh * sn, eh = bw * sn + bh * c;   // bounding box of the rotated image
      const pad = 16;
      view.k = Math.max(0.0001, Math.min((view.sw - 2 * pad) / ew, (view.sh - 2 * pad) / eh));
    }

    function render() {
      if (closed) return;
      measure();
      if (!img) return;
      const ctx = canvas.getContext("2d");
      ctx.setTransform(view.dpr, 0, 0, view.dpr, 0, 0);
      ctx.clearRect(0, 0, view.sw, view.sh);
      ctx.translate(view.sw / 2, view.sh / 2);
      ctx.scale(view.k, view.k);
      ctx.rotate(rad(s.angle) + (s.quarter * Math.PI) / 2);
      if (s.flip) ctx.scale(-1, 1);
      ctx.imageSmoothingEnabled = true;
      ctx.imageSmoothingQuality = "high";
      ctx.drawImage(preview, -s.W / 2, -s.H / 2, s.W, s.H);
      const c = s.crop, k = view.k;
      cropBox.style.left = `${view.sw / 2 + c.x0 * k}px`;
      cropBox.style.top = `${view.sh / 2 + c.y0 * k}px`;
      cropBox.style.width = `${rw(c) * k}px`;
      cropBox.style.height = `${rh(c) * k}px`;
    }

    // ---- pointer interaction -----------------------------------------------------------------------------
    let drag = null;
    const gridOn = (on) => cropBox.classList.toggle("ie-grid-on", on);

    function resized(handle, start, dx, dy, r) {
      const minW = r ? Math.max(MIN_CROP, MIN_CROP * r) : MIN_CROP;
      const minH = r ? minW / r : MIN_CROP;
      let { x0, y0, x1, y1 } = start;
      const west = handle.includes("w"), east = handle.includes("e");
      const north = handle.includes("n"), south = handle.includes("s");
      const corner = (west || east) && (north || south);
      if (!r) {
        if (west) x0 = Math.min(x0 + dx, x1 - minW);
        if (east) x1 = Math.max(x1 + dx, x0 + minW);
        if (north) y0 = Math.min(y0 + dy, y1 - minH);
        if (south) y1 = Math.max(y1 + dy, y0 + minH);
        return { x0, y0, x1, y1 };
      }
      if (corner) {                                          // opposite corner stays, ratio is locked
        const ax = west ? x1 : x0, ay = north ? y1 : y0;
        const px = (west ? x0 : x1) + dx, py = (north ? y0 : y1) + dy;
        let w = Math.max(Math.abs(px - ax), Math.abs(py - ay) * r, minW);
        const h = w / r;
        return { x0: west ? ax - w : ax, x1: west ? ax : ax + w, y0: north ? ay - h : ay, y1: north ? ay : ay + h };
      }
      if (west || east) {                                    // opposite edge stays, centred on the other axis
        const cy = (y0 + y1) / 2;
        const w = Math.max(minW, west ? x1 - (x0 + dx) : x1 + dx - x0), h = w / r;
        return { x0: west ? x1 - w : x0, x1: west ? x1 : x0 + w, y0: cy - h / 2, y1: cy + h / 2 };
      }
      const cx = (x0 + x1) / 2;
      const h = Math.max(minH, north ? y1 - (y0 + dy) : y1 + dy - y0), w = h * r;
      return { y0: north ? y1 - h : y0, y1: north ? y1 : y0 + h, x0: cx - w / 2, x1: cx + w / 2 };
    }

    function moved(start, dx, dy) {
      const shift = (r, x, y) => ({ x0: r.x0 + x, x1: r.x1 + x, y0: r.y0 + y, y1: r.y1 + y });
      const r1 = maxToward(start, shift(start, dx, dy));
      const used = { x: r1.x0 - start.x0, y: r1.y0 - start.y0 };
      const r2 = maxToward(r1, shift(r1, dx - used.x, 0));        // slide along an edge
      return maxToward(r2, shift(r2, 0, dy - used.y));
    }

    function onDown(e) {
      if (busy || !img || (e.pointerType === "mouse" && e.button !== 0)) return;
      const handle = e.target.dataset ? e.target.dataset.h || "move" : "move";
      drag = { id: e.pointerId, handle, x: e.clientX, y: e.clientY, start: { ...s.crop } };
      s.want = { ...s.crop };
      try { cropBox.setPointerCapture(e.pointerId); } catch {}
      gridOn(true);
      e.preventDefault();
    }
    function onMove(e) {
      if (!drag || e.pointerId !== drag.id) return;
      const dx = (e.clientX - drag.x) / view.k, dy = (e.clientY - drag.y) / view.k;
      const next = drag.handle === "move"
        ? moved(drag.start, dx, dy)
        : maxToward(drag.start, resized(drag.handle, drag.start, dx, dy, ratioValue()));
      setCrop(next);
      schedule();
    }
    function onUp(e) {
      if (!drag || e.pointerId !== drag.id) return;
      drag = null;
      gridOn(false);
      schedule();
    }
    cropBox.addEventListener("pointerdown", onDown);
    cropBox.addEventListener("pointermove", onMove);
    cropBox.addEventListener("pointerup", onUp);
    cropBox.addEventListener("pointercancel", onUp);

    // ---- controls ----------------------------------------------------------------------------------------
    slider.addEventListener("input", () => setAngle(parseFloat(slider.value)));
    slider.addEventListener("dblclick", () => setAngle(0));
    slider.addEventListener("pointerdown", () => gridOn(true));
    slider.addEventListener("keydown", () => gridOn(true));
    const gridOff = () => gridOn(false);
    slider.addEventListener("pointerup", gridOff);
    slider.addEventListener("pointercancel", gridOff);
    slider.addEventListener("blur", gridOff);
    slider.addEventListener("keyup", gridOff);
    angleReset.addEventListener("click", () => setAngle(0));
    rotL.addEventListener("click", () => rotate90(-1));
    rotR.addEventListener("click", () => rotate90(1));
    flipB.addEventListener("click", flipH);
    btnReset.addEventListener("click", resetAll);
    btnCancel.addEventListener("click", () => close(null));

    // backdrop click cancels (only when both press and release happen on the backdrop)
    let downOnBackdrop = false;
    overlay.addEventListener("pointerdown", (e) => { downOnBackdrop = e.target === overlay; });
    overlay.addEventListener("click", (e) => { if (e.target === overlay && downOnBackdrop) close(null); });

    function onKey(e) {
      if (e.key === "Escape") { e.preventDefault(); e.stopImmediatePropagation(); close(null); return; }
      if (e.key === "Enter" && !e.isComposing) {
        const t = e.target;
        const tag = t && t.tagName;
        if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || tag === "BUTTON") return;
        e.preventDefault(); e.stopImmediatePropagation(); apply();
      }
    }
    document.addEventListener("keydown", onKey, true);

    const ro = new ResizeObserver(schedule);
    ro.observe(stage);

    // ---- export ------------------------------------------------------------------------------------------
    async function apply() {
      if (busy || !img || closed) return;
      busy = true;
      btnApply.disabled = true;
      btnApply.textContent = "Applying…";
      try {
        const c = s.crop, b = baseDims();
        let w, h, cx, cy;
        if (s.angle === 0) {                 // whole source pixels, no resampling at the edges
          const left = Math.round(c.x0 + b.w / 2), top = Math.round(c.y0 + b.h / 2);
          const right = Math.round(c.x1 + b.w / 2), bottom = Math.round(c.y1 + b.h / 2);
          w = Math.max(1, right - left); h = Math.max(1, bottom - top);
          cx = left + w / 2 - b.w / 2; cy = top + h / 2 - b.h / 2;
        } else {
          w = rw(c); h = rh(c); cx = (c.x0 + c.x1) / 2; cy = (c.y0 + c.y1) / 2;
        }
        const f = Math.min(1, MAX_SIDE / Math.max(w, h));
        const ow = Math.max(1, Math.round(w * f)), oh = Math.max(1, Math.round(h * f));
        const out = document.createElement("canvas");
        out.width = ow; out.height = oh;
        const ctx = out.getContext("2d");
        ctx.imageSmoothingEnabled = true;
        ctx.imageSmoothingQuality = "high";
        ctx.scale(ow / w, oh / h);
        ctx.translate(w / 2 - cx, h / 2 - cy);   // frame point (cx, cy) -> centre of the output
        ctx.rotate(rad(s.angle) + (s.quarter * Math.PI) / 2);
        if (s.flip) ctx.scale(-1, 1);
        ctx.drawImage(img, -s.W / 2, -s.H / 2);
        const blob = await new Promise((res) => out.toBlob(res, "image/png"));
        out.width = out.height = 0;
        if (!blob) throw new Error("PNG export failed");
        close(blob);
      } catch (err) {
        busy = false;
        btnApply.disabled = false;
        btnApply.textContent = "Apply";
        status.hidden = false;
        status.textContent = `Could not export the image: ${err.message || err}`;
      }
    }
    btnApply.addEventListener("click", apply);

    // ---- close -------------------------------------------------------------------------------------------
    function close(result) {
      if (closed) return;
      closed = true;
      if (raf) cancelAnimationFrame(raf);
      ro.disconnect();
      document.removeEventListener("keydown", onKey, true);
      overlay.remove();
      canvas.width = canvas.height = 0;
      preview = null; img = null;
      try { if (prevFocus && prevFocus.focus) prevFocus.focus({ preventScroll: true }); } catch {}
      resolve(result);
    }

    // ---- load --------------------------------------------------------------------------------------------
    document.body.append(overlay);
    overlay.focus({ preventScroll: true });
    sync();

    const image = new Image();
    image.decoding = "async";
    image.onload = () => {
      if (closed) return;
      img = image;
      s.W = image.naturalWidth; s.H = image.naturalHeight;
      const k = Math.min(1, PREVIEW_SIDE / Math.max(s.W, s.H));
      if (k < 1) {
        preview = document.createElement("canvas");
        preview.width = Math.round(s.W * k); preview.height = Math.round(s.H * k);
        const pctx = preview.getContext("2d");
        pctx.imageSmoothingQuality = "high";
        pctx.drawImage(image, 0, 0, preview.width, preview.height);
      } else preview = image;
      status.hidden = true;
      cropBox.hidden = false;
      btnApply.disabled = false;
      refit();
    };
    image.onerror = () => { status.textContent = "Could not load the image."; };
    image.src = url;
  });
}
