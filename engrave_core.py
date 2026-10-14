"""
engrave_core.py: the Imag-R-style processing engine used by engrave_app.py.

This is the pipeline from "--LASER ENGRAVING TOOLS--/Imag-R Mimic Processing/
image_processing.py", reorganized so a UI can call it. Same steps, same order,
same default numbers. Differences from the original:
  - Settings are passed in (a Settings object) instead of read from globals.
  - Invert and mirror are their own switches. Acrylic still turns both on by default.
  - Phone photos are rotated upright using their EXIF tag before processing.
  - Stucki and Bayer dithering run much faster (numba / NumPy). Same output.

Can also be run from the command line for a quick batch:
    python engrave_core.py <image or folder> [more ...] --material acrylic --width 6
"""

import json
import os
from dataclasses import dataclass, asdict, fields

import cv2
import numpy as np
from PIL import Image, ImageChops, ImageEnhance, ImageFilter, ImageOps

HERE = os.path.dirname(os.path.abspath(__file__))
MATERIALS_FILE = os.path.join(HERE, "materials.json")
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tiff", ".tif")
DITHER_MODES = ("stucki", "floyd-steinberg", "bayer", "none")

# Used only if materials.json is missing. Numbers match the original script.
DEFAULT_MATERIALS = {
    "wood":      {"contrast": 1.2, "brightness": 1.2, "gamma": 1.7,  "invert": False, "mirror": False,
                  "look": ["#d9b38c", "#5a3a22"]},
    "acrylic":   {"contrast": 1.4, "brightness": 1.0, "gamma": 0.85, "invert": True,  "mirror": True,
                  "look": ["#1e2228", "#e8eef2"]},
    "leather":   {"contrast": 1.3, "brightness": 1.1, "gamma": 1.1,  "invert": False, "mirror": False,
                  "look": ["#a0703f", "#3b2412"]},
    "slate":     {"contrast": 1.5, "brightness": 0.9, "gamma": 0.9,  "invert": False, "mirror": False,
                  "look": ["#3a3d42", "#b9bcbf"]},
    "cardboard": {"contrast": 1.1, "brightness": 1.2, "gamma": 1.3,  "invert": False, "mirror": False,
                  "look": ["#c8a77a", "#4b3520"]},
}


# ------------------------------------------------------------------------------
# Settings
# ------------------------------------------------------------------------------

@dataclass
class Settings:
    material: str = "acrylic"
    width_in: float = 6.0
    dpi: int = 254
    dither: str = "stucki"
    # Material profile (filled from materials.json, then adjustable)
    contrast: float = 1.4
    brightness: float = 1.0
    gamma: float = 0.85
    invert: bool = True
    mirror: bool = True
    # Shadow detail recovery
    shadow_amount: float = 0.15
    shadow_radius: int = 15
    # CLAHE micro-texture
    use_clahe: bool = True
    clahe_clip: float = 1.5
    clahe_grid: int = 8
    # Unsharp mask
    sharpen_radius: float = 2
    sharpen_percent: int = 150
    sharpen_threshold: int = 3

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


def load_materials():
    try:
        with open(MATERIALS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return {k: v for k, v in data.items() if not k.startswith("_")}
    except (OSError, ValueError):
        return dict(DEFAULT_MATERIALS)


def save_material(name, profile):
    """Add or overwrite one material in materials.json."""
    try:
        with open(MATERIALS_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = dict(DEFAULT_MATERIALS)
    data[name] = profile
    with open(MATERIALS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


# ------------------------------------------------------------------------------
# Pipeline steps
# ------------------------------------------------------------------------------

def load_image(path):
    """Opens any image, rotates it upright, and returns 8-bit grayscale."""
    img = Image.open(path)
    img = ImageOps.exif_transpose(img)
    if img.mode == "L":
        return img
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        img = img.convert("RGBA")
        background = Image.new("RGB", img.size, (255, 255, 255))
        background.paste(img, mask=img.split()[-1])
        return background.convert("L")
    return img.convert("L")


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


def adjust(resized, s):
    """Steps 2-8 of the original: everything between resizing and dithering."""
    img = recover_shadow_detail(resized, s.shadow_amount, s.shadow_radius)
    if s.use_clahe:
        img = apply_clahe(img, s.clahe_clip, s.clahe_grid)
    img = img.filter(ImageFilter.UnsharpMask(
        radius=s.sharpen_radius, percent=s.sharpen_percent, threshold=s.sharpen_threshold))
    img = ImageEnhance.Contrast(img).enhance(s.contrast)
    img = ImageEnhance.Brightness(img).enhance(s.brightness)
    img = apply_gamma(img, s.gamma)
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


# numba compiles the loop to machine code (~100x faster). Without it, the plain
# Python loop still works, just slowly (tens of seconds per photo).
try:
    import numba
    _stucki_fast = numba.njit(cache=True, nogil=True)(_stucki_loop)
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
# Whole-file processing
# ------------------------------------------------------------------------------

def output_filename(path, s):
    base = os.path.splitext(os.path.basename(path))[0]
    return f"{base}_{s.material}_{s.width_in}in_{s.dpi}dpi_{s.dither}.png"


def save_engraving(dithered, out_path, s):
    dithered.save(out_path, dpi=(s.dpi, s.dpi))


def save_comparison(gray, dithered, out_path):
    """Side-by-side JPEG: processed grayscale (left) vs. engraving (right)."""
    w, h = gray.size
    preview = Image.new("L", (w * 2 + 20, h), color=128)
    preview.paste(gray, (0, 0))
    preview.paste(dithered.convert("L"), (w + 20, 0))
    preview.save(out_path[:-4] + "_preview.jpg", "JPEG", quality=85)


def process_file(path, out_dir, s, comparison=False):
    """Runs the full pipeline on one file and returns the output path."""
    gray = adjust(resize_to_target(load_image(path), s), s)
    result = dither(gray, s.dither)
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, output_filename(path, s))
    save_engraving(result, out_path, s)
    if comparison:
        save_comparison(gray, result, out_path)
    return out_path


def list_images(folder):
    return sorted(os.path.join(folder, f) for f in os.listdir(folder)
                  if f.lower().endswith(IMAGE_EXTENSIONS))


if __name__ == "__main__":
    import argparse
    import time

    materials = load_materials()
    ap = argparse.ArgumentParser(description="Batch-process images for laser engraving.")
    ap.add_argument("inputs", nargs="+", help="image files and/or folders")
    ap.add_argument("--material", default="acrylic", choices=sorted(materials))
    ap.add_argument("--width", type=float, default=6.0, help="engraving width in inches")
    ap.add_argument("--dpi", type=int, default=254)
    ap.add_argument("--dither", default="stucki", choices=DITHER_MODES)
    ap.add_argument("--out", default=os.path.join(HERE, "output"))
    ap.add_argument("--preview", action="store_true", help="also save a side-by-side JPEG")
    a = ap.parse_args()

    s = Settings(material=a.material, width_in=a.width, dpi=a.dpi, dither=a.dither)
    s.apply_material(materials)
    files = []
    for p in a.inputs:
        files += list_images(p) if os.path.isdir(p) else [p]
    for f in files:
        t = time.time()
        out = process_file(f, a.out, s, a.preview)
        print(f"{os.path.basename(f)} -> {out}  ({time.time() - t:.1f} s)")
