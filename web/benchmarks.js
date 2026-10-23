// Renders the benchmarks page from data.json (GET /api/benchmarks, App Support/benchmarks). Edit mode (corner button):
// texts become editable, a click on a score dot sets the score, Save (or Cmd+S) writes it back (POST /api/benchmarks).
(async () => {
  const main = document.querySelector("main"), toc = document.querySelector(".toc");
  let data;
  try {
    const r = await fetch("/api/benchmarks", { cache: "no-store" });
    if (!r.ok) throw new Error(r.status);
    data = await r.json();
  } catch {
    main.innerHTML = `<section class="hero"><h1>Benchmarks</h1><p class="lead">No benchmarks yet. They live in
      <code>~/Library/Application Support/Inpaint Studio/benchmarks/</code> (<code>data.json</code> + <code>img/</code>).</p></section>`;
    return;
  }

  // ------------------------------------------------------------------ building blocks
  const zooms = [];          // zoom blocks, initialised once the page is in the DOM
  const binds = new Map();   // editable element -> (html) => store it in data
  const h = (tag, cls, html) => {
    const el = document.createElement(tag);
    if (cls) el.className = cls;
    if (html != null) el.innerHTML = html;
    return el;
  };
  const ed = (el, obj, key) => { binds.set(el, (v) => { obj[key] = v; }); el.dataset.ed = ""; return el; };   // obj[key] holds el's HTML
  const edH = (tag, cls, obj, key) => ed(h(tag, cls, obj[key] ?? ""), obj, key);
  const dots = (s) => {
    const d = h("span", "dots");
    for (let i = 0; i < 5; i++) d.append(h("i", i < s.value ? "on" : ""));
    d._score = s;
    return d;
  };

  function card(c) {
    const a = h("article", "card" + (c.best ? " best" : ""));
    const pic = h("a", "pic");
    pic.href = c.focus ? `#${c.zoom || "upViewer"}` : benchAsset(c.img);
    if (c.focus) { pic.dataset.focus = c.focus; pic.dataset.zoom = c.zoom || "upViewer"; pic.title = "Open in the zoom viewer"; } else pic.target = "_blank";
    const img = h("img"); img.src = benchAsset(c.img); img.alt = ""; img.loading = "lazy";
    pic.append(img);
    const body = h("div", "body"), title = h("div", "title");
    title.append(edH("span", "", c, "title"));
    if (c.badge != null) title.append(edH("span", "badge", c, "badge"));
    body.append(title, edH("div", "set", c, "set"));
    if (c.scores?.length) {
      const sc = h("div", "scores");
      c.scores.forEach((s) => sc.append(edH("span", "", s, "name"), dots(s)));
      body.append(sc);
    }
    if (c.verdict != null) body.append(edH("p", "verdict", c, "verdict"));
    if (c.set2 != null) body.append(edH("div", "set", c, "set2"));
    if (c.run) body.append(edH("div", "run", c, "run"));
    a.append(pic, body);
    return a;
  }

  const BLOCKS = {
    goal: (b) => edH("p", "goal", b, "html"),
    note: (b) => edH("p", "group-note", b, "html"),
    group: (b) => edH("h3", "group", b, "text"),
    input(b) {
      const wrap = h("div", "input"), a = h("a"), img = h("img"), note = h("div", "note");
      a.href = benchAsset(b.img); a.target = "_blank";
      img.src = benchAsset(b.img); img.alt = b.alt;
      Object.assign(img.style, { width: "100%", borderRadius: "var(--r-img)", border: "1px solid var(--hairline)" });
      a.append(img);
      b.notes.forEach((_, i) => note.append(edH("p", "", b.notes, i)));
      wrap.append(a, note);
      return wrap;
    },
    cards(b) {
      const g = h("div", "grid");
      b.cards.forEach((c) => g.append(card(c)));
      return g;
    },
    verdict(b) {
      const box = h("div", "verdict-box");
      box.append(edH("h3", "", b, "title"));
      b.paras.forEach((_, i) => box.append(edH("p", "", b.paras, i)));
      return box;
    },
    zoom(b) {
      const f = h("div");
      f.innerHTML = viewerMarkup(b);
      zooms.push(b);
      return [...f.children];
    },
  };

  // ------------------------------------------------------------------ page
  const hero = h("section", "hero");
  hero.append(edH("h1", "", data, "title"), edH("p", "lead", data, "lead"));
  const rules = h("div", "rules"); rules.id = "rules";
  data.rules.forEach((r) => {
    const el = h("div", "rule");
    el.append(edH("b", "", r, "title"), edH("span", "", r, "html"));
    rules.append(el);
  });
  hero.append(rules);
  const tk = h("div", "takeaways"), ul = h("ul");
  tk.append(h("h2", "", "Takeaways so far"), ul);
  data.takeaways.forEach((_, i) => ul.append(edH("li", "", data.takeaways, i)));
  main.replaceChildren(hero, tk);
  toc.replaceChildren(Object.assign(h("a", "", "How we test"), { href: "#rules" }));

  for (const ch of data.chapters) {
    const sec = h("section", "bench"); sec.id = ch.id;
    const meta = h("div", "meta");
    ch.chips.forEach((c) => meta.append(edH("span", "chip" + (c.win ? " win" : ""), c, "text")));
    sec.append(edH("span", "chapter", ch, "label"), edH("h2", "", ch, "title"), meta);
    for (const b of ch.blocks) {
      const el = BLOCKS[b.type]?.(b);
      if (el) sec.append(...[el].flat());
    }
    main.append(sec);
    toc.append(Object.assign(h("a", "", ch.nav || ch.title.replace(/<[^>]+>/g, "")), { href: `#${ch.id}` }));
  }
  main.append(edH("footer", "", data, "footer"));
  if (location.hash) document.querySelector(location.hash)?.scrollIntoView();
  zooms.forEach((b) => initViewer(document.getElementById(b.id), b));

  // ------------------------------------------------------------------ edit mode
  const fab = document.getElementById("editFab"), toggleBtn = fab.querySelector("[data-a=toggle]"),
    saveBtn = fab.querySelector("[data-a=save]"), msg = fab.querySelector(".msg");
  let editing = false, dirty = false;
  const say = (t) => { msg.hidden = !t; msg.textContent = t || ""; };
  function setEditing(on) {
    editing = on;
    document.body.classList.toggle("editing", on);
    for (const el of binds.keys()) on ? el.setAttribute("contenteditable", "true") : el.removeAttribute("contenteditable");
    toggleBtn.textContent = on ? "Done" : "Edit";
    saveBtn.hidden = !on && !dirty;
    say(on ? "Click any text to change it, click a dot to set a score" : dirty ? "Unsaved changes" : "");
  }
  document.addEventListener("input", (e) => {
    const set = binds.get(e.target.closest("[data-ed]"));
    if (!editing || !set) return;
    set(e.target.closest("[data-ed]").innerHTML.trim());
    dirty = true;
  });
  document.addEventListener("click", (e) => {
    if (!editing) return;
    if (e.target.closest("main a")) e.preventDefault();
    const dot = e.target.closest(".dots i");
    if (!dot) return;
    const box = dot.parentElement, n = [...box.children].indexOf(dot) + 1;
    box._score.value = box._score.value === n ? n - 1 : n;   // the same dot again lowers by one
    [...box.children].forEach((d, i) => d.classList.toggle("on", i < box._score.value));
    dirty = true;
  }, true);
  async function save() {
    say("Saving…");
    try {
      const r = await fetch("/api/benchmarks", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) });
      if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
      dirty = false;
      saveBtn.hidden = !editing;
      say("Saved to data.json");
    } catch (err) {
      say(`Not saved (${err.message})`);
    }
  }
  toggleBtn.onclick = () => setEditing(!editing);
  saveBtn.onclick = save;
  addEventListener("keydown", (e) => { if ((e.metaKey || e.ctrlKey) && e.key === "s" && (editing || dirty)) { e.preventDefault(); save(); } });
  addEventListener("beforeunload", (e) => { if (dirty) e.preventDefault(); });
})();
