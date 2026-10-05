// Mask editor modal for Post-processing: paint or erase the paste mask on top of the result, the original or the raw
// image. openMaskEditor({ maskUrl, images: [{ label, url }] }) -> Promise<Blob | null> (white = pasted, black = original;
// null when cancelled). Reuses the ie- modal styles of imageedit.css, own parts are prefixed me-.

const loadImg = (url) => new Promise((resolve, reject) => {
  const im = new Image();
  im.crossOrigin = "anonymous";
  im.onload = () => resolve(im);
  im.onerror = () => reject(new Error(`could not load ${url}`));
  im.src = url;
});

const el = (tag, props = {}, ...kids) => {
  const e = Object.assign(document.createElement(tag), props);
  e.append(...kids);
  return e;
};

export async function openMaskEditor({ maskUrl, images }) {
  const [mask, ...bgs] = await Promise.all([loadImg(maskUrl), ...images.map((i) => loadImg(i.url))]);
  const W = bgs[0].naturalWidth, H = bgs[0].naturalHeight;

  // the mask lives in an alpha canvas at the image size: painted = opaque white, original = transparent
  const mc = el("canvas", { width: W, height: H });
  const mx = mc.getContext("2d");
  {
    const t = el("canvas", { width: W, height: H }).getContext("2d");
    t.drawImage(mask, 0, 0, W, H);
    const d = t.getImageData(0, 0, W, H);
    for (let i = 0; i < d.data.length; i += 4) { d.data[i + 3] = d.data[i]; d.data[i] = d.data[i + 1] = d.data[i + 2] = 255; }
    mx.putImageData(d, 0, 0);
  }
  const tint = el("canvas", { width: W, height: H });
  const tx = tint.getContext("2d");

  let bg = 0, erase = false, size = Math.round(Math.max(W, H) / 40), showMask = true;
  const undo = [];

  const canvas = el("canvas", { className: "ie-canvas me-canvas" });
  const cursor = el("div", { className: "me-cursor", hidden: true });
  const stage = el("div", { className: "ie-stage" }, canvas, cursor);
  const bgSeg = el("div", { className: "seg", role: "group", ariaLabel: "Background" },
    ...images.map((im, i) => el("button", { textContent: im.label, type: "button", onclick: () => { bg = i; sync(); draw(); } })));
  const toolSeg = el("div", { className: "seg", role: "group", ariaLabel: "Brush" },
    el("button", { textContent: "Paint", type: "button", title: "Add to the pasted area (X switches)", onclick: () => { erase = false; sync(); } }),
    el("button", { textContent: "Erase", type: "button", title: "Keep the original here (X switches)", onclick: () => { erase = true; sync(); } }));
  const sizeOut = el("output");
  const sizeIn = el("input", { type: "range", min: 2, max: Math.round(Math.max(W, H) / 4), value: size,
    oninput: () => { size = +sizeIn.value; sync(); } });
  const maskBox = el("input", { type: "checkbox", checked: true, onchange: () => { showMask = maskBox.checked; draw(); } });
  const undoBtn = el("button", { textContent: "Undo", type: "button", title: "⌘Z", onclick: () => doUndo() });
  const cancelBtn = el("button", { textContent: "Cancel", type: "button" });
  const saveBtn = el("button", { textContent: "Use this mask", type: "button", className: "primary" });
  const box = el("div", { className: "ie-box" },
    el("div", { className: "ie-head" }, el("h3", { textContent: "Adjust the mask" }),
      el("span", { className: "hint ie-label", textContent: "Red is pasted from the edit, the rest stays the original. [ ] change the brush size." })),
    stage,
    el("div", { className: "row wrap" }, toolSeg,
      el("label", { className: "range-row" }, "Brush ", sizeOut, sizeIn),
      el("label", { className: "check" }, maskBox, " Show mask"), undoBtn),
    el("div", { className: "row wrap" }, bgSeg, el("span", { className: "spacer" }), cancelBtn, saveBtn));
  const overlay = el("div", { className: "ie-overlay", tabIndex: -1 }, box);
  document.body.append(overlay);
  overlay.focus();

  let fit = { s: 1, x: 0, y: 0 };
  function layout() {
    const r = stage.getBoundingClientRect();
    const s = Math.min(r.width / W, r.height / H);
    fit = { s, x: (r.width - W * s) / 2, y: (r.height - H * s) / 2 };
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(r.width * dpr); canvas.height = Math.round(r.height * dpr);
    canvas.style.width = `${r.width}px`; canvas.style.height = `${r.height}px`;
    draw();
  }
  function draw() {
    const dpr = window.devicePixelRatio || 1, c = canvas.getContext("2d");
    c.setTransform(dpr, 0, 0, dpr, 0, 0);
    c.clearRect(0, 0, canvas.width, canvas.height);
    c.drawImage(bgs[bg], fit.x, fit.y, W * fit.s, H * fit.s);
    if (!showMask) return;
    tx.globalCompositeOperation = "copy";
    tx.drawImage(mc, 0, 0);
    tx.globalCompositeOperation = "source-in";
    tx.fillStyle = "rgb(255, 40, 40)";
    tx.fillRect(0, 0, W, H);
    c.globalAlpha = 0.45;
    c.drawImage(tint, fit.x, fit.y, W * fit.s, H * fit.s);
    c.globalAlpha = 1;
  }
  function sync() {
    [...bgSeg.children].forEach((b, i) => b.setAttribute("aria-pressed", String(i === bg)));
    [...toolSeg.children].forEach((b, i) => b.setAttribute("aria-pressed", String((i === 1) === erase)));
    sizeIn.value = size;
    sizeOut.value = `${size} px`;
    undoBtn.disabled = !undo.length;
    cursor.style.width = cursor.style.height = `${size * fit.s}px`;
  }

  // painting: one undo step per stroke, circles joined along the pointer path
  let last = null;
  const toImg = (e) => {
    const r = stage.getBoundingClientRect();
    return { x: (e.clientX - r.left - fit.x) / fit.s, y: (e.clientY - r.top - fit.y) / fit.s };
  };
  function dab(a, b) {
    mx.globalCompositeOperation = erase ? "destination-out" : "source-over";
    mx.strokeStyle = mx.fillStyle = "#fff";
    mx.lineCap = "round"; mx.lineWidth = size;
    mx.beginPath(); mx.moveTo(a.x, a.y); mx.lineTo(b.x, b.y); mx.stroke();
  }
  function moveCursor(e) {
    const r = stage.getBoundingClientRect();
    cursor.hidden = false;
    cursor.style.left = `${e.clientX - r.left}px`; cursor.style.top = `${e.clientY - r.top}px`;
    cursor.classList.toggle("me-erase", erase);
  }
  stage.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    try { stage.setPointerCapture(e.pointerId); } catch { /* synthetic pointer */ }
    undo.push(mx.getImageData(0, 0, W, H));
    if (undo.length > 20) undo.shift();
    last = toImg(e);
    dab(last, last);
    sync(); draw();
  });
  stage.addEventListener("pointermove", (e) => {
    moveCursor(e);
    if (!last) return;
    const p = toImg(e);
    dab(last, p);
    last = p;
    draw();
  });
  const end = () => { last = null; };
  stage.addEventListener("pointerup", end);
  stage.addEventListener("pointercancel", end);
  stage.addEventListener("pointerleave", () => { cursor.hidden = true; });
  function doUndo() {
    const d = undo.pop();
    if (!d) return;
    mx.putImageData(d, 0, 0);
    sync(); draw();
  }

  const ro = new ResizeObserver(() => { layout(); sync(); });
  ro.observe(stage);
  layout(); sync();

  return new Promise((resolve) => {
    function onKey(e) {
      if (e.key === "Escape") { e.preventDefault(); close(null); }
      else if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "z") { e.preventDefault(); doUndo(); }
      else if (e.key === "[" || e.key === "]") { size = Math.max(2, Math.min(+sizeIn.max, Math.round(size * (e.key === "]" ? 1.2 : 1 / 1.2)))); sync(); }
      else if (e.key.toLowerCase() === "x" && !e.metaKey && !e.ctrlKey) { erase = !erase; sync(); }
      else return;
      e.stopPropagation();
    }
    function close(result) {
      ro.disconnect();
      document.removeEventListener("keydown", onKey, true);
      overlay.remove();
      resolve(result);
    }
    document.addEventListener("keydown", onKey, true);
    cancelBtn.onclick = () => close(null);
    saveBtn.onclick = () => {
      const out = el("canvas", { width: W, height: H });
      const ox = out.getContext("2d");
      ox.fillStyle = "#000"; ox.fillRect(0, 0, W, H);
      ox.drawImage(mc, 0, 0);
      out.toBlob((blob) => close(blob), "image/png");
    };
  });
}
