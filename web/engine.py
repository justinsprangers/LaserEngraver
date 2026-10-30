"""
engine.py: glue between the web page and engrave_core, run inside Pyodide (in worker.js).

The processing itself is the desktop app's engrave_core.py, unchanged, so the web version's
output matches the desktop's. Only two things are swapped for browser versions:
  - Stucki dithering calls a JavaScript loop (numba doesn't run in the browser);
  - background removal runs the u2net model through onnxruntime-web (worker.js); the pre/post
    processing here copies rembg's U2netSession so the mask matches the desktop's.
"""
import io
import json

import numpy as np
from PIL import Image, ImageOps

import engrave_core as core
from js import stuckiJS
from pyodide.ffi import to_js


def _stucki(arr):
    h, w = arr.shape
    out = stuckiJS(to_js(memoryview(np.ascontiguousarray(arr).ravel())), w, h)
    arr[:] = np.frombuffer(out.to_py(), dtype=np.float64).reshape(h, w)


core._stucki_fast = _stucki
core.FAST_STUCKI = True

_files = {}            # id -> {"name", "path", "gray", "rgb", "alpha"}


def _js(d):
    from js import Object
    return to_js(d, dict_converter=Object.fromEntries)


def _pixels(img):
    """8-bit grayscale pixels for the page to draw."""
    a = np.ascontiguousarray(np.array(img.convert("L"), dtype=np.uint8))
    return to_js(memoryview(a.ravel()))


def _register(fid, name, path):
    gray = core.load_image(path)          # raises if it isn't an image
    _files[fid] = {"name": name, "path": path, "gray": gray, "rgb": None, "alpha": None,
                   "steps": {}, "refined": None}
    return gray


def open_file(fid, name, data):
    path = f"/files/{fid}"
    with open(path, "wb") as f:
        f.write(bytes(data.to_py()))
    gray = _register(fid, name, path)
    return _js({"w": gray.width, "h": gray.height})


def close_file(fid):
    _files.pop(fid, None)


def has_cutout(fid):
    return _files[fid]["alpha"] is not None


# --- Background removal (same steps as rembg's U2netSession) -------------------------------

MEAN, STD, SIZE = (0.485, 0.456, 0.406), (0.229, 0.224, 0.225), (320, 320)


def cutout_input(fid):
    """The model input for this photo: 1x3x320x320 float32, normalized like rembg."""
    f = _files[fid]
    f["rgb"] = ImageOps.exif_transpose(Image.open(f["path"])).convert("RGB")
    im = np.array(f["rgb"].resize(SIZE, Image.Resampling.LANCZOS))
    im = im / max(np.max(im), 1e-6)
    x = np.zeros((SIZE[1], SIZE[0], 3))
    for c in range(3):
        x[:, :, c] = (im[:, :, c] - MEAN[c]) / STD[c]
    x = np.expand_dims(x.transpose((2, 0, 1)), 0).astype(np.float32)
    return to_js(memoryview(x.ravel()))


def cutout_output(fid, pred):
    """Turns the model output (first 320x320 channel) into the alpha mask, like rembg."""
    f = _files[fid]
    p = np.frombuffer(pred.to_py(), dtype=np.float32).reshape(SIZE[1], SIZE[0])
    p = (p - p.min()) / (p.max() - p.min())
    mask = Image.fromarray((p.clip(0, 1) * 255).astype("uint8"), mode="L")
    f["alpha"] = mask.resize(f["rgb"].size, Image.Resampling.LANCZOS)


# --- Fix strokes ------------------------------------------------------------------------------
# Strokes are the desktop's format: [kind, radius, [[x, y], ...]] in source-image pixels.
# core.refine_alpha applies them in order (direction-locked), reusing results for each prefix
# of the list, so adding or undoing one stroke only computes one step.

def _refined(f, strokes):
    """(cut-out alpha with the strokes applied, the subject on white as grayscale)."""
    strokes = [(k, float(r), [(float(x), float(y)) for x, y in pts]) for k, r, pts in strokes]
    key = core.stroke_key(strokes)
    if f["refined"] is None or f["refined"][0] != key:
        steps = f["steps"]
        if len(steps) > 40:                       # keep memory in check on long fix sessions
            steps.clear()
        alpha = core.refine_alpha(f["rgb"], f["alpha"], strokes, steps) if strokes else f["alpha"]
        f["refined"] = (key, alpha, core.compose_on_white(f["rgb"], alpha))
    return f["refined"][1], f["refined"][2]


def _cutout_view(rgb, alpha, max_side=1600):
    """Same as the desktop's Cut-out view: the photo in color, removed areas tinted red."""
    k = min(1.0, max_side / max(rgb.size))
    size = (max(1, round(rgb.width * k)), max(1, round(rgb.height * k)))
    im = rgb.resize(size, Image.Resampling.BILINEAR)
    a = alpha.resize(size, Image.Resampling.BILINEAR)
    tinted = Image.blend(im, Image.new("RGB", size, (190, 20, 20)), 0.6)
    view = Image.composite(im, tinted, a)
    return {"w": size[0], "h": size[1], "factor": rgb.width / size[0],
            "rgb": to_js(memoryview(np.ascontiguousarray(np.array(view, dtype=np.uint8)).ravel()))}


# --- Rendering -------------------------------------------------------------------------------

def _process(fid, settings_json, strokes_json="[]"):
    f = _files[fid]
    s = core.Settings.from_dict(json.loads(settings_json))
    if s.remove_bg and f["alpha"] is not None:
        alpha, src = _refined(f, json.loads(strokes_json))
    else:
        src, alpha = f["gray"], None
    src, alpha = core.prepare(src, alpha, s)
    gray, result, mask = core.render(src, alpha, s)
    return s, src, gray, result, mask


def render(fid, settings_json, strokes_json="[]"):
    """All preview views plus what the page shows about the job."""
    s, src, gray, result, mask = _process(fid, settings_json, strokes_json)
    f = _files[fid]
    burn = np.array(result.convert("L")) < 128
    stats = core.raster_stats(burn)
    orig = src.resize(result.size, Image.Resampling.LANCZOS)   # same framing as the other views
    out = {
        "w": result.width, "h": result.height,
        "original": _pixels(orig), "adjusted": _pixels(gray), "engraving": _pixels(result),
        "stats": list(stats), "fill": float(burn.mean()), "mirror": bool(s.mirror),
        "filename": core.output_filename(f["name"], s),
    }
    if s.remove_bg and f["refined"] is not None:
        out["cutout"] = _js(_cutout_view(f["rgb"], f["refined"][1]))
    return _js(out)


def export(fid, settings_json, strokes_json="[]"):
    """The engraving PNG (with its DPI) and, if asked for, the cut outline SVG."""
    s, src, gray, result, mask = _process(fid, settings_json, strokes_json)
    buf = io.BytesIO()
    result.save(buf, "PNG", dpi=(s.dpi, s.dpi))
    out = {"png": to_js(memoryview(buf.getvalue())), "filename": core.output_filename(_files[fid]["name"], s)}
    if s.outline:
        try:
            out["svg"] = core.outline_svg(mask, s)
        except ValueError as e:
            out["svg_error"] = str(e)
    return _js(out)


# --- Design mode -----------------------------------------------------------------------------
# The desktop's design.py / vectors.py do the work. Differences in the browser:
#   - fonts are the bundled ones in web/fonts (plus any the user adds), not the installed ones;
#   - photo layers' background removal comes from the u2net model in worker.js (keyed by the
#     asset's path), so core.load_source / cached_cutout_alpha are swapped for versions that
#     use it;
#   - the page draws each layer itself (render_layers returns one bitmap per layer, placed by
#     the page), so moving, resizing handles and hit-testing never wait for Python.

_design = {"aliases": set(), "layout": None, "full": None}
FONT_DIR = "/fonts"


def design_init(manifest_json):
    """Called once the worker has loaded shapely + fontTools. manifest = web/fonts/fonts.json."""
    import design as D
    m = json.loads(manifest_json)
    found = {fam: {style: f"{FONT_DIR}/{name}" for style, name in styles.items()}
             for fam, styles in m["families"].items()}
    _design["aliases"] = set()
    for alias, fam in m.get("aliases", {}).items():
        if alias not in found:
            found[alias] = found[fam]
            _design["aliases"].add(alias)
    D._FONTS = found
    _design["layout"] = D.Renderer(draft=True)
    _design["full"] = D.Renderer(limit=24)
    return json.dumps({"fonts": font_list(), "aliases": m.get("aliases", {}), "draft_dpi": D.DRAFT_DPI,
                       "types": D.TYPE_LABELS, "ops": list(D.OPS),
                       "layer_fields": list(D.LAYER_SETTING_FIELDS), "global_fields": list(D.GLOBAL_FIELDS)})


def font_list():
    """{family: [styles]} the page offers (bundled + added, without the Arial-style aliases)."""
    import design as D
    return {f: list(styles) for f, styles in sorted(D._FONTS.items(), key=lambda kv: kv[0].lower())
            if f not in _design["aliases"]}


def font_files(design_json):
    """Font files (under /fonts) the design's text layers need, for the worker to fetch first."""
    import design as D
    need = set()
    for layer in json.loads(design_json)["layers"]:
        if layer["type"] in ("text", "arctext"):
            p = D.font_path(layer.get("font", "Arial"), layer.get("style", "Regular"))
            if p and p.startswith(FONT_DIR + "/") and not p.startswith(FONT_DIR + "/user/"):
                need.add(p[len(FONT_DIR) + 1:])
    return json.dumps(sorted(need))


def add_font(name, data):
    """A font file the user added: stored under /fonts/user, offered by its own family name."""
    import hashlib
    import os
    import design as D
    from PIL import ImageFont
    raw = bytes(data.to_py())
    os.makedirs(FONT_DIR + "/user", exist_ok=True)
    path = f"{FONT_DIR}/user/{hashlib.sha1(raw).hexdigest()[:16]}{os.path.splitext(name)[1].lower() or '.ttf'}"
    with open(path, "wb") as f:
        f.write(raw)
    try:
        fam, style = ImageFont.truetype(path, 12).getname()
    except Exception:
        os.remove(path)
        raise ValueError(f"{name} isn't a font file this app can read (use .ttf or .otf).")
    D._FONTS.setdefault(fam, {})[style] = path
    return json.dumps({"family": fam, "style": style, "fonts": font_list()})


def add_asset(name, data):
    """Stores an image / vector file for a design layer, named by its content like the desktop's
    project assets (so a .lep made here and one made on the desktop share files). Returns its
    path, which layers use as "src"."""
    import hashlib
    import os
    import design as D
    raw = bytes(data.to_py())
    os.makedirs(D.PROJECTS_DIR, exist_ok=True)
    ext = os.path.splitext(name)[1].lower()
    path = f"{D.PROJECTS_DIR}/{hashlib.sha1(raw).hexdigest()[:16]}{ext}"
    if not os.path.exists(path):
        with open(path, "wb") as f:
            f.write(raw)
    out = {"path": path}
    if ext in core.IMAGE_EXTENSIONS:
        if path not in _files:
            gray = _register(path, name, path)
        else:
            gray = _files[path]["gray"]
        out.update(w=gray.width, h=gray.height)
    return json.dumps(out)


def _web_load_source(path, remove_bg=False, strokes=None, model=core.DEFAULT_BG_MODEL):
    f = _files.get(path)
    if not remove_bg:
        return core.load_image(path), None
    if f is None or f["alpha"] is None:
        raise RuntimeError("The background hasn't been removed yet.")
    alpha, src = _refined(f, strokes or [])
    return src, alpha


def _web_cached_alpha(path, strokes=None, model=core.DEFAULT_BG_MODEL):
    f = _files.get(path)
    if f is None or f["alpha"] is None:
        return None
    return _refined(f, strokes or [])[0]


core.load_source = _web_load_source
core.cached_cutout_alpha = _web_cached_alpha


def cutouts_needed(design_json, mode):
    """Asset paths whose background the worker has to remove before rendering (Design view and
    export only; the Layout view uses a cut-out only once it exists, like the desktop's)."""
    if mode == "layout":
        return "[]"
    need = []
    for layer in json.loads(design_json)["layers"]:
        if layer["type"] == "image" and layer.get("visible", True) and layer.get("src") and \
                layer["settings"].get("remove_bg") and layer["src"] in _files and \
                _files[layer["src"]]["alpha"] is None:
            need.append(layer["src"])
    return json.dumps(list(dict.fromkeys(need)))


def new_design(material="wood", width_in=6.0, height_in=4.0, dpi=254):
    import design as D
    return json.dumps(D.new_design(material, width_in, height_in, dpi))


def new_layer(kind, design_json, kw_json="{}"):
    import design as D
    return json.dumps(D.new_layer(kind, json.loads(design_json), **json.loads(kw_json)))


def _rgba(value, cover, draft):
    """A layer's bitmap for the page: gray (Layout) or black ink / white blank (Design), opaque
    where the layer covers what's below it, transparent elsewhere."""
    h, w = cover.shape
    out = np.empty((h, w, 4), np.uint8)
    g = value if draft else np.where(value, 0, 255).astype(np.uint8)
    out[..., 0] = out[..., 1] = out[..., 2] = g
    out[..., 3] = np.where(cover, 255, 0)
    return to_js(memoryview(out.ravel()))


def render_layers(design_json, mode="layout", preview_dpi=0, known_json="[]"):
    """Every visible layer of the design as its own bitmap + score/cut lines, for the page to
    draw. mode "layout" = the fast Layout view (photos as plain pictures, DRAFT_DPI); "design"
    = the real engraving, rendered at preview_dpi (0 = the design's own DPI). Layers whose key is
    in `known` (the page already has them) come back without pixels or lines.

    Returns {dpi, layers: [{id, key, w_in, h_in, raster, [w, h, rgba], [vecs]}]}: rgba is
    rotated already and centered on the layer's (x_in, y_in); vecs = {"cut"|"score":
    [[points relative to the layer center, inches], closed]}."""
    import design as D
    import os
    d = json.loads(design_json)
    draft = mode == "layout"
    if not draft and preview_dpi and preview_dpi < d["dpi"]:
        d["dpi"] = int(preview_dpi)
    r = _design["layout" if draft else "full"]
    dpi = r.dpi(d)
    known = set(json.loads(known_json))
    out = []
    for layer in d["layers"]:
        if not layer.get("visible", True):
            continue
        if layer["type"] in ("image", "logo", "svg") and not (layer.get("src") and os.path.exists(layer["src"])):
            continue
        key = f"{mode}|{dpi}|{D._layer_key(layer, d)}"
        if draft and layer["type"] == "image":
            key += f"|cut={D.draft_cutout_ready(layer)}"
        item = {"id": layer["id"], "key": key}
        vecs = r.layer_vectors(layer, d)               # also sizes text layers
        raster = D.has_raster(layer)
        if raster:
            value, cover = r.layer_masks(layer, d)
        item.update(w_in=layer["w_in"], h_in=layer["h_in"], raster=raster)
        if key not in known:
            if raster:
                item.update(w=int(cover.shape[1]), h=int(cover.shape[0]), rgba=_rgba(value, cover, draft))
            x, y = layer["x_in"], layer["y_in"]
            item["vecs"] = {k: [[[[px - x, py - y] for px, py in pts], bool(c)] for pts, c in paths]
                            for k, paths in vecs.items()}
        out.append(_js(item))
    return _js({"dpi": dpi, "layers": to_js(out)})


def render_region(design_json, x0, y0, x1, y1):
    """The real engraving of part of the canvas (inches) at the design's full DPI, for the
    Design view when zoomed in on a big canvas (whose whole view is shown at a lower DPI).
    Returns {x, y, w, h (dots), burn (1 byte per dot, 1 = laser fires)}."""
    import design as D
    d = json.loads(design_json)
    dpi = d["dpi"]
    W, H = D._px(d["canvas"]["width_in"], dpi), D._px(d["canvas"]["height_in"], dpi)
    px0, py0 = max(0, int(x0 * dpi)), max(0, int(y0 * dpi))
    px1, py1 = min(W, int(x1 * dpi) + 1), min(H, int(y1 * dpi) + 1)
    if px1 <= px0 or py1 <= py0:
        return _js({"x": 0, "y": 0, "w": 0, "h": 0})
    # Renderer.compose's stacking, with each layer placed exactly as in the full engraving
    # (same rounding), but only the region is filled in.
    import os
    r = _design["full"]
    burn = np.zeros((py1 - py0, px1 - px0), bool)
    for layer in d["layers"]:
        if not layer.get("visible", True) or not D.has_raster(layer):
            continue
        if layer["type"] in ("image", "logo", "svg") and not (layer.get("src") and os.path.exists(layer["src"])):
            continue
        ink, cover = r.layer_masks(layer, d)
        h, w = ink.shape
        x0, y0 = round(layer["x_in"] * dpi - w / 2), round(layer["y_in"] * dpi - h / 2)
        cx0, cy0, cx1, cy1 = max(px0, x0), max(py0, y0), min(px1, x0 + w), min(py1, y0 + h)
        if cx1 <= cx0 or cy1 <= cy0:
            continue
        sub_cov = cover[cy0 - y0:cy1 - y0, cx0 - x0:cx1 - x0]
        region = burn[cy0 - py0:cy1 - py0, cx0 - px0:cx1 - px0]
        region[sub_cov] = ink[cy0 - y0:cy1 - y0, cx0 - x0:cx1 - x0][sub_cov]
    return _js({"x": px0, "y": py0, "w": int(burn.shape[1]), "h": int(burn.shape[0]),
                "burn": to_js(memoryview(np.ascontiguousarray(burn, dtype=np.uint8).ravel()))})


def read_asset(path):
    """An asset file's bytes (for the page's saved copy of the design)."""
    with open(path, "rb") as f:
        return to_js(memoryview(f.read()))


def export_design(design_json, name, factor=1.0):
    """Writes the engraving files like the desktop's Save engraving (test copy if factor != 1)
    and returns [{name, bytes}] for the page to download."""
    import design as D
    import os
    import shutil
    d = json.loads(design_json)
    if factor != 1.0:
        d = D.scaled_design(d, factor)
        name += "_test"
    out_dir = "/out"
    shutil.rmtree(out_dir, ignore_errors=True)
    os.makedirs(out_dir)
    r = _design["full"] if factor == 1.0 else D.Renderer(limit=8)
    written = D.save_engraving(d, r, f"{out_dir}/{name}.png")
    files = []
    for p in written:
        with open(p, "rb") as f:
            files.append(_js({"name": os.path.basename(p), "bytes": to_js(memoryview(f.read()))}))
    return to_js(files)


def save_project(design_json):
    """The design as .lep bytes (same format as the desktop's)."""
    import design as D
    D.save_project(json.loads(design_json), "/tmp/project.lep")
    with open("/tmp/project.lep", "rb") as f:
        return to_js(memoryview(f.read()))


def load_project(data):
    """Opens .lep bytes: assets are unpacked (content-named) and registered; returns the design."""
    import os
    import design as D
    with open("/tmp/open.lep", "wb") as f:
        f.write(bytes(data.to_py()))
    d = D.load_project("/tmp/open.lep")
    for layer in d["layers"]:
        src = layer.get("src")
        if src and src not in _files and os.path.splitext(src)[1].lower() in core.IMAGE_EXTENSIONS:
            _register(src, layer.get("name", ""), src)
    return json.dumps(d)


def materials():
    return json.dumps(core.load_materials())


def defaults():
    d = core.Settings().to_dict()
    return json.dumps({"settings": d, "presets": {k: list(v) for k, v in core.PRESET_FIELDS.items()},
                       "dither_modes": list(core.DITHER_MODES), "version": core.VERSION})
