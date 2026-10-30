// tour.js: the tutorial, built like ToolTrace's. Each step dims the page, spotlights one control
// and explains it in a small card (End / Back / Next). The page stays usable underneath, so people
// can try things as they go. It can load a sample photo, and starts by itself on the first visit;
// the Tutorial button replays it. In Design mode it's the Design-mode tour (DESIGN_STEPS).
// Uses app.js globals: files, openFiles, selectFile, closeFile, setView, view, S, LS, el, fail,
// and design.js's DM.
"use strict";

(function () {
  const q = (sel) => document.querySelector(sel);
  const SAMPLE = { url: "samples/astronaut.jpg", name: "sample-astronaut.jpg" };
  const sec = (slug) => `#pane-basic [data-sec="${slug}"]`;
  const basicTab = () => q('.tab[data-tab="basic"]').click();

  // ---------- Steps ----------
  // target: selector (or function returning one); parent: use target.closest(parent);
  // before(): set the page up for the step; noNext: the step's own buttons move on.
  const STEPS = [
    { title: "Welcome to Laser Engraver", noNext: true, html:
      "<p>Turn a photo into a file for your laser, in four steps:</p>" +
      "<ol><li><b>Open</b> a photo</li><li><b>Pick</b> the style, material and size</li>" +
      "<li><b>Check</b> the preview (and fix the background if you removed it)</li>" +
      "<li><b>Download</b> the engraving for your laser software</li></ol>" +
      '<div class="tour-acts"><button class="btn primary" data-act="sample">Try it with a sample photo</button>' +
      '<button class="btn" data-act="next">Use my own photo</button></div>' },
    { target: "#openBtn", title: "Open your photos", html:
      "<ul><li>Click <b>Open photos</b>, or drop photos anywhere on the page (<kbd>Ctrl</kbd>+<kbd>V</kbd> pastes one).</li>" +
      "<li>Open several at once; the settings apply to all of them.</li>" +
      "<li>Nothing is uploaded: your photos stay on this computer.</li></ul>" },
    { target: ".files", title: "Your photos", html:
      "<ul><li>Click a photo to work on it; <b>×</b> closes it.</li>" +
      "<li>Crop, rotation and background fixes belong to each photo.</li></ul>" },
    { target: sec("style-material"), before: basicTab, title: "Style and material", html:
      "<ul><li><b>Photo</b>: realistic shading in fine dots. <b>Comic</b>: flat tones, halftone dots and bold ink lines (burns faster).</li>" +
      "<li>The <b>material</b> sets contrast, brightness, gamma, invert and mirror for that surface.</li>" +
      "<li>Built-in materials are locked; <b>Save as…</b> keeps your own version.</li></ul>" },
    { target: sec("size-resolution"), before: basicTab, title: "Size and resolution", html:
      "<ul><li>Type the <b>width</b> (or pick <b>Height</b>) in inches; the other side follows the photo.</li>" +
      "<li><b>DPI</b> is the line spacing: 254 DPI = 0.1 mm. Use the same value in your laser software.</li>" +
      "<li><b>Dither</b>: stucki for the most detail, floyd-steinberg for portraits, bayer for wood grain.</li></ul>" },
    { target: "#stage", before: () => setView("engraving"), title: "The engraving preview", html:
      "<ul><li>Black is where the laser fires.</li>" +
      "<li>Scroll to zoom, drag to pan, double-click to fit.</li>" +
      "<li><b>1:1</b> (top right) shows one laser dot per screen pixel.</li></ul>" },
    { target: "#views", title: "Views", html:
      "<ul><li><b>Original</b>: your photo. <b>Adjusted</b>: after the tone settings.</li>" +
      "<li><b>Engraving</b>: the real dots. <b>Material look</b>: in the material's colors.</li>" +
      "<li>Keys <kbd>1</kbd> to <kbd>5</kbd> switch views.</li></ul>" },
    { target: "#compare", parent: "label", title: "Before and after", html:
      "<ul><li>Tick <b>Compare</b> to see the original left of a divider that follows the mouse.</li>" +
      "<li>Works on the Adjusted, Engraving and Material look views.</li></ul>" },
    { target: sec("tone"), before: basicTab, title: "Tone", html:
      "<ul><li><b>Contrast</b> pushes tones apart; higher <b>brightness</b> burns less; <b>gamma</b> above 1 lightens midtones.</li>" +
      "<li><b>Invert</b>: for materials where the laser makes light marks (slate, black acrylic).</li>" +
      "<li><b>Mirror</b>: for engraving the back of clear acrylic.</li></ul>" },
    { target: sec("background"), before: basicTab, title: "Remove the background", html:
      "<ul><li>Tick <b>Remove background (AI)</b> to cut out the subject, so the laser skips the background.</li>" +
      "<li>The first time, the AI model downloads once (176 MB); then it takes a few seconds per photo.</li></ul>" },
    { target: () => (q("#fixbar").hidden ? sec("background") : "#fixbar"),
      before: () => { basicTab(); if (S.remove_bg && files.length) setView("cutout"); },
      title: "Fix the cut-out", html: () =>
      (q("#fixbar").hidden ? "<p>With Remove background on, these tools appear above the preview:</p>" : "") +
      "<ul><li>The <b>Cut-out</b> view tints removed areas red.</li>" +
      "<li><b>Smart keep</b> (<kbd>K</kbd>): paint on missing subject. <b>Smart remove</b> (<kbd>R</kbd>): paint on leftover background. Keep only adds; remove only takes away.</li>" +
      "<li><b>Brush keep / erase</b> (<kbd>B</kbd> / <kbd>E</kbd>): exactly what you paint, for touch-ups.</li>" +
      "<li><kbd>[</kbd> <kbd>]</kbd> brush size, <kbd>Ctrl</kbd>+<kbd>Z</kbd> undo.</li></ul>" },
    { target: "#cropSection", before: () => { basicTab(); if (view === "cutout") setView("engraving"); },
      title: "Crop and rotate", html:
      "<ul><li>Turn 90° at a time, straighten a tilted photo, or trim each edge by a percentage.</li>" +
      "<li>Only affects the selected photo.</li></ul>" },
    { target: ".tabs", title: "Tuning tabs and presets", html:
      "<ul><li><b>Photo tuning</b>: shadow recovery, local contrast, sharpening.</li>" +
      "<li><b>Comic tuning</b>: tone bands, dot spacing, ink lines.</li>" +
      "<li>Save a look you like as a <b>preset</b> (kept in this browser).</li></ul>" },
    { target: sec("extras"), before: basicTab, title: "Extras", html:
      "<ul><li><b>Also download cut outline</b>: a red cut line around the subject, as an SVG.</li>" +
      "<li><b>Make test card</b>: a 3×3 grid of the photo at different contrast and gamma. Burn it on scrap and copy the best tile's numbers.</li></ul>" },
    { target: "#downloadBtn", title: "Download", html:
      "<ul><li><b>Download PNG</b> (<kbd>Ctrl</kbd>+<kbd>S</kbd>) saves the selected photo's engraving; <b>Download all</b> does every photo.</li>" +
      "<li>The file name includes the material, size and DPI.</li></ul>" },
    { target: "footer.status", title: "Job time", html:
      "<ul><li>The estimate is the rough engraving (and cutting) time.</li>" +
      "<li>Type your laser's engrave and cut speeds (mm/s) so it fits your machine.</li></ul>" },
    { target: "#modeSeg", title: "Design mode", html:
      "<ul><li>For layouts with several photos, logos, SVG / DXF files, text and shapes, plus cut and score lines, switch to <b>Design</b>.</li>" +
      "<li>The <b>Tutorial</b> button there tours Design mode.</li></ul>" },
    { title: "You're set", html:
      "<ul><li>In your laser software, engrave at the same DPI (254 = 0.1 mm line interval) and <b>turn its own dithering off</b>, or the dots get blotchy.</li>" +
      "<li>Burn a small test on scrap first.</li>" +
      "<li><b>Tutorial</b> (top right) replays this tour.</li></ul>" +
      '<p class="tour-note" id="tourSampleNote" hidden>The sample photo is closed when the tour ends.</p>' },
  ];

  // Design mode's tour (Tutorial in Design mode). Uses design.js's DM.
  const dsec = (slug) => `#pane-design [data-sec="${slug}"]`;
  const tab = (name) => () => q(`.tab[data-tab="${name}"]`).click();
  const DESIGN_STEPS = [
    { title: "Design mode", noNext: true, html:
      "<p>Lay out several items on one canvas and save one engraving plus cut and score lines, ready for your laser software:</p>" +
      "<ol><li><b>Set</b> the canvas (your material)</li><li><b>Add</b> photos, logos, SVG / DXF files, text and shapes</li>" +
      "<li><b>Arrange</b> them and choose engrave, score or cut</li><li><b>Save</b> the engraving and line files</li></ol>" +
      '<div class="tour-acts"><button class="btn primary" data-act="dsample">Try it with a sample photo and text</button>' +
      '<button class="btn" data-act="next">Use my own design</button></div>' },
    { target: "#modeSeg", title: "Photos or Design", html:
      "<ul><li><b>Photos</b>: one photo at a time, photo or comic style.</li>" +
      "<li><b>Design</b>: a layout of several items. Each mode keeps its own settings.</li></ul>" },
    { target: dsec("canvas-your-material"), before: tab("design"), title: "The canvas", html:
      "<ul><li>Width and height are your material's size; <b>Preset</b> has common ones.</li>" +
      "<li><b>Resize from</b>: what stays put when you drag a corner (hold <kbd>Ctrl</kbd> for the other one).</li></ul>" },
    { target: dsec("material-output"), before: tab("design"), title: "Material and DPI", html:
      "<ul><li>Material, DPI, invert and mirror apply to the whole design.</li>" +
      "<li>Mirror flips the engraving and the cut lines together, for the back of clear acrylic.</li></ul>" },
    { target: ".layers", title: "Layers and Add", html:
      "<ul><li><b>Add</b>: photo, logo (PNG), vector file (SVG, DXF), text, arc text, or a rectangle, circle or line that you drag out on the canvas (<kbd>Esc</kbd> cancels).</li>" +
      "<li>The list is top first: ▲ / ▼ change the order, plus Copy, Hide and Delete.</li>" +
      "<li>You can also drop photos and SVG / DXF files on the page.</li></ul>" },
    { target: "#stage", before: () => setView("layout"), title: "Arrange", html:
      "<ul><li>Drag to move: edges and centers snap (pink lines); <kbd>Alt</kbd> moves freely.</li>" +
      "<li>Corner squares resize; the circle on top rotates (<kbd>Shift</kbd> = 15° steps).</li>" +
      "<li><kbd>Ctrl</kbd>+click picks the item underneath; arrow keys nudge.</li>" +
      "<li>Wheel zooms; drag empty space to pan.</li></ul>" },
    { target: "#designViews", title: "Layout and Design views", html:
      "<ul><li><b>Layout</b> is fast for arranging: photos are shown as plain pictures.</li>" +
      "<li><b>Design</b> shows the real engraving in the material's colors.</li>" +
      "<li>Red lines are cuts, blue lines are scores.</li></ul>" },
    { target: "#pane-layer", before: () => {
      if (!DM.selected && DM.design && DM.design.layers.length) DM.select(DM.design.layers[DM.design.layers.length - 1].id);
      tab("layer")();
    }, title: "The Layer tab", html:
      "<ul><li><b>Output</b>: <b>Engrave</b> it, <b>Score</b> a light line along it, or <b>Cut</b> along it.</li>" +
      "<li>Exact position, size and rotation, plus the item's own settings (text, font, shape…).</li>" +
      "<li>Score / cut items can be <b>welded</b>, reduced to their <b>outer outline</b>, or <b>offset</b>.</li>" +
      "<li>SVG / DXF files choose a job for each color instead.</li></ul>" },
    { target: ".tabs", title: "Photo layers", html:
      "<ul><li>Select a photo layer and the <b>Basic</b>, <b>Photo tuning</b> and <b>Comic tuning</b> tabs set that photo's style, tone and background removal.</li>" +
      "<li>Its own views (Original, Cut-out, Engraving…) and the background fix tools work as in Photos mode.</li></ul>" },
    { target: dsec("export"), before: tab("design"), title: "Export options", html:
      "<ul><li><b>Cut / score files</b>: SVG and / or DXF, one file per job.</li>" +
      "<li><b>Alignment frame</b>: a green canvas-sized box in those files, to line them up with the PNG.</li>" +
      "<li><b>All-in-one file</b>: image and lines in one SVG or PDF (LightBurn, Trotec Ruby, xTool).</li>" +
      "<li><b>Save test copy</b>: the whole design shrunk, to try on scrap.</li></ul>" },
    { target: "#exportBtn", title: "Save engraving", html:
      "<ul><li>Downloads the engraving PNG and the <code>_cut</code> / <code>_score</code> / <code>_combined</code> files you chose.</li>" +
      "<li>Your browser may ask once to allow several downloads.</li></ul>" },
    { target: "#saveProjectBtn", title: "Projects", html:
      "<ul><li>Your design is kept in this browser automatically, even after a reload.</li>" +
      "<li><b>Save project</b> (<kbd>Ctrl</kbd>+<kbd>S</kbd>) writes a <code>.lep</code> file that also opens in the desktop app; <b>Open…</b> loads one.</li>" +
      "<li><b>Clear saved design</b> (Design tab) forgets the browser's copy.</li></ul>" },
    { target: "footer.status", title: "Job time", html:
      "<ul><li>Engraving and cutting time from your engrave and cut speeds.</li>" +
      "<li>The engraving part updates when you look at the Design view.</li></ul>" },
    { title: "You're set", html:
      "<ul><li><kbd>Ctrl</kbd>+<kbd>Z</kbd> / <kbd>Ctrl</kbd>+<kbd>Y</kbd> undo and redo; <kbd>Ctrl</kbd>+<kbd>D</kbd> duplicates; <kbd>Delete</kbd> removes.</li>" +
      "<li>In your laser software, engrave the PNG at the same DPI with its own dithering off.</li>" +
      "<li><b>Tutorial</b> (top right) replays this tour.</li></ul>" +
      '<p class="tour-note" id="tourSampleNote" hidden>The sample photo and text are removed when the tour ends.</p>' },
  ];
  let steps = STEPS, sampleLayers = [];

  // ---------- Engine ----------

  let dim, hi, card, idx = 0, open = false, usedSample = false, saved = null;
  const val = (v) => (typeof v === "function" ? v() : v);

  function build() {
    dim = el("div", { id: "tourDim" });
    hi = el("div", { id: "tourHi" });
    card = el("div", { id: "tourCard", role: "dialog" });
    card.innerHTML = '<div class="tour-step"></div><h3></h3><div class="tour-body"></div>' +
      '<div class="tour-nav"><button class="btn sm" data-act="end">End</button><span></span>' +
      '<button class="btn sm" data-act="back">Back</button><button class="btn sm primary" data-act="next">Next</button></div>';
    document.body.append(dim, hi, card);
    card.addEventListener("click", (e) => {
      const act = e.target.dataset && e.target.dataset.act;
      if (act === "next") go(idx + 1);
      else if (act === "back") go(idx - 1);
      else if (act === "end") end();
      else if (act === "sample") loadSample(e.target);
      else if (act === "dsample") loadDesignSample(e.target);
    });
    window.addEventListener("resize", () => open && place());
    window.addEventListener("scroll", () => open && place(false), true);
  }

  async function loadSample(btn) {
    btn.disabled = true;
    btn.textContent = "Loading the sample…";
    try {
      const have = files.find((f) => f.name === SAMPLE.name);
      if (have) selectFile(have);
      else {
        const r = await fetch(SAMPLE.url);
        if (!r.ok) throw new Error(`status ${r.status}`);
        await openFiles([new File([await r.blob()], SAMPLE.name, { type: "image/jpeg" })]);
        usedSample = true;
      }
    } catch (err) {
      fail(`Couldn't load the sample photo: ${err.message}`);
    }
    if (open) go(idx + 1);
  }

  // Design tour: a sample photo and a text layer, removed again when the tour ends.
  async function loadDesignSample(btn) {
    btn.disabled = true;
    btn.textContent = "Adding the sample…";
    try {
      const before = new Set(DM.design.layers.map((l) => l.id));
      const r = await fetch(SAMPLE.url);
      if (!r.ok) throw new Error(`status ${r.status}`);
      await DM.addFiles([new File([await r.blob()], SAMPLE.name, { type: "image/jpeg" })]);
      DM.addSimple("text");
      sampleLayers = DM.design.layers.filter((l) => !before.has(l.id)).map((l) => l.id);
      usedSample = sampleLayers.length > 0;
    } catch (err) {
      fail(`Couldn't load the sample: ${err.message}`);
    }
    if (open) go(idx + 1);
  }

  function targetEl(st) {
    if (!st.target) return null;
    let t = q(val(st.target));
    if (t && st.parent) t = t.closest(st.parent) || t;
    if (!t || t.hidden || !t.getClientRects().length) return null;
    return t;
  }

  function go(i) {
    if (i < 0) return;
    if (i >= steps.length) { end(); return; }
    idx = i;
    const st = steps[i];
    try { if (st.before) st.before(); } catch (e) { console.error(e); }
    card.querySelector(".tour-step").textContent = `Step ${i + 1} of ${steps.length}`;
    card.querySelector("h3").textContent = st.title;
    card.querySelector(".tour-body").innerHTML = val(st.html);
    const nav = card.querySelector(".tour-nav");
    nav.querySelector('[data-act="back"]').disabled = i === 0;
    nav.querySelector('[data-act="next"]').hidden = !!st.noNext;
    nav.querySelector('[data-act="next"]').textContent = i === steps.length - 1 ? "Finish" : "Next";
    const note = q("#tourSampleNote");
    if (note) note.hidden = !usedSample;
    place();
    setTimeout(() => open && place(), 350);      // layout can shift a moment later (renders, tabs)
    setTimeout(() => open && place(), 900);
  }

  // Spotlight the step's control (scrolled into view) and put the card beside it, or centre the
  // card when there's nothing to point at.
  function place(scroll = true) {
    const st = steps[idx], t = targetEl(st);
    if (t && scroll) t.scrollIntoView({ block: "nearest", inline: "nearest" });
    const vw = window.innerWidth, vh = window.innerHeight, cw = card.offsetWidth, ch = card.offsetHeight, pad = 6;
    if (!t) {                                   // nothing to point at: dim everything, card centred
      dim.style.clipPath = "none";
      hi.style.display = "none";
      card.style.left = `${Math.max(12, (vw - cw) / 2)}px`;
      card.style.top = `${Math.max(12, (vh - ch) / 2)}px`;
      return;
    }
    const r = t.getBoundingClientRect();
    const x0 = r.left - pad, y0 = r.top - pad, x1 = r.right + pad, y1 = r.bottom + pad;
    hi.style.cssText = `display:block;left:${x0}px;top:${y0}px;width:${x1 - x0}px;height:${y1 - y0}px`;
    // The dim layer with a hole cut out over the target.
    dim.style.clipPath = `polygon(evenodd, 0 0, 100% 0, 100% 100%, 0 100%, 0 0, ${x0}px ${y0}px, ` +
      `${x1}px ${y0}px, ${x1}px ${y1}px, ${x0}px ${y1}px, ${x0}px ${y0}px)`;
    let x, y;
    if (r.width > vw * 0.45 && r.height > vh * 0.35) { x = r.left + 16; y = r.top + 16; }  // the preview: card in its corner
    else if (r.right + 18 + cw < vw) { x = r.right + 18; y = r.top; }
    else if (r.left - 18 - cw > 0) { x = r.left - 18 - cw; y = r.top; }
    else if (r.bottom + 14 + ch < vh) { x = r.left; y = r.bottom + 14; }
    else { x = r.left; y = r.top - 14 - ch; }
    card.style.left = `${Math.max(12, Math.min(vw - cw - 12, x))}px`;
    card.style.top = `${Math.max(12, Math.min(vh - ch - 12, y))}px`;
  }

  function start() {
    if (!dim) build();
    const help = q("#help");
    if (help.open) help.close();
    steps = DM.active ? DESIGN_STEPS : STEPS;       // the tour of the mode you're in
    window.tour.steps = steps;
    usedSample = false;
    sampleLayers = [];
    saved = { view, compare: q("#compare").checked };
    open = true;
    document.body.classList.add("touring");
    dim.hidden = hi.hidden = card.hidden = false;
    go(0);
  }

  function end() {
    if (!open) return;
    open = false;
    document.body.classList.remove("touring");
    dim.hidden = hi.hidden = card.hidden = true;
    // Put things back: the sample photo goes (photos you opened stay), and the view you had.
    if (usedSample && steps === DESIGN_STEPS) {
      const gone = new Set(sampleLayers);
      if (DM.design && DM.design.layers.some((l) => gone.has(l.id))) {
        DM.design.layers = DM.design.layers.filter((l) => !gone.has(l.id));
        DM.select(DM.design.layers.length ? DM.design.layers[DM.design.layers.length - 1].id : null);
        DM.changed();
      }
    } else if (usedSample) {
      const f = files.find((x) => x.name === SAMPLE.name);
      if (f) closeFile(f);
    }
    if (saved) {
      q("#compare").checked = saved.compare;
      setView(saved.view);
      saved = null;
    }
    LS.set("tourDone", true);
  }

  // Esc ends, arrows / Enter step through. Other keys go to the page as usual.
  document.addEventListener("keydown", (e) => {
    if (!open) return;
    if (e.key === "Escape") end();
    else if (e.key === "ArrowRight" || (e.key === "Enter" && !e.target.closest("button"))) {
      if (!steps[idx].noNext) go(idx + 1);
    } else if (e.key === "ArrowLeft") go(idx - 1);
    else return;
    e.preventDefault();
    e.stopPropagation();
  }, true);

  window.tour = { start, end, steps, designSteps: DESIGN_STEPS };
  q("#tourBtn").addEventListener("click", start);
  q("#helpTourBtn").addEventListener("click", start);
  q("#emptyTourBtn").addEventListener("click", start);
  q("#sampleBtn").addEventListener("click", async () => {
    try {
      const r = await fetch(SAMPLE.url);
      await openFiles([new File([await r.blob()], SAMPLE.name, { type: "image/jpeg" })]);
    } catch (err) { fail(`Couldn't load the sample photo: ${err.message}`); }
  });
  // First visit: start by itself once the app has loaded.
  window.addEventListener("le-ready", () => { if (!LS.get("tourDone", false)) setTimeout(start, 300); });
})();
