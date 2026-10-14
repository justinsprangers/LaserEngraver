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

import json
import os
import sys
from dataclasses import dataclass, asdict, fields

VERSION = "1.0.0"

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
        """Pixel size of the output, same rounding as the original script."""
        w = int(self.width_in * self.dpi)
        h = int(w * (src_h / src_w))
        return w, h

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


def save_material(name, profile):
    """Add or overwrite one material in materials.json."""
    data = _read_materials_file()
    data[name] = profile
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(MATERIALS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


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


_rembg_session = None


def remove_background(path):
    """AI subject cut-out. Returns (grayscale on white, alpha mask). Slow: ~1-5 s per photo."""
    global _rembg_session
    from rembg import new_session, remove      # imported here: heavy, and only needed for this
    if _rembg_session is None:
        _rembg_session = new_session("u2net")
    img = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    cut = remove(img, session=_rembg_session)
    return _flatten_on_white(cut).convert("L"), cut.split()[-1]


def load_source(path, remove_bg=False):
    """(grayscale, alpha or None). alpha marks the subject when the background was removed."""
    if remove_bg:
        return remove_background(path)
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
    return f"{base}_{s.material}_{s.width_in}in_{s.dpi}dpi_{kind}.png"


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


def process_file(path, out_dir, s, comparison=False):
    """Runs the full pipeline on one file and returns the output path."""
    src, alpha = load_source(path, s.remove_bg)
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
    side = int(min(src.width * tile_in / s.width_in, src.width, src.height))
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


def save_test_card(path, out_dir, s):
    src, alpha = load_source(path, s.remove_bg)
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
    ap.add_argument("--remove-bg", action="store_true", help="AI background removal (slow)")
    ap.add_argument("--outline", action="store_true", help="also save a cut-outline SVG")
    ap.add_argument("--outline-offset", type=float, default=0.1, help="inches outside the subject")
    ap.add_argument("--test-card", action="store_true", help="save a contrast/gamma test card instead")
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--preview", action="store_true", help="also save a side-by-side JPEG")
    a = ap.parse_args()

    s = Settings(style=a.style, material=a.material, width_in=a.width, dpi=a.dpi, dither=a.dither,
                 remove_bg=a.remove_bg, outline=a.outline, outline_offset_in=a.outline_offset)
    s.apply_material(materials)
    files = []
    for p in a.inputs:
        files += list_images(p) if os.path.isdir(p) else [p]
    for f in files:
        t = time.time()
        out = save_test_card(f, a.out, s) if a.test_card else process_file(f, a.out, s, a.preview)
        print(f"{os.path.basename(f)} -> {out}  ({time.time() - t:.1f} s)")
