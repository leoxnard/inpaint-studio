// Setup page: installs the required components (and optional masking) and shows live progress.
// Built once per open; rows are updated in place so CSS animations are not restarted by polling.

const QUANT_HINT = "Q4 = smaller and faster, BF16 = best quality but needs a lot of RAM.";
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

// deps: { api, postJson, root, onReady(data), onBack() }
export function createSetup({ api, postJson, root, onReady, onBack }) {
  let data = null;
  let selected = new Set();
  let ui = null;              // element references of the current skeleton
  let pollTimer = 0;
  let waitingComfy = false;
  let wasRunning = false;
  let seeded = false;

  function stopPoll() { clearTimeout(pollTimer); pollTimer = 0; }

  function seedSelection() {
    selected = new Set(data.steps.filter((s) => !s.installed && !s.optional).map((s) => s.id));
    seeded = true;
  }

  async function load() {
    data = await api("/api/setup");
    if (!seeded) seedSelection();
  }

  // ---------------------------------------------------------------- skeleton
  function build() {
    root.textContent = "";
    ui = { rows: {} };
    const inner = el("div", "setup-inner");
    root.appendChild(inner);

    const head = el("div", "row wrap");
    ui.title = el("h2", null, "Setup");
    head.appendChild(ui.title);
    ui.back = el("button", "small", "Back to app");
    ui.back.style.marginLeft = "auto";
    ui.back.onclick = () => { stopPoll(); onBack(); };
    head.appendChild(ui.back);
    inner.appendChild(head);
    inner.appendChild(el("p", "lead", "Inpaint Studio needs a few components. They are downloaded from GitHub and Hugging Face."));

    ui.overall = el("div", "setup-overall");
    ui.overallRow = el("div", "row");
    ui.overallIcon = el("span");
    ui.overallText = el("span");
    ui.overallRow.append(ui.overallIcon, ui.overallText);
    ui.overall.appendChild(ui.overallRow);
    ui.overallBar = el("div", "pbar");
    ui.overallBar.appendChild(el("i"));
    ui.overall.appendChild(ui.overallBar);
    inner.appendChild(ui.overall);

    ui.error = el("div", "setup-error");
    inner.appendChild(ui.error);

    ui.list = el("div", "setup-list");
    for (const s of data.steps) {
      const row = buildRow(s);
      ui.rows[s.id] = row;
      ui.list.appendChild(row.el);
    }
    inner.appendChild(ui.list);

    inner.appendChild(buildPaths());

    const actions = el("div", "setup-actions");
    ui.installSel = el("button", "primary", "Install selected");
    ui.installAll = el("button", null, "Install everything missing");
    ui.cancel = el("button", null, "Cancel");
    ui.total = el("span", "total");
    ui.installSel.onclick = () => startInstall([...selected]);
    ui.installAll.onclick = () => startInstall(data.steps.filter((s) => !s.installed).map((s) => s.id));
    ui.cancel.onclick = async () => {
      ui.cancel.disabled = true;
      try { await postJson("/api/setup/cancel", {}); } catch (e) { showErr(e.message); }
    };
    actions.append(ui.installSel, ui.installAll, ui.cancel, ui.total);
    inner.appendChild(actions);
  }

  function buildRow(s) {
    const r = { el: el("div", "step"), step: s };
    r.icon = el("div", "lead-icon");
    r.cb = el("input");
    r.cb.type = "checkbox";
    r.cb.onchange = () => { if (r.cb.checked) selected.add(s.id); else selected.delete(s.id); updateActions(); };
    r.name = el("div", "name");
    r.name.appendChild(el("span", null, s.title));
    r.tagSlot = el("span");
    r.name.appendChild(r.tagSlot);
    r.size = el("div", "size");
    r.desc = el("div", "desc", s.description);
    r.extra = el("div", "extra");
    r.msg = el("div", "msg");
    r.bar = el("div", "pbar");
    r.bar.appendChild(el("i"));
    r.detail = el("div", "detail");
    r.extra.append(r.msg, r.bar, r.detail);
    r.el.append(r.icon, r.name, r.size, r.desc);
    if (s.id === "unet") {
      r.quantWrap = el("div", "extra");
      const lab = el("label", null, "Quantisation");
      r.quant = el("select");
      lab.appendChild(r.quant);
      r.quantWrap.append(lab, el("div", "hint", QUANT_HINT));
      r.quant.onchange = changeQuant;
      r.el.appendChild(r.quantWrap);
    }
    r.el.appendChild(r.extra);
    return r;
  }

  function buildPaths() {
    const d = el("details");
    ui.paths = d;
    d.appendChild(el("summary", null, "Folders"));
    d.appendChild(el("div", "hint", "An existing Comfy Desktop install is detected automatically."));
    ui.pathInputs = {};
    for (const [key, label] of PATH_FIELDS) {
      const l = el("label", null, label);
      const inp = el("input");
      inp.type = "text";
      inp.spellcheck = false;
      ui.pathInputs[key] = inp;
      l.appendChild(inp);
      d.appendChild(l);
    }
    ui.savePaths = el("button", null, "Save paths");
    ui.savePaths.style.alignSelf = "flex-start";
    ui.savePaths.onclick = async () => {
      const body = {};
      for (const [key] of PATH_FIELDS) body[key] = ui.pathInputs[key].value.trim();
      ui.savePaths.disabled = true;
      try {
        data = await postJson("/api/setup/config", body);
        dropInstalledFromSelection();
        refreshAll(true);
      } catch (e) { showErr(e.message); }
      finally { updateActions(); }
    };
    d.appendChild(ui.savePaths);
    return d;
  }

  async function changeQuant() {
    const q = ui.rows.unet.quant.value;
    try {
      data = await postJson("/api/setup/config", { quant: q });
      dropInstalledFromSelection();
      refreshAll();
    } catch (e) {
      showErr(e.message);
      refreshAll();
    }
  }

  function dropInstalledFromSelection() {
    for (const s of data.steps) if (s.installed) selected.delete(s.id);
  }

  // ---------------------------------------------------------------- updates
  function showErr(msg) {
    ui.error.textContent = msg || "";
    ui.error.hidden = !msg;
  }

  const running = () => !!data.install?.running;

  function refreshAll(fillPaths = false) {
    // rows may change (installed flags); rebuild fields in place
    for (const s of data.steps) {
      const r = ui.rows[s.id];
      r.step = s;
      updateRow(r);
    }
    if (fillPaths || !ui.pathsFilled) {
      for (const [key] of PATH_FIELDS) ui.pathInputs[key].value = data.config[key] || "";
      ui.pathsFilled = true;
    }
    updateOverall();
    updateActions();
  }

  function updateRow(r) {
    const s = r.step;
    const ist = data.install?.steps?.[s.id];
    const inProgress = !!ist && (running() || ist.state === "error" || ist.state === "cancelled");
    const state = inProgress ? ist.state : (s.installed ? "installed" : "idle");
    r.el.className = `step ${state === "installed" ? "installed" : state === "idle" ? "" : state}`;

    r.tagSlot.textContent = "";
    if (state === "installed") {
      const b = el("span", "badge ok", "Installed");
      r.tagSlot.appendChild(b);
    } else if (s.optional) {
      r.tagSlot.appendChild(el("span", "tag-opt", "Optional"));
    }

    if (r.iconKey !== state) {
      r.iconKey = state;
      r.icon.textContent = "";
      if (state === "idle") {
        r.icon.appendChild(r.cb);
      } else if (state === "installed") {
        r.icon.appendChild(el("span", "icon-check static", "✓"));
      } else if (state === "pending") {
        r.icon.appendChild(el("span", "icon-wait"));
      } else if (state === "running") {
        r.icon.appendChild(el("span", "icon-spin"));
      } else if (state === "done") {
        r.icon.appendChild(el("span", "icon-check", "✓"));
      } else if (state === "error") {
        r.icon.appendChild(el("span", "icon-cross", "!"));
      } else {
        r.icon.appendChild(el("span", "icon-dash"));
      }
    }
    r.cb.checked = selected.has(s.id);
    r.cb.disabled = running();
    r.cb.setAttribute("aria-label", s.title);
    r.size.textContent = fmtBytes(s.size);
    if (r.quantWrap) {
      r.quant.innerHTML = "";
      for (const [q, n] of Object.entries(data.quants)) r.quant.add(new Option(`${q} (${fmtBytes(n)})`, q));
      r.quant.value = data.config.quant;
      r.quant.disabled = running();
    }

    // progress area
    const showExtra = inProgress && ["running", "error", "done", "cancelled"].includes(state) && (ist.message || state === "running");
    r.extra.hidden = !showExtra;
    if (!showExtra) return;
    const total = ist.total, have = ist.done || 0;
    const det = state === "running" && total;
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
    r.bar.hidden = state !== "running";
    r.bar.classList.toggle("indeterminate", state === "running" && !total);
    r.bar.firstChild.style.width = det ? `${Math.min(100, (have / total) * 100).toFixed(1)}%` : "";
    r.detail.textContent = state === "running" ? (ist.detail || "") : "";
    r.detail.hidden = !r.detail.textContent;
  }

  function updateOverall() {
    const inst = data.install || {};
    const ids = Object.keys(inst.steps || {});
    const title = (id) => data.steps.find((s) => s.id === id)?.title || id;
    let text = "", icon = "", show = true;
    if (waitingComfy) {
      text = "Starting ComfyUI…";
      icon = "icon-spin";
    } else if (inst.running) {
      const cur = ids.find((id) => inst.steps[id].state === "running");
      const idx = cur ? ids.indexOf(cur) : ids.findIndex((id) => inst.steps[id].state === "pending");
      text = cur ? `Step ${idx + 1} of ${ids.length}: ${title(cur)}` : `Preparing ${ids.length} steps…`;
      icon = "icon-spin";
    } else {
      show = false;
    }
    ui.overall.hidden = !show;
    ui.overallIcon.className = icon;
    ui.overallText.textContent = text;
    const done = ids.filter((id) => inst.steps[id].state === "done" || inst.steps[id].state === "skipped").length;
    ui.overallBar.classList.toggle("indeterminate", waitingComfy);
    ui.overallBar.firstChild.style.width = waitingComfy ? "" : `${ids.length ? (done / ids.length) * 100 : 0}%`;
    ui.error.hidden = !ui.error.textContent;
  }

  function updateActions() {
    const busy = running() || waitingComfy;
    let sum = 0;
    for (const s of data.steps) if (selected.has(s.id) && !s.installed) sum += s.size || 0;
    ui.total.textContent = selected.size ? `Selected: ${fmtBytes(sum) || "0 MB"}` : "";
    ui.installSel.disabled = busy || !selected.size;
    const missing = data.steps.filter((s) => !s.installed);
    ui.installAll.disabled = busy || !missing.length;
    ui.cancel.hidden = !running();
    ui.cancel.disabled = false;
    ui.savePaths.disabled = busy;
    for (const [key] of PATH_FIELDS) ui.pathInputs[key].disabled = busy;
    ui.back.hidden = !data.ready || busy;
    ui.title.textContent = data.ready ? "Setup" : "Welcome to Inpaint Studio";
  }

  // ---------------------------------------------------------------- install flow
  async function startInstall(ids) {
    if (!ids.length) return;
    showErr("");
    try {
      data = await postJson("/api/setup/install", { steps: ids });
      wasRunning = true;
      refreshAll();
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
    refreshAll();
    if (running()) { schedulePoll(); return; }
    if (!wasRunning) return;
    wasRunning = false;
    const err = data.install?.error;
    if (err) {
      showErr(err);
      dropInstalledFromSelection();
      refreshAll();
      return;
    }
    await waitForComfy();
  }

  async function waitForComfy() {
    waitingComfy = true;
    refreshAll();
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
    dropInstalledFromSelection();
    refreshAll();
    if (!up) {
      showErr("ComfyUI did not start within 3 minutes. Check ~/Library/Logs/InpaintStudio-ComfyUI.log for details.");
      return;
    }
    if (data.ready) onReady(data);
  }

  // ---------------------------------------------------------------- public
  async function open() {
    stopPoll();
    waitingComfy = false;
    root.hidden = false;
    await load();
    build();
    refreshAll(true);
    showErr("");
    // the server may be installing already (page reloaded mid-install)
    if (running()) { wasRunning = true; schedulePoll(); }
    ui.paths.open = !data.ready;
  }

  function close() {
    stopPoll();
    waitingComfy = false;
    root.hidden = true;
  }

  return { open, close };
}
