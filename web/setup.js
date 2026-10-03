// Setup & downloads: first-run guide, component and model download centre, live install progress.
// Sections are rebuilt only on structural changes; polling just updates the progress panel in place,
// so CSS animations are not restarted.

const PATH_FIELDS = [
  ["comfy_dir", "ComfyUI folder"], ["models_dir", "Models folder"],
  ["input_dir", "Input folder"], ["output_dir", "Output folder"],
];
const COLLAPSE_AFTER = 4;   // quant lists longer than this are collapsed

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
  const expanded = new Set();     // preset ids with the full quant list open
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
  const sorted = () => [...data.presets].sort((a, b) => (b.recommended ? 1 : 0) - (a.recommended ? 1 : 0));
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
    ui.title = el("h2", null, data.ready ? "Setup & downloads" : "Welcome to Inpaint Studio");
    head.appendChild(ui.title);
    ui.back = el("button", "small", "Back to app");
    ui.back.style.marginLeft = "auto";
    ui.back.onclick = () => { stopPoll(); onBack(); };
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
    } else {
      seedFirstRun();
      inner.appendChild(buildGuided());
    }
    inner.appendChild(buildPaths());
    updateAll();
    root.scrollTop = scroll;
  }

  // ---------------------------------------------------------------- progress panel
  function buildProgressPanel() {
    ui.panel = el("div", "setup-overall");
    const row = el("div", "row");
    ui.overallIcon = el("span");
    ui.overallText = el("span");
    ui.cancel = el("button", "small", "Cancel");
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
      r.el.append(r.icon, r.title, r.msg, r.bar, r.detail);
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
      side.appendChild(actBtn("Install", "primary", () => startInstall([id])));
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
    for (const u of data.components.filter((c) => c.kind === "upscaler" || c.kind === "upscaler_vae")) {
      sec.appendChild(itemRow({
        id: u.id, title: u.title, desc: u.description, size: u.size, installed: u.installed, deletable: true, optional: true,
        onDelete: () => confirmDelete(u.id, u.title, u.size, ""),
      }));
    }
    return sec;
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

    const long = p.quants.length > COLLAPSE_AFTER;
    const open = expanded.has(p.id);
    const shown = !long || open ? p.quants : p.quants.filter((q) => q.installed || q.quant === recQuant(p));
    const table = el("div", "qtable");
    for (const q of shown) {
      const row = el("div", "qrow");
      row.appendChild(el("span", "q", q.quant));
      const grow = el("span", "grow");
      grow.appendChild(el("span", "sz", fmtBytes(q.size)));
      if (q.memory) grow.appendChild(el("span", "sz", `~${fmtBytes(q.memory)} memory`));
      const fb = fitBadge(q);
      if (fb) grow.appendChild(fb);
      if (q.quant === recQuant(p)) grow.appendChild(el("span", "tag-rec", p.recommended_quant ? "Recommended for your Mac" : "recommended"));
      grow.appendChild(stateTag(q.id));
      row.appendChild(grow);
      if (q.installed) {
        row.appendChild(installedBadge());
        row.appendChild(actBtn("Delete", "danger", () => confirmDelete(q.id, `${p.title} ${q.quant}`, q.size)));
      } else {
        const d = downloadLabel(p, q);
        row.appendChild(actBtn(d.label, "primary", () => {
          if (q.fit === "no" && !window.confirm(`${p.title} ${q.quant} is probably too large for this Mac (needs ~${fmtBytes(q.memory)}). Download anyway?`)) return;
          startInstall([q.id]);
        }, d.title));
      }
      table.appendChild(row);
    }
    card.appendChild(table);
    if (long) {
      const t = actBtn(open ? "Show fewer quantisations" : `Show all ${p.quants.length} quantisations`, "linkbtn", () => {
        if (open) expanded.delete(p.id); else expanded.add(p.id);
        structural();
      });
      t.style.alignSelf = "flex-start";
      card.appendChild(t);
    }
    return card;
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
      r.appendChild(el("div", "desc", "Computes masks from a text description. You can add it later under Downloads."));
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
    for (const b of root.querySelectorAll("button.act, select.act, input.act")) b.disabled = busy || b.dataset.locked === "1";
    for (const i of root.querySelectorAll("input.act-input")) i.disabled = busy;
    ui.back.hidden = !data.ready || busy;
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
      readyAtStart = data.ready;
      data = await postJson("/api/setup/install", { items });
      wasRunning = true;
      updateAll();
      root.scrollTo({ top: 0 });
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
    waitingComfy = false;
    root.hidden = false;
    await load();
    build();
    const anchor = target === "models" && root.querySelector("#setupModels");
    if (anchor) anchor.scrollIntoView({ block: "start" });
    else root.scrollTop = 0;
    if (running()) { wasRunning = true; readyAtStart = data.ready; schedulePoll(); }
  }

  function close() {
    stopPoll();
    waitingComfy = false;
    root.hidden = true;
  }

  return { open, close };
}
