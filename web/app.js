// app.js: the page. All processing happens in worker.js (Python engrave_core via Pyodide);
// this file is the interface: settings, file list, preview canvas, presets and materials.
"use strict";

const $ = (s) => document.querySelector(s);
const el = (tag, props = {}, ...kids) => {
  const e = Object.assign(document.createElement(tag), props);
  for (const k of kids) e.append(k);
  return e;
};

// --- Browser storage (per-viewer conveniences only; the app works without it)
const LS = {
  get(k, d) { try { const v = localStorage.getItem("le." + k); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem("le." + k, JSON.stringify(v)); } catch { /* storage blocked */ } },
};

// --- Talking to the worker
const worker = new Worker("worker.js");
let reqSeq = 0;
const waiting = new Map();
worker.onmessage = (e) => {
  const m = e.data;
  if (m.type === "progress") return showProgress(m.text, m.frac);
  const p = waiting.get(m.reqId);
  if (!p) return;
  waiting.delete(m.reqId);
  m.ok ? p.resolve(m.value) : p.reject(new Error(m.error));
};
worker.onerror = (e) => fail(`The processing engine stopped: ${e.message || "unknown error"}`);
function call(type, args = {}, transfer = []) {
  return new Promise((resolve, reject) => {
    const reqId = ++reqSeq;
    waiting.set(reqId, { resolve, reject });
    worker.postMessage({ reqId, type, ...args }, transfer);
  });
}

// --- Constants (same as the desktop app)
const TIPS = {
  contrast: "1.0 = unchanged. Higher pushes tones apart.",
  brightness: "1.0 = unchanged. Higher = less laser firing.",
  gamma: ">1 lightens midtones, <1 darkens them.",
  shadow_amount: "How much to lift dark areas so their texture survives.",
  shadow_radius: "Blur size (px) used to find dark regions.",
  clahe_clip: "Local contrast strength. Higher = more texture, more noise.",
  clahe_grid: "Number of tiles across. Higher = more local.",
  sharpen_radius: "Unsharp-mask radius in px.",
  sharpen_percent: "Unsharp-mask strength.",
  sharpen_threshold: "Only sharpen edges stronger than this.",
  comic_levels: "Number of tone bands. 4 = black, crosshatch, dots, blank.",
  comic_smooth: "Flattening passes. More = smoother, more cartoon-like regions.",
  comic_dot_mm: "Spacing of halftone dots and hatch lines, in mm. Go bigger if fine dots burn together.",
  comic_line_block: "Area (px) used to find ink lines. Bigger = bolder shapes, fewer fine lines.",
  comic_line_c: "Line sensitivity. Higher = fewer, cleaner lines.",
  comic_line_weight: "Thicken ink lines by this many pixels.",
  comic_despeckle: "Remove ink specks smaller than this many pixels.",
  outline_offset_in: "How far outside the subject the cut line runs.",
};
const INT_FIELDS = ["dpi", "shadow_radius", "clahe_grid", "sharpen_percent", "sharpen_threshold", "comic_levels",
  "comic_smooth", "comic_line_block", "comic_line_weight", "comic_despeckle"];
const CROP_KEYS = ["rotate", "crop_l", "crop_t", "crop_r", "crop_b"];      // kept per photo
const MATERIAL_KEYS = ["contrast", "brightness", "gamma", "invert", "mirror"];
const DPI_CHOICES = [[254, "0.10 mm"], [318, "0.08 mm"], [423, "0.06 mm"], [508, "0.05 mm"]];

// --- State
let DEF = null;              // defaults from engrave_core (settings, preset fields, version)
let S = null;                // current settings (everything except per-photo crop/rotate)
let materials = {}, builtinMaterials = {};
let presets = { photo: {}, comic: {} };
const files = [];            // {id, name, url, w, h, crop}
let current = null;
let views = null, viewCache = {};
let view = LS.get("view", "engraving");
let modelCached = false;
const controls = [];         // {key, set(value)} to refresh inputs from S

// ============================================================ status

const statusEl = $("#status"), barEl = $("#bar");
function setStatus(text, cls = "") { statusEl.textContent = text; statusEl.className = cls; }
function showProgress(text, frac) {
  if (!$("#loading").classList.contains("hidden")) {
    $("#loadText").textContent = text;
    if (frac != null) $("#loadBar").style.width = `${Math.round(frac * 100)}%`;
    return;
  }
  setStatus(text);
  barEl.hidden = false;
  barEl.classList.toggle("indeterminate", frac == null);
  barEl.firstElementChild.style.width = frac == null ? "" : `${Math.round(frac * 100)}%`;
}
function doneProgress() { barEl.hidden = true; }
function fail(msg) { doneProgress(); setStatus(msg, "error"); }

// ============================================================ settings panels

function section(parent, title) {
  const s = el("div", { className: "section" }, el("h4", { textContent: title }));
  parent.append(s);
  return s;
}
function labelWithTip(text, key) {
  const l = el("label", { textContent: text });
  if (TIPS[key]) { l.title = TIPS[key]; l.append(el("span", { className: "tip", textContent: " ⓘ" })); }
  return l;
}
function slider(parent, key, lo, hi, step, label) {
  label = label || key.replace(/_/g, " ").replace(/^(clahe|sharpen|shadow|comic) /, "").replace(/^\w/, (c) => c.toUpperCase());
  const range = el("input", { type: "range", min: lo, max: hi, step });
  const num = el("input", { type: "number", min: lo, max: hi, step, className: "val" });
  const row = el("div", { className: "row" }, labelWithTip(label, key), range, num);
  if (TIPS[key]) range.title = TIPS[key];
  const set = (v) => { range.value = v; num.value = +(+v).toFixed(4); };
  controls.push({ key, set });
  const apply = (v) => {
    v = Math.min(hi, Math.max(lo, +v));
    if (Number.isNaN(v)) return set(S[key]);
    v = +(lo + Math.round((v - lo) / step) * step).toFixed(4);
    if (INT_FIELDS.includes(key)) v = Math.round(v);
    set(v);
    change(key, v);
  };
  range.addEventListener("input", () => apply(range.value));
  num.addEventListener("change", () => apply(num.value));
  parent.append(row);
  return row;
}
function checkbox(parent, key, label, tip) {
  const box = el("input", { type: "checkbox" });
  const l = el("label", { className: "check" }, box, " " + label);
  if (tip) l.title = tip;
  controls.push({ key, set: (v) => { box.checked = !!v; } });
  box.addEventListener("change", () => change(key, box.checked));
  parent.append(el("div", { className: "line" }, l));
  return box;
}
function select(key, options, onPick) {
  const sel = el("select");
  for (const [value, text] of options) sel.append(el("option", { value, textContent: text }));
  controls.push({ key, set: (v) => { sel.value = String(v); } });
  sel.addEventListener("change", () => (onPick || ((v) => change(key, v)))(sel.value));
  return sel;
}

function buildBasic(p) {
  // Style & material
  let s = section(p, "Style & material");
  const styles = el("div", { className: "radios" });
  for (const [v, t] of [["photo", "Photo"], ["comic", "Comic"]]) {
    const r = el("input", { type: "radio", name: "style", value: v });
    r.addEventListener("change", () => change("style", v));
    controls.push({ key: "style", set: (x) => { r.checked = x === v; } });
    styles.append(el("label", { className: "check" }, r, " " + t));
  }
  s.append(el("div", { className: "row wide" }, el("label", { textContent: "Style" }), styles));
  const mat = el("select", { id: "materialSelect" });
  mat.addEventListener("change", () => change("material", mat.value));
  controls.push({ key: "material", set: (v) => { refreshMaterialList(mat); mat.value = v; } });
  s.append(el("div", { className: "row wide" }, el("label", { textContent: "Material" }), mat));
  const links = el("div", { className: "line" });
  for (const [t, fn, tip, edit] of [["Reset", resetMaterial, "Put back this material's saved tone settings"],
    ["Save as…", saveMaterial, "Save the tone settings below as your own material"],
    ["Rename…", renameMaterial, "", true], ["Delete", deleteMaterial, "", true]]) {
    const b = el("button", { className: "link" + (edit ? " mat-edit" : ""), textContent: t, title: tip, type: "button" });
    b.addEventListener("click", fn);
    links.append(b);
  }
  s.append(links);

  // Size & resolution
  s = section(p, "Size & resolution");
  for (const [by, label] of [["width", "Width (in)"], ["height", "Height (in)"]]) {
    const r = el("input", { type: "radio", name: "size_by", value: by });
    r.addEventListener("change", () => change("size_by", by));
    controls.push({ key: "size_by", set: (x) => { r.checked = x === by; } });
    const key = `${by}_in`;
    const n = el("input", { type: "number", min: 0.25, max: 48, step: 0.25 });
    n.addEventListener("change", () => { const v = Math.min(48, Math.max(0.25, +n.value || 0.25)); n.value = v; change(key, v); });
    n.addEventListener("focus", () => { if (S.size_by !== by) change("size_by", by); });
    controls.push({ key, set: (v) => { n.value = v; } });
    s.append(el("div", { className: "row size" }, el("label", { className: "check" }, r, " " + label),
      n, el("span", { className: "hint", id: `${by}Hint` })));
  }
  const dpi = select("dpi", DPI_CHOICES.map(([d, mm]) => [d, `${d} · ${mm}`]));
  s.append(el("div", { className: "row wide", title: "Engrave at this same resolution in your laser software" },
    el("label", { textContent: "DPI" }), dpi));
  s.append(el("div", { className: "row wide" }, el("label", { textContent: "Dither" }), ditherSelect()));
  s.append(el("p", { className: "hint", textContent: "stucki: most detail (acrylic, glass) · floyd-steinberg: photos, " +
    "portraits · bayer: wood grain, geometric · none: hard cutoff" }));

  // Background
  s = section(p, "Background");
  checkbox(s, "remove_bg", "Remove background (AI)",
    "Cuts out the main subject and makes the background blank, so the laser skips it. " +
    "Also gives the cut outline a real silhouette.");
  s.append(el("p", { className: "hint", id: "bgNote" }));

  // Tone
  s = section(p, "Tone");
  slider(s, "contrast", 0.5, 2.5, 0.05);
  slider(s, "brightness", 0.5, 2.0, 0.05);
  slider(s, "gamma", 0.4, 3.0, 0.05);
  checkbox(s, "invert", "Invert (laser makes light marks)");
  checkbox(s, "mirror", "Mirror (engrave from the back)");

  // Crop & rotate (per photo)
  s = section(p, "Crop & rotate (this photo)");
  s.id = "cropSection";
  const rot = el("div", { className: "line" });
  for (const [t, d] of [["⟲ 90°", -90], ["⟳ 90°", 90]]) {
    const b = el("button", { className: "btn sm", textContent: t, type: "button" });
    b.addEventListener("click", () => setCrop("rotate", ((cropOf().rotate + d) % 360 + 360) % 360));
    rot.append(b);
  }
  rot.append(el("span", { className: "hint", id: "rotLabel" }));
  s.append(rot);
  const st = el("input", { type: "range", min: -15, max: 15, step: 0.25, id: "straighten" });
  const stv = el("input", { type: "number", min: -15, max: 15, step: 0.25, className: "val", id: "straightenVal" });
  const applyStraighten = (v) => {
    v = Math.min(15, Math.max(-15, +v || 0));
    const base = 90 * Math.round(cropOf().rotate / 90);
    setCrop("rotate", +(((base + v) % 360 + 360) % 360).toFixed(3));
  };
  st.addEventListener("input", () => applyStraighten(st.value));
  stv.addEventListener("change", () => applyStraighten(stv.value));
  s.append(el("div", { className: "row" }, el("label", { textContent: "Straighten (°)" }), st, stv));
  const crop = el("div", { className: "crop" });
  for (const [k, t] of [["crop_l", "L"], ["crop_t", "T"], ["crop_r", "R"], ["crop_b", "B"]]) {
    const n = el("input", { type: "number", min: 0, max: 90, step: 1, id: k });
    n.addEventListener("change", () => setCrop(k, Math.min(90, Math.max(0, +n.value || 0)) / 100));
    crop.append(el("span", { textContent: t }), n);
  }
  s.append(el("div", { className: "line" }, el("span", { textContent: "Crop (% from each edge)" })), crop);
  const reset = el("button", { className: "btn sm", textContent: "Reset crop & rotate", type: "button", style: "margin-top:8px" });
  reset.addEventListener("click", () => { if (current) { current.crop = blankCrop(); refreshCrop(); scheduleRender(); } });
  s.append(reset);

  // Extras
  s = section(p, "Extras");
  checkbox(s, "outline", "Also download cut outline (SVG)", "A red cut line around the subject, at true size.");
  slider(s, "outline_offset_in", 0, 0.5, 0.01, "Outline offset (in)");
}

function ditherSelect() { return select("dither", DEF.dither_modes.map((m) => [m, m])); }

function presetBar(p, style) {
  const s = section(p, "Preset");
  s.querySelector("h4").append(el("span", { className: "changed", id: `changed-${style}` }));
  const sel = el("select", { id: `preset-${style}` });
  sel.addEventListener("change", () => applyPreset(style, sel.value));
  const btns = el("div", { className: "btns" });
  for (const [t, fn] of [["Save as…", savePreset], ["Rename…", renamePreset], ["Delete", deletePreset]]) {
    const b = el("button", { className: "btn sm", textContent: t, type: "button" });
    b.addEventListener("click", () => fn(style));
    btns.append(b);
  }
  s.append(el("div", { className: "preset-bar" }, sel, btns));
  s.title = "Pick a saved look. \"changed\" means the sliders differ from it: use Save as… to keep them. " +
    "Material settings (contrast, gamma, ...) are saved separately.";
}

function buildPhoto(p) {
  presetBar(p, "photo");
  let s = section(p, "Dither");
  s.append(el("div", { className: "row wide" }, el("label", { textContent: "Dither" }), ditherSelect()));
  s = section(p, "Shadow detail recovery");
  slider(s, "shadow_amount", 0, 0.6, 0.01);
  slider(s, "shadow_radius", 1, 60, 1);
  s = section(p, "Micro-texture (CLAHE)");
  checkbox(s, "use_clahe", "Enabled");
  slider(s, "clahe_clip", 0.5, 5, 0.1);
  slider(s, "clahe_grid", 2, 32, 1);
  s = section(p, "Sharpening");
  slider(s, "sharpen_radius", 0, 8, 0.5);
  slider(s, "sharpen_percent", 0, 400, 5);
  slider(s, "sharpen_threshold", 0, 20, 1);
  revertButton(p, "photo");
}

function buildComic(p) {
  presetBar(p, "comic");
  p.append(el("p", { className: "hint", textContent: "Used when Style = Comic." }));
  let s = section(p, "Tones");
  slider(s, "comic_levels", 2, 5, 1, "Tone bands");
  slider(s, "comic_smooth", 0, 10, 1, "Flatten");
  slider(s, "comic_dot_mm", 0.3, 3, 0.05, "Dot spacing (mm)");
  s = section(p, "Ink lines");
  slider(s, "comic_line_block", 3, 61, 2, "Line scale");
  slider(s, "comic_line_c", 0, 20, 0.5, "Sensitivity");
  slider(s, "comic_line_weight", 0, 4, 1, "Thickness");
  slider(s, "comic_despeckle", 0, 300, 5, "Despeckle");
  p.append(el("p", { className: "hint", textContent: "Contrast, brightness, gamma, invert and mirror on the Basic " +
    "tab apply to comic too. With Invert on, shading flips but ink lines stay engraved (a scratchboard " +
    "look on slate or black acrylic)." }));
  revertButton(p, "comic");
}

function revertButton(p, style) {
  const b = el("button", { className: "btn", textContent: "Revert to preset", type: "button" });
  b.addEventListener("click", () => applyPreset(style, $(`#preset-${style}`).value));
  p.append(b);
}

function refreshControls() {
  for (const c of controls) c.set(S[c.key]);
  refreshCrop();
  refreshPresetMarks();
  refreshHints();
}

// ============================================================ changes

let saveTimer = null;
function change(key, value) {
  if (INT_FIELDS.includes(key)) value = Math.round(+value);
  S[key] = value;
  if (key === "material") {
    applyMaterialProfile();
    refreshControls();
  } else {
    for (const c of controls) if (c.key === key) c.set(value);
  }
  refreshPresetMarks();
  refreshHints();
  refreshFixbar();
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => LS.set("settings", S), 400);
  scheduleRender();
}

function refreshHints() {
  if (views && current) {
    const aspect = views.h / views.w;
    $("#widthHint").textContent = S.size_by === "height" ? `→ ${(S.height_in / aspect).toFixed(2)} in wide` : "";
    $("#heightHint").textContent = S.size_by === "width" ? `→ ${(S.width_in * aspect).toFixed(2)} in tall` : "";
  }
  $("#bgNote").textContent = S.remove_bg || modelCached
    ? (modelCached ? "AI model ready (saved in this browser). A few seconds per photo."
      : "The AI model (176 MB) downloads once, then stays in this browser.")
    : "The first use downloads the AI model (176 MB) once.";
}

// --- Per-photo crop and rotate
function blankCrop() { return { rotate: 0, crop_l: 0, crop_t: 0, crop_r: 0, crop_b: 0 }; }
function cropOf() { return current ? current.crop : blankCrop(); }
function setCrop(key, v) {
  if (!current) return;
  current.crop[key] = v;
  refreshCrop();
  scheduleRender();
}
function refreshCrop() {
  const c = cropOf();
  const fine = +(c.rotate - 90 * Math.round(c.rotate / 90)).toFixed(3);
  $("#straighten").value = fine;
  $("#straightenVal").value = fine;
  $("#rotLabel").textContent = c.rotate ? `rotated ${+c.rotate.toFixed(2)}°` : "";
  for (const k of ["crop_l", "crop_t", "crop_r", "crop_b"]) $("#" + k).value = Math.round(c[k] * 100);
  for (const i of $("#cropSection").querySelectorAll("input, button")) i.disabled = !current;
}

// ============================================================ materials

// Built-in materials (materials.json) are read-only here; your own live in this browser and
// can't reuse a built-in name, so a shared computer always has the original profiles.
let myMaterials = {};
function loadMaterials(builtin) {
  builtinMaterials = builtin;
  myMaterials = LS.get("myMaterials", null);
  if (!myMaterials) {                       // older versions stored one combined list
    const old = LS.get("materials", {}) || {};
    myMaterials = Object.fromEntries(Object.entries(old).filter(([k]) => !(k in builtin)));
    LS.set("myMaterials", myMaterials);
    LS.set("materials", null);
  }
  for (const k of Object.keys(myMaterials)) if (k in builtin) delete myMaterials[k];
  materials = { ...structuredClone(builtin), ...myMaterials };
}
function storeMaterials() {
  LS.set("myMaterials", myMaterials);
  materials = { ...structuredClone(builtinMaterials), ...myMaterials };
}
const isBuiltin = (name) => name in builtinMaterials;
function refreshMaterialList(sel) {
  sel = sel || $("#materialSelect");
  const builtin = Object.keys(builtinMaterials).sort(), mine = Object.keys(myMaterials).sort();
  const sig = builtin.join() + "|" + mine.join();
  if (sel.dataset.sig !== sig) {
    const group = (label, names) => el("optgroup", { label },
      ...names.map((n) => el("option", { value: n, textContent: n })));
    sel.replaceChildren(group("Built-in", builtin), ...(mine.length ? [group("My materials", mine)] : []));
    sel.dataset.sig = sig;
  }
  const locked = isBuiltin(S.material);
  for (const b of document.querySelectorAll(".mat-edit")) {
    b.disabled = locked;
    b.title = locked ? "Built-in materials can't be renamed or deleted. Use Save as… to make your own copy." : "";
  }
}
function applyMaterialProfile() {
  const m = materials[S.material];
  if (!m) return;
  for (const k of MATERIAL_KEYS) if (k in m) S[k] = m[k];
}
function materialName(text) {
  // Same rule as engrave_core.material_name: names end up in file names.
  const keep = [...text.trim().toLowerCase().replace(/ /g, "_")].map((c) => /[a-z0-9\-_]/.test(c) ? c : "_").join("");
  return keep.replace(/^_+|_+$/g, "");
}
function resetMaterial() { applyMaterialProfile(); refreshControls(); scheduleRender(); }
function saveMaterial() {
  const suggest = isBuiltin(S.material) ? `my_${S.material}` : S.material;
  const name = materialName(prompt("Name for your material (e.g. maple, black acrylic):", suggest) || "");
  if (!name) return;
  if (isBuiltin(name)) return alert(`'${name}' is a built-in material and can't be changed. Pick another name.`);
  if (myMaterials[name] && !confirm(`'${name}' already exists. Overwrite its settings?`)) return;
  const look = (materials[S.material] || {}).look || ["#d9b38c", "#5a3a22"];
  myMaterials[name] = { ...Object.fromEntries(MATERIAL_KEYS.map((k) => [k, S[k]])), look };
  storeMaterials();
  S.material = name;
  refreshControls();
  LS.set("settings", S);
  setStatus(`Saved material '${name}' in this browser`);
}
function renameMaterial() {
  const old = S.material;
  if (isBuiltin(old)) return;
  const name = materialName(prompt(`New name for '${old}':`, old) || "");
  if (!name || name === old) return;
  if (materials[name]) return alert(`There's already a material called '${name}'.`);
  myMaterials = Object.fromEntries(Object.entries(myMaterials).map(([k, v]) => [k === old ? name : k, v]));
  storeMaterials();
  S.material = name;              // same settings, new name
  refreshControls();
  LS.set("settings", S);
  scheduleRender();
}
function deleteMaterial() {
  const name = S.material;
  if (isBuiltin(name)) return;
  if (!confirm(`Delete your material '${name}'?`)) return;
  delete myMaterials[name];
  storeMaterials();
  change("material", Object.keys(builtinMaterials).includes("wood") ? "wood" : Object.keys(materials).sort()[0]);
  setStatus(`Deleted material '${name}'`);
}

// ============================================================ presets

function allPresets(style) {
  const d = Object.fromEntries(DEF.presets[style].map((k) => [k, DEF.settings[k]]));
  return { Default: d, ...presets[style] };
}
function refreshPresetLists() {
  for (const style of ["photo", "comic"]) {
    const sel = $(`#preset-${style}`), keep = sel.value || LS.get(`preset-${style}`, "Default");
    sel.replaceChildren(...Object.keys(allPresets(style)).map((n) => el("option", { value: n, textContent: n })));
    sel.value = allPresets(style)[keep] ? keep : "Default";
  }
  refreshPresetMarks();
}
function refreshPresetMarks() {
  for (const style of ["photo", "comic"]) {
    const sel = $(`#preset-${style}`);
    if (!sel) continue;
    const vals = allPresets(style)[sel.value] || {};
    const changed = Object.entries(vals).some(([k, v]) =>
      typeof v === "number" ? Math.abs(+S[k] - v) > 1e-6 : S[k] !== v);
    $(`#changed-${style}`).textContent = changed ? "• changed" : "";
  }
}
function applyPreset(style, name) {
  const vals = allPresets(style)[name];
  if (!vals) return;
  Object.assign(S, vals);
  LS.set(`preset-${style}`, name);
  LS.set("settings", S);
  refreshControls();
  scheduleRender();
}
function savePreset(style) {
  const cur = $(`#preset-${style}`).value;
  const name = (prompt(`Name for these ${style} settings (e.g. Portrait, Bold lines):`, cur === "Default" ? "" : cur) || "").trim();
  if (!name) return;
  if (name === "Default") return alert("'Default' is built in. Pick another name.");
  if (presets[style][name] && !confirm(`Preset '${name}' already exists. Replace it with the current settings?`)) return;
  presets[style][name] = Object.fromEntries(DEF.presets[style].map((k) => [k, S[k]]));
  LS.set("presets", presets);
  LS.set(`preset-${style}`, name);
  $(`#preset-${style}`).value = name;
  refreshPresetLists();
  setStatus(`Saved ${style} preset '${name}' in this browser`);
}
function renamePreset(style) {
  const old = $(`#preset-${style}`).value;
  if (old === "Default") return alert("The Default preset can't be renamed.");
  const name = (prompt(`New name for '${old}':`, old) || "").trim();
  if (!name || name === old) return;
  if (name === "Default" || presets[style][name]) return alert(`There's already a preset called '${name}'.`);
  presets[style] = Object.fromEntries(Object.entries(presets[style]).map(([k, v]) => [k === old ? name : k, v]));
  LS.set("presets", presets);
  LS.set(`preset-${style}`, name);
  $(`#preset-${style}`).value = name;
  refreshPresetLists();
}
function deletePreset(style) {
  const name = $(`#preset-${style}`).value;
  if (name === "Default") return alert("The Default preset can't be deleted.");
  if (!confirm(`Delete the ${style} preset '${name}'? Your current slider values stay as they are.`)) return;
  delete presets[style][name];
  LS.set("presets", presets);
  $(`#preset-${style}`).value = "Default";
  LS.set(`preset-${style}`, "Default");
  refreshPresetLists();
}

// ============================================================ files

async function openFiles(list) {
  const images = [...list].filter((f) => f.type.startsWith("image/") || /\.(png|jpe?g|bmp|webp|tiff?)$/i.test(f.name));
  if (!images.length) return;
  let first = null;
  for (const [i, file] of images.entries()) {
    showProgress(`Opening ${file.name} (${i + 1} of ${images.length})…`, null);
    const id = (crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`).replace(/-/g, "");
    const bytes = new Uint8Array(await file.arrayBuffer());
    try {
      const r = await call("open", { id, name: file.name, bytes }, [bytes.buffer]);
      const f = { id, name: file.name, url: URL.createObjectURL(file), w: r.w, h: r.h, crop: blankCrop(), strokes: [] };
      files.push(f);
      first = first || f;
    } catch (e) {
      fail(`Couldn't open ${file.name}: it doesn't look like an image.`);
      continue;
    }
  }
  doneProgress();
  renderFileList();
  if (first) selectFile(first);
}

function renderFileList() {
  const ul = $("#fileList");
  ul.replaceChildren(...files.map((f) => {
    const x = el("button", { className: "x", textContent: "×", title: "Close this photo", type: "button" });
    x.addEventListener("click", (e) => { e.stopPropagation(); closeFile(f); });
    const li = el("li", { className: f === current ? "active" : "", title: `${f.name} (${f.w} × ${f.h} px)` },
      el("img", { src: f.url, alt: "" }), el("span", { className: "name", textContent: f.name }), x);
    li.addEventListener("click", () => selectFile(f));
    return li;
  }));
  $("#filesEmpty").hidden = files.length > 0;
  $("#clearBtn").hidden = files.length < 2;
  $("#downloadBtn").disabled = !current;
  $("#downloadAllBtn").disabled = files.length < 2;
  $("#empty").hidden = files.length > 0;
}

function selectFile(f) {
  current = f;
  views = null;
  viewCache = {};
  needFit = true;
  renderFileList();
  refreshCrop();
  refreshFixbar();
  draw();
  scheduleRender(0);
}

function closeFile(f) {
  const i = files.indexOf(f);
  files.splice(i, 1);
  URL.revokeObjectURL(f.url);
  call("close", { id: f.id }).catch(() => {});
  if (f === current) {
    current = null;
    views = null;
    $("#info").textContent = "";
    $("#estimate").textContent = "";
    if (files.length) selectFile(files[Math.min(i, files.length - 1)]);
  }
  renderFileList();
  refreshCrop();
  refreshFixbar();
  draw();
}

// ============================================================ rendering

let renderTimer = null, busy = false, again = false, needFit = true;
function scheduleRender(delay = 220) {
  clearTimeout(renderTimer);
  renderTimer = setTimeout(renderNow, delay);
}
function jobSettings(f) {
  const s = { ...S, ...f.crop };
  for (const k of INT_FIELDS) s[k] = Math.round(+s[k]);
  return s;
}
async function renderNow() {
  if (!current) return;
  if (busy) { again = true; return; }
  busy = true;
  const f = current, t0 = performance.now();
  showProgress(S.remove_bg && !modelCached ? "Preparing the AI model…" : "Processing…", null);
  try {
    const r = await call("render", { id: f.id, settings: jobSettings(f), strokes: f.strokes });
    if (S.remove_bg) { modelCached = true; refreshHints(); }
    if (f === current) {
      views = r;
      viewCache = {};
      if (needFit) fit();
      draw();
      updateInfo();
      setStatus(`Ready (${((performance.now() - t0) / 1000).toFixed(1)} s)`);
    }
    doneProgress();
  } catch (e) {
    fail(`Couldn't process ${f.name}: ${e.message}`);
  } finally {
    busy = false;
    if (again) { again = false; renderNow(); }
  }
}

function updateInfo() {
  if (!views) return;
  const { w, h } = views, dpi = S.dpi;
  $("#info").textContent = `${views.filename}  ·  ${(w / dpi).toFixed(2)} × ${(h / dpi).toFixed(2)} in  ·  ` +
    `${w} × ${h} px at ${dpi} DPI  ·  laser fires on ${(views.fill * 100).toFixed(0)}% of the area`;
  refreshHints();
  updateEstimate();
}

// Same as engrave_core.estimate_seconds / format_duration.
function updateEstimate() {
  if (!views) { $("#estimate").textContent = ""; return; }
  const [px, rows] = views.stats, speed = +$("#speed").value;
  if (!rows) { $("#estimate").textContent = "nothing to burn"; return; }
  if (!(speed > 0)) { $("#estimate").textContent = ""; return; }
  const sec = Math.round((px * 25.4 / S.dpi + rows * 2 * 2.5) / speed);
  const hh = Math.floor(sec / 3600), mm = Math.floor(sec % 3600 / 60), ss = sec % 60;
  const d = hh ? `${hh} h ${String(mm).padStart(2, "0")} min` : mm ? `${mm} min ${String(ss).padStart(2, "0")} s` : `${ss} s`;
  $("#estimate").textContent = `engrave ≈ ${d}`;
  $("#estimate").title = "Rough estimate: scanned length plus overscan at constant speed. Real jobs usually take a bit longer.";
}

// ============================================================ preview canvas

const canvas = $("#canvas"), ctx = canvas.getContext("2d");
let zoom = 1, panX = 0, panY = 0, mouseX = null;

function hexRGB(h) { const n = parseInt(h.slice(1), 16); return [n >> 16, (n >> 8) & 255, n & 255]; }
// The Cut-out view is the uncropped source photo (up to 1600 px); the others are the engraving.
function viewSize(name = view) {
  return name === "cutout" && views.cutout ? views.cutout : views;
}
function viewCanvas(name) {
  if (viewCache[name]) return viewCache[name];
  if (name === "cutout") {
    const { w, h, rgb } = views.cutout;
    const c = el("canvas", { width: w, height: h }), cx = c.getContext("2d");
    const img = cx.createImageData(w, h), d = img.data;
    for (let i = 0, j = 0; i < w * h; i++, j += 3) {
      d[i * 4] = rgb[j]; d[i * 4 + 1] = rgb[j + 1]; d[i * 4 + 2] = rgb[j + 2]; d[i * 4 + 3] = 255;
    }
    cx.putImageData(img, 0, 0);
    return (viewCache[name] = c);
  }
  const { w, h } = views;
  const c = el("canvas", { width: w, height: h });
  const cx = c.getContext("2d"), img = cx.createImageData(w, h), d = img.data;
  const flip = (name === "look" && views.mirror) || name === "original_flip";
  const src = name === "look" ? views.engraving : name === "original_flip" ? views.original : views[name];
  let lut = null;
  if (name === "look") {
    const look = (materials[S.material] || {}).look || ["#ffffff", "#000000"];
    const [m, b] = [hexRGB(look[0]), hexRGB(look[1])];
    lut = (v) => (v < 128 ? b : m);         // black = laser fires: paint it in the burn color
  }
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      const v = src[y * w + (flip ? w - 1 - x : x)], o = (y * w + x) * 4;
      if (lut) { const c3 = lut(v); d[o] = c3[0]; d[o + 1] = c3[1]; d[o + 2] = c3[2]; }
      else { d[o] = d[o + 1] = d[o + 2] = v; }
      d[o + 3] = 255;
    }
  }
  cx.putImageData(img, 0, 0);
  return (viewCache[name] = c);
}

function draw() {
  const dpr = window.devicePixelRatio || 1;
  const W = canvas.clientWidth, H = canvas.clientHeight;
  if (canvas.width !== Math.round(W * dpr) || canvas.height !== Math.round(H * dpr)) {
    canvas.width = Math.round(W * dpr);
    canvas.height = Math.round(H * dpr);
  }
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  if (!views || !current) return;
  if (view === "cutout" && !views.cutout) return;
  const dots = view === "engraving" || view === "look";
  ctx.imageSmoothingEnabled = !(dots && zoom * dpr >= 1);
  ctx.imageSmoothingQuality = "high";
  ctx.setTransform(dpr * zoom, 0, 0, dpr * zoom, dpr * panX, dpr * panY);
  ctx.drawImage(viewCanvas(view), 0, 0);
  if (view === "cutout") {
    drawStrokes();
    drawBrush(dpr);
    return;
  }
  if ($("#compare").checked && view !== "original" && mouseX != null) {
    // Original left of the divider, lined up with the shown view (mirrored views flip it too).
    const flip = views.mirror && view !== "look";
    const split = (mouseX - panX) / zoom;
    if (split > 0) {
      ctx.save();
      ctx.beginPath();
      ctx.rect(0, 0, Math.min(split, views.w), views.h);
      ctx.clip();
      ctx.imageSmoothingEnabled = true;
      ctx.drawImage(viewCanvas(flip ? "original_flip" : "original"), 0, 0);
      ctx.restore();
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = "#2563eb";
    ctx.fillRect(mouseX - 1, 0, 2, H);
  }
}

function fit() {
  if (!views) return;
  const W = canvas.clientWidth, H = canvas.clientHeight, { w, h } = viewSize();
  if (!W || !H) { needFit = true; return; }      // not laid out yet (hidden): fit when shown
  needFit = false;
  zoom = Math.min(W / w, H / h) * 0.94;
  panX = (W - w * zoom) / 2;
  panY = (H - h * zoom) / 2;
  draw();
}

// --- Fix strokes on the Cut-out view (same tools and stroke format as the desktop app)
const STROKE_COLORS = { keep: "#22c55e", remove: "#ef4444", paint_keep: "#38bdf8", paint_remove: "#f59e0b" };
let tool = "pan", live = null, mouse = null, spaceDown = false;
const brush = $("#brush");
brush.value = LS.get("brush", 30);

function drawing() {
  return tool !== "pan" && view === "cutout" && views && views.cutout && S.remove_bg && current && zoom > 0;
}
function toSrc(sx, sy) {           // screen (CSS px in the canvas) -> source-image pixels
  const f = views.cutout.factor;
  return [+((sx - panX) / zoom * f).toFixed(1), +((sy - panY) / zoom * f).toFixed(1)];
}
function strokePath(kind, radius, pts) {
  // Drawn in view pixels (the transform is already applied).
  const f = views.cutout.factor;
  ctx.strokeStyle = ctx.fillStyle = STROKE_COLORS[kind];
  ctx.lineWidth = Math.max(2 / zoom, 2 * radius / f);
  ctx.lineCap = ctx.lineJoin = "round";
  ctx.beginPath();
  ctx.moveTo(pts[0][0] / f, pts[0][1] / f);
  if (pts.length === 1) ctx.lineTo(pts[0][0] / f + 0.01, pts[0][1] / f);
  for (const [x, y] of pts.slice(1)) ctx.lineTo(x / f, y / f);
  ctx.stroke();
}
function drawStrokes() {
  ctx.save();
  ctx.globalAlpha = 0.4;
  for (const [kind, radius, pts] of current.strokes) strokePath(kind, radius, pts);
  if (live) {
    ctx.globalAlpha = 0.6;
    strokePath(live.kind, brushRadius(), live.pts);
  }
  ctx.restore();
}
function brushRadius() { return +(brush.value / 2 / zoom * views.cutout.factor).toFixed(1); }
function drawBrush(dpr) {
  if (!mouse || !drawing()) return;
  const r = Math.max(1, brush.value / 2);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.lineWidth = 1;
  ctx.strokeStyle = "#fff";
  ctx.beginPath(); ctx.arc(mouse[0], mouse[1], r + 1, 0, 7); ctx.stroke();
  ctx.lineWidth = 1.5;
  ctx.strokeStyle = STROKE_COLORS[tool];
  ctx.beginPath(); ctx.arc(mouse[0], mouse[1], r, 0, 7); ctx.stroke();
}
function setTool(t) {
  if (t !== "pan" && !(S.remove_bg && current)) return;      // fix tools need Remove background
  tool = t;
  for (const b of document.querySelectorAll("#tools button")) b.classList.toggle("active", b.dataset.tool === t);
  if (t !== "pan" && view !== "cutout") setView("cutout");
  canvas.classList.toggle("drawing", t !== "pan");
  draw();
}
function strokesChanged() {
  refreshFixbar();
  scheduleRender(0);
}
function undoStroke() {
  if (current && current.strokes.length) { current.strokes.pop(); strokesChanged(); }
}
function clearStrokes() {
  if (current && current.strokes.length && confirm("Remove all background fixes on this photo?")) {
    current.strokes = [];
    strokesChanged();
  }
}
function refreshFixbar() {
  const on = !!(S.remove_bg && current);
  $("#fixbar").hidden = !on;
  $("#cutoutBtn").hidden = !S.remove_bg;
  if (!S.remove_bg && view === "cutout") setView("engraving");
  if (!on && tool !== "pan") setTool("pan");
  const n = current ? current.strokes.length : 0;
  $("#fixCount").textContent = `${n} fix${n === 1 ? "" : "es"} ⓘ`;
  $("#undoBtn").disabled = $("#clearFixBtn").disabled = !n;
}
for (const b of document.querySelectorAll("#tools button")) b.addEventListener("click", () => setTool(b.dataset.tool));
$("#undoBtn").addEventListener("click", undoStroke);
$("#clearFixBtn").addEventListener("click", clearStrokes);
brush.addEventListener("input", () => { LS.set("brush", +brush.value); draw(); });
function zoomAt(sx, sy, factor) {
  const nz = Math.min(64, Math.max(0.02, zoom * factor));
  panX = sx - (sx - panX) * (nz / zoom);
  panY = sy - (sy - panY) * (nz / zoom);
  zoom = nz;
  draw();
}

canvas.addEventListener("wheel", (e) => {
  e.preventDefault();
  const r = canvas.getBoundingClientRect();
  zoomAt(e.clientX - r.left, e.clientY - r.top, Math.pow(1.0015, -e.deltaY));
}, { passive: false });
let drag = null;
canvas.addEventListener("contextmenu", (e) => e.preventDefault());
canvas.addEventListener("pointerdown", (e) => {
  canvas.setPointerCapture(e.pointerId);
  const r = canvas.getBoundingClientRect(), sx = e.clientX - r.left, sy = e.clientY - r.top;
  if (e.button === 0 && !spaceDown && drawing()) {
    live = { kind: tool, sx, sy, pts: [toSrc(sx, sy)] };
    draw();
    return;
  }
  drag = { x: e.clientX, y: e.clientY, px: panX, py: panY };       // left drag, right drag, Space
  canvas.classList.add("panning");
});
canvas.addEventListener("pointermove", (e) => {
  const r = canvas.getBoundingClientRect(), sx = e.clientX - r.left, sy = e.clientY - r.top;
  mouseX = sx;
  mouse = [sx, sy];
  if (live) {
    if (Math.abs(sx - live.sx) + Math.abs(sy - live.sy) >= 3) {
      live.sx = sx; live.sy = sy;
      live.pts.push(toSrc(sx, sy));
    }
  } else if (drag) {
    panX = drag.px + e.clientX - drag.x;
    panY = drag.py + e.clientY - drag.y;
  }
  if (live || drag || $("#compare").checked || drawing()) draw();
});
canvas.addEventListener("pointerup", () => {
  if (live) {
    current.strokes.push([live.kind, brushRadius(), live.pts]);
    live = null;
    strokesChanged();
  }
  drag = null;
  canvas.classList.remove("panning");
});
canvas.addEventListener("pointerleave", () => { mouseX = null; mouse = null; draw(); });
canvas.addEventListener("dblclick", fit);
new ResizeObserver(() => (needFit && views ? fit() : draw())).observe(canvas);

$("#zoomFit").addEventListener("click", fit);
$("#zoomIn").addEventListener("click", () => zoomAt(canvas.clientWidth / 2, canvas.clientHeight / 2, 1.25));
$("#zoomOut").addEventListener("click", () => zoomAt(canvas.clientWidth / 2, canvas.clientHeight / 2, 0.8));
$("#zoom100").addEventListener("click", () => zoomAt(canvas.clientWidth / 2, canvas.clientHeight / 2, 1 / (window.devicePixelRatio || 1) / zoom));
$("#compare").addEventListener("change", draw);

function setView(v) {
  if (v === "cutout" && !S.remove_bg) v = "engraving";
  const refit = (v === "cutout") !== (view === "cutout");   // different framing: fit again
  view = v;
  if (v !== "cutout") LS.set("view", v);
  for (const b of document.querySelectorAll("#views button")) b.classList.toggle("active", b.dataset.view === v);
  if (v !== "cutout" && tool !== "pan") setTool("pan");
  if (refit && views) fit(); else draw();
}
for (const b of document.querySelectorAll("#views button")) b.addEventListener("click", () => setView(b.dataset.view));

// ============================================================ downloads

function saveBlob(blob, name) {
  const a = el("a", { href: URL.createObjectURL(blob), download: name });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 4000);
}
async function exportFile(f) {
  const r = await call("export", { id: f.id, settings: jobSettings(f), strokes: f.strokes });
  saveBlob(new Blob([r.png], { type: "image/png" }), r.filename);
  if (r.svg) saveBlob(new Blob([r.svg], { type: "image/svg+xml" }), r.filename.replace(/\.png$/, "_outline.svg"));
  return r;
}
$("#downloadBtn").addEventListener("click", async () => {
  if (!current) return;
  showProgress("Making the engraving…", null);
  try {
    const r = await exportFile(current);
    doneProgress();
    setStatus(`Downloaded ${r.filename}${r.svg_error ? ` (no outline: ${r.svg_error})` : ""}`);
  } catch (e) { fail(`Download failed: ${e.message}`); }
});
$("#downloadAllBtn").addEventListener("click", async () => {
  try {
    for (const [i, f] of files.entries()) {
      showProgress(`Making engraving ${i + 1} of ${files.length}: ${f.name}…`, i / files.length);
      await exportFile(f);
      await new Promise((r) => setTimeout(r, 300));     // browsers drop downloads fired too fast
    }
    doneProgress();
    setStatus(`Downloaded ${files.length} engravings (your browser may ask to allow multiple downloads)`);
  } catch (e) { fail(`Download failed: ${e.message}`); }
});

// ============================================================ page wiring

$("#fileInput").addEventListener("change", (e) => { openFiles(e.target.files); e.target.value = ""; });
$("#clearBtn").addEventListener("click", () => { if (confirm("Close all photos?")) [...files].forEach(closeFile); });
let dragDepth = 0;
window.addEventListener("dragenter", (e) => { if ([...e.dataTransfer.types].includes("Files")) { dragDepth++; document.body.classList.add("dragging"); } });
window.addEventListener("dragleave", () => { if (--dragDepth <= 0) { dragDepth = 0; document.body.classList.remove("dragging"); } });
window.addEventListener("dragover", (e) => e.preventDefault());
window.addEventListener("drop", (e) => {
  e.preventDefault();
  dragDepth = 0;
  document.body.classList.remove("dragging");
  if (DEF) openFiles(e.dataTransfer.files);
});
window.addEventListener("paste", (e) => { if (DEF && e.clipboardData) openFiles(e.clipboardData.files); });

for (const t of document.querySelectorAll(".tab")) {
  t.addEventListener("click", () => {
    for (const x of document.querySelectorAll(".tab")) x.classList.toggle("active", x === t);
    for (const p of document.querySelectorAll(".pane")) p.classList.toggle("active", p.id === `pane-${t.dataset.tab}`);
    LS.set("tab", t.dataset.tab);
  });
}

const THEMES = ["system", "light", "dark"];
function applyTheme(t) {
  if (t === "system") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = t;
  $("#themeBtn").title = `Theme: ${t} (click to change)`;
}
$("#themeBtn").addEventListener("click", () => {
  const t = THEMES[(THEMES.indexOf(LS.get("theme", "system")) + 1) % THEMES.length];
  LS.set("theme", t);
  applyTheme(t);
});
applyTheme(LS.get("theme", "system"));

$("#helpBtn").addEventListener("click", () => $("#help").showModal());
const speed = $("#speed");
speed.value = LS.get("speed", 300);
speed.addEventListener("change", () => { LS.set("speed", +speed.value); updateEstimate(); });

// Same keys as the desktop app's defaults where they exist.
window.addEventListener("keydown", (e) => {
  if (e.target instanceof Element && e.target.matches("input[type=number], input[type=text], select, textarea")) return;
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z") { e.preventDefault(); undoStroke(); return; }
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  const viewKeys = { "1": "original", "2": "adjusted", "3": "engraving", "4": "look", "5": "cutout" };
  const toolKeys = { k: "keep", r: "remove", b: "paint_keep", e: "paint_remove", p: "pan" };
  const k = e.key.toLowerCase();
  if (viewKeys[e.key]) setView(viewKeys[e.key]);
  else if (toolKeys[k]) setTool(toolKeys[k]);
  else if (e.key === "[" || e.key === "]") {
    brush.value = Math.min(100, Math.max(2, Math.round(brush.value * (e.key === "]" ? 1.2 : 1 / 1.2))));
    LS.set("brush", +brush.value);
    draw();
  } else if (e.key === " ") { e.preventDefault(); spaceDown = true; canvas.classList.add("panning"); }
  else if (e.key === "Escape") setTool("pan");
  else if (k === "f" || e.key === "0") fit();
  else if (e.key === "+" || e.key === "=") $("#zoomIn").click();
  else if (e.key === "-") $("#zoomOut").click();
});
window.addEventListener("keyup", (e) => {
  if (e.key === " ") { spaceDown = false; canvas.classList.remove("panning"); }
});

// ============================================================ start

(async () => {
  try {
    const { defaults, materials: builtin } = await call("init");
    DEF = defaults;
    loadMaterials(builtin);
    presets = Object.assign({ photo: {}, comic: {} }, LS.get("presets", {}));
    S = { ...DEF.settings, ...LS.get("settings", {}) };
    for (const k of CROP_KEYS) S[k] = DEF.settings[k];        // crop/rotate live on each photo
    if (!materials[S.material]) { S.material = Object.keys(materials).sort()[0]; applyMaterialProfile(); }
    buildBasic($("#pane-basic"));
    buildPhoto($("#pane-photo"));
    buildComic($("#pane-comic"));
    refreshPresetLists();
    refreshControls();
    const tab = document.querySelector(`.tab[data-tab="${LS.get("tab", "basic")}"]`);
    if (tab) tab.click();
    setView(view);
    $("#versionLine").textContent = `Laser Engraver ${DEF.version} (web). Same processing engine as the desktop app.`;
    modelCached = await call("hasModel").catch(() => false);
    refreshHints();
    refreshFixbar();
    $("#loading").classList.add("hidden");
    renderFileList();
    setStatus("Ready. Open photos to start.");
  } catch (e) {
    $("#loadText").textContent = `Couldn't start: ${e.message}. Check your internet connection and reload the page.`;
    $("#loadText").className = "error";
  }
})();
