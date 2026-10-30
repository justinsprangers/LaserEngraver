// design.js: Design mode (the layout editor), the web version of the desktop's design_ui.py.
// The design is a plain object in the desktop's format (design.py). The page draws every layer
// from its own bitmap, so moving, the resize / rotate handles and clicking on items never wait
// for Python; the worker (engine.render_layers) re-renders only the layers whose content
// changed. Photo layers reuse the Photos-mode tabs, views and fix tools from app.js: the
// selected photo layer acts as app.js's `current` file. Uses app.js globals (S, current, view,
// call, el, $, canvas, ctx, controls, ...).
"use strict";

const HANDLE = 6;              // half-size of the corner handles, screen px
const ROT_OFFSET = 28;         // rotation handle distance above the box, screen px
const SNAP_PX = 6;             // snap edges / centers within this many screen px (Alt = off)
const ACCENT = "#2563eb";
const LINE_COLORS = { cut: "#ef4444", score: "#3b82f6" };
const CANVAS_PRESETS = { "4 × 4 in (coaster)": [4, 4], "6 × 4 in": [6, 4], "8 × 8 in": [8, 8],
  "8 × 10 in": [8, 10], "12 × 12 in": [12, 12] };
const ASPECT_LOCKED = ["image", "logo", "svg"];
const PHOTO_VIEWS = ["original", "cutout", "adjusted", "engraving", "look"];
const DESIGN_VIEWS = ["layout", "design"];
const OP_LABELS = { engrave: "Engrave", score: "Score", cut: "Cut" };
const PREVIEW_MAX_PX = 8e6;    // the Design view renders at a lower DPI above this many dots
const KEEP_SPARE = 40;         // rendered layers kept for undo / show-hide beyond the visible ones
const IMAGE_TYPES = /\.(png|jpe?g|bmp|webp|tiff?|gif)$/i;
const FMT_LABELS = { none: "None", svg: "SVG", dxf: "DXF", "svg+dxf": "SVG + DXF" };
const COMB_LABELS = { none: "None", svg: "SVG", pdf: "PDF", "svg+pdf": "SVG + PDF" };
const GUARD_PX = 100e6;        // above this many dots, warn that saving may run out of memory
const DETAIL_MAX_PX = 12e6;    // largest zoomed-in area rendered at full DPI
const MIME = { png: "image/png", svg: "image/svg+xml", dxf: "application/dxf", pdf: "application/pdf" };

// The design, its photos and added fonts, kept in this browser (IndexedDB) so they survive a
// reload. Everything is wrapped: with storage blocked the app works, it just doesn't remember.
const IDB = {
  ok: false, db: null,
  async open() {
    try {
      this.db = await new Promise((resolve, reject) => {
        const r = indexedDB.open("laser-engraver-design", 1);
        r.onupgradeneeded = () => { for (const s of ["kv", "assets", "fonts"]) r.result.createObjectStore(s); };
        r.onsuccess = () => resolve(r.result);
        r.onerror = () => reject(r.error);
      });
      this.ok = true;
    } catch { this.ok = false; }
  },
  req(store, mode, fn) {
    if (!this.ok) return Promise.resolve(undefined);
    return new Promise((resolve) => {
      try {
        const r = fn(this.db.transaction(store, mode).objectStore(store));
        r.onsuccess = () => resolve(r.result);
        r.onerror = () => resolve(undefined);
      } catch { resolve(undefined); }
    });
  },
  get(store, key) { return this.req(store, "readonly", (s) => s.get(key)); },
  put(store, key, v) { return this.req(store, "readwrite", (s) => s.put(v, key)); },
  del(store, key) { return this.req(store, "readwrite", (s) => s.delete(key)); },
  keys(store) { return this.req(store, "readonly", (s) => s.getAllKeys()).then((k) => k || []); },
  clear(store) { return this.req(store, "readwrite", (s) => s.clear()); },
};

const rad = (d) => d * Math.PI / 180;
const r4 = (v) => +(+v).toFixed(4);
const newId = () => [...crypto.getRandomValues(new Uint8Array(5))].map((b) => b.toString(16).padStart(2, "0")).join("");
const clone = (o) => JSON.parse(JSON.stringify(o));

Object.assign(DM, {
  active: false,
  info: null,                  // from engine.design_init: fonts, type labels, field lists
  design: null,
  selected: null,              // layer id
  undoStack: [], redoStack: [], lastSnap: null, commitTimer: null, dirty: false,
  cache: { layout: new Map(), design: new Map() },     // content key -> rendered layer
  placed: { layout: new Map(), design: new Map() },    // layer id -> rendered layer (last render)
  stale: { layout: true, design: true },
  inflight: false, pending: false,
  zoom: 50, panX: 0, panY: 0, needFit: true,           // zoom = screen (CSS) px per inch
  drag: null, pan: null, drawKind: null, handles: null,
  templates: {},
  photoS: null, photoCurrent: null, photoStale: true, sWidth: null, strokeSig: "",
  lookSig: "", info2: "",
  ver: 0, detail: null,        // ver counts design changes (a zoomed-in detail is for one version)
  projectName: "", projectHandle: null, saveTimer: null,

  // ================================================================ helpers

  shown() { return this.active && DESIGN_VIEWS.includes(view); },
  mode() { return view === "design" ? "design" : "layout"; },
  layer(id) { return this.design ? this.design.layers.find((l) => l.id === id) || null : null; },
  sel() { return this.layer(this.selected); },
  selImage() { const l = this.sel(); return l && l.type === "image" ? l : null; },
  look() { return (materials[this.design.material] || {}).look || ["#ffffff", "#000000"]; },
  label(l) {
    const kind = this.info.types[l.type];
    let detail;
    if (l.type === "text" || l.type === "arctext") detail = (l.text || "").split("\n")[0].slice(0, 24);
    else if (l.type === "shape") detail = { rect: "rectangle", ellipse: "ellipse", line: "line" }[l.shape || "rect"];
    else detail = l.name && l.name !== kind ? l.name : (l.src || "").split("/").pop() || "(no file)";
    const op = l.type !== "svg" && (l.op || "engrave") !== "engrave" ? OP_LABELS[l.op] : "";
    return `${kind}: ${detail}${op ? `  [${op}]` : ""}${l.visible === false ? "  (hidden)" : ""}`;
  },
  // Screen (CSS px in the canvas) <-> canvas inches.
  toScreen(x, y) { return [this.panX + x * this.zoom, this.panY + y * this.zoom]; },
  toInch(sx, sy) { return [(sx - this.panX) / this.zoom, (sy - this.panY) / this.zoom]; },
  corners(l) {                 // rotated box corners in inches (TL, TR, BR, BL), like design.corners_px
    const a = rad(l.rotation), ca = Math.cos(a), sa = Math.sin(a), hw = l.w_in / 2, hh = l.h_in / 2;
    return [[-hw, -hh], [hw, -hh], [hw, hh], [-hw, hh]].map(([x, y]) => [l.x_in + x * ca - y * sa, l.y_in + x * sa + y * ca]);
  },
  box(l) {
    const p = this.corners(l), xs = p.map((q) => q[0]), ys = p.map((q) => q[1]);
    return [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)];
  },
  // A rendered layer is drawn with the geometry it was rendered at; while a resize / rotate
  // waits for its new render, it's stretched and turned to the layer's current box.
  mapper(l, e) {
    const g = e.geom, sx = l.w_in / (g.w_in || 1e-6), sy = l.h_in / (g.h_in || 1e-6);
    const a0 = rad(-g.rotation), a1 = rad(l.rotation);
    const c0 = Math.cos(a0), s0 = Math.sin(a0), c1 = Math.cos(a1), s1 = Math.sin(a1);
    const fwd = (x, y) => {    // layer-relative inches (as rendered) -> canvas inches
      const ux = (x * c0 - y * s0) * sx, uy = (x * s0 + y * c0) * sy;
      return [l.x_in + ux * c1 - uy * s1, l.y_in + ux * s1 + uy * c1];
    };
    const inv = (X, Y) => {
      const dx = X - l.x_in, dy = Y - l.y_in;
      const ux = (dx * c1 + dy * s1) / (sx || 1e-6), uy = (-dx * s1 + dy * c1) / (sy || 1e-6);
      return [ux * c0 + uy * s0, -ux * s0 + uy * c0];
    };
    return { fwd, inv, sx, sy, a0, a1 };
  },

  // ================================================================ mode switch

  async enter() {
    if (this.active) return;
    if (window.tour) tour.end();         // (the tutorial is about Photos mode)
    showProgress("Loading the design tools…", null);
    try {
      this.info = await call("designInit");
      if (!this.design) {
        await IDB.open();
        if (!(await this.restoreSaved())) {
          this.design = await call("newDesign", { material: S.material, width_in: 6, height_in: 4, dpi: +S.dpi || 254 });
          this.lastSnap = JSON.stringify(this.design);
        }
      }
      for (const k of ["shape", "text", "arctext"])
        this.templates[k] ||= await call("newLayer", { kind: k, design: this.design });
    } catch (e) {
      fail(`Couldn't start Design mode: ${e.message}`);
      setModeButtons("photos");
      return;
    }
    doneProgress();
    this.active = true;
    LS.set("mode", "design");
    setModeButtons("design");
    document.body.classList.add("design-mode");
    this.photoS = structuredClone(S);
    this.photoCurrent = current;
    current = null;
    views = null;
    viewCache = {};
    $("#empty").hidden = true;
    $("#info").textContent = "";
    for (const t of ["design", "layer"]) document.querySelector(`.tab[data-tab="${t}"]`).hidden = false;
    this.buildDesignTab();
    const top = this.design.layers[this.design.layers.length - 1];
    this.select(top ? top.id : null, false);
    this.needFit = true;
    const v = LS.get("dview", "layout");
    setView(DESIGN_VIEWS.includes(v) || current ? v : "layout");
    this.showTab(LS.get("dtab", this.design.layers.length ? "layer" : "design"));
    this.updateTitle();
    this.requestRender();
    setStatus("Design mode: add photos, logos, text and shapes with Add (left).");
  },

  leave() {
    if (!this.active) return;
    if (this.drawKind) this.cancelDraw();
    this.commitNow();
    this.active = false;
    this.updateTitle();
    LS.set("mode", "photos");
    setModeButtons("photos");
    document.body.classList.remove("design-mode");
    canvas.style.cursor = "";
    for (const t of ["design", "layer"]) document.querySelector(`.tab[data-tab="${t}"]`).hidden = true;
    this.showPhotoTabs(true);
    $("#views").hidden = false;
    S = this.photoS || S;
    refreshControls();
    current = null;
    views = null;
    const f = this.photoCurrent && files.includes(this.photoCurrent) ? this.photoCurrent : files[0];
    renderFileList();
    setView(LS.get("view", "engraving"));
    if (f) selectFile(f); else { $("#info").textContent = ""; draw(); }
    const tab = document.querySelector(`.tab[data-tab="${LS.get("tab", "basic")}"]`);
    if (tab) tab.click();
    setStatus("Photos mode");
  },

  showPhotoTabs(on) {
    for (const t of ["basic", "photo", "comic"]) document.querySelector(`.tab[data-tab="${t}"]`).hidden = !on;
    const active = document.querySelector(".tab.active");
    if (!on && active && ["basic", "photo", "comic"].includes(active.dataset.tab)) this.showTab("layer");
  },
  showTab(name) {
    const t = document.querySelector(`.tab[data-tab="${name}"]`);
    (t && !t.hidden ? t : document.querySelector('.tab[data-tab="layer"]')).click();
  },

  // ================================================================ selection

  select(id, render = true) {
    this.selected = id;
    const l = this.sel();
    if (l && l.type === "image") {
      Object.assign(S, l.settings, this.globals(), { width_in: l.w_in, size_by: "width" }, blankCrop());
      this.sWidth = l.w_in;
      const lid = l.id, dm = this;
      current = {
        id: l.src, name: l.name || "photo", crop: blankCrop(), layerId: lid,
        get strokes() { const x = dm.layer(lid); return x ? (x.strokes ||= []) : []; },
        set strokes(v) { const x = dm.layer(lid); if (x) x.strokes = v; },
      };
      this.strokeSig = JSON.stringify(l.strokes || []);
    } else {
      Object.assign(S, this.globals());
      current = null;
    }
    views = null;
    viewCache = {};
    needFit = true;
    this.photoStale = true;
    refreshControls();
    refreshFixbar();
    this.showPhotoTabs(!!current);
    $("#views").hidden = !current;
    if (!current && PHOTO_VIEWS.includes(view)) setView("layout");
    else if (current && PHOTO_VIEWS.includes(view) && render) scheduleRender(0);
    this.buildLayerTab();
    this.refreshList();
    if (this.shown()) draw();
  },

  globals() { return Object.fromEntries(this.info.global_fields.filter((f) => f in this.design).map((f) => [f, this.design[f]])); },

  // S (the Basic / Photo / Comic controls) -> the design and the selected photo layer. Called
  // by app.js's scheduleRender after every settings change.
  settingsChanged() {
    const d = this.design;
    let changed = false;
    for (const f of this.info.global_fields) {
      if (d[f] !== S[f]) { d[f] = S[f]; changed = true; }
    }
    const l = this.selImage();
    if (l) {
      for (const f of this.info.layer_fields) {
        if (l.settings[f] !== S[f]) { l.settings[f] = S[f]; changed = true; }
      }
      if (S.width_in !== this.sWidth && Math.abs(S.width_in - l.w_in) > 1e-4) {
        l.h_in = S.width_in * l.h_in / l.w_in;
        l.w_in = S.width_in;
        changed = true;
      }
      this.sWidth = S.width_in;
      const sig = JSON.stringify(l.strokes || []);
      if (sig !== this.strokeSig) { this.strokeSig = sig; changed = true; }
    }
    if (changed) {
      this.refreshFields();
      this.changed();
    }
  },

  // ================================================================ changes / undo

  changed() {
    this.dirty = true;
    this.ver++;
    this.updateTitle();
    this.updateTestSize();
    this.requestRender();
    clearTimeout(this.commitTimer);
    this.commitTimer = setTimeout(() => this.commit(), 500);
  },
  commit() {                   // records an undo step (edits within half a second are one step)
    this.commitTimer = null;
    const now = JSON.stringify(this.design);
    if (this.lastSnap !== null && this.lastSnap !== now) {
      this.undoStack.push(this.lastSnap);
      this.undoStack.splice(0, this.undoStack.length - 50);
      this.redoStack = [];
    }
    this.lastSnap = now;
    this.autosave();
  },
  commitNow() { if (this.commitTimer) { clearTimeout(this.commitTimer); this.commit(); } },
  undo() {
    this.commitNow();
    if (!this.undoStack.length) return setStatus("Nothing to undo");
    this.redoStack.push(JSON.stringify(this.design));
    this.restore(this.undoStack.pop());
  },
  redo() {
    if (!this.redoStack.length) return setStatus("Nothing to redo");
    this.undoStack.push(JSON.stringify(this.design));
    this.restore(this.redoStack.pop());
  },
  restore(snap) {
    this.design = JSON.parse(snap);
    this.lastSnap = snap;
    this.dirty = true;
    this.ver++;
    this.autosave();
    this.updateTitle();
    this.refreshDesignTab();
    this.select(this.layer(this.selected) ? this.selected : null);
    this.requestRender();
  },

  // ================================================================ add / remove / order

  add(l) {
    this.design.layers.push(l);
    this.select(l.id);
    this.showTab("layer");
    this.changed();
  },
  async addFiles(list, kind = "image") {
    const lep = [...list].find((f) => /\.lep$/i.test(f.name));
    if (lep) return this.confirmDiscard("Open this project?") && this.openProjectFile(lep);
    const imgs = [...list].filter((f) => f.type.startsWith("image/") || IMAGE_TYPES.test(f.name));
    if (!imgs.length) return;
    const c = this.design.canvas;
    for (const [i, file] of imgs.entries()) {
      showProgress(`Adding ${file.name} (${i + 1} of ${imgs.length})…`, null);
      try {
        const bytes = new Uint8Array(await file.arrayBuffer());
        const keep = IDB.ok ? bytes.slice() : null;                // (the original goes to the worker)
        const a = await call("addAsset", { name: file.name, bytes }, [bytes.buffer]);
        if (!a.w) throw new Error("it doesn't look like an image");
        if (keep) await IDB.put("assets", a.path, { name: file.name, bytes: keep });
        const w = +(Math.min(c.width_in, c.height_in) * (kind === "image" ? 0.6 : 0.35)).toFixed(2);
        const l = await call("newLayer", { kind, design: this.design, kw: { src: a.path, name: file.name, w_in: w } });
        if (kind === "image") {
          for (const f of this.info.layer_fields)
            if (!["contrast", "brightness", "gamma"].includes(f) && f in S) l.settings[f] = S[f];
        }
        this.add(l);
      } catch (e) {
        fail(`Couldn't add ${file.name}: ${e.message}`);
        return;
      }
    }
    doneProgress();
    setStatus(`Added ${imgs.length === 1 ? imgs[0].name : `${imgs.length} images`}`);
  },
  addSimple(kind) {
    const l = Object.assign(clone(this.templates[kind]), { id: newId() });
    const c = this.design.canvas;
    l.x_in = c.width_in / 2;
    l.y_in = c.height_in / 2;
    if (kind === "arctext") l.radius_in = Math.min(c.width_in, c.height_in) * 0.35;
    this.add(l);
  },
  deleteLayer() {
    const l = this.sel();
    if (!l) return;
    const layers = this.design.layers, i = layers.indexOf(l);
    layers.splice(i, 1);
    this.select(layers.length ? layers[Math.min(i, layers.length - 1)].id : null);
    this.changed();
  },
  duplicate() {
    const l = this.sel();
    if (!l) return;
    const dup = Object.assign(clone(l), { id: newId(), x_in: l.x_in + 0.15, y_in: l.y_in + 0.15 });
    const layers = this.design.layers;
    layers.splice(layers.indexOf(l) + 1, 0, dup);
    this.select(dup.id);
    this.changed();
  },
  move(step) {
    const l = this.sel();
    if (!l) return;
    const layers = this.design.layers, i = layers.indexOf(l);
    const j = Math.min(layers.length - 1, Math.max(0, i + step));
    if (i === j) return;
    layers.splice(j, 0, layers.splice(i, 1)[0]);
    this.refreshList();
    this.changed();
  },
  toggleVisible() {
    const l = this.sel();
    if (!l) return;
    l.visible = l.visible === false;
    this.refreshList();
    this.changed();
  },
  nudge(dx, dy, big) {
    const l = this.sel();
    if (!l) return;
    const step = big ? 0.1 : 0.01;
    l.x_in = r4(l.x_in + dx * step);
    l.y_in = r4(l.y_in + dy * step);
    this.refreshFields();
    draw();
    this.changed();
  },
  async newDesign(force = false) {
    if (!force && !this.confirmDiscard("Start a new, empty design?")) return;
    const d = this.design;
    this.load(await call("newDesign", { material: d.material, width_in: d.canvas.width_in, height_in: d.canvas.height_in, dpi: d.dpi }), "");
    this.showTab("design");
  },
  confirmDiscard(question) {
    return !(this.dirty && this.design.layers.length) ||
      confirm(`${question} The current design hasn't been saved as a project file (Save project).`);
  },
  // A different design on screen (new, opened, restored): fresh undo history.
  load(design, name, dirty = false) {
    this.design = design;
    this.projectName = name;
    this.projectHandle = null;
    this.undoStack = [];
    this.redoStack = [];
    this.lastSnap = JSON.stringify(design);
    this.dirty = dirty;
    this.ver++;
    this.detail = null;
    this.refreshDesignTab();
    const top = design.layers[design.layers.length - 1];
    this.select(top ? top.id : null);
    this.needFit = true;
    if (this.shown()) this.fit();
    this.requestRender();
    this.updateTitle();
    this.autosave();
  },
  updateTitle() {
    document.title = this.active ? `${this.projectName || "Untitled design"}${this.dirty ? " *" : ""} · Laser Engraver`
      : "Laser Engraver";
  },
  setDesignOpt(key, value) {
    if (this.design[key] !== value) { this.design[key] = value; this.changed(); }
  },
  testFactor() { return (this.design.test_pct ?? 25) / 100; },
  updateTestSize() {
    const f = this.testFactor(), c = this.design.canvas, t = $("#testSize");
    if (t) t.textContent = `→ ${(c.width_in * f).toFixed(2)} × ${(c.height_in * f).toFixed(2)} in`;
  },

  // ================================================================ saving files

  // The engraving PNG, a cut / score file per job and the all-in-one file(s), as downloads.
  // Same files and names as the desktop's Save engraving (factor < 1 = the test copy).
  async exportDesign(factor = 1) {
    const d = this.design;
    if (!d.layers.length) return alert("Add something to the design first (Add).");
    const dots = d.canvas.width_in * d.canvas.height_in * (d.dpi * factor) ** 2;
    if (dots > GUARD_PX && !confirm(`This engraving is ${Math.round(dots / 1e6)} million dots. The browser may run out ` +
      "of memory and reload the page (your design is kept). A lower DPI or a smaller canvas is safer. Continue?")) return;
    const g = (v) => String(+v.toFixed(2));
    const name = `${this.projectName || "design"}_${d.material}_${g(d.canvas.width_in * factor)}x` +
      `${g(d.canvas.height_in * factor)}in_${d.dpi}dpi`;
    showProgress(factor !== 1 ? "Saving the test copy…" : "Saving the engraving…", null);
    try {
      const files = await call("exportDesign", { design: d, name, factor });
      doneProgress();
      if (!files.length) return setStatus("Nothing to save: everything is hidden or empty.");
      for (const [i, f] of files.entries()) {
        if (i) await new Promise((r) => setTimeout(r, 300));      // browsers drop downloads fired too fast
        saveBlob(new Blob([f.bytes], { type: MIME[f.name.split(".").pop()] || "application/octet-stream" }), f.name);
      }
      setStatus(`Saved ${files.map((f) => f.name).join(", ")}` +
        (files.length > 1 ? " (your browser may ask to allow multiple downloads)" : ""));
    } catch (e) { fail(`Saving failed: ${e.message}`); }
  },

  // Projects (.lep, the desktop's format). Where the browser allows it (Chrome, Edge), Save
  // writes back to the file you opened or saved; elsewhere every save is a download.
  async saveProject(asNew = false) {
    try {
      showProgress("Saving the project…", null);
      const bytes = await call("saveProject", { design: this.design });
      doneProgress();
      const name = (this.projectName || "design") + ".lep";
      if (window.showSaveFilePicker) {
        if (asNew || !this.projectHandle) {
          this.projectHandle = await showSaveFilePicker({ suggestedName: name,
            types: [{ description: "Laser Engraver project", accept: { "application/octet-stream": [".lep"] } }] });
        }
        const w = await this.projectHandle.createWritable();
        await w.write(bytes);
        await w.close();
        this.projectName = this.projectHandle.name.replace(/\.lep$/i, "");
      } else {
        if (asNew || !this.projectName) {
          const n = prompt("Project name:", this.projectName || "design");
          if (!n) return;
          this.projectName = n.replace(/\.lep$/i, "").replace(/[\\/:*?"<>|]/g, "_");
        }
        saveBlob(new Blob([bytes], { type: "application/octet-stream" }), this.projectName + ".lep");
      }
      this.dirty = false;
      this.updateTitle();
      this.autosave();
      setStatus(`Saved project ${this.projectName}.lep`);
    } catch (e) {
      doneProgress();
      if (e.name !== "AbortError") fail(`Couldn't save the project: ${e.message}`);
    }
  },
  async openProject() {
    if (!this.confirmDiscard("Open another project?")) return;
    if (!window.showOpenFilePicker) return $("#lepInput").click();      // -> openProjectFile
    try {
      const [h] = await showOpenFilePicker({ types: [{ description: "Laser Engraver project",
        accept: { "application/octet-stream": [".lep"] } }] });
      await this.openProjectFile(await h.getFile(), h);
    } catch (e) { if (e.name !== "AbortError") fail(`Couldn't open the project: ${e.message}`); }
  },
  async openProjectFile(file, handle = null) {
    showProgress(`Opening ${file.name}…`, null);
    try {
      const bytes = new Uint8Array(await file.arrayBuffer());
      const d = await call("openProject", { bytes }, [bytes.buffer]);
      this.load(d, file.name.replace(/\.lep$/i, ""));
      this.projectHandle = handle;
      this.showTab(d.layers.length ? "layer" : "design");
      doneProgress();
      setStatus(`Opened ${file.name}`);
      // Keep the project's photos in this browser too, for the saved copy.
      const have = new Set(await IDB.keys("assets"));
      for (const l of d.layers) {
        if (IDB.ok && l.src && !have.has(l.src)) {
          have.add(l.src);
          await IDB.put("assets", l.src, { name: l.name || l.src.split("/").pop(), bytes: await call("readAsset", { path: l.src }) });
        }
      }
    } catch (e) { fail(`Couldn't open ${file.name}: ${e.message}`); }
  },

  // ================================================================ kept in this browser

  autosave() {
    clearTimeout(this.saveTimer);
    if (!IDB.ok || !this.design) return;
    this.saveTimer = setTimeout(() => IDB.put("kv", "design",
      { design: this.design, name: this.projectName, dirty: this.dirty }), 800);
  },
  async restoreSaved() {
    const saved = await IDB.get("kv", "design");
    if (!saved || !saved.design) return false;
    showProgress("Opening your saved design…", null);
    for (const k of await IDB.keys("fonts")) {
      const f = await IDB.get("fonts", k);
      try { this.info.fonts = (await call("addFont", { name: f.name, bytes: f.bytes })).fonts; } catch { /* skip it */ }
    }
    const need = new Set(saved.design.layers.map((l) => l.src).filter(Boolean));
    for (const path of need) {
      const a = await IDB.get("assets", path);
      if (a) { try { await call("addAsset", { name: a.name, bytes: a.bytes }); } catch { /* layer shows nothing */ } }
    }
    for (const k of await IDB.keys("assets")) if (!need.has(k)) IDB.del("assets", k);   // (no longer used)
    this.design = saved.design;
    this.projectName = saved.name || "";
    this.dirty = !!saved.dirty;
    this.lastSnap = JSON.stringify(this.design);
    return true;
  },
  async clearSaved() {
    if (!confirm("Remove the design kept in this browser (with its photos and added fonts) and start a new, " +
      "empty design? Project files you saved aren't affected.")) return;
    for (const s of ["kv", "assets", "fonts"]) await IDB.clear(s);
    await this.newDesign(true);
    setStatus("Cleared the design kept in this browser");
  },

  // ================================================================ zoomed-in detail

  // The Design view of a big canvas is rendered at a lower DPI (previewDpi). Zoomed in past
  // that, the visible part is rendered again at full DPI and drawn on top.
  wantDetail() {
    if (view !== "design" || !this.previewDpi()) return;
    const dpr = window.devicePixelRatio || 1, d = this.design;
    if (this.zoom * dpr <= this.previewDpi() * 1.15) return;
    const [ax, ay] = this.toInch(0, 0), [bx, by] = this.toInch(canvas.clientWidth, canvas.clientHeight);
    const box = [Math.max(0, ax), Math.max(0, ay), Math.min(d.canvas.width_in, bx), Math.min(d.canvas.height_in, by)];
    if (box[2] <= box[0] || box[3] <= box[1] || (box[2] - box[0]) * (box[3] - box[1]) * d.dpi ** 2 > DETAIL_MAX_PX) return;
    const t = this.detail;
    if (t && t.ver === this.ver && t.box[0] <= box[0] && t.box[1] <= box[1] && t.box[2] >= box[2] && t.box[3] >= box[3]) return;
    clearTimeout(this.detailTimer);
    this.detailTimer = setTimeout(() => this.requestDetail(box), 250);
  },
  async requestDetail(box) {
    if (this.detailBusy) { this.detailAgain = true; return; }
    this.detailBusy = true;
    const ver = this.ver;
    try {
      const r = await call("renderRegion", { design: this.design, box });
      if (ver === this.ver && r.w) {
        const [blank, ink] = this.look().map(hexRGB), px = new Uint8ClampedArray(r.w * r.h * 4);
        for (let i = 0; i < r.w * r.h; i++) {
          const c = r.burn[i] ? ink : blank;
          px[i * 4] = c[0]; px[i * 4 + 1] = c[1]; px[i * 4 + 2] = c[2]; px[i * 4 + 3] = 255;
        }
        const dpi = this.design.dpi;
        this.detail = { ver, box: [r.x / dpi, r.y / dpi, (r.x + r.w) / dpi, (r.y + r.h) / dpi], dpi, x: r.x, y: r.y,
          w: r.w, h: r.h, bmp: await createImageBitmap(new ImageData(px, r.w, r.h)) };
        draw();
      }
    } catch { /* keep the lower-DPI view */ } finally {
      doneProgress();
      this.detailBusy = false;
      if (this.detailAgain) { this.detailAgain = false; this.wantDetail(); }
    }
  },

  // ================================================================ drawing shapes

  startDraw(shape) {
    this.drawKind = shape;
    if (!this.shown()) setView("layout");
    canvas.style.cursor = "crosshair";
    const name = { rect: "rectangle", ellipse: "circle / ellipse", line: "line" }[shape];
    const square = shape === "line" ? "15° steps" : shape === "ellipse" ? "circle" : "square";
    setStatus(`Drag on the canvas to draw the ${name}: Ctrl = ${square}, Shift = from the center, ` +
      "Alt = no snapping, Esc = cancel. A click makes a 1-inch one.");
  },
  cancelDraw() {
    if (this.drag && this.drag.mode === "draw") {
      const l = this.layer(this.drag.id);
      if (l) this.design.layers.splice(this.design.layers.indexOf(l), 1);
      this.drag = null;
      this.select(null);
    }
    this.drawKind = null;
    canvas.style.cursor = "";
    setStatus("");
    draw();
  },
  point(sx, sy, l, guides, e) {  // mouse in canvas inches, snapped (Alt = off)
    let [x, y] = this.toInch(sx, sy);
    if (!e.altKey) {
      const [xs, ys] = this.snapTargets(l), tol = SNAP_PX / this.zoom;
      const bx = xs.reduce((a, b) => (Math.abs(b - x) < Math.abs(a - x) ? b : a));
      const by = ys.reduce((a, b) => (Math.abs(b - y) < Math.abs(a - y) ? b : a));
      if (Math.abs(bx - x) < tol) { x = bx; guides.push(["x", bx]); }
      if (Math.abs(by - y) < tol) { y = by; guides.push(["y", by]); }
    }
    return [x, y];
  },
  drawPress(sx, sy, e) {
    const kind = this.drawKind, guides = [];
    const l = Object.assign(clone(this.templates.shape), { id: newId(), shape: kind, w_in: 0.01,
      h_in: kind === "line" ? 0.05 : 0.01, rotation: 0 });
    const [x, y] = this.point(sx, sy, l, guides, e);
    Object.assign(l, { x_in: x, y_in: y });
    this.design.layers.push(l);
    this.selected = l.id;
    this.drag = { id: l.id, mode: "draw", p0: [x, y], start: clone(l), snap: guides };
  },
  drawUpdate(l, sx, sy, e) {
    const guides = [];
    let [x0, y0] = this.drag.p0;
    const [x1, y1] = this.point(sx, sy, l, guides, e);
    let dx = x1 - x0, dy = y1 - y0;
    const ctrl = e.ctrlKey || e.metaKey, shift = e.shiftKey;
    if (l.shape === "line") {
      if (ctrl) {
        const a = Math.round(Math.atan2(dy, dx) / rad(15)) * rad(15), n = Math.hypot(dx, dy);
        dx = n * Math.cos(a); dy = n * Math.sin(a);
      }
      if (shift) { x0 -= dx; y0 -= dy; dx *= 2; dy *= 2; }
      Object.assign(l, { x_in: x0 + dx / 2, y_in: y0 + dy / 2, w_in: Math.max(Math.hypot(dx, dy), 0.01),
        rotation: +(((Math.atan2(dy, dx) * 180 / Math.PI) % 360 + 360) % 360).toFixed(2) });
    } else {
      if (ctrl) {
        const side = Math.max(Math.abs(dx), Math.abs(dy));
        dx = Math.sign(dx || 1) * side; dy = Math.sign(dy || 1) * side;
      }
      if (shift) Object.assign(l, { x_in: x0, y_in: y0, w_in: Math.max(2 * Math.abs(dx), 0.01), h_in: Math.max(2 * Math.abs(dy), 0.01) });
      else Object.assign(l, { x_in: x0 + dx / 2, y_in: y0 + dy / 2, w_in: Math.max(Math.abs(dx), 0.01), h_in: Math.max(Math.abs(dy), 0.01) });
    }
    this.drag.snap = guides;
  },
  drawRelease(l) {
    const [x0, y0] = this.drag.p0;
    if (Math.max(l.w_in, l.shape === "line" ? 0 : l.h_in) < 0.05) {
      Object.assign(l, { x_in: x0, y_in: y0, w_in: 1, rotation: 0 });
      if (l.shape !== "line") l.h_in = 1;
    }
    for (const k of ["x_in", "y_in", "w_in", "h_in"]) l[k] = r4(l[k]);
    this.drag = null;
    this.drawKind = null;
    canvas.style.cursor = "";
    setStatus("");
    this.select(l.id);
    this.showTab("layer");
    this.changed();
  },

  // ================================================================ rendering

  requestRender() {
    this.stale.layout = this.stale.design = true;
    if (this.shown()) this.kick();
  },
  previewDpi() {
    const d = this.design, area = d.canvas.width_in * d.canvas.height_in;
    return area * d.dpi * d.dpi > PREVIEW_MAX_PX ? Math.floor(Math.sqrt(PREVIEW_MAX_PX / area)) : 0;
  },
  // One render at a time; changes made meanwhile are sent together when it finishes.
  kick() {
    if (this.inflight) { this.pending = true; return; }
    const mode = this.mode(), d = this.design;
    this.inflight = true;
    this.pending = false;
    const geo = Object.fromEntries(d.layers.map((l) => [l.id, l.rotation]));
    const t0 = performance.now();
    call("renderLayers", { design: d, mode, previewDpi: mode === "design" ? this.previewDpi() : 0,
      known: [...this.cache[mode].keys()] })
      .then((r) => this.apply(mode, r, geo, performance.now() - t0))
      .catch((e) => {
        $("#info").textContent = `Couldn't render the design: ${e.message}`;
        $("#info").className = "error";
        if (!this.pending) this.stale[mode] = false;           // (don't retry the same design)
      })
      .finally(() => {
        this.inflight = false;
        doneProgress();
        if (this.pending && this.active) { this.pending = false; this.stale[this.mode()] = true; }
        if (this.active && this.shown() && this.stale[this.mode()]) this.kick();
      });
  },
  async apply(mode, r, geo, ms) {
    const cache = this.cache[mode], look = this.look(), sig = look.join();
    if (mode === "design" && sig !== this.lookSig) await this.recolor(sig);
    await Promise.all(r.layers.filter((it) => !cache.has(it.key) && (it.rgba || it.vecs)).map(async (it) => {
      const e = { key: it.key, dpi: r.dpi, raster: it.raster, vecs: it.vecs || {}, w: it.w, h: it.h, rgba: it.rgba,
        geom: { w_in: it.w_in, h_in: it.h_in, rotation: geo[it.id] || 0 } };
      if (it.rgba) e.bmp = await this.bitmap(mode, e);
      cache.set(it.key, e);
    }));
    const placed = new Map();
    for (const it of r.layers) {
      const e = cache.get(it.key);
      if (e) { cache.delete(it.key); cache.set(it.key, e); placed.set(it.id, e); }     // (most recent last)
      const l = this.layer(it.id);
      if (l && (l.type === "text" || l.type === "arctext") && !(this.drag && this.drag.id === l.id && this.drag.mode === "scale")) {
        l.w_in = it.w_in;              // text size comes from the font
        l.h_in = it.h_in;
      }
    }
    this.placed[mode] = placed;
    const used = new Set([...placed.values()].map((e) => e.key));
    for (const k of cache.keys()) {
      if (cache.size <= used.size + KEEP_SPARE) break;
      if (!used.has(k)) cache.delete(k);
    }
    if (!this.pending) this.stale[mode] = false;
    this.info2 = `${(ms / 1000).toFixed(2)} s`;
    this.refreshFields();
    this.updateInfo();
    if (this.shown()) draw();
  },
  // Layout: gray as rendered. Design: ink / blank in the material's colors (the Material look).
  async bitmap(mode, e) {
    let px = e.rgba;
    if (mode === "design") {
      const [blank, ink] = this.look().map(hexRGB);
      px = new Uint8Array(e.rgba);
      for (let i = 0; i < px.length; i += 4) {
        const c = px[i] < 128 ? ink : blank;
        px[i] = c[0]; px[i + 1] = c[1]; px[i + 2] = c[2];
      }
    }
    return createImageBitmap(new ImageData(new Uint8ClampedArray(px.buffer, px.byteOffset, px.length), e.w, e.h));
  },
  async recolor(sig) {
    this.lookSig = sig;
    await Promise.all([...this.cache.design.values()].filter((e) => e.rgba).map(async (e) => { e.bmp = await this.bitmap("design", e); }));
  },
  updateInfo() {
    if (!this.shown()) return;
    const d = this.design, n = d.layers.length;
    let t = `Canvas ${d.canvas.width_in} × ${d.canvas.height_in} in @ ${d.dpi} DPI  ·  ${n} layer${n === 1 ? "" : "s"}`;
    if (view === "design") {
      const p = this.previewDpi();
      if (p) t += `  ·  shown at ${p} DPI (big canvas; the saved engraving is full ${d.dpi} DPI)`;
      if (d.mirror) t += "  ·  mirrored on export";
    } else t += "  ·  Layout view: photos shown unprocessed (the Design view shows the engraving)";
    const dots = d.canvas.width_in * d.canvas.height_in * d.dpi ** 2, big = dots > GUARD_PX;
    if (big) t = `Very large: ${Math.round(dots / 1e6)} million dots, saving may run out of memory (lower the DPI ` +
      `or the canvas size)  ·  ${t}`;
    $("#info").textContent = t;
    $("#info").className = big ? "warn" : "";
    $("#designEmpty").hidden = n > 0;
  },
  onShown() {                  // a design view was switched to
    canvas.classList.remove("drawing");
    this.updateInfo();
    if (view === "design" && this.look().join() !== this.lookSig) this.recolor(this.look().join()).then(draw);
    if (this.needFit) this.fit(); else draw();
    if (this.stale[this.mode()]) this.kick();
  },

  // ================================================================ drawing the canvas

  draw() {
    const dpr = window.devicePixelRatio || 1;
    const W = canvas.clientWidth, H = canvas.clientHeight;
    if (canvas.width !== Math.round(W * dpr) || canvas.height !== Math.round(H * dpr)) {
      canvas.width = Math.round(W * dpr);
      canvas.height = Math.round(H * dpr);
    }
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    const d = this.design;
    if (!d) return;
    const mode = this.mode(), placed = this.placed[mode], z = this.zoom, k = dpr * z;
    const cw = d.canvas.width_in, ch = d.canvas.height_in;
    ctx.setTransform(k, 0, 0, k, dpr * this.panX, dpr * this.panY);
    ctx.fillStyle = mode === "design" ? this.look()[0] : "#ffffff";
    ctx.fillRect(0, 0, cw, ch);
    ctx.save();
    ctx.beginPath();
    ctx.rect(0, 0, cw, ch);
    ctx.clip();
    for (const l of d.layers) {
      const e = l.visible !== false && placed.get(l.id);
      if (!e || !e.bmp) continue;
      const m = this.mapper(l, e);
      ctx.imageSmoothingEnabled = !(mode === "design" && k / e.dpi >= 1);
      ctx.imageSmoothingQuality = "high";
      if (Math.abs(m.sx - 1) < 1e-6 && Math.abs(m.sy - 1) < 1e-6 && l.rotation === e.geom.rotation) {
        // On whole dots, like design.Renderer.compose, so the Design view matches the export.
        const x0 = Math.round(l.x_in * e.dpi - e.w / 2) / e.dpi, y0 = Math.round(l.y_in * e.dpi - e.h / 2) / e.dpi;
        ctx.drawImage(e.bmp, x0, y0, e.w / e.dpi, e.h / e.dpi);
        continue;
      }
      ctx.save();
      ctx.translate(l.x_in, l.y_in);
      ctx.rotate(m.a1);
      ctx.scale(m.sx, m.sy);
      ctx.rotate(m.a0);
      ctx.drawImage(e.bmp, -e.w / 2 / e.dpi, -e.h / 2 / e.dpi, e.w / e.dpi, e.h / e.dpi);
      ctx.restore();
    }
    const t = this.detail;
    if (mode === "design" && t && t.ver === this.ver && !this.drag) {
      ctx.imageSmoothingEnabled = k / t.dpi < 1;
      ctx.drawImage(t.bmp, t.x / t.dpi, t.y / t.dpi, t.w / t.dpi, t.h / t.dpi);
    }
    ctx.restore();
    if (mode === "design") this.wantDetail();
    // Score / cut lines (blue / red), in screen pixels so they stay thin at any zoom.
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.lineWidth = 1.5;
    ctx.lineJoin = "round";
    for (const l of d.layers) {
      const e = l.visible !== false && placed.get(l.id);
      if (!e) continue;
      const m = this.mapper(l, e);
      for (const [kind, paths] of Object.entries(e.vecs)) {
        ctx.strokeStyle = LINE_COLORS[kind] || "#ef4444";
        ctx.beginPath();
        for (const [pts, closed] of paths) {
          pts.forEach(([x, y], i) => {
            const [sx, sy] = this.toScreen(...m.fwd(x, y));
            i ? ctx.lineTo(sx, sy) : ctx.moveTo(sx, sy);
          });
          if (closed) ctx.closePath();
        }
        ctx.stroke();
      }
    }
    // Canvas edge, snap guides, selection handles.
    const [ox, oy] = this.toScreen(0, 0);
    ctx.setLineDash([4, 3]);
    ctx.strokeStyle = "#94a3b8";
    ctx.lineWidth = 1;
    ctx.strokeRect(ox + 0.5, oy + 0.5, cw * z, ch * z);
    if (this.drag && this.drag.snap) {
      ctx.setLineDash([2, 2]);
      ctx.strokeStyle = "#ec4899";
      ctx.beginPath();
      for (const [axis, v] of this.drag.snap) {
        if (axis === "x") { ctx.moveTo(ox + v * z, oy); ctx.lineTo(ox + v * z, oy + ch * z); }
        else { ctx.moveTo(ox, oy + v * z); ctx.lineTo(ox + cw * z, oy + v * z); }
      }
      ctx.stroke();
    }
    ctx.setLineDash([]);
    this.handles = null;
    const l = this.sel();
    if (!l) return;
    const pts = this.corners(l).map(([x, y]) => this.toScreen(x, y));
    ctx.strokeStyle = ACCENT;
    ctx.lineWidth = 2;
    ctx.beginPath();
    pts.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
    ctx.closePath();
    ctx.stroke();
    const tm = [(pts[0][0] + pts[1][0]) / 2, (pts[0][1] + pts[1][1]) / 2];
    const c = [pts.reduce((a, p) => a + p[0], 0) / 4, pts.reduce((a, p) => a + p[1], 0) / 4];
    const n = Math.hypot(tm[0] - c[0], tm[1] - c[1]) || 1;
    const rot = [tm[0] + (tm[0] - c[0]) / n * ROT_OFFSET, tm[1] + (tm[1] - c[1]) / n * ROT_OFFSET];
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(...tm);
    ctx.lineTo(...rot);
    ctx.stroke();
    ctx.fillStyle = "#ffffff";
    for (const [x, y] of pts) {
      ctx.fillRect(x - HANDLE, y - HANDLE, 2 * HANDLE, 2 * HANDLE);
      ctx.strokeRect(x - HANDLE + 0.5, y - HANDLE + 0.5, 2 * HANDLE - 1, 2 * HANDLE - 1);
    }
    ctx.beginPath();
    ctx.arc(rot[0], rot[1], HANDLE, 0, 7);
    ctx.fill();
    ctx.stroke();
    this.handles = { corners: pts, rot, center: c };
  },

  fit() {
    const W = canvas.clientWidth, H = canvas.clientHeight, c = this.design && this.design.canvas;
    if (!c || !W || !H) { this.needFit = true; return; }
    this.needFit = false;
    this.zoom = Math.min(W / c.width_in, H / c.height_in) * 0.94;
    this.panX = (W - c.width_in * this.zoom) / 2;
    this.panY = (H - c.height_in * this.zoom) / 2;
    draw();
  },
  zoomAt(sx, sy, f) {
    const nz = Math.min(20000, Math.max(2, this.zoom * f));
    this.panX = sx - (sx - this.panX) * (nz / this.zoom);
    this.panY = sy - (sy - this.panY) * (nz / this.zoom);
    this.zoom = nz;
    draw();
  },
  zoom100() {                  // one laser dot = one screen pixel
    this.zoomAt(canvas.clientWidth / 2, canvas.clientHeight / 2, this.design.dpi / (window.devicePixelRatio || 1) / this.zoom);
  },
  resized() { this.needFit ? this.fit() : draw(); },

  // ================================================================ mouse

  // Topmost visible layers under a screen point: their real pixels (so thin text is clickable)
  // and their score / cut lines.
  hits(sx, sy) {
    const placed = this.placed[this.mode()], [X, Y] = this.toInch(sx, sy), found = [];
    for (const l of [...this.design.layers].reverse()) {
      const e = l.visible !== false && placed.get(l.id);
      if (!e) continue;
      const m = this.mapper(l, e);
      if (e.rgba && this.pixelHit(e, m, X, Y)) { found.push(l); continue; }
      if (this.lineHit(e, m, sx, sy)) found.push(l);
    }
    return found;
  },
  pixelHit(e, m, X, Y) {
    const [u, v] = m.inv(X, Y);
    const px = Math.floor(u * e.dpi + e.w / 2), py = Math.floor(v * e.dpi + e.h / 2);
    const t = Math.max(1, Math.min(12, Math.round(4 * e.dpi / this.zoom)));
    if (px < -t || py < -t || px >= e.w + t || py >= e.h + t) return false;
    for (let y = Math.max(0, py - t); y <= Math.min(e.h - 1, py + t); y++)
      for (let x = Math.max(0, px - t); x <= Math.min(e.w - 1, px + t); x++)
        if (e.rgba[(y * e.w + x) * 4 + 3]) return true;
    return false;
  },
  lineHit(e, m, sx, sy) {
    for (const paths of Object.values(e.vecs)) {
      for (const [pts, closed] of paths) {
        const s = pts.map(([x, y]) => this.toScreen(...m.fwd(x, y)));
        if (closed && s.length > 2) s.push(s[0]);
        for (let i = 1; i < s.length; i++) if (segDist(sx, sy, s[i - 1], s[i]) <= 5) return true;
      }
    }
    return false;
  },
  inside(l, sx, sy) {
    const pts = this.corners(l).map(([x, y]) => this.toScreen(x, y));
    let inside = false;
    for (let i = 0; i < 4; i++) {
      const [x1, y1] = pts[i], [x2, y2] = pts[(i + 1) % 4];
      if ((y1 > sy) !== (y2 > sy) && sx < (x2 - x1) * (sy - y1) / ((y2 - y1) || 1e-9) + x1) inside = !inside;
    }
    return inside;
  },
  handleAt(sx, sy) {
    const h = this.handles;
    if (!h) return null;
    if (Math.hypot(sx - h.rot[0], sy - h.rot[1]) <= HANDLE + 3) return { mode: "rotate" };
    for (const [i, [x, y]] of h.corners.entries())
      if (Math.abs(sx - x) <= HANDLE + 3 && Math.abs(sy - y) <= HANDLE + 3) return { mode: "scale", corner: i };
    return null;
  },
  local(e) { const r = canvas.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; },

  pointerdown(e) {
    canvas.setPointerCapture(e.pointerId);
    const [sx, sy] = this.local(e);
    if (e.button !== 0 || spaceDown) return this.startPan(e);
    if (this.drawKind) return this.drawPress(sx, sy, e);
    const l = this.sel(), h = this.handleAt(sx, sy);
    if (l && h) {
      this.drag = { id: l.id, x0: sx, y0: sy, start: clone(l), ...h };
      if (h.mode === "rotate") this.drag.a0 = Math.atan2(sy - this.handles.center[1], sx - this.handles.center[0]);
      return;
    }
    const hits = this.hits(sx, sy);
    let hit = hits[0] || null;
    if ((e.ctrlKey || e.metaKey) && hits.length) {          // Ctrl+click: the item underneath
      const i = hits.findIndex((x) => x.id === this.selected);
      hit = i >= 0 ? hits[(i + 1) % hits.length] : hits[0];
    }
    if (!hit && l && this.inside(l, sx, sy)) hit = l;
    if (!hit) {
      if (this.selected) this.select(null);
      return this.startPan(e);
    }
    if (hit.id !== this.selected) this.select(hit.id);
    this.drag = { id: hit.id, x0: sx, y0: sy, start: clone(hit), mode: "move" };
  },
  startPan(e) {
    this.pan = { x: e.clientX, y: e.clientY, px: this.panX, py: this.panY };
    canvas.classList.add("panning");
  },
  pointermove(e) {
    const [sx, sy] = this.local(e);
    if (this.pan) {
      this.panX = this.pan.px + e.clientX - this.pan.x;
      this.panY = this.pan.py + e.clientY - this.pan.y;
      return draw();
    }
    if (!this.drag) return this.hover(sx, sy);
    const l = this.layer(this.drag.id);
    if (!l) return;
    const st = this.drag.start, z = this.zoom, mode = this.drag.mode;
    if (mode === "move") {
      let dx = (sx - this.drag.x0) / z, dy = (sy - this.drag.y0) / z;
      if (e.shiftKey) [dx, dy] = Math.abs(dx) > Math.abs(dy) ? [dx, 0] : [0, dy];      // one axis
      l.x_in = st.x_in + dx;
      l.y_in = st.y_in + dy;
      let snaps = [];
      if (!e.altKey) {
        const [ddx, ddy, g] = this.snap(l, SNAP_PX / z);
        l.x_in += ddx;
        l.y_in += ddy;
        snaps = g;
      }
      this.drag.snap = snaps;
      l.x_in = r4(l.x_in);
      l.y_in = r4(l.y_in);
    } else if (mode === "rotate") {
      const [cx, cy] = this.toScreen(st.x_in, st.y_in);
      let r = st.rotation + (Math.atan2(sy - cy, sx - cx) - this.drag.a0) * 180 / Math.PI;
      if (e.shiftKey) r = Math.round(r / 15) * 15;
      l.rotation = +((r % 360 + 360) % 360).toFixed(2);
    } else if (mode === "scale") {
      this.scale(l, st, e, sx, sy);
    } else if (mode === "draw") {
      this.drawUpdate(l, sx, sy, e);
    }
    this.refreshFields();
    // Resizing / rotating re-renders as you drag, except photos on the Design view (slow):
    // those are stretched until you let go. Moving never needs a render.
    if (mode !== "move" && (view === "layout" || l.type !== "image")) this.requestRender();
    draw();
  },
  pointerup() {
    if (this.pan) { this.pan = null; canvas.classList.remove("panning"); return; }
    if (!this.drag) return;
    const l = this.layer(this.drag.id);
    if (this.drag.mode === "draw") { if (l) this.drawRelease(l); return; }
    const moved = l && JSON.stringify(l) !== JSON.stringify(this.drag.start);
    this.drag = null;
    if (moved) {
      for (const k of ["x_in", "y_in", "w_in", "h_in"]) l[k] = r4(l[k]);
      this.refreshFields();
      this.changed();
    }
    draw();
  },
  hover(sx, sy) {              // cursor feedback: resize arrows, rotate, move
    if (this.drawKind) { canvas.style.cursor = "crosshair"; return; }
    if (spaceDown) { canvas.style.cursor = ""; return; }
    const h = this.handleAt(sx, sy);
    let cur = "default";
    if (h && h.mode === "rotate") cur = "alias";
    else if (h) {
      const [x, y] = this.handles.corners[h.corner], [cx, cy] = this.handles.center;
      cur = (x - cx) * (y - cy) > 0 ? "nwse-resize" : "nesw-resize";
    } else {
      const l = this.sel();
      if ((l && this.inside(l, sx, sy)) || this.hits(sx, sy).length) cur = "move";
    }
    canvas.style.cursor = cur;
  },

  snapTargets(layer) {         // canvas edges / center and every other visible item's edges / centers
    const c = this.design.canvas, xs = [0, c.width_in / 2, c.width_in], ys = [0, c.height_in / 2, c.height_in];
    for (const o of this.design.layers) {
      if (o.id === layer.id || o.visible === false) continue;
      const [l, t, r, b] = this.box(o);
      xs.push(l, (l + r) / 2, r);
      ys.push(t, (t + b) / 2, b);
    }
    return [xs, ys];
  },
  snap(layer, tol) {
    const [xs, ys] = this.snapTargets(layer), [l, t, r, b] = this.box(layer), guides = [], shift = [0, 0];
    for (const [axis, mine, targets] of [[0, [l, (l + r) / 2, r], xs], [1, [t, (t + b) / 2, b], ys]]) {
      let best = null;
      for (const m of mine) for (const v of targets)
        if (Math.abs(v - m) < tol && (!best || Math.abs(v - m) < Math.abs(best[0]))) best = [v - m, v];
      if (best) { shift[axis] = best[0]; guides.push([axis ? "y" : "x", best[1]]); }
    }
    return [shift[0], shift[1], guides];
  },
  // Corner-handle resize, as on the desktop: from the opposite corner (or the center: setting,
  // or hold Ctrl for the other one). Photos / logos / text keep their proportions; shapes resize
  // freely (Shift keeps proportions). Edges snap (Alt = off).
  scale(l, st, e, sx, sy) {
    const fromCenter = (this.scaleFrom() === "center") !== (e.ctrlKey || e.metaKey);
    const [gx, gy] = [[-1, -1], [1, -1], [1, 1], [-1, 1]][this.drag.corner];
    const w0 = st.w_in, h0 = st.h_in, a = rad(st.rotation);
    const ux = [Math.cos(a), Math.sin(a)], uy = [-Math.sin(a), Math.cos(a)];
    let px, py, c0;
    if (fromCenter) { px = st.x_in; py = st.y_in; c0 = [gx * w0 / 2, gy * h0 / 2]; }
    else {
      const ax = -gx * w0 / 2, ay = -gy * h0 / 2;
      px = st.x_in + ax * ux[0] + ay * uy[0];
      py = st.y_in + ax * ux[1] + ay * uy[1];
      c0 = [gx * w0, gy * h0];
    }
    const [mx, my] = this.toInch(sx, sy);
    const lx = (mx - px) * ux[0] + (my - py) * ux[1], ly = (mx - px) * uy[0] + (my - py) * uy[1];
    const locked = l.type !== "shape" || e.shiftKey, line = l.type === "shape" && l.shape === "line";
    let fx, fy;
    if (locked) fx = fy = (lx * c0[0] + ly * c0[1]) / Math.max(c0[0] ** 2 + c0[1] ** 2, 1e-9);
    else { fx = c0[0] ? lx / c0[0] : 1; fy = c0[1] && !line ? ly / c0[1] : 1; }
    const guides = [];
    if (Math.abs(st.rotation % 360) < 0.01 && !e.altKey) {
      // Snap the moving edges (unrotated items only, where edges are well defined).
      const [xs, ys] = this.snapTargets(l), tol = SNAP_PX / this.zoom, best = {};
      for (const [axis, p, c, fcur, targets] of [[0, px, c0[0], fx, xs], [1, py, c0[1], fy, ys]]) {
        if (!c || (axis === 1 && line)) continue;
        const edge = p + c * fcur;
        for (const t of targets)
          if (Math.abs(t - edge) < tol && (!(axis in best) || Math.abs(t - edge) < best[axis][0])) best[axis] = [Math.abs(t - edge), (t - p) / c, t];
      }
      const axes = Object.keys(best).map(Number);
      if (locked && axes.length) {
        const axis = axes.reduce((a2, b2) => (best[b2][0] < best[a2][0] ? b2 : a2));
        fx = fy = best[axis][1];
        guides.push([axis ? "y" : "x", best[axis][2]]);
      } else {
        for (const axis of axes) {
          if (axis === 0) fx = best[axis][1]; else fy = best[axis][1];
          guides.push([axis ? "y" : "x", best[axis][2]]);
        }
      }
    }
    const minf = 0.05 / Math.max(w0, h0, 1e-6);
    fx = Math.max(fx, minf);
    fy = Math.max(fy, minf);
    l.w_in = w0 * fx;
    l.h_in = line ? h0 : h0 * fy;
    const cx = c0[0] * fx / 2, cy = c0[1] * (line ? 1 : fy) / 2;
    if (fromCenter) { l.x_in = st.x_in; l.y_in = st.y_in; }
    else { l.x_in = px + cx * ux[0] + cy * uy[0]; l.y_in = py + cx * ux[1] + cy * uy[1]; }
    if (l.type === "text" || l.type === "arctext") {
      l.size_pt = +Math.max(4, st.size_pt * fx).toFixed(2);
      if (l.type === "arctext") l.radius_in = r4(Math.max(0.1, st.radius_in * fx));
    }
    this.drag.snap = guides;
  },
  scaleFrom() { return LS.get("scaleFrom", "corner"); },

  // ================================================================ keys

  key(e) {
    const ctrl = e.ctrlKey || e.metaKey, k = e.key.toLowerCase();
    if (ctrl && k === "z") { e.preventDefault(); e.shiftKey ? this.redo() : this.undo(); return true; }
    if (ctrl && k === "y") { e.preventDefault(); this.redo(); return true; }
    if (ctrl && k === "d") { e.preventDefault(); this.duplicate(); return true; }
    if (ctrl && k === "s") { e.preventDefault(); this.saveProject(e.shiftKey); return true; }
    if (ctrl || e.altKey) return false;
    if ((e.key === "Delete" || e.key === "Backspace") && this.selected) { e.preventDefault(); this.deleteLayer(); return true; }
    const arrows = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };
    if (arrows[e.key] && this.selected) { e.preventDefault(); this.nudge(...arrows[e.key], e.shiftKey); return true; }
    if (e.key === "Escape" && this.drawKind) { this.cancelDraw(); return true; }
    if ("12345".includes(e.key) && !current) return true;          // photo views need a photo layer
    return false;
  },

  // ================================================================ layers panel

  refreshList() {
    const ul = $("#layerList"), layers = [...this.design.layers].reverse();
    ul.replaceChildren(...layers.map((l) => {
      const li = el("li", { className: (l.id === this.selected ? "active" : "") + (l.visible === false ? " off" : ""),
        title: this.label(l) }, el("span", { className: "kind", textContent: this.info.types[l.type][0] }),
      el("span", { className: "name", textContent: this.label(l).replace(/^[^:]+: /, "") }));
      li.addEventListener("click", () => this.select(l.id));
      return li;
    }));
    $("#layersEmpty").hidden = layers.length > 0;
    for (const id of ["layerUp", "layerDown", "layerCopy", "layerHide", "layerDel"]) $("#" + id).disabled = !this.selected;
    const l = this.sel();
    $("#layerHide").textContent = l && l.visible === false ? "Show" : "Hide";
    const t = $("#layerTitle");
    if (t && l) t.textContent = this.label(l);
    $("#designEmpty").hidden = layers.length > 0 || !this.shown();
  },

  // ================================================================ Layer tab

  fields: {},
  buildLayerTab() {
    const p = $("#pane-layer");
    p.replaceChildren();
    this.fields = {};
    const l = this.sel();
    if (!l) {
      p.append(el("p", { className: "hint", textContent: "No layer selected. Use Add (above) to add a photo, logo, " +
        "text, arc text or shape, or click an item on the canvas." }));
      return;
    }
    p.append(el("h4", { className: "layer-title", id: "layerTitle", textContent: this.label(l) }));
    if (l.type !== "svg") {                  // (vector files will choose a job per color instead)
      const out = el("div", { className: "line radios", title: "Engrave: burned into the engraving image. " +
        "Score: a light vector line (blue, _score file). Cut: a vector line through the material (red, _cut file). " +
        "Shapes follow their outline, text its letter outlines, logos and photos their traced outline." },
      el("b", { textContent: "Output" }));
      for (const [v, t] of Object.entries(OP_LABELS)) {
        const r = el("input", { type: "radio", name: "layerOp", value: v, checked: (l.op || "engrave") === v });
        r.addEventListener("change", () => { this.set("op", v); this.buildLayerTab(); });
        out.append(el("label", { className: "check" }, r, " " + t));
      }
      out.append(el("span", { className: "tip", textContent: "ⓘ" }));
      p.append(out);
    }
    let s = section(p, "Position & size");
    this.num(s, "X center (in)", "x_in", 0.05);
    this.num(s, "Y center (in)", "y_in", 0.05);
    if (l.type !== "text" && l.type !== "arctext") {
      this.num(s, "Width (in)", "w_in", 0.05, 0.02);
      this.num(s, "Height (in)", "h_in", 0.05, 0.02);
    }
    this.num(s, "Rotation (°)", "rotation", 1, -360, 360);
    const t = l.type;
    if (t === "image") {
      s = section(p, "Crop (% from each edge)");
      ["Left", "Top", "Right", "Bottom"].forEach((side, i) => this.crop(s, side, i));
      p.append(el("p", { className: "hint", textContent: "Style, tone and background removal for this photo are on the " +
        "Basic / Photo tuning / Comic tuning tabs." }));
    } else if (t === "logo") {
      s = section(p, "Logo");
      this.num(s, "Dark threshold (0-255)", "threshold", 5, 0, 255, true);
      this.check(s, "Engrave the light parts instead", "invert_art");
      s.append(el("p", { className: "hint", textContent: "Opaque pixels darker than the threshold are engraved; " +
        "transparent areas let lower layers show through." }));
    } else if (t === "text" || t === "arctext") {
      s = section(p, "Text");
      this.textBox(s, t === "text");
      this.font(s);
      this.num(s, "Size (pt)", "size_pt", 1, 4, 400);
      if (t === "text") {
        this.choice(s, "Align", "align", [["left", "Left"], ["center", "Center"], ["right", "Right"]]);
        this.num(s, "Line spacing", "line_spacing", 0.05, 0.6, 3);
      } else {
        this.num(s, "Circle radius (in)", "radius_in", 0.05, 0.1);
        this.choice(s, "Position", "arc", [["top", "Top"], ["bottom", "Bottom"]]);
        this.num(s, "Letter spacing", "tracking", 0.02, -0.3, 2);
      }
      this.check(s, "Outline only", "outline");
      this.num(s, "Outline width (mm)", "stroke_mm", 0.1, 0.1, 5);
    } else if (t === "shape") {
      s = section(p, "Shape");
      this.choice(s, "Shape", "shape", [["rect", "Rectangle"], ["ellipse", "Ellipse"], ["line", "Line"]]);
      this.check(s, "Filled", "fill");
      this.num(s, "Line width (mm)", "stroke_mm", 0.1, 0.1, 20);
      this.num(s, "Corner radius (in)", "corner_in", 0.05, 0, 10);
    }
    if (t === "svg" || (l.op || "engrave") !== "engrave") {
      s = section(p, "Score / cut lines");
      if (t !== "shape") this.check(s, "Weld overlapping shapes", "weld", "Merges shapes that overlap (touching " +
        "letters, stacked shapes) into one outline, so the laser doesn't cut through the middle.");
      this.check(s, "Outer outline only (no holes)", "outer", "Keeps only the outside edge: a sticker / cut-out " +
        "outline around the whole thing, with the insides of letters left alone.");
      this.num(s, "Offset (mm)", "offset_mm", 0.5, -50, 50, false, "Pushes the lines out (+) or in (−): a margin " +
        "around text or a logo, or kerf compensation (about half the laser's cut width). Offset and Outer outline " +
        "also weld. Open lines aren't offset.");
    }
  },
  row(parent, label, input, tip) {
    const r = el("div", { className: "row field" }, el("label", { textContent: label }), input);
    if (tip) r.title = tip;
    parent.append(r);
    return r;
  },
  num(parent, label, key, step, lo = -1000, hi = 1000, integer = false, tip = "") {
    const n = el("input", { type: "number", step, min: lo, max: hi });
    const set = (v) => { if (document.activeElement !== n) n.value = r4(v ?? 0); };
    set(this.sel()[key]);
    n.addEventListener("input", () => {
      let v = parseFloat(n.value);
      if (Number.isNaN(v)) return;
      v = Math.min(hi, Math.max(lo, v));
      this.set(key, integer ? Math.round(v) : v);
    });
    n.addEventListener("change", () => { n.value = r4(this.sel()[key]); });
    this.fields[key] = set;
    this.row(parent, tip ? label + " ⓘ" : label, n, tip);
  },
  check(parent, label, key, tip = "") {
    const box = el("input", { type: "checkbox", checked: !!this.sel()[key] });
    box.addEventListener("change", () => this.set(key, box.checked));
    this.fields[key] = (v) => { box.checked = !!v; };
    const lbl = el("label", { className: "check", title: tip }, box, " " + label + (tip ? " ⓘ" : ""));
    parent.append(el("div", { className: "line" }, lbl));
  },
  choice(parent, label, key, options) {
    const sel = el("select");
    for (const [v, t] of options) sel.append(el("option", { value: v, textContent: t }));
    sel.value = this.sel()[key];
    sel.addEventListener("change", () => this.set(key, sel.value));
    this.fields[key] = (v) => { sel.value = v; };
    this.row(parent, label, sel);
  },
  crop(parent, side, i) {
    const l = this.sel();
    const n = el("input", { type: "number", min: 0, max: 95, step: 1 });
    const show = () => { const c = this.sel().crop || [0, 0, 1, 1]; n.value = +((i < 2 ? c[i] : 1 - c[i]) * 100).toFixed(1); };
    show();
    n.addEventListener("input", () => {
      const v = Math.min(95, Math.max(0, parseFloat(n.value))) / 100;
      if (Number.isNaN(v)) return;
      const old = l.crop || [0, 0, 1, 1], c = [...old];
      c[i] = i < 2 ? v : 1 - v;
      if (c[2] - c[0] < 0.05 || c[3] - c[1] < 0.05) return;
      // Height follows the new aspect ratio (the photo's own aspect is the same before and after).
      const src = (l.h_in / l.w_in) * (old[2] - old[0]) / (old[3] - old[1]);
      l.crop = c;
      l.h_in = l.w_in * src * (c[3] - c[1]) / (c[2] - c[0]);
      this.refreshFields();
      this.changed();
    });
    n.addEventListener("change", show);
    this.row(parent, `  ${side}`, n);
  },
  textBox(parent, multiline) {
    const l = this.sel();
    const box = multiline ? el("textarea", { rows: 3, value: l.text }) : el("input", { type: "text", value: l.text });
    box.className = "textbox";
    box.addEventListener("input", () => {
      const x = this.sel();
      if (x && x.text !== box.value) { x.text = box.value; this.refreshList(); this.changed(); }
    });
    parent.append(el("div", { className: "line" }, el("label", { textContent: "Text" })), box);
  },
  font(parent) {
    const l = this.sel(), fonts = this.info.fonts, aliases = this.info.aliases || {};
    const fam = el("select"), sty = el("select");
    const stylesOf = (f) => fonts[f] || fonts[aliases[f]];
    const fill = () => {
      // Arial / Times New Roman / Courier New (the desktop's defaults) are drawn with look-alikes.
      const names = [...Object.keys(aliases), ...Object.keys(fonts)].sort((a, b) => a.localeCompare(b));
      fam.replaceChildren(...names.map((f) => el("option", { value: f,
        textContent: aliases[f] ? `${f} (as ${aliases[f]})` : f })));
      if (!stylesOf(l.font)) fam.append(el("option", { value: l.font, textContent: `${l.font} (not available)` }));
      fam.value = l.font;
      const styles = stylesOf(l.font) || [l.style];
      sty.replaceChildren(...styles.map((s) => el("option", { value: s, textContent: s })));
      sty.value = l.style;
    };
    fill();
    fam.addEventListener("change", () => {
      const styles = stylesOf(fam.value) || ["Regular"];
      const style = styles.includes(l.style) ? l.style : styles.includes("Regular") ? "Regular" : styles[0];
      this.set("font", fam.value, { style });
      fill();
    });
    sty.addEventListener("change", () => this.set("style", sty.value));
    this.row(parent, "Font", fam, "Fonts that come with the web version (Arial, Times New Roman and Courier New " +
      "use look-alikes with the same letter widths), plus any you add.");
    this.row(parent, "Style", sty);
    const add = el("label", { className: "link", title: "Use a font file from your computer (.ttf or .otf). " +
      "It's kept in this browser with the design; others opening your project need it too." }, "Add a font…",
    el("input", { type: "file", accept: ".ttf,.otf,.ttc", hidden: true }));
    add.querySelector("input").addEventListener("change", async (e) => {
      const f = e.target.files[0];
      e.target.value = "";
      if (!f) return;
      try {
        const bytes = new Uint8Array(await f.arrayBuffer());
        const keep = IDB.ok ? bytes.slice() : null;
        const r = await call("addFont", { name: f.name, bytes }, [bytes.buffer]);
        this.info.fonts = r.fonts;
        if (keep) await IDB.put("fonts", `${r.family}|${r.style}`, { name: f.name, bytes: keep });
        this.set("font", r.family, { style: r.style });
        this.buildLayerTab();
        setStatus(`Added the font ${r.family} ${r.style}`);
      } catch (err) { fail(err.message); }
    });
    parent.append(el("div", { className: "line" }, add));
  },
  set(key, value, also = {}) {
    const l = this.sel();
    if (!l) return;
    const aspect = l.h_in / Math.max(l.w_in, 1e-6);       // photos / logos keep their proportions
    l[key] = value;
    Object.assign(l, also);
    if (ASPECT_LOCKED.includes(l.type) && (key === "w_in" || key === "h_in")) {
      if (key === "w_in") l.h_in = l.w_in * aspect; else l.w_in = l.h_in / Math.max(aspect, 1e-6);
      this.refreshFields();
    }
    if (["shape", "op", "text"].includes(key)) this.refreshList();
    if (l.type === "image" && (key === "w_in" || key === "h_in")) this.syncWidth(l);
    this.changed();
  },
  refreshFields() {            // layer values -> Layer tab (after drags, crops, undo)
    const l = this.sel();
    if (!l) return;
    for (const [k, set] of Object.entries(this.fields)) if (k in l) set(l[k]);
    if (l.type === "image") this.syncWidth(l);
  },
  syncWidth(l) {               // the Basic tab's Width follows the photo layer's width
    if (Math.abs(S.width_in - l.w_in) > 1e-4) {
      S.width_in = r4(l.w_in);
      this.sWidth = S.width_in;
      for (const c of controls) if (c.key === "width_in") c.set(S.width_in);
      this.photoStale = true;
    }
  },

  // ================================================================ Design tab

  buildDesignTab() {
    const p = $("#pane-design");
    if (p.dataset.built) return this.refreshDesignTab();
    p.dataset.built = "1";
    let s = section(p, "Canvas (your material)");
    this.canvasFields = {};
    for (const [label, k] of [["Width (in)", "width_in"], ["Height (in)", "height_in"]]) {
      const n = el("input", { type: "number", min: 0.5, max: 48, step: 0.25 });
      n.addEventListener("input", () => {
        const v = parseFloat(n.value);
        if (!(v >= 0.25 && v <= 48)) return;
        this.design.canvas = { ...this.design.canvas, [k]: v };
        this.needFit = true;
        this.fit();
        this.updateInfo();
        this.changed();
      });
      this.canvasFields[k] = n;
      this.row(s, label, n);
    }
    const pre = el("select", {}, el("option", { value: "", textContent: "Choose…" }),
      ...Object.keys(CANVAS_PRESETS).map((k) => el("option", { value: k, textContent: k })));
    pre.addEventListener("change", () => {
      if (!pre.value) return;
      const [w, h] = CANVAS_PRESETS[pre.value];
      this.design.canvas = { width_in: w, height_in: h };
      pre.value = "";
      this.refreshDesignTab();
      this.fit();
      this.updateInfo();
      this.changed();
    });
    this.row(s, "Preset", pre);
    const rf = el("div", { className: "radios" });
    for (const [t, v] of [["Opposite corner", "corner"], ["Center", "center"]]) {
      const r = el("input", { type: "radio", name: "scaleFrom", value: v, checked: this.scaleFrom() === v });
      r.addEventListener("change", () => LS.set("scaleFrom", v));
      rf.append(el("label", { className: "check" }, r, " " + t));
    }
    this.row(s, "Resize from", rf, "What stays put when you drag a corner handle. Hold Ctrl while dragging to use the other one.");

    s = section(p, "Material & output");
    const mat = el("select");
    mat.addEventListener("change", () => change("material", mat.value));
    controls.push({ key: "material", set: (v) => { refreshMaterialList(mat); mat.value = v; } });
    this.row(s, "Material", mat);
    this.row(s, "DPI", select("dpi", DPI_CHOICES.map(([d, mm]) => [d, `${d} · ${mm}`])), "Engrave at this same resolution in your laser software");
    checkbox(s, "invert", "Invert (laser makes light marks)");
    checkbox(s, "mirror", "Mirror (engrave from the back)");

    s = section(p, "Export");
    this.exportFields = {};
    const opt = (key, labels, label, tip) => {
      const sel = el("select", {}, ...Object.entries(labels).map(([v, t]) => el("option", { value: v, textContent: t })));
      sel.addEventListener("change", () => this.setDesignOpt(key, sel.value));
      this.exportFields[key] = (v) => { sel.value = v === true ? "svg" : v === false ? "none" : v; };   // (a checkbox before v1.11)
      this.row(s, label + " ⓘ", sel, tip);
    };
    opt("cut_format", FMT_LABELS, "Cut / score files", "One vector file per job, in mm: _cut (red lines) and _score " +
      "(blue lines). SVG: xTool, LaserGRBL, LightBurn, Trotec Ruby. DXF: Trotec Ruby, LightBurn, xTool. Separate files " +
      "suit LaserGRBL, which runs one power setting per file.");
    const frame = el("input", { type: "checkbox" });
    frame.addEventListener("change", () => this.setDesignOpt("frame", frame.checked));
    this.exportFields.frame = (v) => { frame.checked = v !== false; };
    s.append(el("div", { className: "line" }, el("label", { className: "check", title: "Adds a green rectangle the " +
      "size of the canvas to the cut and score files. The engraving PNG is the same size: line their corners up in " +
      "your laser software, then delete or disable the green frame." }, frame, " Add alignment frame ⓘ")));
    opt("combined", COMB_LABELS, "All-in-one file", "One file with the engraving image and every score / cut line, " +
      "already lined up (_combined.svg / .pdf): open it and assign the colors. PDF: Trotec Ruby, LightBurn, xTool. " +
      "SVG: LightBurn, xTool. Not tested on every program: if the image doesn't come in, use the PNG + cut / score " +
      "files instead.");
    checkbox(s, "outline", "Cut outline around everything engraved");
    slider(s, "outline_offset_in", 0, 1, 0.02, "Outline offset (in)");
    const pct = el("input", { type: "number", min: 5, max: 100, step: 5 });
    pct.addEventListener("input", () => {
      const v = parseFloat(pct.value);
      if (v >= 1 && v <= 100) this.setDesignOpt("test_pct", v);
      this.updateTestSize();
    });
    this.exportFields.test_pct = (v) => { if (document.activeElement !== pct) pct.value = v ?? 25; };
    this.row(s, "Test copy size (%)", pct);
    const testBtn = el("button", { className: "btn sm", type: "button", textContent: "Save test copy",
      title: "Saves a shrunken copy of the whole design (same DPI, material and settings, so it burns like the real " +
        "job) to try on scrap first. Files end in _test." });
    testBtn.addEventListener("click", () => this.exportDesign(this.testFactor()));
    s.append(el("div", { className: "line" }, testBtn, el("span", { className: "hint", id: "testSize" })));

    s = section(p, "This browser");
    const clr = el("button", { className: "btn sm", type: "button", textContent: "Clear saved design",
      title: "The design is kept in this browser (with its photos and added fonts) so it's still here next time. " +
        "This removes that copy and starts a new, empty design. Project files you saved aren't touched." });
    clr.addEventListener("click", () => this.clearSaved());
    s.append(el("p", { className: "hint", id: "autosaveNote" }), clr);

    p.append(el("p", { className: "hint", textContent: "Material, DPI, invert and mirror apply to the whole design. " +
      "Each photo layer has its own style and settings: select it and use the Basic / Photo tuning / Comic tuning tabs." }));
    p.append(el("p", { className: "hint", textContent: "Arrange on the Layout view (fast; photos shown unprocessed), check " +
      "the result on the Design view (the real engraving)." }));
    p.append(el("p", { className: "hint", textContent: "Drag to move: edges and centers snap to other items and the canvas " +
      "(hold Alt to move freely). Ctrl+click picks the item underneath. Corner squares resize, the circle on top rotates " +
      "(Shift snaps to 15°). Arrow keys nudge (Shift = bigger steps). Ctrl+Z / Ctrl+Y undo and redo, Ctrl+D duplicates, " +
      "Delete removes. Mouse wheel zooms, drag empty space to pan." }));
    this.refreshDesignTab();
    refreshControls();
  },
  refreshDesignTab() {
    if (!this.canvasFields) return;
    for (const [k, n] of Object.entries(this.canvasFields)) n.value = this.design.canvas[k];
    for (const [k, set] of Object.entries(this.exportFields)) set(this.design[k] ?? { cut_format: "svg", combined: "none" }[k]);
    this.updateTestSize();
    $("#autosaveNote").textContent = IDB.ok ? "Your design is saved in this browser as you work." :
      "This browser isn't keeping a copy of the design (storage is blocked), so save a project file.";
    Object.assign(S, this.globals());
    refreshControls();
  },
});

function segDist(px, py, [x1, y1], [x2, y2]) {
  const dx = x2 - x1, dy = y2 - y1, n = dx * dx + dy * dy;
  const t = n ? Math.max(0, Math.min(1, ((px - x1) * dx + (py - y1) * dy) / n)) : 0;
  return Math.hypot(px - x1 - t * dx, py - y1 - t * dy);
}

// ============================================================ page wiring

function setModeButtons(mode) {
  for (const b of document.querySelectorAll("#modeSeg button")) b.classList.toggle("active", b.dataset.mode === mode);
}
function setMode(mode) {
  if (mode === "design") DM.enter(); else DM.leave();
}
for (const b of document.querySelectorAll("#modeSeg button")) b.addEventListener("click", () => setMode(b.dataset.mode));

const addMenu = $("#addMenu");
$("#addBtn").addEventListener("click", (e) => { e.stopPropagation(); addMenu.hidden = !addMenu.hidden; });
document.addEventListener("click", () => { addMenu.hidden = true; });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") addMenu.hidden = true; });
for (const b of addMenu.querySelectorAll("[data-add]")) {
  b.addEventListener("click", () => {
    addMenu.hidden = true;
    const k = b.dataset.add;
    if (k === "image" || k === "logo") {
      const inp = $("#artInput");
      inp.dataset.kind = k;
      inp.multiple = k === "image";
      inp.click();
    } else if (k === "text" || k === "arctext") DM.addSimple(k);
    else DM.startDraw(k);
  });
}
$("#artInput").addEventListener("change", (e) => { DM.addFiles(e.target.files, e.target.dataset.kind); e.target.value = ""; });
$("#layerUp").addEventListener("click", () => DM.move(1));
$("#layerDown").addEventListener("click", () => DM.move(-1));
$("#layerCopy").addEventListener("click", () => DM.duplicate());
$("#layerHide").addEventListener("click", () => DM.toggleVisible());
$("#layerDel").addEventListener("click", () => DM.deleteLayer());
$("#newDesignBtn").addEventListener("click", () => DM.newDesign());
$("#openProjectBtn").addEventListener("click", () => DM.openProject());
$("#saveProjectBtn").addEventListener("click", () => DM.saveProject());
$("#saveProjectAsBtn").addEventListener("click", () => DM.saveProject(true));
$("#exportBtn").addEventListener("click", () => DM.exportDesign());
$("#lepInput").addEventListener("change", (e) => { const f = e.target.files[0]; e.target.value = ""; if (f) DM.openProjectFile(f); });
// Last-moment save of the design kept in this browser (normally saved 0.8 s after each change).
window.addEventListener("pagehide", () => {
  if (!DM.design || !IDB.ok) return;
  DM.commitNow();
  clearTimeout(DM.saveTimer);
  IDB.put("kv", "design", { design: DM.design, name: DM.projectName, dirty: DM.dirty });
});
for (const b of document.querySelectorAll("#designViews button")) b.addEventListener("click", () => setView(b.dataset.view));
