"""
engrave_core.py: the processing engine used by engrave_app.py.

Two styles:
  photo  The pipeline from "--LASER ENGRAVING TOOLS--/Imag-R Mimic Processing/
         image_processing.py", reorganized so a UI can call it. Same steps, same order,
         same default numbers. With background removal off, output is pixel-identical.
  comic  Flat tones filled with halftone/hatch patterns plus ink outlines. Built from
         the adaptive-threshold idea in "Justin Custom Processing".

Differences from the original photo script:
  - Settings are passed in (a Settings object) instead of read from globals.
  - Invert and mirror are their own switches. Acrylic still turns both on by default.
  - Phone photos are rotated upright using their EXIF tag before processing.
  - Stucki and Bayer dithering run much faster (numba / NumPy). Same output.
  - Optional AI background removal (rembg) and cut-outline SVG export.

Can also be run from the command line:
    python engrave_core.py <image or folder> [more ...] --material acrylic --width 6
    python engrave_core.py photo.jpg --style comic --remove-bg --outline
"""

import hashlib
import json
import math
import os
import sys
import threading
from dataclasses import dataclass, asdict, fields

VERSION = "1.9.1"

# Where things live. Running from source, everything is in this folder. As an installed app
# (PyInstaller sets sys.frozen), the program folder is read-only, so user data goes to
# %APPDATA%\LaserEngraver and engravings default to Documents\Laser Engraver.
FROZEN = getattr(sys, "frozen", False)
HERE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))   # bundled files
if FROZEN:
    DATA_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "LaserEngraver")
    DEFAULT_OUT = os.path.join(os.path.expanduser("~"), "Documents", "Laser Engraver")
    os.environ.setdefault("NUMBA_CACHE_DIR", os.path.join(
        os.environ.get("LOCALAPPDATA", DATA_DIR), "LaserEngraver", "numba_cache"))
else:
    DATA_DIR = HERE
    DEFAULT_OUT = os.path.join(HERE, "output")

import cv2
import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

MATERIALS_FILE = os.path.join(DATA_DIR, "materials.json")     # your editable copy
BUNDLED_MATERIALS = os.path.join(HERE, "materials.json")      # shipped defaults
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tiff", ".tif")
DITHER_MODES = ("stucki", "floyd-steinberg", "bayer", "none")
STYLES = ("photo", "comic")

# Used only if materials.json is missing. Photo numbers match the original script.
DEFAULT_MATERIALS = {
    "wood":      {"contrast": 1.2, "brightness": 1.2, "gamma": 1.7,  "invert": False, "mirror": False,
                  "look": ["#d9b38c", "#5a3a22"]},
    "acrylic":   {"contrast": 1.4, "brightness": 1.0, "gamma": 0.85, "invert": True,  "mirror": True,
                  "look": ["#1e2228", "#e8eef2"]},
    "leather":   {"contrast": 1.3, "brightness": 1.1, "gamma": 1.1,  "invert": False, "mirror": False,
                  "look": ["#a0703f", "#3b2412"]},
    "slate":     {"contrast": 1.5, "brightness": 0.9, "gamma": 0.9,  "invert": True,  "mirror": False,
                  "look": ["#3a3d42", "#b9bcbf"]},
    "cardboard": {"contrast": 1.1, "brightness": 1.2, "gamma": 1.3,  "invert": False, "mirror": False,
                  "look": ["#c8a77a", "#4b3520"]},
}


# ------------------------------------------------------------------------------
# Settings
# ------------------------------------------------------------------------------

@dataclass
class Settings:
    style: str = "photo"
    material: str = "acrylic"
    width_in: float = 6.0
    dpi: int = 254
    dither: str = "stucki"
    remove_bg: bool = False
    bg_model: str = "u2net"        # one of BG_MODELS
    # Material profile (filled from materials.json, then adjustable)
    contrast: float = 1.4
    brightness: float = 1.0
    gamma: float = 0.85
    invert: bool = True
    mirror: bool = True
    # Photo: shadow detail recovery
    shadow_amount: float = 0.15
    shadow_radius: int = 15
    # Photo: CLAHE micro-texture
    use_clahe: bool = True
    clahe_clip: float = 1.5
    clahe_grid: int = 8
    # Photo: unsharp mask
    sharpen_radius: float = 2
    sharpen_percent: int = 150
    sharpen_threshold: int = 3
    # Size by width (default) or height; crop / rotate before processing (Photos mode)
    size_by: str = "width"
    height_in: float = 4.0
    rotate: float = 0.0            # degrees clockwise; non-90° angles auto-crop the empty corners
    crop_l: float = 0.0            # fraction removed from each edge (0 = none)
    crop_t: float = 0.0
    crop_r: float = 0.0
    crop_b: float = 0.0
    # Comic
    comic_levels: int = 4          # tone bands, 2-5 (darkest = solid, lightest = blank)
    comic_smooth: int = 4          # bilateral passes; more = flatter, more cartoon-like
    comic_dot_mm: float = 0.8      # spacing of halftone dots / hatch lines
    comic_line_block: int = 15     # neighborhood (px) for finding ink lines; bigger = bolder shapes
    comic_line_c: float = 6        # line sensitivity; higher = fewer lines
    comic_line_weight: int = 1     # thicken ink lines by this many px
    comic_despeckle: int = 40      # drop ink specks smaller than this many px
    # Cut outline
    outline: bool = False
    outline_offset_in: float = 0.1

    @classmethod
    def from_dict(cls, d):
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})

    def to_dict(self):
        return asdict(self)

    def apply_material(self, materials):
        p = materials[self.material]
        for key in ("contrast", "brightness", "gamma", "invert", "mirror"):
            if key in p:
                setattr(self, key, p[key])

    def output_size(self, src_w, src_h):
        """Pixel size of the output, same rounding as the original script (by width), or set
        by height instead."""
        if self.size_by == "height":
            h = int(self.height_in * self.dpi)
            return int(h * (src_w / src_h)), h
        w = int(self.width_in * self.dpi)
        h = int(w * (src_h / src_w))
        return w, h

    def size_in(self, src_w, src_h):
        """(width, height) of the engraving in inches."""
        w, h = self.output_size(src_w, src_h)
        return w / self.dpi, h / self.dpi

    def mm_to_px(self, mm):
        return mm * self.dpi / 25.4


def _read_materials_file():
    """Full materials.json contents: your copy if it exists, else the shipped one."""
    for path in (MATERIALS_FILE, BUNDLED_MATERIALS):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            pass
    return dict(DEFAULT_MATERIALS)


def load_materials():
    return {k: v for k, v in _read_materials_file().items() if not k.startswith("_")}


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def save_material(name, profile):
    """Add or overwrite one material in materials.json."""
    data = _read_materials_file()
    data[name] = profile
    _write_json(MATERIALS_FILE, data)


def rename_material(old, new):
    """Renames a material, keeping its position in the file."""
    data = _read_materials_file()
    _write_json(MATERIALS_FILE, {(new if k == old else k): v for k, v in data.items()})


def delete_material(name):
    data = _read_materials_file()
    data.pop(name, None)
    _write_json(MATERIALS_FILE, data)


def material_name(text):
    """Material names end up in output filenames, so keep them simple: lowercase, underscores."""
    keep = "".join(c if c.isalnum() or c in "-_" else "_" for c in text.strip().lower().replace(" ", "_"))
    return keep.strip("_")


# ------------------------------------------------------------------------------
# Style presets (named sets of photo or comic settings), stored in presets.json
# ------------------------------------------------------------------------------

PRESETS_FILE = os.path.join(DATA_DIR, "presets.json")
PRESET_FIELDS = {
    "photo": ("dither", "shadow_amount", "shadow_radius", "use_clahe", "clahe_clip", "clahe_grid",
              "sharpen_radius", "sharpen_percent", "sharpen_threshold"),
    "comic": ("comic_levels", "comic_smooth", "comic_dot_mm", "comic_line_block", "comic_line_c",
              "comic_line_weight", "comic_despeckle"),
}
DEFAULT_PRESET = "Default"      # built in (the original settings); can't be renamed or deleted


def _read_presets():
    try:
        with open(PRESETS_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    return {style: dict(data.get(style, {})) for style in PRESET_FIELDS}


def load_presets(style):
    """{name: {field: value}} for 'photo' or 'comic', Default first."""
    d = Settings()
    presets = {DEFAULT_PRESET: {f: getattr(d, f) for f in PRESET_FIELDS[style]}}
    presets.update({k: v for k, v in _read_presets()[style].items() if k != DEFAULT_PRESET})
    return presets


def save_preset(style, name, settings):
    data = _read_presets()
    data[style][name] = {f: getattr(settings, f) for f in PRESET_FIELDS[style]}
    _write_json(PRESETS_FILE, data)


def rename_preset(style, old, new):
    data = _read_presets()
    data[style] = {(new if k == old else k): v for k, v in data[style].items()}
    _write_json(PRESETS_FILE, data)


def delete_preset(style, name):
    data = _read_presets()
    data[style].pop(name, None)
    _write_json(PRESETS_FILE, data)


# ------------------------------------------------------------------------------
# Loading
# ------------------------------------------------------------------------------

def _flatten_on_white(img):
    """RGBA/LA/transparent-P -> RGB on white, matching the original script."""
    img = img.convert("RGBA")
    background = Image.new("RGB", img.size, (255, 255, 255))
    background.paste(img, mask=img.split()[-1])
    return background


def load_image(path):
    """Opens any image, rotates it upright, and returns 8-bit grayscale."""
    img = Image.open(path)
    img = ImageOps.exif_transpose(img)
    if img.mode == "L":
        return img
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        return _flatten_on_white(img).convert("L")
    return img.convert("L")


_rembg_sessions = {}
_rembg_lock = threading.Lock()

# Background-removal AI models (rembg names). Each is a one-time download to %USERPROFILE%\.u2net.
BG_MODELS = {                  # name -> (menu label, download size, speed note)
    # Speeds measured on this PC's CPU (16 threads); portrait not measured.
    "u2net":                 ("General (u2net)", "170 MB", "~0.5 s/photo"),
    "u2net_human_seg":       ("People only (u2net human)", "170 MB", "~0.5 s/photo, people only"),
    "isnet-general-use":     ("Sharper edges (IS-Net)", "180 MB", "~1 s/photo"),
    "birefnet-general-lite": ("Best quality, lite (BiRefNet)", "220 MB", "~7-17 s/photo"),
    "birefnet-portrait":     ("Best portraits (BiRefNet)", "970 MB", "slowest, likely 30 s+/photo"),
}
DEFAULT_BG_MODEL = "u2net"

# The model downloader prints a progress bar; the installed app has no console to print to.
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")


def model_path(model):
    home = os.environ.get("U2NET_HOME", os.path.join(os.path.expanduser("~"), ".u2net"))
    return os.path.join(home, f"{model}.onnx")


def model_downloaded(model):
    return os.path.exists(model_path(model))

# Cut-out results are cached on disk, keyed by file path + modified time (+ strokes), so
# nothing is recomputed when you reopen an image, Process all, or make a test card.
CACHE_DIR = os.path.join(DATA_DIR, "cutout_cache")
CACHE_VERSION = "3"           # bump when the cut-out algorithm changes, to ignore old results
CACHE_LIMIT = 300             # newest files kept


def warm_up(model=DEFAULT_BG_MODEL):
    """Loads an AI model (~4 s; downloads it first if needed). Call early, in a background thread."""
    with _rembg_lock:
        if model not in _rembg_sessions:
            from rembg import new_session      # imported here: heavy, and only needed for this
            _rembg_sessions[model] = new_session(model)
    return _rembg_sessions[model]


def _cache_file(path, tag):
    st = os.stat(path)
    base = hashlib.sha1(f"{os.path.abspath(path)}|{st.st_mtime}|{st.st_size}|{CACHE_VERSION}"
                        .encode()).hexdigest()[:24]
    return os.path.join(CACHE_DIR, f"{base}_{tag}.png")


def _read_mask(f, size):
    try:
        m = Image.open(f)
        m.load()
        return m if m.size == size and m.mode == "L" else None
    except (OSError, ValueError):
        return None


def _write_mask(f, mask):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = f + ".tmp"
        mask.save(tmp, "PNG")
        os.replace(tmp, f)
        files = sorted((os.path.join(CACHE_DIR, n) for n in os.listdir(CACHE_DIR)
                        if n.endswith(".png")), key=os.path.getmtime)
        for old in files[:-CACHE_LIMIT]:
            os.remove(old)
    except OSError:
        pass                                   # caching is optional


def ai_cutout(path, model=DEFAULT_BG_MODEL):
    """AI subject cut-out via rembg. Returns (RGB image, alpha mask). ~1 s per photo for u2net."""
    img = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    f = _cache_file(path, f"ai-{model}")
    hit = _read_mask(f, img.size)
    if hit is not None:
        return img, hit
    from rembg import remove
    alpha = remove(img, session=warm_up(model)).split()[-1]
    _write_mask(f, alpha)
    return img, alpha


def compose_on_white(rgb, alpha):
    """Subject over a white background, as grayscale (what the engraving pipeline uses)."""
    return _flatten_on_white(Image.merge("RGBA", (*rgb.split(), alpha))).convert("L")


# --- Feedback fixes ---------------------------------------------------------
# A "stroke" is (kind, radius, [(x, y), ...]) in source-image pixels; kind is "keep" or "remove".
# refine_alpha() re-segments the image using the strokes as labeled training pixels:
# GrabCut fits Gaussian color models for subject vs. background (seeded by the AI's guess
# plus your strokes) and solves a graph cut. Only wrong regions you touched are changed.

# Stroke kinds. "keep"/"remove" are smart (re-cut the surrounding region with GrabCut);
# "paint_keep"/"paint_remove" are a plain brush: exactly the painted pixels, nothing else.
KEEP_KINDS = ("keep", "paint_keep")


def _rasterize(strokes, shape, scale=1.0):
    """Masks (keep, remove) with the strokes painted in, at the given scale."""
    keep, remove = np.zeros(shape, np.uint8), np.zeros(shape, np.uint8)
    for kind, radius, pts in strokes:
        target = keep if kind in KEEP_KINDS else remove
        r = max(1, int(round(radius * scale)))
        p = np.round(np.array(pts, np.float32) * scale).astype(np.int32)
        if len(p) == 1:
            cv2.circle(target, tuple(int(v) for v in p[0]), r, 1, -1)
        else:
            cv2.polylines(target, [p.reshape(-1, 1, 2)], False, 1, thickness=2 * r, lineType=cv2.LINE_8)
            for q in (p[0], p[-1]):
                cv2.circle(target, tuple(int(v) for v in q), r, 1, -1)
    return keep.astype(bool), remove.astype(bool)


def stroke_key(strokes):
    """Stable ID for a list of strokes (same whether loaded from JSON or drawn)."""
    norm = [(k, round(float(r), 1), [(round(float(x), 1), round(float(y), 1)) for x, y in p])
            for k, r, p in strokes]
    return hashlib.sha1(repr(norm).encode()).hexdigest()


def refine_alpha(rgb, alpha, strokes, cache=None):
    """
    Corrected alpha mask: applies the strokes one at a time, each on top of the previous
    result. A Keep stroke can only add to the subject and a Remove stroke can only take away,
    so a new fix never undoes or shifts an earlier one. `cache` (a dict) remembers results
    for each prefix of the stroke list, so adding one stroke only computes one step.
    """
    cur, start = alpha, 0
    if cache is not None:
        for i in range(len(strokes), 0, -1):
            hit = cache.get(stroke_key(strokes[:i]))
            if hit is not None:
                cur, start = hit, i
                break
    for i in range(start, len(strokes)):
        cur = _apply_stroke(rgb, cur, strokes[:i + 1])
        if cache is not None:
            cache[stroke_key(strokes[:i + 1])] = cur
    return cur


def _paint_stroke(alpha, stroke):
    """Plain brush: sets exactly the painted pixels (with a 1 px anti-aliased edge)."""
    kind, radius, pts = stroke
    A = np.array(alpha).astype(np.float32) / 255
    m = np.zeros(A.shape, np.uint8)
    r = max(1, int(round(radius)))
    p = np.round(np.array(pts, np.float32) * 16).astype(np.int32)     # 4 fractional bits
    if len(p) == 1:
        cv2.circle(m, tuple(int(v) for v in p[0]), r * 16, 255, -1, cv2.LINE_AA, shift=4)
    else:
        cv2.polylines(m, [p.reshape(-1, 1, 2)], False, 255, 2 * r, cv2.LINE_AA, shift=4)
        for q in (p[0], p[-1]):
            cv2.circle(m, tuple(int(v) for v in q), r * 16, 255, -1, cv2.LINE_AA, shift=4)
    m = m.astype(np.float32) / 255
    A = np.maximum(A, m) if kind in KEEP_KINDS else np.minimum(A, 1 - m)
    return Image.fromarray(np.round(A * 255).astype(np.uint8))


def _apply_stroke(rgb, alpha, strokes, max_side=400, iterations=4):
    """Applies the last stroke in `strokes` to `alpha` (earlier strokes stay as fixed labels)."""
    kind, radius, pts = strokes[-1]
    if kind.startswith("paint_"):
        return _paint_stroke(alpha, strokes[-1])
    adding = kind == "keep"
    W, H = rgb.size
    k = min(1.0, max_side / max(W, H))
    w, h = max(1, round(W * k)), max(1, round(H * k))
    img = cv2.cvtColor(np.array(rgb.resize((w, h), Image.Resampling.BILINEAR)), cv2.COLOR_RGB2BGR)
    a0 = np.array(alpha.resize((w, h), Image.Resampling.BILINEAR)) >= 128
    new_k, new_r = _rasterize([strokes[-1]], (h, w), k)
    new = new_k if adding else new_r

    mask = np.where(a0, cv2.GC_PR_FGD, cv2.GC_PR_BGD).astype(np.uint8)
    # Spread the new stroke's label over nearby, connected, similar-colored pixels, so the
    # color models aren't trained on the very region you said is wrong.
    if new.any():
        lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB).astype(np.float32)
        region = _similar_region(lab, new, max(4 * radius * k, 0.06 * max(w, h)))
        mask[region] = cv2.GC_PR_FGD if adding else cv2.GC_PR_BGD
    for st in strokes:                         # all strokes are firm labels; later ones win
        sk, sr = _rasterize([st], (h, w), k)
        is_keep = st[0] in KEEP_KINDS
        mask[sk if is_keep else sr] = cv2.GC_FGD if is_keep else cv2.GC_BGD

    has_fg = ((mask == cv2.GC_FGD) | (mask == cv2.GC_PR_FGD)).any()
    has_bg = ((mask == cv2.GC_BGD) | (mask == cv2.GC_PR_BGD)).any()
    accepted = np.zeros((h, w), bool)
    if has_fg and has_bg:
        # GrabCut: fit color models to the labeled pixels, then a graph cut re-labels the rest.
        bgd, fgd = np.zeros((1, 65), np.float64), np.zeros((1, 65), np.float64)
        cv2.setRNGSeed(0)       # same strokes -> same result (preview matches the saved file)
        cv2.grabCut(img, mask, None, bgd, fgd, iterations, cv2.GC_INIT_WITH_MASK)
        fg = (mask == cv2.GC_FGD) | (mask == cv2.GC_PR_FGD)
        # Only changes in this stroke's direction, in regions this stroke touches.
        change = (fg & ~a0) if adding else (a0 & ~fg)
        n, labels = cv2.connectedComponents(change.astype(np.uint8), connectivity=8)
        near = cv2.dilate(new.astype(np.uint8), np.ones((5, 5), np.uint8)) > 0
        touched = np.unique(labels[near & change])
        accepted = np.isin(labels, touched[touched > 0])

    # Back to full size with smooth boundaries (blur before upscaling avoids stair-steps).
    up = cv2.resize(cv2.GaussianBlur(accepted.astype(np.float32), (0, 0), 0.8), (W, H),
                    interpolation=cv2.INTER_CUBIC) >= 0.5
    new_fk, new_fr = _rasterize([strokes[-1]], (H, W))
    new_full = new_fk if adding else new_fr
    region = up | new_full                      # everything this stroke changes
    A = np.array(alpha).astype(np.float32) / 255
    hard = (A >= 0.5) | region if adding else (A >= 0.5) & ~region

    # Re-soften edges around the change so they follow the photo's real edges, and clear the
    # AI's old soft fringe around removed areas (otherwise it lingers as ghosts / dashed
    # outlines). Direction-locked too: Keep only raises alpha, Remove only lowers it.
    band = max(7, round(max(W, H) / 80))
    weight = cv2.dilate(region.astype(np.uint8),
                        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * band + 1, 2 * band + 1)))
    weight = np.clip(cv2.GaussianBlur(weight.astype(np.float32), (0, 0), band / 3) * 1.5, 0, 1)
    ys, xs = np.nonzero(weight > 0)
    if len(ys):
        r = max(2, round(max(W, H) / 400))
        y0, y1 = max(0, ys.min() - 2 * r), min(H, ys.max() + 2 * r + 1)
        x0, x1 = max(0, xs.min() - 2 * r), min(W, xs.max() + 2 * r + 1)
        guide = np.array(rgb.convert("L"))[y0:y1, x0:x1].astype(np.float32) / 255
        soft = np.clip(_guided_filter(guide, hard[y0:y1, x0:x1].astype(np.float32), r, 1e-3), 0, 1)
        wgt, old = weight[y0:y1, x0:x1], A[y0:y1, x0:x1]
        blended = wgt * soft + (1 - wgt) * old
        A[y0:y1, x0:x1] = np.maximum(old, blended) if adding else np.minimum(old, blended)
    A[new_full] = 1.0 if adding else 0.0        # painted pixels are always obeyed
    return Image.fromarray(np.round(A * 255).astype(np.uint8))


def _similar_region(lab, painted, reach, tol=12.0):
    """
    Pixels near the paint (within `reach` px) that are connected to it and close in color to
    it. Deliberately short-range: it only seeds GrabCut, which decides the rest of the region.
    """
    center = np.median(lab[painted], axis=0)
    close = np.sqrt(((lab - center) ** 2).sum(axis=2)) <= tol
    dist = cv2.distanceTransform((~painted).astype(np.uint8), cv2.DIST_L2, 3)
    cand = ((close & (dist <= reach)) | painted).astype(np.uint8)
    n, labels = cv2.connectedComponents(cand, connectivity=4)
    hit = np.unique(labels[painted])
    return np.isin(labels, hit[hit > 0])


def _guided_filter(guide, src, r, eps):
    """Edge-aware smoothing (He et al.): softens src's edges so they follow edges in guide."""
    box = lambda m: cv2.boxFilter(m, -1, (2 * r + 1, 2 * r + 1))
    mean_i, mean_p = box(guide), box(src)
    var_i = box(guide * guide) - mean_i * mean_i
    a = (box(guide * src) - mean_i * mean_p) / (var_i + eps)
    b = mean_p - a * mean_i
    return box(a) * guide + box(b)


def refined_cutout(path, rgb, alpha, strokes, cache=None, model=DEFAULT_BG_MODEL):
    """refine_alpha() with a disk cache, so Save / Process all / test card reuse the preview's work."""
    if not strokes:
        return alpha
    f = _cache_file(path, f"fix-{model}-" + stroke_key(strokes)[:20])
    hit = _read_mask(f, rgb.size)
    if hit is not None:
        return hit
    alpha = refine_alpha(rgb, alpha, strokes, cache)
    _write_mask(f, alpha)
    return alpha


def cached_cutout_alpha(path, strokes=None, model=DEFAULT_BG_MODEL):
    """The cut-out mask from the disk cache only (never runs the AI). None if not cached yet."""
    try:
        tag = f"fix-{model}-" + stroke_key(strokes)[:20] if strokes else f"ai-{model}"
        f = _cache_file(path, tag)
        if not os.path.exists(f):
            return None
        m = Image.open(f)
        m.load()
        return m if m.mode == "L" else None
    except (OSError, ValueError):
        return None


def remove_background(path, strokes=None, model=DEFAULT_BG_MODEL):
    """(grayscale on white, alpha mask), with feedback strokes applied if given."""
    rgb, alpha = ai_cutout(path, model)
    alpha = refined_cutout(path, rgb, alpha, strokes, model=model)
    return compose_on_white(rgb, alpha), alpha


def load_source(path, remove_bg=False, strokes=None, model=DEFAULT_BG_MODEL):
    """(grayscale, alpha or None). alpha marks the subject when the background was removed."""
    if remove_bg:
        return remove_background(path, strokes, model)
    return load_image(path), None


# ------------------------------------------------------------------------------
# Photo style (Imag-R pipeline)
# ------------------------------------------------------------------------------

def resize_to_target(img, s):
    return img.resize(s.output_size(img.width, img.height), Image.Resampling.LANCZOS)


def apply_gamma(img, gamma):
    if gamma == 1.0:
        return img
    lut = [int((i / 255.0) ** (1.0 / gamma) * 255) for i in range(256)]
    return img.point(lut)


def recover_shadow_detail(img, amount, radius):
    """Lightens dark areas (screen blend of a blurred negative) to keep shadow texture."""
    if amount <= 0:
        return img
    inv = ImageOps.invert(img)
    blur = inv.filter(ImageFilter.GaussianBlur(radius=radius))
    dark_blur = Image.blend(Image.new("L", img.size, 0), blur, amount)
    return ImageChops.screen(img, dark_blur)


def apply_clahe(img, clip, grid):
    clahe = cv2.createCLAHE(clipLimit=clip, tileGridSize=(grid, grid))
    return Image.fromarray(clahe.apply(np.array(img)))


def tone(img, s):
    """Material profile: contrast, brightness, gamma (same order as the original)."""
    img = ImageEnhance.Contrast(img).enhance(s.contrast)
    img = ImageEnhance.Brightness(img).enhance(s.brightness)
    return apply_gamma(img, s.gamma)


def adjust(resized, s):
    """Steps 2-8 of the original: everything between resizing and dithering."""
    img = recover_shadow_detail(resized, s.shadow_amount, s.shadow_radius)
    if s.use_clahe:
        img = apply_clahe(img, s.clahe_clip, s.clahe_grid)
    img = img.filter(ImageFilter.UnsharpMask(
        radius=s.sharpen_radius, percent=s.sharpen_percent, threshold=s.sharpen_threshold))
    img = tone(img, s)
    if s.invert:
        img = ImageOps.invert(img)
    if s.mirror:
        img = ImageOps.mirror(img)
    return img


# --- Dithering ---

def _stucki_loop(arr):
    """Stucki error diffusion, in place on a float64 array. Same math as the original."""
    h, w = arr.shape
    for y in range(h):
        for x in range(w):
            old = arr[y, x]
            new = 255.0 if old > 127.0 else 0.0
            arr[y, x] = new
            err = old - new
            if x + 1 < w: arr[y, x + 1] += err * (8 / 42)
            if x + 2 < w: arr[y, x + 2] += err * (4 / 42)
            if y + 1 < h:
                if x - 2 >= 0: arr[y + 1, x - 2] += err * (2 / 42)
                if x - 1 >= 0: arr[y + 1, x - 1] += err * (4 / 42)
                arr[y + 1, x] += err * (8 / 42)
                if x + 1 < w: arr[y + 1, x + 1] += err * (4 / 42)
                if x + 2 < w: arr[y + 1, x + 2] += err * (2 / 42)
            if y + 2 < h:
                if x - 2 >= 0: arr[y + 2, x - 2] += err * (1 / 42)
                if x - 1 >= 0: arr[y + 2, x - 1] += err * (2 / 42)
                arr[y + 2, x] += err * (4 / 42)
                if x + 1 < w: arr[y + 2, x + 1] += err * (2 / 42)
                if x + 2 < w: arr[y + 2, x + 2] += err * (1 / 42)


# numba compiles the loop to machine code (~40x faster). Without it, the plain
# Python loop still works, just slowly (tens of seconds per photo).
try:
    import numba
    try:
        _stucki_fast = numba.njit(cache=True, nogil=True)(_stucki_loop)
    except Exception:
        # Caching needs the .py source next to the code; a packaged app may not have it.
        # Still fast, just recompiles (~1 s) on first use each launch.
        _stucki_fast = numba.njit(nogil=True)(_stucki_loop)
    FAST_STUCKI = True
except Exception:
    _stucki_fast = _stucki_loop
    FAST_STUCKI = False


def stucki_dither(img):
    arr = np.array(img, dtype=np.float64)
    _stucki_fast(arr)
    return Image.fromarray(np.where(arr > 127.0, 255, 0).astype(np.uint8)).convert("1")


BAYER_8X8 = np.array([
    [ 0, 32,  8, 40,  2, 34, 10, 42],
    [48, 16, 56, 24, 50, 18, 58, 26],
    [12, 44,  4, 36, 14, 46,  6, 38],
    [60, 28, 52, 20, 62, 30, 54, 22],
    [ 3, 35, 11, 43,  1, 33,  9, 41],
    [51, 19, 59, 27, 49, 17, 57, 25],
    [15, 47,  7, 39, 13, 45,  5, 37],
    [63, 31, 55, 23, 61, 29, 53, 21],
])


def bayer_dither(img):
    arr = np.array(img)
    h, w = arr.shape
    thresh = (BAYER_8X8 * (255 / 64)).astype(int)
    tiled = np.tile(thresh, (h // 8 + 1, w // 8 + 1))[:h, :w]
    return Image.fromarray(np.where(arr > tiled, 255, 0).astype(np.uint8)).convert("1")


def dither(img, mode):
    if mode == "stucki":
        return stucki_dither(img)
    if mode == "floyd-steinberg":
        return img.convert("1", dither=Image.Dither.FLOYDSTEINBERG)
    if mode == "bayer":
        return bayer_dither(img)
    return img.convert("1", dither=Image.Dither.NONE)


# ------------------------------------------------------------------------------
# Comic style
# ------------------------------------------------------------------------------

# Fill pattern for each tone band, darkest first. True = laser fires.
PATTERN_SETS = {
    2: ("solid", "blank"),
    3: ("solid", "dots", "blank"),
    4: ("solid", "crosshatch", "dots", "blank"),
    5: ("solid", "crosshatch", "hatch", "dots", "blank"),
}


def _pattern(name, h, w, cell):
    if name == "solid":
        return np.ones((h, w), bool)
    if name == "blank":
        return np.zeros((h, w), bool)
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    line = max(1.0, cell * 0.22)
    if name == "dots":
        # Classic comic dots on a 45-degree grid.
        u, v = (xx + yy) / 1.4142, (xx - yy) / 1.4142
        du, dv = (u % cell) - cell / 2, (v % cell) - cell / 2
        return du * du + dv * dv < (cell * 0.3) ** 2
    hatch = ((xx + yy) % cell) < line
    if name == "hatch":
        return hatch
    return hatch | (((xx - yy) % cell) < line)          # crosshatch


def _smooth(gray_arr, passes):
    """Flattens shading into cartoon-like regions. Runs at <=1200 px for speed."""
    if passes <= 0:
        return gray_arr
    h, w = gray_arr.shape
    k = min(1.0, 1200 / max(h, w))
    small = cv2.resize(gray_arr, (round(w * k), round(h * k)), interpolation=cv2.INTER_AREA) if k < 1 else gray_arr
    for _ in range(passes):
        small = cv2.bilateralFilter(small, 9, 30, 7)
    return cv2.resize(small, (w, h), interpolation=cv2.INTER_CUBIC) if k < 1 else small


def _despeckle(mask, min_area):
    if min_area <= 0:
        return mask
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=8)
    keep = stats[:, cv2.CC_STAT_AREA] >= min_area
    keep[0] = False
    return keep[labels]


def comic(resized, s, alpha=None):
    """Returns (tone-band preview, 1-bit engraving)."""
    flat = _smooth(np.array(resized), s.comic_smooth)
    toned = np.array(tone(Image.fromarray(flat), s))

    # Ink lines: pixels noticeably darker than their neighborhood.
    block = max(3, int(s.comic_line_block) | 1)
    ink = cv2.adaptiveThreshold(cv2.medianBlur(flat, 5), 255, cv2.ADAPTIVE_THRESH_MEAN_C,
                                cv2.THRESH_BINARY, block, s.comic_line_c) == 0
    if alpha is not None:
        # Outline the cut-out subject too, and never draw ink on the removed background.
        a = np.array(alpha) > 127
        edge = cv2.morphologyEx(a.astype(np.uint8), cv2.MORPH_GRADIENT, np.ones((3, 3), np.uint8)) > 0
        ink = (ink & a) | edge
    ink = _despeckle(ink, s.comic_despeckle)
    if s.comic_line_weight > 0:
        k = 2 * int(s.comic_line_weight) + 1
        ink = cv2.dilate(ink.astype(np.uint8), np.ones((k, k), np.uint8)) > 0

    # Tone bands. Inverted = scratchboard look: shadows stay unburned, ink stays burned.
    levels = int(min(5, max(2, s.comic_levels)))
    bands = np.minimum((toned.astype(np.int32) * levels) // 256, levels - 1)
    if s.invert:
        bands = levels - 1 - bands
    h, w = bands.shape
    cell = max(3.0, s.mm_to_px(s.comic_dot_mm))
    fill = np.zeros((h, w), bool)
    for i, name in enumerate(PATTERN_SETS[levels]):
        fill |= (bands == i) & _pattern(name, h, w, cell)
    if alpha is not None:
        fill &= np.array(alpha) > 127

    burn = fill | ink
    preview = (bands * (255 // (levels - 1))).astype(np.uint8)
    preview[ink] = 0
    out = Image.fromarray(np.where(burn, 0, 255).astype(np.uint8)).convert("1")
    preview = Image.fromarray(preview)
    if s.mirror:
        out, preview = ImageOps.mirror(out), ImageOps.mirror(preview)
    return preview, out


# ------------------------------------------------------------------------------
# Rendering, outline, files
# ------------------------------------------------------------------------------

def render(src, alpha, s):
    """Full pipeline on a loaded source. Returns (adjusted preview, 1-bit result, subject mask)."""
    resized = resize_to_target(src, s)
    ra = alpha.resize(resized.size, Image.Resampling.LANCZOS) if alpha is not None else None
    if s.style == "comic":
        gray, result = comic(resized, s, ra)
    else:
        gray = adjust(resized, s)
        result = dither(gray, s.dither)
    # Subject mask for the cut outline: the cut-out if we have one, else anything not paper-white.
    mask = (np.array(ra) > 127) if ra is not None else (np.array(resized) < 250)
    if s.mirror:
        mask = mask[:, ::-1]
    return gray, result, mask


def outline_svg(mask, s):
    """Cut line around the subject, grown by outline_offset_in, as an SVG at true physical size."""
    h, w = mask.shape
    m = mask.astype(np.uint8) * 255
    # Close small gaps so the cut follows the overall silhouette, not every hair.
    close = max(3, int(s.mm_to_px(2)) | 1)
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (close, close)))
    off = int(round(s.outline_offset_in * s.dpi))
    pad = abs(off) + 2
    m = cv2.copyMakeBorder(m, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=0)
    if off != 0:
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * abs(off) + 1, 2 * abs(off) + 1))
        m = cv2.dilate(m, k) if off > 0 else cv2.erode(m, k)
    contours, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not contours:
        raise ValueError("No subject found to outline.")
    c = max(contours, key=cv2.contourArea)
    c = cv2.approxPolyDP(c, max(0.5, s.dpi / 254 * 0.75), True)
    pts = " ".join(f"{x - pad:.1f},{y - pad:.1f}" for x, y in c[:, 0, :])
    # Canvas grows by the offset so nothing is clipped. User units = engraving pixels, so the
    # engraving PNG spans 0,0 to w,h and the canvas starts at -pad.
    W, H = w + 2 * pad, h + 2 * pad
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W / s.dpi:.4f}in" height="{H / s.dpi:.4f}in" '
            f'viewBox="{-pad} {-pad} {W} {H}">\n'
            f'  <!-- Engraving PNG occupies 0,0 to {w},{h} in these units ({w / s.dpi:.3f} x {h / s.dpi:.3f} in). -->\n'
            f'  <polygon points="{pts}" fill="none" stroke="#ff0000" stroke-width="{s.dpi / 254:.2f}"/>\n'
            f'</svg>\n')


def output_filename(path, s):
    base = os.path.splitext(os.path.basename(path))[0]
    kind = "comic" if s.style == "comic" else s.dither
    size = f"{s.width_in}in" if s.size_by != "height" else f"{s.height_in}in-tall"
    return f"{base}_{s.material}_{size}_{s.dpi}dpi_{kind}.png"


# ------------------------------------------------------------------------------
# Crop / rotate (Photos mode), and job-time estimates
# ------------------------------------------------------------------------------

def _inscribed(w, h, angle):
    """Largest axis-aligned rectangle inside a w x h rectangle rotated by `angle` radians."""
    if w <= 0 or h <= 0:
        return 0, 0
    long_side, short_side = (w, h) if w >= h else (h, w)
    sa, ca = abs(math.sin(angle)), abs(math.cos(angle))
    if short_side <= 2 * sa * ca * long_side or abs(sa - ca) < 1e-10:
        x = 0.5 * short_side
        wr, hr = (x / sa, x / ca) if w >= h else (x / ca, x / sa)
    else:
        cos_2a = ca * ca - sa * sa
        wr, hr = (w * ca - h * sa) / cos_2a, (h * ca - w * sa) / cos_2a
    return wr, hr


def prepare(src, alpha, s):
    """Applies the Photos-mode rotate and crop to the source (and its cut-out mask)."""
    rot = s.rotate % 360
    if rot:
        quarter = {90: Image.Transpose.ROTATE_270, 180: Image.Transpose.ROTATE_180,
                   270: Image.Transpose.ROTATE_90}
        if rot in quarter:
            src = src.transpose(quarter[rot])
            alpha = alpha.transpose(quarter[rot]) if alpha is not None else None
        else:
            w, h = src.size
            src = src.rotate(-rot, resample=Image.Resampling.BICUBIC, expand=True, fillcolor=255)
            alpha = alpha.rotate(-rot, resample=Image.Resampling.BICUBIC, expand=True,
                                 fillcolor=0) if alpha is not None else None
            # Straightening: crop away the empty corners the rotation creates.
            wr, hr = _inscribed(w, h, math.radians(rot))
            box = (round((src.width - wr) / 2), round((src.height - hr) / 2),
                   round((src.width + wr) / 2), round((src.height + hr) / 2))
            src = src.crop(box)
            alpha = alpha.crop(box) if alpha is not None else None
    if s.crop_l or s.crop_t or s.crop_r or s.crop_b:
        W, H = src.size
        box = (round(s.crop_l * W), round(s.crop_t * H), round((1 - s.crop_r) * W), round((1 - s.crop_b) * H))
        if box[2] - box[0] >= 8 and box[3] - box[1] >= 8:
            src = src.crop(box)
            alpha = alpha.crop(box) if alpha is not None else None
    return src, alpha


def raster_stats(burn):
    """(total scanned px, rows with burn) for a burn mask (True = laser fires). Each row is
    scanned from its first to its last burn pixel, like most laser software does."""
    rows = np.flatnonzero(burn.any(axis=1))
    if not len(rows):
        return 0, 0
    sub = burn[rows]
    first = sub.argmax(axis=1)
    last = sub.shape[1] - 1 - sub[:, ::-1].argmax(axis=1)
    return int((last - first + 1).sum()), int(len(rows))


def estimate_seconds(stats, dpi, speed_mm_s, overscan_mm=2.5):
    """Rough raster engraving time: scanned length plus overscan per line, at constant speed
    (ignores acceleration, so real jobs usually take somewhat longer)."""
    total_px, rows = stats
    if not rows or speed_mm_s <= 0:
        return 0.0
    return (total_px * 25.4 / dpi + rows * 2 * overscan_mm) / speed_mm_s


def format_duration(seconds):
    m, s = divmod(int(round(seconds)), 60)
    h, m = divmod(m, 60)
    return f"{h} h {m:02d} min" if h else (f"{m} min {s:02d} s" if m else f"{s} s")


def save_engraving(dithered, out_path, s):
    dithered.save(out_path, dpi=(s.dpi, s.dpi))


def save_comparison(gray, dithered, out_path):
    """Side-by-side JPEG: processed grayscale (left) vs. engraving (right)."""
    w, h = gray.size
    preview = Image.new("L", (w * 2 + 20, h), color=128)
    preview.paste(gray, (0, 0))
    preview.paste(dithered.convert("L"), (w + 20, 0))
    preview.save(out_path[:-4] + "_preview.jpg", "JPEG", quality=85)


def save_outputs(path, out_dir, s, gray, result, mask, comparison=False):
    """Writes the engraving plus any extras. Returns the engraving's path."""
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, output_filename(path, s))
    save_engraving(result, out_path, s)
    if comparison:
        save_comparison(gray, result, out_path)
    if s.outline:
        with open(out_path[:-4] + "_outline.svg", "w", encoding="utf-8") as f:
            f.write(outline_svg(mask, s))
    return out_path


def process_file(path, out_dir, s, comparison=False, strokes=None):
    """Runs the full pipeline on one file and returns the output path."""
    src, alpha = prepare(*load_source(path, s.remove_bg, strokes, s.bg_model), s)
    gray, result, mask = render(src, alpha, s)
    return save_outputs(path, out_dir, s, gray, result, mask, comparison)


# ------------------------------------------------------------------------------
# Test card
# ------------------------------------------------------------------------------

def _font(px):
    try:
        return ImageFont.truetype("arialbd.ttf", px)
    except OSError:
        return ImageFont.load_default(size=px)


def test_card(src, alpha, s, tile_in=1.0):
    """
    3x3 grid of the image's center, each tile at a different contrast (columns) and gamma (rows)
    around the current values, with a 0-100% tone ramp under each. Burn it on scrap and copy
    the numbers under the best tile into the app. Returns a 1-bit image at s.dpi.
    """
    contrasts = [round(s.contrast * f, 2) for f in (0.85, 1.0, 1.15)]
    gammas = [round(s.gamma * f, 2) for f in (0.8, 1.0, 1.25)]
    tile = int(tile_in * s.dpi)
    ramp_h, label_h, gap = int(0.15 * s.dpi), int(0.14 * s.dpi), int(0.12 * s.dpi)
    cell_h = tile + ramp_h + label_h + gap
    head_h = int(0.3 * s.dpi)
    W = 3 * tile + 4 * gap
    H = head_h + 3 * cell_h + gap

    # Center crop covering one tile's worth of the real engraving, so detail is at true scale.
    side = int(min(src.width * tile_in / s.size_in(src.width, src.height)[0], src.width, src.height))
    box = ((src.width - side) // 2, (src.height - side) // 2)
    box = box + (box[0] + side, box[1] + side)
    crop_src = src.crop(box)
    crop_alpha = alpha.crop(box) if alpha is not None else None
    ramp = Image.fromarray(np.tile(np.linspace(0, 255, tile).astype(np.uint8), (ramp_h, 1)))

    card = Image.new("L", (W, H), 255)
    draw = ImageDraw.Draw(card)
    font = _font(max(10, int(label_h * 0.75)))
    hf = _font(max(10, int(head_h * 0.36)))
    kind = "comic" if s.style == "comic" else s.dither
    draw.text((gap, int(head_h * 0.05)), f"{s.material} / {kind} / {s.dpi} DPI", fill=0, font=hf)
    draw.text((gap, int(head_h * 0.5)), "columns: contrast    rows: gamma", fill=0, font=hf)
    one = Settings.from_dict({**s.to_dict(), "width_in": tile_in, "mirror": False})
    for r, g in enumerate(gammas):
        for c, k in enumerate(contrasts):
            t = Settings.from_dict({**one.to_dict(), "contrast": k, "gamma": g})
            x, y = gap + c * (tile + gap), head_h + r * cell_h
            _, img, _ = render(crop_src, crop_alpha, t)
            card.paste(img.convert("L").resize((tile, tile)), (x, y))
            # The ramp shows how this contrast/gamma maps every tone from black to white.
            rimg = comic(ramp, t)[1] if s.style == "comic" else dither(adjust(ramp, t), t.dither)
            card.paste(rimg.convert("L"), (x, y + tile))
            draw.text((x, y + tile + ramp_h + 2), f"C {k:g}   G {g:g}", fill=0, font=font)
    out = card.convert("1", dither=Image.Dither.NONE)
    return ImageOps.mirror(out) if s.mirror else out


def save_test_card(path, out_dir, s, strokes=None):
    src, alpha = prepare(*load_source(path, s.remove_bg, strokes, s.bg_model), s)
    card = test_card(src, alpha, s)
    os.makedirs(out_dir, exist_ok=True)
    base = os.path.splitext(os.path.basename(path))[0]
    out = os.path.join(out_dir, f"{base}_testcard_{s.material}_{s.style}_{s.dpi}dpi.png")
    card.save(out, dpi=(s.dpi, s.dpi))
    return out


def list_images(folder):
    return sorted(os.path.join(folder, f) for f in os.listdir(folder)
                  if f.lower().endswith(IMAGE_EXTENSIONS))


if __name__ == "__main__":
    import argparse
    import time

    materials = load_materials()
    ap = argparse.ArgumentParser(description="Batch-process images for laser engraving.")
    ap.add_argument("inputs", nargs="+", help="image files and/or folders")
    ap.add_argument("--style", default="photo", choices=STYLES)
    ap.add_argument("--material", default="acrylic", choices=sorted(materials))
    ap.add_argument("--width", type=float, default=6.0, help="engraving width in inches")
    ap.add_argument("--dpi", type=int, default=254)
    ap.add_argument("--dither", default="stucki", choices=DITHER_MODES)
    ap.add_argument("--remove-bg", action="store_true", help="AI background removal")
    ap.add_argument("--bg-model", default=DEFAULT_BG_MODEL, choices=list(BG_MODELS),
                    help="AI model for background removal (downloads once)")
    ap.add_argument("--outline", action="store_true", help="also save a cut-outline SVG")
    ap.add_argument("--outline-offset", type=float, default=0.1, help="inches outside the subject")
    ap.add_argument("--test-card", action="store_true", help="save a contrast/gamma test card instead")
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--preview", action="store_true", help="also save a side-by-side JPEG")
    a = ap.parse_args()

    s = Settings(style=a.style, material=a.material, width_in=a.width, dpi=a.dpi, dither=a.dither,
                 remove_bg=a.remove_bg, bg_model=a.bg_model, outline=a.outline, outline_offset_in=a.outline_offset)
    s.apply_material(materials)
    files = []
    for p in a.inputs:
        files += list_images(p) if os.path.isdir(p) else [p]
    for f in files:
        t = time.time()
        out = save_test_card(f, a.out, s) if a.test_card else process_file(f, a.out, s, a.preview)
        print(f"{os.path.basename(f)} -> {out}  ({time.time() - t:.1f} s)")
