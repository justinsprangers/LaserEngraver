// tour.js: the guided tour (Help → Take the tour, offered on a first visit) and the sample photo.
// Each step highlights one part of the page and explains it, in the order you'd use the app.
// Uses app.js globals: openFiles, files, selectFile, LS.
"use strict";

const SAMPLE = { url: "samples/astronaut.jpg", name: "sample-astronaut.jpg" };

async function loadSample() {
  const have = files.find((f) => f.name === SAMPLE.name);
  if (have) { selectFile(have); return; }
  const r = await fetch(SAMPLE.url);
  if (!r.ok) throw new Error("couldn't load the sample photo");
  await openFiles([new File([await r.blob()], SAMPLE.name, { type: "image/jpeg" })]);
}

const fixbarShown = () => !document.querySelector("#fixbar").hidden;
const FIX_TOOLS = "<b>Smart keep</b> (green): paint on part of the subject that got removed; it brings " +
  "back that whole region. <b>Smart remove</b> (red): paint on leftover background; it removes that " +
  "region. Keep only ever adds and remove only ever takes away. <b>Brush keep / erase</b> " +
  "(blue / orange) change exactly what you paint, for touch-ups. <b>Undo</b> or Ctrl+Z takes back " +
  "the last fix; <b>[</b> and <b>]</b> change the brush size.";

const STEPS = [
  { welcome: true, title: "Welcome to Laser Engraver",
    text: "This tour shows where everything is and walks through making an engraving, start to " +
      "finish. It takes about a minute. Load the sample photo to follow along, or use your own." },
  { target: "#openBtn", title: "1. Open your photos",
    text: "Click <b>Open photos</b>, or drag photos anywhere onto the page (Ctrl+V pastes one). " +
      "Open as many as you like. Your photos stay on this computer: <b>nothing is uploaded</b>." },
  { target: ".files", title: "Your photos",
    text: "Every open photo is listed here. Click one to work on it; <b>×</b> closes it. Crop, " +
      "rotation and background fixes belong to each photo; all other settings apply to every photo." },
  { target: '#pane-basic [data-sec="style-material"]', tab: "basic", title: "2. Pick a style and material",
    text: "<b>Photo</b> is realistic shading made of fine dots. <b>Comic</b> is flat tones, halftone " +
      "dots and bold ink lines, and burns faster. The <b>material</b> sets contrast, brightness, gamma, " +
      "invert and mirror for that surface. Built-in materials can't be changed: adjust the sliders " +
      "and use <b>Save as…</b> to keep your own version." },
  { target: '#pane-basic [data-sec="size-resolution"]', tab: "basic", title: "3. Set the size",
    text: "Type the <b>width</b> in inches (or pick <b>Height</b>); the other side follows the photo. " +
      "<b>DPI</b> is how close the laser lines are: 254 DPI = 0.10 mm line interval. Use the same " +
      "value in your laser software. <b>Dither</b> turns gray into dots: stucki for the most detail, " +
      "floyd-steinberg for portraits, bayer for wood grain." },
  { target: '#pane-basic [data-sec="background"]', tab: "basic", title: "4. Remove the background (optional)",
    text: "Cuts the subject out so the laser skips the background, and gives the cut outline a real " +
      "silhouette. The first time, the AI model (176 MB) downloads once; after that it takes a few " +
      "seconds per photo." },
  { target: () => (fixbarShown() ? "#fixbar" : '#pane-basic [data-sec="background"]'), tab: "basic",
    title: "Fix the cut-out",
    text: () => (fixbarShown()
      ? "The AI isn't perfect, so paint on its mistakes in the <b>Cut-out</b> view (removed areas " +
        "are tinted red). " + FIX_TOOLS
      : "When Remove background is on, a <b>Fix the cut-out</b> toolbar appears above the preview, " +
        "and the <b>Cut-out</b> view shows removed areas tinted red. Paint on the AI's mistakes: " + FIX_TOOLS) },
  { target: "#views", title: "5. Check the result",
    text: "<b>Original</b> is your photo, <b>Adjusted</b> is after the tone settings, <b>Engraving</b> " +
      "is the real laser dots (black = the laser fires; zoom in to see them), and <b>Material look</b> " +
      "previews it in the material's colors. Keys 1 to 5 switch views." },
  { target: ".viewbar .zoom", title: "Zoom and compare",
    text: "Scroll the mouse wheel to zoom and drag to pan. <b>Fit</b> shows the whole image; " +
      "<b>1:1</b> shows one laser dot per screen pixel. Tick <b>Compare</b> to see the original left " +
      "of a divider that follows your mouse." },
  { target: '#pane-basic [data-sec="tone"]', tab: "basic", title: "6. Fine-tune the tone",
    text: "<b>Contrast</b> pushes tones apart, <b>brightness</b> higher means less burning, and " +
      "<b>gamma</b> above 1 lightens the midtones. <b>Invert</b> is for materials where the laser makes " +
      "light marks (slate, black acrylic). <b>Mirror</b> is for engraving the back of clear acrylic." },
  { target: "#cropSection", tab: "basic", title: "Crop and rotate",
    text: "Turn the photo 90° at a time, straighten a tilted one, or trim each edge by a percentage. " +
      "These belong to the selected photo only." },
  { target: ".tabs", title: "More tuning and presets",
    text: "<b>Photo tuning</b> has shadow recovery, local contrast and sharpening; <b>Comic tuning</b> " +
      "has tone bands, dot spacing and ink lines. Found a look you like? Save it as a <b>preset</b>. " +
      "Presets and your own materials are saved in this browser only." },
  { target: "#downloadBtn", title: "7. Download the engraving",
    text: "<b>Download PNG</b> saves the selected photo's engraving; <b>Download all</b> does every " +
      "photo. The file name includes the material, size and DPI. For a red cut line around the " +
      "subject, tick <b>Also download cut outline</b> under Extras on the Basic tab." },
  { target: "footer.status", title: "How long will it take?",
    text: "The status bar shows a rough engraving time. Type your laser's engraving speed (mm/s) " +
      "so the estimate fits your machine." },
  { center: true, title: "8. In your laser software",
    text: "Import the PNG at the same resolution it was saved at (254 DPI = 100 lines/cm = 0.1 mm " +
      "line interval) and <b>turn off the software's own dithering</b> (use black & white or " +
      "threshold mode). If the software resizes or re-dithers the dots, you get blotchy, boxy " +
      "patches. Burn a small test on scrap first. You can reopen this tour from <b>Help</b>." },
];

let tour = null;          // {i, root, hole, card}

function startTour() {
  if (tour) return;
  const help = document.querySelector("#help");
  if (help.open) help.close();
  hidePrompt();
  LS.set("tourSeen", true);
  const root = el("div", { className: "tour" });
  const dim = el("div", { className: "tour-dim" });           // dark layer with a hole cut out
  const hole = el("div", { className: "tour-hole" });         // the accent ring around the hole
  const card = el("div", { className: "tour-card", role: "dialog" });
  card.setAttribute("aria-live", "polite");
  root.append(dim, hole, card);
  document.body.append(root);
  tour = { i: 0, root, dim, hole, card };
  showStep(0);
  requestAnimationFrame(track);
}

function endTour() {
  if (!tour) return;
  tour.root.remove();
  tour = null;
}

const val = (v) => (typeof v === "function" ? v() : v);

function showStep(i) {
  tour.i = i;
  const step = STEPS[i];
  if (step.tab) document.querySelector(`.tab[data-tab="${step.tab}"]`)?.click();
  const target = step.target ? document.querySelector(val(step.target)) : null;
  if (target) target.scrollIntoView({ block: "nearest", inline: "nearest" });
  tour.target = target;

  const card = tour.card;
  const btn = (text, cls, fn) => {
    const b = el("button", { className: `btn ${cls}`, textContent: text, type: "button" });
    b.addEventListener("click", fn);
    return b;
  };
  const head = el("div", { className: "tour-head" },
    el("span", { className: "tour-count", textContent: step.welcome ? "Guided tour" : `${i} of ${STEPS.length - 1}` }),
    btn("✕", "ghost sm tour-x", endTour));
  head.lastChild.title = "Close the tour (Esc)";
  const body = el("div", { className: "tour-text" });
  body.innerHTML = val(step.text);
  const actions = el("div", { className: "tour-actions" });
  if (step.welcome) {
    if (!files.length) {
      actions.append(btn("Use the sample photo", "primary", async (e) => {
        e.target.disabled = true;
        e.target.textContent = "Loading…";
        try { await loadSample(); } catch { /* the tour works without it */ }
        if (tour) showStep(1);
      }));
    }
    actions.append(btn(files.length ? "Start the tour" : "Use my own photos", files.length ? "primary" : "", () => showStep(1)),
      btn("Not now", "ghost", endTour));
  } else {
    actions.append(btn("Back", "ghost", () => showStep(i - 1)));
    const last = i === STEPS.length - 1;
    actions.append(btn(last ? "Finish" : "Next", "primary", () => (last ? endTour() : showStep(i + 1))));
  }
  card.replaceChildren(head, el("h3", { textContent: step.title }), body, actions);
  card.classList.toggle("centered", !target);
  place();
  actions.querySelector(".primary")?.focus({ preventScroll: true });
}

function place() {
  if (!tour) return;
  const { dim, hole, card, target } = tour;
  const vw = window.innerWidth, vh = window.innerHeight, pad = 6, gap = 14;
  if (!target || !target.getClientRects().length) {
    dim.style.clipPath = `path("M0 0H${vw}V${vh}H0Z")`;          // whole page dimmed
    hole.hidden = true;
    Object.assign(card.style, { left: `${Math.max(12, (vw - card.offsetWidth) / 2)}px`,
      top: `${Math.max(12, (vh - card.offsetHeight) / 2)}px` });
    return;
  }
  const r = target.getBoundingClientRect();
  const box = { left: r.left - pad, top: r.top - pad, width: r.width + 2 * pad, height: r.height + 2 * pad };
  hole.hidden = false;
  Object.assign(hole.style, { left: `${box.left}px`, top: `${box.top}px`, width: `${box.width}px`, height: `${box.height}px` });
  dim.style.clipPath = `path(evenodd, "M0 0H${vw}V${vh}H0Z M${box.left} ${box.top}` +
    `h${box.width}v${box.height}h${-box.width}Z")`;
  const cw = card.offsetWidth, ch = card.offsetHeight;
  if (vw < 600) {                                   // phones: card pinned to the edge away from the target
    const below = box.top + box.height / 2 < vh / 2;
    Object.assign(card.style, { left: "12px", top: below ? `${vh - ch - 12}px` : "12px" });
    return;
  }
  const fits = {
    right: box.left + box.width + gap + cw <= vw - 8,
    below: box.top + box.height + gap + ch <= vh - 8,
    left: box.left - gap - cw >= 8,
    above: box.top - gap - ch >= 8,
  };
  let x, y;
  if (fits.right) { x = box.left + box.width + gap; y = box.top; }
  else if (fits.below) { x = box.left; y = box.top + box.height + gap; }
  else if (fits.left) { x = box.left - gap - cw; y = box.top; }
  else if (fits.above) { x = box.left; y = box.top - gap - ch; }
  else { x = (vw - cw) / 2; y = vh - ch - 12; }
  Object.assign(card.style, { left: `${Math.min(Math.max(8, x), vw - cw - 8)}px`,
    top: `${Math.min(Math.max(8, y), vh - ch - 8)}px` });
}

function track() {                 // follow the target if the page scrolls, resizes or re-lays out
  if (!tour) return;
  place();
  requestAnimationFrame(track);
}

// Keys: arrows / Enter move through the tour, Esc closes it. The app's own shortcuts are off meanwhile.
window.addEventListener("keydown", (e) => {
  if (!tour) return;
  if (e.key === "Tab" || e.key === "Enter" || e.key === " ") return;     // the card's own buttons
  const step = STEPS[tour.i];
  if (e.key === "Escape") endTour();
  else if (e.key === "ArrowRight" && !step.welcome) {
    tour.i === STEPS.length - 1 ? endTour() : showStep(tour.i + 1);
  } else if (e.key === "ArrowLeft" && tour.i > 1) showStep(tour.i - 1);
  e.stopImmediatePropagation();
  e.preventDefault();
}, true);

// --- First visit: a small, dismissable offer instead of starting the tour on its own
let prompt = null;
function hidePrompt() { prompt?.remove(); prompt = null; }
window.addEventListener("le-ready", () => {
  if (LS.get("tourSeen", false)) return;
  const start = el("button", { className: "btn primary sm", textContent: "Take the tour", type: "button" });
  const no = el("button", { className: "btn ghost sm", textContent: "No thanks", type: "button" });
  prompt = el("div", { className: "tour-prompt", role: "status" },
    el("div", {}, el("b", { textContent: "New here? " }), "Take a 1-minute tour of the tools."),
    el("div", { className: "tour-actions" }, no, start));
  start.addEventListener("click", startTour);
  no.addEventListener("click", () => { LS.set("tourSeen", true); hidePrompt(); });
  document.body.append(prompt);
});

document.querySelector("#helpTourBtn").addEventListener("click", startTour);   // startTour closes Help
document.querySelector("#emptyTourBtn").addEventListener("click", startTour);
document.querySelector("#sampleBtn").addEventListener("click", async () => {
  try { await loadSample(); } catch (err) { fail(`Couldn't load the sample photo: ${err.message}`); }
});
window.startTour = startTour;
