// Prompt presets: built-in list + custom presets in localStorage, and the "save your prompt?" dialog.
import { BUILTIN_PROMPTS } from "/prompts.js";

const KEY = "inpaint-studio-prompts-v1";
const $ = (id) => document.getElementById(id);

function loadCustom() {
  try {
    const list = JSON.parse(localStorage.getItem(KEY) || "[]");
    return Array.isArray(list) ? list.filter((p) => p && p.id && p.title && typeof p.prompt === "string") : [];
  } catch { return []; }
}
function storeCustom(list) {
  try { localStorage.setItem(KEY, JSON.stringify(list)); } catch { /* storage optional */ }
}

// Opens the dialog. Resolves to {action: "save"|"skip"|"cancel", name}.
function askDialog({ title, name, withSkip, saveLabel }) {
  const modal = $("modal"), input = $("modalName");
  $("modalTitle").textContent = title;
  $("modalSave").textContent = saveLabel;
  $("modalSkip").hidden = !withSkip;
  input.value = name;
  modal.hidden = false;
  input.focus();
  input.select();
  return new Promise((resolve) => {
    const done = (action) => {
      modal.hidden = true;
      modal.removeEventListener("keydown", onKey);
      $("modalSave").onclick = $("modalSkip").onclick = $("modalCancel").onclick = null;
      modal.onclick = null;
      resolve({ action, name: input.value.trim() });
    };
    const onKey = (e) => {
      if (e.key === "Escape") { e.preventDefault(); done("cancel"); }
      else if (e.key === "Enter" && e.target.tagName !== "BUTTON") { e.preventDefault(); if (input.value.trim()) done("save"); }
      else if (e.key === "Tab") {   // keep focus inside the dialog
        const els = [input, ...modal.querySelectorAll("button:not([hidden])")];
        const i = els.indexOf(document.activeElement);
        const next = e.shiftKey ? (i <= 0 ? els.length - 1 : i - 1) : (i + 1) % els.length;
        e.preventDefault(); els[next].focus();
      }
    };
    modal.addEventListener("keydown", onKey);
    $("modalSave").onclick = () => { if (input.value.trim()) done("save"); else input.focus(); };
    $("modalSkip").onclick = () => done("skip");
    $("modalCancel").onclick = () => done("cancel");
    modal.onclick = (e) => { if (e.target === modal) done("cancel"); };
  });
}

// deps: { getTask(): "edit"|"generate" }
export function initPromptPresets({ getTask }) {
  const sel = $("promptPreset"), area = $("prompt"), neg = $("negative");
  let custom = loadCustom();
  let userEdited = false;   // true once the user typed in the textarea (restored text does not count)

  const all = () => [...BUILTIN_PROMPTS, ...custom];
  const norm = (t) => t.trim();
  const matching = () => all().find((p) => norm(p.prompt) === norm(area.value));
  const isCustomText = () => userEdited && norm(area.value) !== "" && !matching();

  function refresh() {
    const task = getTask();
    sel.innerHTML = "";
    const ph = new Option("Choose a preset…", "");
    ph.disabled = true;
    sel.add(ph);
    const addGroup = (label, list) => {
      if (!list.length) return;
      const g = document.createElement("optgroup");
      g.label = label;
      for (const p of list) g.appendChild(new Option(p.title, p.id));
      sel.appendChild(g);
    };
    addGroup("Built-in", BUILTIN_PROMPTS.filter((p) => p.task === task));
    addGroup("My presets", custom.filter((p) => p.task === task));
    syncSelect();
  }

  function syncSelect() {
    const m = matching();
    const inList = m && [...sel.options].some((o) => o.value === m.id);
    sel.options[0].textContent = norm(area.value) ? "Custom" : "Choose a preset…";
    sel.value = inList ? m.id : "";
    $("promptDelete").disabled = !(inList && custom.some((p) => p.id === m.id));
    $("promptSave").disabled = !norm(area.value);
  }

  function apply(p) {
    area.value = p.prompt;
    if (p.negative) neg.value = p.negative;
    userEdited = false;
    area.dispatchEvent(new Event("change", { bubbles: true }));
    neg.dispatchEvent(new Event("change", { bubbles: true }));
    syncSelect();
  }

  function addCustom(name) {
    const p = { id: `custom-${Date.now().toString(36)}`, task: getTask(), title: name, prompt: area.value.trim(), negative: neg.value.trim() };
    custom.push(p);
    storeCustom(custom);
    return p;
  }

  area.addEventListener("input", () => { userEdited = true; syncSelect(); });

  sel.addEventListener("change", async () => {
    const target = all().find((p) => p.id === sel.value);
    if (!target) { syncSelect(); return; }
    if (isCustomText()) {
      const text = area.value.trim();
      const res = await askDialog({
        title: "Save your current prompt as a preset?", name: text.slice(0, 40), withSkip: true, saveLabel: "Save & switch",
      });
      if (res.action === "cancel") { syncSelect(); return; }
      if (res.action === "save") addCustom(res.name || text.slice(0, 40));
      refresh();
    }
    apply(target);
  });

  $("promptSave").onclick = async () => {
    if (!norm(area.value)) return;
    const res = await askDialog({ title: "Save prompt as preset", name: area.value.trim().slice(0, 40), withSkip: false, saveLabel: "Save" });
    if (res.action !== "save") return;
    addCustom(res.name);
    userEdited = false;
    refresh();
  };

  $("promptDelete").onclick = () => {
    const id = sel.value;
    custom = custom.filter((p) => p.id !== id);
    storeCustom(custom);
    refresh();
  };

  refresh();
  return { refresh };
}
