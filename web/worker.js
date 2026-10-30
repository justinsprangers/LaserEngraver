// worker.js: runs the processing off the page's main thread so the page never freezes.
// Pyodide (Python in WebAssembly) runs the desktop app's engrave_core.py through engine.py;
// onnxruntime-web runs the background-removal model.

const PYODIDE = "https://cdn.jsdelivr.net/pyodide/v0.27.7/full/";
const ORT = "https://cdn.jsdelivr.net/npm/onnxruntime-web@1.20.1/dist/";
const MODEL_CACHE = "laser-engraver-models-v1";

importScripts(PYODIDE + "pyodide.js", ORT + "ort.min.js");

let py = null, engine = null, session = null;

function progress(text, frac = null) { postMessage({ type: "progress", text, frac }); }

// Stucki error diffusion: the same float64 math as engrave_core._stucki_loop.
self.stuckiJS = function (arr, w, h) {
  const a = Float64Array.from(arr);
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      const i = y * w + x, old = a[i], nw = old > 127.0 ? 255.0 : 0.0;
      a[i] = nw;
      const err = old - nw;
      if (x + 1 < w) a[i + 1] += err * (8 / 42);
      if (x + 2 < w) a[i + 2] += err * (4 / 42);
      if (y + 1 < h) {
        const j = i + w;
        if (x - 2 >= 0) a[j - 2] += err * (2 / 42);
        if (x - 1 >= 0) a[j - 1] += err * (4 / 42);
        a[j] += err * (8 / 42);
        if (x + 1 < w) a[j + 1] += err * (4 / 42);
        if (x + 2 < w) a[j + 2] += err * (2 / 42);
      }
      if (y + 2 < h) {
        const k = i + 2 * w;
        if (x - 2 >= 0) a[k - 2] += err * (1 / 42);
        if (x - 1 >= 0) a[k - 1] += err * (2 / 42);
        a[k] += err * (4 / 42);
        if (x + 1 < w) a[k + 1] += err * (2 / 42);
        if (x + 2 < w) a[k + 2] += err * (1 / 42);
      }
    }
  }
  return a;
};

async function fetchBytes(url) {
  const r = await fetch(url, { cache: "no-cache" });      // always check for a newer copy
  if (!r.ok) throw new Error(`Couldn't load ${url} (${r.status})`);
  return new Uint8Array(await r.arrayBuffer());
}

async function init() {
  progress("Loading Python…", 0.05);
  py = await loadPyodide({ indexURL: PYODIDE });
  progress("Loading image libraries…", 0.35);
  await py.loadPackage(["numpy", "opencv-python", "Pillow"]);
  progress("Loading the engraving engine…", 0.85);
  py.FS.mkdirTree("/app");
  py.FS.mkdirTree("/files");
  // engrave_core.py and materials.json are the desktop app's own files (one folder up).
  py.FS.writeFile("/app/engrave_core.py", await fetchBytes("../engrave_core.py"));
  py.FS.writeFile("/app/materials.json", await fetchBytes("../materials.json"));
  py.FS.writeFile("/app/engine.py", await fetchBytes("engine.py"));
  py.runPython("import sys; sys.path.insert(0, '/app')");
  engine = py.pyimport("engine");
  return { defaults: JSON.parse(engine.defaults()), materials: JSON.parse(engine.materials()) };
}

// --- Background-removal model: downloaded once in pieces, checked, kept in the browser cache.
async function loadModel(name = "u2net") {
  if (session) return session;
  const manifest = await (await fetch(`models/${name}.json`)).json();
  const cache = await caches.open(MODEL_CACHE);
  const model = new Uint8Array(manifest.size);
  let offset = 0, fromCache = true;
  for (const part of manifest.parts) {
    const url = new URL(`models/${part}`, self.location).href;
    let resp = await cache.match(url);
    if (!resp) {
      fromCache = false;
      resp = await fetch(url);
      if (!resp.ok) throw new Error(`Couldn't download the AI model (${resp.status}).`);
      const reader = resp.body.getReader(), chunks = [];
      let got = 0;
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        chunks.push(value);
        got += value.length;
        progress(`Downloading the AI model (one time, ${(manifest.size / 1e6).toFixed(0)} MB)…`,
                 (offset + got) / manifest.size);
      }
      const blob = new Blob(chunks);
      await cache.put(url, new Response(blob));
      model.set(new Uint8Array(await blob.arrayBuffer()), offset);
      offset += blob.size;
    } else {
      const buf = new Uint8Array(await resp.arrayBuffer());
      model.set(buf, offset);
      offset += buf.length;
    }
  }
  if (!fromCache) {
    progress("Checking the AI model…", null);
    const hash = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", model)))
      .map(b => b.toString(16).padStart(2, "0")).join("");
    if (hash !== manifest.sha256) {
      await caches.delete(MODEL_CACHE);
      throw new Error("The AI model download was damaged. Try again.");
    }
  }
  progress("Starting the AI model…", null);
  ort.env.wasm.wasmPaths = ORT;
  ort.env.wasm.numThreads = self.crossOriginIsolated ? Math.min(4, navigator.hardwareConcurrency || 1) : 1;
  session = await ort.InferenceSession.create(model, { executionProviders: ["wasm"] });
  return session;
}

async function cutout(id) {
  if (engine.has_cutout(id)) return true;
  const s = await loadModel();
  progress("Removing the background…", null);
  const input = engine.cutout_input(id);
  const out = await s.run({ [s.inputNames[0]]: new ort.Tensor("float32", input, [1, 3, 320, 320]) });
  engine.cutout_output(id, out[s.outputNames[0]].data.subarray(0, 320 * 320));
  return true;
}

function plain(proxy) {
  // Python dicts arrive as JS objects already (dict_converter); just collect transferables
  // (pixel buffers, also inside lists such as render_layers' layers).
  const transfer = [];
  const walk = (o, depth) => {
    if (o instanceof Uint8Array) { if (!transfer.includes(o.buffer)) transfer.push(o.buffer); return; }
    if (depth > 3 || !o || typeof o !== "object") return;
    for (const v of Array.isArray(o) ? o : Object.values(o)) walk(v, depth + 1);
  };
  walk(proxy, 0);
  return [proxy, transfer];
}

// --- Design mode: loaded the first time it's needed (shapely, fontTools, the design engine,
// the bundled fonts). Fonts are fetched one file at a time when a design uses them.
let designReady = null;
const fontsWritten = new Set();

function designInit() {
  designReady ??= (async () => {
    progress("Loading the design tools…", null);
    await py.loadPackage(["shapely", "fonttools"]);
    for (const f of ["design.py", "vectors.py", "vecfile.py"])
      py.FS.writeFile(`/app/${f}`, await fetchBytes(`../${f}`));
    py.FS.mkdirTree("/fonts");
    const manifest = new TextDecoder().decode(await fetchBytes("fonts/fonts.json"));
    const info = JSON.parse(engine.design_init(manifest));
    await ensureFonts(Object.values(JSON.parse(manifest).families.Arimo));   // the default font
    return info;
  })();
  designReady.catch(() => { designReady = null; });
  return designReady;
}

async function ensureFonts(files) {
  for (const f of files) {
    if (fontsWritten.has(f)) continue;
    py.FS.writeFile(`/fonts/${f}`, await fetchBytes(`fonts/${f}`));
    fontsWritten.add(f);
  }
}

// Vector files: SVG text is turned into outlines with any bundled font, so all of them are
// written first; DXF needs ezdxf (web/wheels, unzipped into site-packages once).
const EZDXF_WHEEL = "wheels/ezdxf-1.4.5-py3-none-any.whl";
let allFonts = null, ezdxfReady = null;
function ensureAllFonts() {
  allFonts ??= (async () => {
    const m = JSON.parse(new TextDecoder().decode(await fetchBytes("fonts/fonts.json")));
    await ensureFonts(Object.values(m.families).flatMap((s) => Object.values(s)));
  })();
  allFonts.catch(() => { allFonts = null; });
  return allFonts;
}
function ensureEzdxf() {
  ezdxfReady ??= (async () => {
    progress("Loading the DXF reader…", null);
    await py.loadPackage(["pyparsing", "typing-extensions"]);
    await ensureAllFonts();
    py.FS.writeFile("/tmp/ezdxf.whl", await fetchBytes(EZDXF_WHEEL));
    py.runPython(`
import site, zipfile
zipfile.ZipFile("/tmp/ezdxf.whl").extractall(site.getsitepackages()[0])
from ezdxf.fonts import fonts
fonts.font_manager.build(["/fonts"], support_dirs=False)   # DXF text uses the bundled fonts
fonts.font_manager._fallback_font_name = "Arimo-Regular.ttf"   # (ezdxf's own fallback, DejaVu, isn't bundled)
`);
  })();
  ezdxfReady.catch(() => { ezdxfReady = null; });
  return ezdxfReady;
}
async function ensureVectorTools(paths) {
  if (paths.some((p) => /\.svg$/i.test(p))) await ensureAllFonts();
  if (paths.some((p) => /\.dxf$/i.test(p))) await ensureEzdxf();
}

// Everything a design render / export needs first: its fonts and background removals.
async function prepareDesign(design, mode) {
  await designInit();
  const json = JSON.stringify(design);
  await ensureVectorTools(design.layers.filter((l) => l.type === "svg").map((l) => l.src || ""));
  await ensureFonts(JSON.parse(engine.font_files(json)));
  for (const path of JSON.parse(engine.cutouts_needed(json, mode))) await cutout(path);
  return json;
}

const handlers = {
  init,
  open: ({ id, name, bytes }) => engine.open_file(id, name, bytes),
  close: ({ id }) => engine.close_file(id),
  cutout: ({ id }) => cutout(id),
  hasModel: async () => !!(await (await caches.open(MODEL_CACHE)).keys()).length,
  render: async ({ id, settings, strokes = [] }) => {
    if (settings.remove_bg) await cutout(id);
    if (settings.remove_bg && strokes.length) progress(`Applying fixes…`, null);
    return engine.render(id, JSON.stringify(settings), JSON.stringify(strokes));
  },
  export: async ({ id, settings, strokes = [] }) => {
    if (settings.remove_bg) await cutout(id);
    return engine.export(id, JSON.stringify(settings), JSON.stringify(strokes));
  },
  // Design mode
  designInit,
  newDesign: async ({ material, width_in, height_in, dpi }) =>
    (await designInit(), JSON.parse(engine.new_design(material, width_in, height_in, dpi))),
  newLayer: async ({ kind, design, kw = {} }) =>
    (await designInit(), JSON.parse(engine.new_layer(kind, JSON.stringify(design), JSON.stringify(kw)))),
  addAsset: async ({ name, bytes }) => (await designInit(), JSON.parse(engine.add_asset(name, bytes))),
  addFont: async ({ name, bytes }) => (await designInit(), JSON.parse(engine.add_font(name, bytes))),
  renderLayers: async ({ design, mode = "layout", previewDpi = 0, known = [] }) => {
    const json = await prepareDesign(design, mode);
    if (mode !== "layout") progress("Rendering the engraving…", null);
    return engine.render_layers(json, mode, previewDpi, JSON.stringify(known));
  },
  renderRegion: async ({ design, box }) => {
    const json = await prepareDesign(design, "design");
    return engine.render_region(json, ...box);
  },
  readAsset: ({ path }) => engine.read_asset(path),  addVector: async ({ design, path, name }) => {
    await designInit();
    await ensureVectorTools([path]);
    progress(`Reading ${name}…`, null);
    return JSON.parse(engine.vector_add(JSON.stringify(design), path, name));
  },
  vectorInfo: async ({ layer }) => (await ensureVectorTools([layer.src || ""]), JSON.parse(engine.vector_info(JSON.stringify(layer)))),
  vectorSplit: async ({ layer, by }) => (await ensureVectorTools([layer.src || ""]), JSON.parse(engine.vector_split(JSON.stringify(layer), by))),
  exportDesign: async ({ design, name, factor = 1 }) => {
    const json = await prepareDesign(design, "export");
    progress("Saving the engraving…", null);
    return engine.export_design(json, name, factor);
  },
  saveProject: async ({ design }) => (await designInit(), engine.save_project(JSON.stringify(design))),
  openProject: async ({ bytes }) => {
    await designInit();
    const design = JSON.parse(engine.load_project(bytes));
    await ensureFonts(JSON.parse(engine.font_files(JSON.stringify(design))));
    return design;
  },
};

// One job at a time, in order.
let queue = Promise.resolve();
onmessage = (e) => {
  const { reqId, type, ...args } = e.data;
  queue = queue.then(async () => {
    try {
      const result = await handlers[type](args);
      const [value, transfer] = result && typeof result === "object" ? plain(result) : [result, []];
      postMessage({ reqId, ok: true, value }, transfer);
    } catch (err) {
      let msg = String(err && err.message || err);
      if (msg.includes("Traceback")) msg = msg.trim().split("\n").filter(Boolean).pop();
      msg = msg.replace(/^(ValueError|RuntimeError|FileNotFoundError): /, "");   // (the message is for people)
      postMessage({ reqId, ok: false, error: msg });
    }
  });
};
