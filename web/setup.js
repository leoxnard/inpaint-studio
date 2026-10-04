// Setup & downloads: first-run guide, component and model download centre, live install progress.
// Sections are rebuilt only on structural changes; polling just updates the progress panel in place,
// so CSS animations are not restarted.

const PATH_FIELDS = [
  ["comfy_dir", "ComfyUI folder"], ["models_dir", "Models folder"],
  ["input_dir", "Input folder"], ["output_dir", "Output folder"],
];

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

export function fmtBytes(n) {
  if (n == null) return "";
  if (n >= 1e9) return `${(n / 1e9).toFixed(1)} GB`;
  if (n >= 1e6) return `${Math.round(n / 1e6)} MB`;
  return `${Math.max(1, Math.round(n / 1e3))} KB`;
}
const fmtRate = (r) => `${fmtBytes(r)}/s`;
function fmtEta(s) {
  if (!isFinite(s) || s < 0) return "";
  if (s < 90) return `~${Math.max(1, Math.round(s))} s left`;
  return `~${Math.round(s / 60)} min left`;
}

// deps: { api, postJson, root, onReady(data), onBack(), onChanged(data) }
export function createSetup({ api, postJson, root, onReady, onBack, onChanged }) {
  let data = null;
  let ui = null;
  let pollTimer = 0;
  let waitingComfy = false;
  let wasRunning = false;
  let readyAtStart = false;
  let builtReady = null;          // mode of the current skeleton (ready or first run)
  let pathsOpen = false;
  const picked = new Map();       // picker key (preset id / "seedvr2") -> chosen item id
  const loraOpen = new Set(), loraClosed = new Set();   // LoRA groups opened / closed by hand
  const baseSel = new Set();      // first run: base steps to install
  let pick = { preset: null, quant: null };
  let sam3Pick = false;

  const stopPoll = () => { clearTimeout(pollTimer); pollTimer = 0; };
  const running = () => !!data.install?.running;
  const preset = (id) => data.presets.find((p) => p.id === id);
  const comp = (key) => data.components.find((c) => c.key === key);
  // everything a preset needs besides the model file: text encoder, VAE and custom nodes
  const needKeys = (p) => [p.text_encoder, p.vae, ...(p.nodes || [])];
  const missingKeys = (p) => needKeys(p).filter((k) => !comp(k)?.installed);
  const recQuant = (p) => p.recommended_quant || p.default_quant;
  const sorted = () => data.presets.filter((p) => !p.imported).sort((a, b) => (b.recommended ? 1 : 0) - (a.recommended ? 1 : 0));
  const FIT = {
    good: ["ok", "Fits"], tight: ["warn", "Tight – may swap, slow"], no: ["bad", "Too large"],
  };
  function fitBadge(q) {
    const f = FIT[q.fit];
    if (!f) return null;
    const b = el("span", `badge ${f[0]}`, f[1]);
    if (q.memory) b.title = `Estimated peak memory ~${fmtBytes(q.memory)}`;
    return b;
  }
  const systemLine = () => {
    const sy = data.system;
    if (!sy || !sy.ram) return null;
    return el("div", "hint", `Your Mac: ${fmtBytes(sy.ram)} RAM (about ${fmtBytes(sy.gpu_budget)} usable for images)`);
  };

  async function load() {
    data = await api("/api/setup");
  }

  function seedFirstRun() {
    baseSel.clear();
    for (const b of data.base) if (!b.installed) baseSel.add(b.id);
    const p = preset(pick.preset) || preset(data.default_preset) || sorted()[0];
    pick.preset = p.id;
    if (!p.quants.some((q) => q.quant === pick.quant)) pick.quant = recQuant(p);
    sam3Pick = false;
  }

  // ---------------------------------------------------------------- skeleton
  function build() {
    const scroll = root.scrollTop;
    root.textContent = "";
    ui = { pRows: {}, states: {} };
    builtReady = data.ready;
    const inner = el("div", "setup-inner");
    root.appendChild(inner);

    const head = el("div", "row wrap");
    ui.title = el("h2", null, data.ready ? "Download Center" : "Welcome to Inpaint Studio");
    head.appendChild(ui.title);
    ui.back = el("button", "small", "Back to app");
    ui.back.style.marginLeft = "auto";
    ui.back.onclick = () => onBack();   // downloads go on in the background
    head.appendChild(ui.back);
    inner.appendChild(head);
    inner.appendChild(el("p", "lead", data.ready
      ? "Models and components are downloaded from GitHub and Hugging Face. Delete what you no longer need."
      : "Inpaint Studio needs a few components. They are downloaded from GitHub and Hugging Face."));

    inner.appendChild(buildProgressPanel());
    ui.error = el("div", "setup-error");
    ui.error.hidden = true;
    inner.appendChild(ui.error);
    ui.restart = el("div", "warn", "Restart ComfyUI (Comfy Desktop) so the new node is loaded.");
    inner.appendChild(ui.restart);

    if (data.ready) {
      inner.appendChild(buildComponents());
      inner.appendChild(buildModels());
      inner.appendChild(buildUpscalers());
      inner.appendChild(buildControl());
      inner.appendChild(buildLoras());
      inner.appendChild(buildImports());
      inner.insertBefore(buildToc(inner), ui.panel);
    } else {
      seedFirstRun();
      inner.appendChild(buildGuided());
    }
    inner.appendChild(buildPaths());
    updateAll();
    root.scrollTop = scroll;
  }

  // ---------------------------------------------------------------- section jump bar (ready mode)
  // one button per section heading; sticks to the top while scrolling and marks the section in view
  function buildToc(inner) {
    const nav = el("nav", "setup-toc");
    nav.setAttribute("aria-label", "Sections");
    const links = [...inner.querySelectorAll(".plist > h3.sec")].map((h) => {
      const b = el("button", null, h.textContent);
      b.onclick = () => h.parentElement.scrollIntoView({ behavior: "smooth", block: "start" });
      nav.appendChild(b);
      return [b, h.parentElement];
    });
    const mark = () => {
      const line = root.getBoundingClientRect().top + nav.offsetHeight + 48;
      const cur = links.filter(([, s]) => s.getBoundingClientRect().top <= line).pop() || links[0];
      for (const l of links) l[0].classList.toggle("on", l === cur);
    };
    root.onscroll = mark;
    requestAnimationFrame(mark);
    return nav;
  }

  // ---------------------------------------------------------------- progress panel
  function buildProgressPanel() {
    ui.panel = el("div", "setup-overall");
    const row = el("div", "row");
    ui.overallIcon = el("span");
    ui.overallText = el("span");
    ui.cancel = el("button", "small", "Cancel all");
    ui.cancel.style.marginLeft = "auto";
    ui.cancel.onclick = async () => {
      ui.cancel.disabled = true;
      try { await postJson("/api/setup/cancel", {}); } catch (e) { showErr(e.message); }
    };
    row.append(ui.overallIcon, ui.overallText, ui.cancel);
    ui.panel.appendChild(row);
    ui.overallBar = el("div", "pbar");
    ui.overallBar.appendChild(el("i"));
    ui.panel.appendChild(ui.overallBar);
    ui.plist = el("div", "plist");
    ui.panel.appendChild(ui.plist);
    return ui.panel;
  }

  function rebuildPRows(ids) {
    ui.plist.textContent = "";
    ui.pRows = {};
    for (const id of ids) {
      const r = { el: el("div", "prow") };
      r.icon = el("div", "pi");
      r.title = el("div", "pt");
      r.msg = el("div", "msg");
      r.bar = el("div", "pbar");
      r.bar.appendChild(el("i"));
      r.detail = el("div", "detail");
      r.x = el("button", "px", "×");
      r.x.title = "Cancel this download";
      r.x.onclick = async () => {
        r.x.disabled = true;
        try { await postJson("/api/setup/cancel", { item: id }); } catch (e) { showErr(e.message); }
      };
      r.el.append(r.icon, r.title, r.msg, r.bar, r.detail, r.x);
      ui.pRows[id] = r;
      ui.plist.appendChild(r.el);
    }
    ui.pKey = ids.join("|");
  }

  function setIcon(r, state) {
    if (r.iconKey === state) return;
    r.iconKey = state;
    r.icon.textContent = "";
    const mk = {
      pending: () => el("span", "icon-wait"), running: () => el("span", "icon-spin"),
      done: () => el("span", "icon-check", "✓"), skipped: () => el("span", "icon-check static", "✓"),
      error: () => el("span", "icon-cross", "!"),
    }[state] || (() => el("span", "icon-dash"));
    r.icon.appendChild(mk());
  }

  function updatePRow(id, r, ist) {
    r.el.className = `prow ${ist.state}`;
    setIcon(r, ist.state);
    r.title.textContent = ist.title || id;
    const total = ist.total, have = ist.done || 0;
    const det = ist.state === "running" && total;
    let msg = ist.message || "";
    if (det) {
      const parts = [`${fmtBytes(have)} / ${fmtBytes(total)}`];
      if (ist.rate > 0) {
        parts.push(fmtRate(ist.rate));
        const eta = fmtEta((total - have) / ist.rate);
        if (eta) parts.push(eta);
      }
      msg = `${msg ? msg + " · " : ""}${parts.join(" · ")}`;
    }
    r.msg.textContent = msg;
    r.msg.hidden = !msg;
    r.bar.hidden = ist.state !== "running";
    r.bar.classList.toggle("indeterminate", ist.state === "running" && !total);
    r.bar.firstChild.style.width = det ? `${Math.min(100, (have / total) * 100).toFixed(1)}%` : "";
    r.detail.textContent = ist.state === "running" ? (ist.detail || "") : "";
    r.detail.hidden = !r.detail.textContent;
    const active = ist.state === "pending" || ist.state === "running";
    r.x.hidden = !active || !running();
    if (!active) r.x.disabled = false;
  }

  function updateProgress() {
    const inst = data.install || {};
    const steps = inst.steps || {};
    const ids = Object.keys(steps);
    const bad = ids.some((id) => steps[id].state === "error" || steps[id].state === "cancelled");
    const show = running() || waitingComfy || (bad && ids.length);
    ui.panel.hidden = !show;
    if (!show) return;
    if (ui.pKey !== ids.join("|")) rebuildPRows(ids);
    for (const id of ids) updatePRow(id, ui.pRows[id], steps[id]);
    let text = "", icon = "";
    if (waitingComfy) { text = "Starting ComfyUI…"; icon = "icon-spin"; }
    else if (running()) {
      const cur = ids.find((id) => steps[id].state === "running");
      const idx = cur ? ids.indexOf(cur) : ids.findIndex((id) => steps[id].state === "pending");
      text = cur ? `Step ${idx + 1} of ${ids.length}: ${steps[cur].title || cur}` : `Preparing ${ids.length} steps…`;
      icon = "icon-spin";
    } else text = "Install stopped";
    ui.overallIcon.className = icon;
    ui.overallText.textContent = text;
    const done = ids.filter((id) => ["done", "skipped"].includes(steps[id].state)).length;
    ui.overallBar.classList.toggle("indeterminate", waitingComfy);
    ui.overallBar.firstChild.style.width = waitingComfy ? "" : `${ids.length ? (done / ids.length) * 100 : 0}%`;
    ui.cancel.hidden = !running();
    ui.cancel.disabled = false;
  }

  // small per-item state tag next to buttons in the lists (details are in the progress panel)
  function stateTag(id) {
    const t = el("span", "pstate");
    t.dataset.item = id;
    (ui.states[id] ||= []).push(t);
    return t;
  }
  function updateStateTags() {
    const steps = data.install?.steps || {};
    for (const [id, tags] of Object.entries(ui.states)) {
      const st = steps[id]?.state;
      const label = { pending: "Queued", running: "Downloading…", error: "Failed", cancelled: "Cancelled" }[st] || "";
      const show = label && (running() || st === "error" || st === "cancelled");
      for (const t of tags) {
        t.textContent = show ? label : "";
        t.className = `pstate ${show ? st : ""}`;
      }
    }
  }

  // ---------------------------------------------------------------- generic pieces
  function actBtn(label, cls, onclick, title) {
    const b = el("button", `act ${cls || ""}`.trim(), label);
    if (title) b.title = title;
    b.onclick = onclick;
    return b;
  }
  // download/install buttons stay usable while a download runs: a click adds the item to the queue
  function queueBtn(id, b) {
    b.classList.add("queue");
    b.dataset.item = id;
    return b;
  }
  const installedBadge = () => el("span", "badge ok", "Installed");

  async function confirmDelete(item, title, size, extra) {
    const msg = `Delete ${title}${size ? ` (${fmtBytes(size)})` : ""}? The file is removed from disk and has to be downloaded again.${extra ? `\n\n${extra}` : ""}`;
    if (!window.confirm(msg)) return;
    showErr("");
    try {
      data = await postJson("/api/setup/delete", { item });
      structural();
      onChanged(data);
    } catch (e) { showErr(e.message); }
  }

  const usersOf = (key, exceptPreset) => data.presets.filter(
    (p) => p.id !== exceptPreset && needKeys(p).includes(key) && p.installed_quants.length);

  // ---------------------------------------------------------------- components (ready mode)
  function section(title) {
    const wrap = el("div", "plist");
    wrap.style.gap = "8px";
    wrap.appendChild(el("h3", "sec", title));
    return wrap;
  }

  function itemRow({ id, title, desc, size, installed, deletable, optional, onDelete }) {
    const r = el("div", "irow");
    const name = el("div", "name");
    name.appendChild(el("span", null, title));
    if (optional) name.appendChild(el("span", "tag-opt", "Optional"));
    r.appendChild(name);
    r.appendChild(el("div", "desc", desc));
    const side = el("div", "side");
    if (size) side.appendChild(el("span", "hint", fmtBytes(size)));
    side.appendChild(stateTag(id));
    if (installed) {
      side.appendChild(installedBadge());
      if (deletable) side.appendChild(actBtn("Delete", "danger", onDelete));
    } else {
      side.appendChild(queueBtn(id, actBtn("Install", "primary", () => startInstall([id]))));
    }
    r.appendChild(side);
    return r;
  }

  function buildComponents() {
    const sec = section("Components");
    for (const b of data.base) {
      sec.appendChild(itemRow({ id: b.id, title: b.title, desc: b.description, installed: b.installed }));
    }
    const sam = comp("sam3");
    if (sam) {
      sec.appendChild(itemRow({
        id: sam.id, title: "Masking (SAM3)", desc: "Computes masks from a text description. Without it the masking tools stay hidden.",
        size: sam.size, installed: sam.installed, deletable: true, optional: true,
        onDelete: () => confirmDelete(sam.id, "Masking (SAM3)", sam.size, "The masking tools will be hidden afterwards."),
      }));
    }
    return sec;
  }

  // a plain list of optional downloads (Install / Delete per row)
  const optionalRow = (c) => itemRow({
    id: c.id, title: c.title, desc: c.description, size: c.size, installed: c.installed, deletable: true, optional: true,
    onDelete: () => confirmDelete(c.id, c.title, c.size, ""),
  });

  function buildUpscalers() {
    const sec = section("Upscalers");
    sec.id = "setupUpscalers";
    sec.appendChild(el("div", "hint", "For the Upscale task and for upscaling an edit's result. Pixel upscalers are fast; SeedVR2 adds detail but needs more time and RAM."));
    for (const u of data.components.filter((c) => c.kind === "upscaler" && !c.group && !c.imported)) sec.appendChild(optionalRow(u));
    const sv = data.components.filter((c) => c.group === "SeedVR2");
    if (sv.length) sec.appendChild(buildSeedvr2Card(sv));
    return sec;
  }

  function buildControl() {
    const sec = section("Control");
    sec.id = "setupControl";
    sec.appendChild(el("div", "hint", "Generate with the layout of another image: its edges or its depth. One patch per model line; depth also needs Depth Anything."));
    for (const c of data.components.filter((c) => c.group === "Control" && !c.imported)) sec.appendChild(optionalRow(c));
    return sec;
  }

  // SeedVR2: one card, a row per size/precision; the VAE (and the 1.4B node) come along with the first download
  function buildSeedvr2Card(variants) {
    const card = el("div", `mcard${variants.some((v) => v.installed) ? " complete" : ""}`);
    const head = el("div", "head");
    head.appendChild(el("b", null, "SeedVR2 upscaler"));
    head.appendChild(el("span", "tag-opt", "Optional"));
    card.appendChild(head);
    card.appendChild(el("div", "hint", "Diffusion upscaler that redraws real detail, any factor. Bigger = more detail but slower and more memory; int8 is close to fp16 at half the size. Memory grows with the output size: the estimate is for a 4 MP result (×2 of a ~1 MP image)."));
    const needs = el("div", "needs");
    const deps = [...new Set(variants.flatMap((v) => v.needs))].map(comp).filter(Boolean);
    for (const d of deps) {
      const n = el("div", "n");
      n.appendChild(el("span", null, `${d.kind === "upscaler_node" ? "Node" : "VAE"}: ${d.title} (${fmtBytes(d.size)})`));
      if (d.installed) {
        n.appendChild(installedBadge());
        n.appendChild(actBtn("Delete", "danger linkbtn", () => confirmDelete(d.id, d.title, d.size,
          d.kind === "upscaler_node" ? "SeedVR2 1.4B stops working." : "All SeedVR2 upscalers stop working until it is downloaded again.")));
      } else {
        const users = variants.filter((v) => v.needs.includes(d.key)).map((v) => v.title.replace("SeedVR2 ", ""));
        n.appendChild(el("span", "pstate", `downloaded together with ${users.length === variants.length ? "the first model" : users.join(", ")}`));
      }
      needs.appendChild(n);
    }
    card.appendChild(needs);
    const opts = variants.map((v) => {
      const [, name, quant] = v.title.match(/^SeedVR2 (.*) \((\w+)\)$/) || [, v.title, ""];
      return { id: v.id, name, format: "safetensors", quant, size: v.size, memory: v.memory, fit: v.fit,
               installed: v.installed, note: v.description, memNote: `at ${data.system.seedvr2_ref_mp} MP` };
    });
    card.appendChild(variantPicker("seedvr2", opts, (o) => {
      const v = variants.find((x) => x.id === o.id);
      if (v.installed) return [installedBadge(), actBtn("Delete", "danger", () => confirmDelete(v.id, v.title, v.size))];
      const extra = v.needs.map(comp).filter((d) => d && !d.installed);
      const total = v.size + extra.reduce((n, d) => n + d.size, 0);
      return [queueBtn(v.id, actBtn(`Download ${fmtBytes(total)}`, "primary", () => startInstall([v.id]),
        extra.length ? `${fmtBytes(v.size)} model + ${extra.map((d) => `${fmtBytes(d.size)} ${d.title}`).join(" + ")}` : ""))];
    }));
    return card;
  }

  // ---------------------------------------------------------------- models (ready mode)
  function missingSize(p) {
    return missingKeys(p).reduce((n, k) => n + (comp(k)?.size || 0), 0);
  }

  function downloadLabel(p, q) {
    const extra = missingSize(p);
    const total = q.size + extra;
    const label = `Download ${fmtBytes(total)}`;
    let title = `${fmtBytes(q.size)} model`;
    for (const k of missingKeys(p)) title += ` + ${fmtBytes(comp(k)?.size)} ${comp(k)?.title || k}`;
    if (extra) title += ` = ${fmtBytes(total)} in total`;
    return { label, title };
  }

  function compLine(p, key, kind) {
    const c = comp(key);
    if (!c) return el("div");
    const n = el("div", "n");
    n.appendChild(el("span", null, `${kind}: ${c.title} (${fmtBytes(c.size)})`));
    if (c.installed) {
      n.appendChild(installedBadge());
      const others = usersOf(key, p.id);
      if (others.length) n.appendChild(el("span", null, `shared with ${others.map((o) => o.title).join(", ")}`));
      const all = usersOf(key, null).map((o) => o.title);
      n.appendChild(actBtn("Delete", "danger linkbtn", () => confirmDelete(
        c.id, c.title, c.size, all.length ? `Used by: ${all.join(", ")}. These models stop working until it is downloaded again.` : "")));
    } else {
      n.appendChild(el("span", "pstate error", "missing, downloaded together with the model"));
    }
    return n;
  }

  function buildCard(p) {
    const card = el("div", `mcard${p.complete ? " complete" : ""}`);
    const head = el("div", "head");
    head.appendChild(el("b", null, p.title));
    if (p.recommended) head.appendChild(el("span", "chip rec", "Recommended"));
    if (p.experimental) head.appendChild(el("span", "chip exp", "Experimental"));
    for (const m of p.modes) head.appendChild(el("span", "chip", m === "edit" ? "Edit" : "Generate"));
    card.appendChild(head);
    if (p.good_for) card.appendChild(el("div", "goodfor", p.good_for));
    card.appendChild(el("div", "hint", p.note));
    const needs = el("div", "needs");
    needs.appendChild(compLine(p, p.text_encoder, "Text encoder"));
    needs.appendChild(compLine(p, p.vae, "VAE"));
    for (const k of p.nodes || []) needs.appendChild(compLine(p, k, "Custom node"));
    card.appendChild(needs);

    const opts = p.quants.map((q) => ({
      id: q.id, format: q.file.endsWith(".gguf") ? "GGUF" : "safetensors",
      quant: q.quant.replace("_convrot", ""), size: q.size, memory: q.memory, fit: q.fit, installed: q.installed,
      rec: q.quant === recQuant(p), recTitle: p.recommended_quant ? "Recommended for your Mac" : "Recommended",
    }));
    card.appendChild(variantPicker(p.id, opts, (o) => {
      const q = p.quants.find((x) => x.id === o.id);
      if (q.installed) return [installedBadge(), actBtn("Delete", "danger", () => confirmDelete(q.id, `${p.title} ${q.quant}`, q.size))];
      const d = downloadLabel(p, q);
      return [queueBtn(q.id, actBtn(d.label, "primary", () => {
        if (q.fit === "no" && !window.confirm(`${p.title} ${q.quant} is probably too large for this Mac (needs ~${fmtBytes(q.memory)}). Download anyway?`)) return;
        startInstall([q.id]);
      }, d.title))];
    }));
    return card;
  }

  // ---------------------------------------------------------------- variant picker (LM Studio style)
  // One button with the chosen quantisation/size and its labels; a click opens the list of all of them.
  // Default choice: the recommended one if installed, else an installed one, else the recommended one.
  const FIT_SHORT = { good: ["ok", "Fits"], tight: ["warn", "Tight"], no: ["bad", "Likely too large"] };

  function optionLine(o, inButton = false) {
    const line = el("span", "vline");
    line.appendChild(el("span", `vfmt ${o.format === "GGUF" ? "gguf" : ""}`, o.format === "GGUF" ? "GGUF" : "ST"));
    line.lastChild.title = o.format;
    if (o.name) line.appendChild(el("span", "vname", o.name));
    if (o.quant) line.appendChild(el("span", "vquant", o.quant));
    const f = FIT_SHORT[o.fit];
    if (f) {
      const b = el("span", `badge ${f[0]}`, f[1]);
      if (o.memory) b.title = `Estimated peak memory ~${fmtBytes(o.memory)}${o.memNote ? ` ${o.memNote}` : ""}`;
      line.appendChild(b);
    }
    if (o.rec) line.appendChild(el("span", "tag-rec", o.recTitle || "Recommended"));
    line.appendChild(el("span", "vgrow"));
    if (o.installed && !inButton) line.appendChild(el("span", "vdone", "Downloaded"));
    line.appendChild(stateTag(o.id));
    const sz = el("span", "vsize", fmtBytes(o.size));
    if (o.memory) sz.title = `~${fmtBytes(o.memory)} memory${o.memNote ? ` ${o.memNote}` : ""}`;
    line.appendChild(sz);
    return line;
  }

  function variantPicker(key, opts, actions) {
    const wrap = el("div", "vpick");
    const def = opts.find((o) => o.rec && o.installed) || opts.find((o) => o.installed) || opts.find((o) => o.rec) || opts[0];
    const cur = opts.find((o) => o.id === picked.get(key)) || def;
    const row = el("div", "vrow");
    const btn = el("button", "vbtn");
    btn.type = "button";
    btn.setAttribute("aria-haspopup", "listbox");
    btn.setAttribute("aria-expanded", "false");
    btn.appendChild(optionLine(cur, true));
    btn.appendChild(el("span", "vcaret", "▾"));
    row.appendChild(btn);
    const side = el("div", "vact");
    for (const a of actions(cur)) side.appendChild(a);
    row.appendChild(side);
    wrap.appendChild(row);
    const meta = [];
    if (cur.memory) meta.push(`~${fmtBytes(cur.memory)} memory${cur.memNote ? ` ${cur.memNote}` : ""}`);
    if (cur.note) meta.push(cur.note);
    const inst = opts.filter((o) => o.installed && o !== cur);
    if (inst.length) meta.push(`also downloaded: ${inst.map((o) => o.quant || o.name).join(", ")}`);
    if (meta.length) wrap.appendChild(el("div", "hint vmeta", meta.join(" · ")));

    let menu = null;
    const close = () => {
      if (!menu) return;
      menu.remove();
      menu = null;
      btn.setAttribute("aria-expanded", "false");
      document.removeEventListener("pointerdown", outside, true);
      document.removeEventListener("keydown", onKey, true);
    };
    const outside = (e) => { if (!wrap.contains(e.target)) close(); };
    const onKey = (e) => {
      if (e.key === "Escape") { close(); btn.focus(); return; }
      if (!["ArrowDown", "ArrowUp"].includes(e.key)) return;
      e.preventDefault();
      const items = [...menu.querySelectorAll(".vopt")];
      const i = items.indexOf(document.activeElement);
      items[(i + (e.key === "ArrowDown" ? 1 : items.length - 1)) % items.length]?.focus();
    };
    btn.onclick = () => {
      if (menu) { close(); return; }
      menu = el("div", "vmenu");
      menu.setAttribute("role", "listbox");
      for (const o of opts) {
        const it = el("button", `vopt${o === cur ? " sel" : ""}`);
        it.type = "button";
        it.setAttribute("role", "option");
        it.setAttribute("aria-selected", String(o === cur));
        it.appendChild(el("span", "vcheck", o === cur ? "✓" : ""));
        it.appendChild(optionLine(o));
        it.onclick = () => { picked.set(key, o.id); close(); structural(); };
        menu.appendChild(it);
      }
      wrap.appendChild(menu);
      btn.setAttribute("aria-expanded", "true");
      updateStateTags();
      menu.querySelector(".vopt.sel")?.focus();
      document.addEventListener("pointerdown", outside, true);
      document.addEventListener("keydown", onKey, true);
    };
    return wrap;
  }

  function buildModels() {
    const sec = section("Models");
    sec.id = "setupModels";
    sec.appendChild(el("div", "hint", "Q4 = smaller and faster, BF16 = best quality but needs a lot of RAM. A download also fetches the text encoder and VAE if they are missing."));
    const sl = systemLine();
    if (sl) sec.appendChild(sl);
    for (const p of sorted()) sec.appendChild(buildCard(p));
    return sec;
  }

  // ---------------------------------------------------------------- LoRAs (ready mode)
  // One collapsible group per model line; a group is open when one of its models is installed or it has a LoRA.
  function buildLoras() {
    const sec = section("LoRAs");
    sec.appendChild(el("div", "hint", "Small add-ons for one model line (speed, styles, camera angles). Pick them under Advanced → LoRAs; a LoRA only works with the models of its group."));
    const loras = data.components.filter((c) => c.kind === "lora" && !c.imported);
    for (const [group, families] of Object.entries(data.lora_groups || {})) {
      const items = loras.filter((l) => l.families.some((f) => families.includes(f)));
      if (!items.length) continue;
      const models = data.presets.filter((p) => families.includes(p.family));
      const det = el("details", "lgroup");
      const used = models.some((p) => p.installed_quants.length) || items.some((l) => l.installed);
      det.open = loraOpen.has(group) || (!loraClosed.has(group) && used);
      det.ontoggle = () => {
        if (det.open) { loraOpen.add(group); loraClosed.delete(group); } else { loraClosed.add(group); loraOpen.delete(group); }
      };
      const sum = el("summary");
      sum.appendChild(el("b", null, group));
      if (models.length > 1) sum.appendChild(el("span", "hint", models.map((p) => shortTitle(p, group)).join(" · ")));
      const n = items.filter((l) => l.installed).length;
      sum.appendChild(el("span", "hint lcount", n ? `${n}/${items.length} installed` : `${items.length} LoRAs`));
      det.appendChild(sum);
      const table = el("div", "qtable");
      for (const l of items) table.appendChild(loraRow(l, models, group));
      det.appendChild(table);
      sec.appendChild(det);
    }
    return sec;
  }

  // "Qwen-Image 2.1 UC" in group "Qwen-Image 2.1" -> "UC"; the plain group model -> "official"
  const shortTitle = (p, group) => (p.title.startsWith(group) ? p.title.slice(group.length).trim() : p.title) || "official";

  function loraRow(l, models, group) {
    const row = el("div", "lrow");
    const info = el("div", "linfo");
    const name = el("div", "lname");
    const a = el("a", null, l.title);
    a.href = `https://huggingface.co/${l.repo}`;
    a.target = "_blank";
    a.rel = "noopener";
    a.title = `${l.repo} on Hugging Face`;
    name.appendChild(a);
    const not = models.filter((p) => !l.families.includes(p.family));
    if (not.length) name.appendChild(el("span", "chip", `not for ${not.map((p) => shortTitle(p, group)).join(", ")}`));
    if (l.strength !== 1) name.appendChild(el("span", "hint", `strength ${l.strength}`));
    name.appendChild(stateTag(l.id));
    info.appendChild(name);
    info.appendChild(el("div", "desc", l.description));
    row.appendChild(info);
    const side = el("div", "side");
    side.appendChild(el("span", "sz", fmtBytes(l.size)));
    if (l.installed) {
      side.appendChild(installedBadge());
      side.appendChild(actBtn("Delete", "danger", () => confirmDelete(l.id, `LoRA ${l.title}`, l.size)));
    } else {
      side.appendChild(queueBtn(l.id, actBtn("Download", "primary", () => startInstall([l.id]))));
    }
    row.appendChild(side);
    return row;
  }

  // ---------------------------------------------------------------- first run
  function guidedItems(all) {
    const ids = [];
    for (const b of data.base) if (!b.installed && (all || baseSel.has(b.id))) ids.push(b.id);
    const p = preset(pick.preset);
    const q = p.quants.find((x) => x.quant === pick.quant);
    if (q && !q.installed) ids.push(q.id);   // the server adds the missing encoder and VAE
    else for (const k of missingKeys(p)) ids.push(`component:${k}`);
    const sam = comp("sam3");
    if (sam && sam3Pick && !sam.installed) ids.push(sam.id);
    return ids;
  }

  function buildGuided() {
    const box = el("div", "guided");
    box.appendChild(el("h3", "sec", "1. Base components"));
    for (const b of data.base) {
      const r = el("label", "irow check-row");
      if (b.installed) {
        r.appendChild(el("span"));
      } else {
        const cb = el("input");
        cb.type = "checkbox";
        cb.checked = baseSel.has(b.id);
        cb.className = "act";
        cb.onchange = () => { if (cb.checked) baseSel.add(b.id); else baseSel.delete(b.id); updateActions(); };
        r.appendChild(cb);
      }
      const t = el("div", "name");
      t.appendChild(el("span", null, b.title));
      r.appendChild(t);
      const side = el("div", "side");
      side.appendChild(stateTag(b.id));
      if (b.installed) side.appendChild(installedBadge());
      r.appendChild(side);
      r.appendChild(el("div", "desc", b.description));
      box.appendChild(r);
    }

    box.appendChild(el("h3", "sec", "2. Choose a model"));
    const sel = el("select", "act");
    for (const p of sorted()) sel.add(new Option(`${p.title}${p.recommended ? " – recommended" : ""}${p.experimental ? " (experimental)" : ""}`, p.id));
    sel.value = pick.preset;
    const qsel = el("select", "act");
    const note = el("div", "hint");
    const total = el("div", "hint");
    const fillQuant = () => {
      const p = preset(pick.preset);
      qsel.innerHTML = "";
      for (const q of p.quants) {
        const fit = FIT[q.fit] ? ` · ${FIT[q.fit][1].split(" –")[0].toLowerCase()}` : "";
        qsel.add(new Option(`${q.quant} (${fmtBytes(q.size)})${fit}${q.quant === recQuant(p) ? " – recommended" : ""}`, q.quant));
      }
      if (!p.quants.some((q) => q.quant === pick.quant)) pick.quant = recQuant(p);
      qsel.value = pick.quant;
      note.textContent = [p.good_for, p.note, p.experimental ? "Experimental." : ""].filter(Boolean).join(" ");
      updateTotal();
    };
    const updateTotal = () => {
      const p = preset(pick.preset);
      const q = p.quants.find((x) => x.quant === pick.quant);
      const parts = [];
      let sum = 0;
      if (q && !q.installed) { sum += q.size; parts.push(`${fmtBytes(q.size)} model`); }
      for (const k of missingKeys(p)) { sum += comp(k).size; parts.push(`${fmtBytes(comp(k).size)} ${comp(k).title}`); }
      total.textContent = sum ? `Model download: ${fmtBytes(sum)} (${parts.join(" + ")})` : "Model already installed.";
      updateActions();
    };
    sel.onchange = () => { pick.preset = sel.value; pick.quant = recQuant(preset(pick.preset)); fillQuant(); };
    qsel.onchange = () => { pick.quant = qsel.value; updateTotal(); };
    const lab = el("label", null, "Model");
    lab.appendChild(sel);
    const lab2 = el("label", null, "Quantisation");
    lab2.appendChild(qsel);
    const sl = systemLine();
    box.append(lab, lab2, note, total);
    if (sl) box.appendChild(sl);

    const sam = comp("sam3");
    if (sam) {
      box.appendChild(el("h3", "sec", "3. Optional"));
      const r = el("label", "irow check-row");
      const cb = el("input");
      cb.type = "checkbox";
      cb.className = "act";
      cb.checked = sam3Pick;
      cb.dataset.locked = sam.installed ? "1" : "";
      cb.onchange = () => { sam3Pick = cb.checked; updateActions(); };
      r.appendChild(cb);
      const t = el("div", "name");
      t.appendChild(el("span", null, "Masking (SAM3)"));
      t.appendChild(el("span", "tag-opt", "Optional"));
      r.appendChild(t);
      const side = el("div", "side");
      side.appendChild(el("span", "hint", fmtBytes(sam.size)));
      side.appendChild(stateTag(sam.id));
      if (sam.installed) side.appendChild(installedBadge());
      r.appendChild(side);
      r.appendChild(el("div", "desc", "Computes masks from a text description. You can add it later in the Download Center."));
      box.appendChild(r);
    }

    const actions = el("div", "setup-actions");
    ui.installSel = actBtn("Install selected", "primary", () => startInstall(guidedItems(false)));
    ui.installAll = actBtn("Install everything missing", "", () => startInstall(guidedItems(true)));
    actions.append(ui.installSel, ui.installAll);
    box.appendChild(actions);
    ui.fillQuant = fillQuant;
    fillQuant();
    return box;
  }

  // ---------------------------------------------------------------- folders
  function buildPaths() {
    const d = el("details");
    d.open = pathsOpen;
    d.addEventListener("toggle", () => { pathsOpen = d.open; });
    d.appendChild(el("summary", null, "Folders"));
    d.appendChild(el("div", "hint", "An existing Comfy Desktop install is detected automatically."));
    ui.pathInputs = {};
    for (const [key, label] of PATH_FIELDS) {
      const l = el("label", null, label);
      const inp = el("input");
      inp.type = "text";
      inp.spellcheck = false;
      inp.value = data.config[key] || "";
      inp.className = "act-input";
      ui.pathInputs[key] = inp;
      l.appendChild(inp);
      d.appendChild(l);
    }
    const save = actBtn("Save paths", "", async () => {
      const body = {};
      for (const [key] of PATH_FIELDS) body[key] = ui.pathInputs[key].value.trim();
      try {
        data = await postJson("/api/setup/config", body);
        showErr("");
        structural();
        onChanged(data);
      } catch (e) { showErr(e.message); }
    });
    save.style.alignSelf = "flex-start";
    d.appendChild(save);
    return d;
  }

  // ---------------------------------------------------------------- updates
  // ---------------------------------------------------------------- own files (ready mode)
  // A file already on the Mac is linked into the matching models/ folder (not copied); Remove deletes the link only.
  let importDraft = null;   // the picked file while its kind etc. are being chosen
  function buildImports() {
    const sec = section("Your files");
    sec.id = "setupImports";
    sec.appendChild(el("div", "hint", "Use a model, LoRA, upscaler or other model file that is already on this Mac. It is linked into the models folder, not copied."));
    for (const i of data.imports || []) {
      const what = data.import_kinds?.[i.kind] || i.kind;
      const base = i.kind === "model" ? data.presets.find((p) => p.id === i.base)?.title : "";
      const groups = i.kind === "lora" ? Object.entries(data.lora_groups || {}).filter(([, f]) => f.some((x) => (i.families || []).includes(x))).map(([g]) => g) : [];
      const r = el("div", "irow");
      const name = el("div", "name");
      name.append(el("span", null, i.title), el("span", "tag-opt", what));
      r.append(name, el("div", "desc", [base && `runs like ${base}`, groups.length && `for ${groups.join(", ")}`,
        i.kind === "upscaler" && `${i.scale}×`, i.source].filter(Boolean).join(" · ")));
      const side = el("div", "side");
      side.append(el("span", "hint", fmtBytes(i.size)), actBtn("Remove", "danger", async () => {
        showErr("");
        try { data = await api(`/api/imports/${encodeURIComponent(i.id)}`, { method: "DELETE" }); structural(); onChanged(data); }
        catch (e) { showErr(e.message); }
      }, "Removes the link; your file stays where it is"));
      r.appendChild(side);
      sec.appendChild(r);
    }
    if (importDraft) sec.appendChild(importForm(importDraft));
    else {
      const pick = actBtn("Import file…", "", async () => {
        showErr(""); pick.disabled = true;
        try {
          const g = await postJson("/api/imports/pick", {});
          if (g.path) { importDraft = g; structural(); }
        } catch (e) { showErr(e.message); }
        finally { pick.disabled = false; }
      });
      const row = el("div", "row");
      row.appendChild(pick);
      sec.appendChild(row);
    }
    return sec;
  }

  function importForm(g) {
    const f = el("div", "irow import-form");
    const top = el("div", "name");
    top.append(el("span", null, g.name), el("span", "hint", fmtBytes(g.size)));
    f.appendChild(top);
    const fields = el("div", "grid2");
    const field = (label, input) => { const l = el("label"); l.append(el("span", "lbl", label), input); fields.appendChild(l); return input; };
    const kind = field("What is it?", el("select"));
    for (const [k, label] of Object.entries(data.import_kinds || {})) kind.add(new Option(label, k, false, k === g.kind));
    const title = field("Name", el("input"));
    title.type = "text";
    title.value = g.name.replace(/\.[^.]+$/, "");
    const base = field("Runs like", el("select"));
    for (const p of data.presets.filter((p) => !p.imported)) base.add(new Option(p.title, p.id));
    const group = field("Works with", el("select"));
    for (const name of Object.keys(data.lora_groups || {})) group.add(new Option(name, name));
    const scale = field("Scale", el("select"));
    for (const s of [1, 2, 4, 8]) scale.add(new Option(`${s}×`, s, false, s === g.scale));
    const sync = () => {
      base.parentElement.hidden = kind.value !== "model";
      group.parentElement.hidden = kind.value !== "lora";
      scale.parentElement.hidden = kind.value !== "upscaler";
    };
    kind.onchange = sync; sync();
    f.appendChild(fields);
    if (!g.supported) f.appendChild(el("div", "warn", "Only .safetensors, .gguf, .sft, .pth, .pt, .ckpt and .bin files can be imported."));
    const actions = el("div", "row");
    actions.append(actBtn("Import", "primary", async () => {
      showErr("");
      try {
        const r = await postJson("/api/imports", {
          path: g.path, kind: kind.value, title: title.value, base: base.value,
          families: data.lora_groups?.[group.value] || [], scale: parseInt(scale.value, 10),
        });
        importDraft = null; data = r.setup; structural(); onChanged(data);
      } catch (e) { showErr(e.message); }
    }), actBtn("Cancel", "", () => { importDraft = null; structural(); }));
    f.appendChild(actions);
    return f;
  }

  function showErr(msg) {
    if (!ui) return;
    ui.error.textContent = msg || "";
    ui.error.hidden = !msg;
  }

  // rebuild the lists (installed flags or layout changed)
  function structural() { build(); }

  function updateActions() {
    const busy = running() || waitingComfy;
    root.classList.toggle("busy", busy);
    const steps = data.install?.steps || {};
    const queued = (id) => running() && ["pending", "running"].includes(steps[id]?.state);
    for (const b of root.querySelectorAll("button.act, select.act, input.act")) {
      const canQueue = b.classList.contains("queue") && data.ready && !waitingComfy;
      b.disabled = (canQueue ? queued(b.dataset.item) : busy) || b.dataset.locked === "1";
    }
    for (const i of root.querySelectorAll("input.act-input")) i.disabled = busy;
    ui.back.hidden = !data.ready;
    if (ui.installSel) {
      ui.installSel.disabled = busy || !guidedItems(false).length;
      ui.installAll.disabled = busy || !guidedItems(true).length;
    }
  }

  function updateAll() {
    ui.restart.hidden = !data.install?.restart_comfy;
    updateProgress();
    updateStateTags();
    updateActions();
  }

  // ---------------------------------------------------------------- install flow
  async function startInstall(items) {
    if (!items.length) return;
    showErr("");
    try {
      const queueing = running();
      if (!queueing) readyAtStart = data.ready;
      data = await postJson("/api/setup/install", { items });
      wasRunning = true;
      updateAll();
      if (!queueing) root.scrollTo({ top: 0 });
      schedulePoll();
    } catch (e) { showErr(e.message); }
  }

  function schedulePoll() {
    stopPoll();
    pollTimer = setTimeout(poll, 700);
  }

  async function poll() {
    try {
      data = await api("/api/setup");
    } catch (e) {
      showErr(e.message);
      schedulePoll();
      return;
    }
    updateAll();
    if (running()) { schedulePoll(); return; }
    if (!wasRunning) return;
    wasRunning = false;
    const err = data.install?.error;
    if (err) {
      structural();
      showErr(err);
      onChanged(data);
      return;
    }
    // nothing to start yet (cancelled before the base install finished)
    if (!data.ready) { structural(); onChanged(data); return; }
    await waitForComfy();
  }

  async function waitForComfy() {
    waitingComfy = true;
    updateAll();
    const t0 = Date.now();
    let up = false;
    while (Date.now() - t0 < 180000 && waitingComfy) {
      try { up = !!(await api("/api/status")).comfy; } catch { up = false; }
      if (up) break;
      await new Promise((r) => setTimeout(r, 2000));
    }
    if (!waitingComfy) return;   // view was closed meanwhile
    waitingComfy = false;
    try { data = await api("/api/setup"); } catch { /* keep old data */ }
    structural();
    if (!up) {
      showErr("ComfyUI did not start within 3 minutes. Check ~/Library/Logs/InpaintStudio-ComfyUI.log for details.");
      return;
    }
    if (data.ready && !readyAtStart) onReady(data);
    else onChanged(data);
  }

  // ---------------------------------------------------------------- public
  // section "models": jump straight to the model list (download centre)
  async function open({ section: target } = {}) {
    stopPoll();
    root.hidden = false;
    await load();
    build();
    const ids = { models: "setupModels", upscalers: "setupUpscalers", control: "setupControl", files: "setupImports" };
    const anchor = ids[target] && root.querySelector(`#${ids[target]}`);
    if (anchor) anchor.scrollIntoView({ block: "start" });
    else root.scrollTop = 0;
    if (waitingComfy) return;   // a finished queue is still waiting for ComfyUI (polling went on in the background)
    if (running() && !wasRunning) { wasRunning = true; readyAtStart = data.ready; }
    if (wasRunning) schedulePoll();
  }

  // a running queue keeps being polled after closing, so the app picks up new models when it finishes
  function close() {
    if (!wasRunning) stopPoll();
    root.hidden = true;
  }

  return { open, close };
}
