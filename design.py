"""
design.py: multi-element engraving designs (images, logos, SVGs, text, arc text, shapes),
compositing them into one engraving, and project files.

A design is a plain dict (so it saves straight to JSON):
    {"version", "canvas": {"width_in", "height_in"}, "dpi", "material", "invert", "mirror",
     "outline", "outline_offset_in", "layers": [layer, ...]}      # layers bottom -> top

Every layer has: id, type, name, visible, x_in / y_in (center on the canvas, inches),
w_in / h_in (unrotated size), rotation (degrees, clockwise). Type-specific fields are listed
in new_layer(). Image layers carry their own photo/comic settings and cut-out fix strokes.

Rendering: each layer becomes two masks at the design DPI: `ink` (laser fires) and `cover`
(the layer is opaque here and hides what's below). Layers are stacked bottom to top, then
mirror is applied to the whole engraving last, so text flips correctly for back-engraving.
"""

import copy
import glob
import hashlib
import json
import math
import os
import uuid
import zipfile

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps

import engrave_core as core

DESIGN_VERSION = 1
PROJECT_EXT = ".lep"            # Laser Engraver Project: a zip with project.json + assets/
LAYER_TYPES = ("image", "logo", "svg", "text", "arctext", "shape")
TYPE_LABELS = {"image": "Photo", "logo": "Logo", "svg": "SVG", "text": "Text",
               "arctext": "Arc text", "shape": "Shape"}
# Image-layer fields that belong to each image (everything else in Settings is design-wide).
LAYER_SETTING_FIELDS = (
    "style", "dither", "remove_bg", "bg_model", "contrast", "brightness", "gamma",
    "shadow_amount", "shadow_radius", "use_clahe", "clahe_clip", "clahe_grid",
    "sharpen_radius", "sharpen_percent", "sharpen_threshold",
) + core.PRESET_FIELDS["comic"]
GLOBAL_FIELDS = ("material", "dpi", "invert", "mirror", "outline", "outline_offset_in")
PROJECTS_DIR = os.path.join(core.DATA_DIR, "project_assets")


# ------------------------------------------------------------------------------
# Model
# ------------------------------------------------------------------------------

def new_design(material="wood", width_in=6.0, height_in=4.0, dpi=254):
    materials = core.load_materials()
    m = materials.get(material, {})
    return {"version": DESIGN_VERSION, "canvas": {"width_in": width_in, "height_in": height_in},
            "dpi": dpi, "material": material, "invert": bool(m.get("invert", False)),
            "mirror": bool(m.get("mirror", False)), "outline": False, "outline_offset_in": 0.1,
            # Export: vector file(s) for cut lines, alignment frame, combined SVG (image + cuts)
            "cut_format": "svg", "frame": True, "combined": False,
            "layers": []}


def new_layer(kind, design, **kw):
    """A layer with sensible defaults, centered on the canvas."""
    cw, ch = design["canvas"]["width_in"], design["canvas"]["height_in"]
    base = {"id": uuid.uuid4().hex[:10], "type": kind, "name": TYPE_LABELS[kind], "visible": True,
            "x_in": cw / 2, "y_in": ch / 2, "w_in": min(cw, ch) * 0.6, "h_in": min(cw, ch) * 0.6,
            "rotation": 0.0,
            "cut": kind == "shape"}       # Cut = exported as vector cut lines, not engraved
    if kind == "image":
        s = core.Settings(material=design["material"])
        materials = core.load_materials()
        if design["material"] in materials:
            s.apply_material(materials)          # start from the material's tone settings
        base.update(src="", settings={f: getattr(s, f) for f in LAYER_SETTING_FIELDS},
                    strokes=[], crop=[0.0, 0.0, 1.0, 1.0])
    elif kind in ("logo", "svg"):
        base.update(src="", threshold=128, invert_art=False)
    elif kind in ("text", "arctext"):
        base.update(text="Your text", font="Arial", style="Bold", size_pt=36.0, outline=False,
                    stroke_mm=0.4, tracking=0.0)
        if kind == "text":
            base.update(align="center", line_spacing=1.1)
        else:
            base.update(radius_in=min(cw, ch) * 0.35, arc="top")
    elif kind == "shape":
        base.update(shape="rect", fill=False, stroke_mm=0.8, corner_in=0.0)
    base.update(kw)
    if kind in ("image", "logo", "svg") and base["src"]:
        fit_to_source(base)
    return base


def source_aspect(layer):
    """height / width of the layer's artwork (after crop for images)."""
    src = layer.get("src")
    if not src or not os.path.exists(src):
        return layer["h_in"] / max(layer["w_in"], 1e-6)
    if layer["type"] == "svg":
        r = _svg_page(src).rect
        return r.height / max(r.width, 1e-6)
    with Image.open(src) as im:
        w, h = ImageOps.exif_transpose(im).size
    if layer["type"] == "image":
        l, t, r, b = layer.get("crop", [0, 0, 1, 1])
        w, h = w * max(r - l, 0.01), h * max(b - t, 0.01)
    return h / max(w, 1e-6)


def fit_to_source(layer, max_in=None):
    """Sets h_in from the artwork's aspect ratio (keeps w_in)."""
    if max_in:
        layer["w_in"] = max_in
    layer["h_in"] = layer["w_in"] * source_aspect(layer)


def layer_settings(layer, design):
    """core.Settings for rendering an image layer at its size in this design."""
    d = dict(layer["settings"])
    d.update({f: design[f] for f in GLOBAL_FIELDS if f in design})
    d.update(width_in=layer["w_in"], mirror=False)      # mirror is applied to the whole design
    return core.Settings.from_dict(d)


def corners_px(layer, dpi):
    """The layer's rotated rectangle corners on the canvas, in pixels (TL, TR, BR, BL)."""
    cx, cy = layer["x_in"] * dpi, layer["y_in"] * dpi
    hw, hh = layer["w_in"] * dpi / 2, layer["h_in"] * dpi / 2
    a = math.radians(layer["rotation"])
    ca, sa = math.cos(a), math.sin(a)
    return [(cx + x * ca - y * sa, cy + x * sa + y * ca)
            for x, y in ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh))]


# ------------------------------------------------------------------------------
# Fonts
# ------------------------------------------------------------------------------

_FONTS = None


def fonts():
    """{family: {style: path}} for installed fonts (Windows + per-user fonts)."""
    global _FONTS
    if _FONTS is None:
        found = {}
        dirs = [os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"),
                os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Windows\Fonts")]
        for d in dirs:
            for path in glob.glob(os.path.join(d, "*")):
                if not path.lower().endswith((".ttf", ".otf", ".ttc")):
                    continue
                try:
                    fam, style = ImageFont.truetype(path, 12).getname()
                except Exception:
                    continue
                found.setdefault(fam, {}).setdefault(style, path)
        _FONTS = dict(sorted(found.items(), key=lambda kv: kv[0].lower()))
    return _FONTS


def font_path(family, style):
    styles = fonts().get(family) or fonts().get("Arial") or next(iter(fonts().values()), {})
    if not styles:
        return None
    return styles.get(style) or styles.get("Regular") or next(iter(styles.values()))


def _font(layer, dpi):
    px = max(4, round(layer["size_pt"] / 72 * dpi))
    path = font_path(layer["font"], layer["style"])
    return ImageFont.truetype(path, px) if path else ImageFont.load_default(size=px)


# ------------------------------------------------------------------------------
# Rendering each layer type -> (ink, cover) as uint8 L images (255 = yes), unrotated
# ------------------------------------------------------------------------------

def _px(v_in, dpi):
    return max(1, round(v_in * dpi))


def _art_masks(rgba, layer):
    """Logo / SVG artwork: opaque dark pixels burn; opaque pixels cover what's below."""
    arr = np.array(rgba.convert("RGBA"))
    alpha = arr[..., 3] > 127
    lum = arr[..., :3].astype(np.float32) @ np.array([0.299, 0.587, 0.114], np.float32)
    dark = lum < layer.get("threshold", 128)
    ink = alpha & (~dark if layer.get("invert_art") else dark)
    return _L(ink), _L(alpha)


def _L(mask):
    return Image.fromarray(np.where(mask, 255, 0).astype(np.uint8))


def _svg_page(path):
    import pymupdf
    return pymupdf.open(path)[0]


def render_logo(layer, dpi):
    size = (_px(layer["w_in"], dpi), _px(layer["h_in"], dpi))
    im = ImageOps.exif_transpose(Image.open(layer["src"])).convert("RGBA")
    return _art_masks(im.resize(size, Image.Resampling.LANCZOS), layer)


def render_svg(layer, dpi):
    import pymupdf
    page = _svg_page(layer["src"])
    w_px = _px(layer["w_in"], dpi)
    zoom = w_px / max(page.rect.width, 1e-6)
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=True)
    im = Image.frombytes("RGBA", (pix.width, pix.height), pix.samples)
    return _art_masks(im.resize((w_px, _px(layer["h_in"], dpi)), Image.Resampling.LANCZOS), layer)


def render_text(layer, dpi):
    font = _font(layer, dpi)
    stroke = max(1, round(layer.get("stroke_mm", 0.4) / 25.4 * dpi)) if layer.get("outline") else 0
    spacing = round((layer.get("line_spacing", 1.1) - 1) * font.size + font.size * 0.1)
    draw = ImageDraw.Draw(Image.new("L", (1, 1)))
    text = layer["text"] or " "
    l, t, r, b = draw.multiline_textbbox((0, 0), text, font=font, spacing=spacing,
                                        align=layer.get("align", "center"), stroke_width=stroke)
    l, t, r, b = math.floor(l), math.floor(t), math.ceil(r), math.ceil(b)
    pad = 2 + stroke
    im = Image.new("L", (r - l + 2 * pad, b - t + 2 * pad), 0)
    ImageDraw.Draw(im).multiline_text(
        (pad - l, pad - t), text, font=font, spacing=spacing, align=layer.get("align", "center"),
        fill=0 if stroke else 255, stroke_width=stroke, stroke_fill=255)
    ink = im.point(lambda v: 255 if v >= 128 else 0)
    return ink, ink


def render_arctext(layer, dpi):
    """Letters placed one by one along a circle: 'top' reads clockwise over the top,
    'bottom' reads left-to-right along the bottom (letters upright from below)."""
    font = _font(layer, dpi)
    stroke = max(1, round(layer.get("stroke_mm", 0.4) / 25.4 * dpi)) if layer.get("outline") else 0
    r = max(1.0, layer["radius_in"] * dpi)
    asc, desc = font.getmetrics()
    track = layer.get("tracking", 0.0) * font.size
    text = layer["text"] or " "
    adv = [font.getlength(ch) + track for ch in text]
    span = sum(adv) / r
    R = int(r + asc + desc + stroke + 4)
    canvas = Image.new("L", (2 * R, 2 * R), 0)
    top = layer.get("arc", "top") == "top"
    theta0 = -math.pi / 2 - span / 2 if top else math.pi / 2 + span / 2
    cum = 0.0
    for ch, a in zip(text, adv):
        mid = (cum + a / 2) / r
        th = theta0 + mid if top else theta0 - mid
        cum += a
        if ch.isspace():
            continue
        S = int(2 * (max(a, asc + desc) + stroke + 4))
        g = Image.new("L", (S, S), 0)                   # glyph with baseline-center at the middle
        ImageDraw.Draw(g).text((S / 2 - (a - track) / 2, S / 2 - asc), ch, font=font,
                               fill=0 if stroke else 255, stroke_width=stroke, stroke_fill=255)
        turn = math.degrees(th) + (90 if top else -90)  # clockwise turn so "up" faces out / in
        g = g.rotate(-turn, resample=Image.Resampling.BICUBIC)
        px, py = R + r * math.cos(th), R + r * math.sin(th)
        canvas.paste(255, (int(round(px - S / 2)), int(round(py - S / 2))), g)
    ink = canvas.point(lambda v: 255 if v >= 128 else 0)
    return ink, ink


def render_shape(layer, dpi):
    w, h = _px(layer["w_in"], dpi), _px(layer["h_in"], dpi)
    sw = max(1, round(layer.get("stroke_mm", 0.8) / 25.4 * dpi))
    im = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(im)
    fill = 255 if layer.get("fill") else None
    kind = layer.get("shape", "rect")
    if kind == "line":
        d.line((0, h / 2, w - 1, h / 2), fill=255, width=max(sw, 1))
    elif kind == "ellipse":
        d.ellipse((0, 0, w - 1, h - 1), fill=fill, outline=255, width=sw)
    else:
        rad = round(layer.get("corner_in", 0.0) * dpi)
        d.rounded_rectangle((0, 0, w - 1, h - 1), radius=min(rad, w // 2, h // 2), fill=fill,
                            outline=255, width=sw)
    return im, im


def render_image(layer, design):
    """Photo layer through the photo/comic pipeline, rotated BEFORE dithering so the dots stay
    on the laser's grid. Returns already-rotated (ink, cover)."""
    s = layer_settings(layer, design)
    src, alpha = core.load_source(layer["src"], s.remove_bg, layer.get("strokes") or None, s.bg_model)
    l, t, r, b = layer.get("crop", [0, 0, 1, 1])
    if (l, t, r, b) != (0, 0, 1, 1):
        box = (round(l * src.width), round(t * src.height), round(r * src.width), round(b * src.height))
        src = src.crop(box)
        alpha = alpha.crop(box) if alpha is not None else None
    if alpha is None:
        alpha = Image.new("L", src.size, 255)
    rot = layer["rotation"] % 360
    if rot:
        src = src.rotate(-rot, resample=Image.Resampling.BICUBIC, expand=True, fillcolor=255)
        alpha = alpha.rotate(-rot, resample=Image.Resampling.BICUBIC, expand=True, fillcolor=0)
    # Width of the rotated bounding box, so the unrotated image ends up exactly w_in wide.
    a = math.radians(rot)
    s.width_in = abs(layer["w_in"] * math.cos(a)) + abs(layer["h_in"] * math.sin(a))
    _, result, _ = core.render(src, alpha, s)
    cover = alpha.resize(result.size, Image.Resampling.LANCZOS).point(lambda v: 255 if v > 127 else 0)
    ink = ImageOps.invert(result.convert("L"))               # black (fire) -> 255
    return Image.fromarray(np.minimum(np.array(ink), np.array(cover))), cover


RENDERERS = {"logo": render_logo, "svg": render_svg, "text": render_text,
             "arctext": render_arctext, "shape": render_shape}

DRAFT_DPI = 100          # Layout view resolution: plenty for arranging, fast to redraw


def draft_cutout_ready(layer):
    s = layer["settings"]
    return bool(s.get("remove_bg")) and core.cached_cutout_alpha(
        layer["src"], layer.get("strokes") or None, s.get("bg_model", core.DEFAULT_BG_MODEL)) is not None


def render_image_draft(layer, dpi):
    """Photo as a plain grayscale picture (no tone, dithering or AI) for the Layout view.
    Uses the cut-out only if it's already cached. Returns rotated (tone uint8, cover bool)."""
    src = core.load_image(layer["src"])
    s = layer["settings"]
    alpha = core.cached_cutout_alpha(layer["src"], layer.get("strokes") or None,
                                     s.get("bg_model", core.DEFAULT_BG_MODEL)) if s.get("remove_bg") else None
    if alpha is not None and alpha.size != src.size:
        alpha = None
    l, t, r, b = layer.get("crop", [0, 0, 1, 1])
    if (l, t, r, b) != (0, 0, 1, 1):
        box = (round(l * src.width), round(t * src.height), round(r * src.width), round(b * src.height))
        src = src.crop(box)
        alpha = alpha.crop(box) if alpha is not None else None
    size = (_px(layer["w_in"], dpi), _px(layer["h_in"], dpi))
    src = src.resize(size, Image.Resampling.BILINEAR)
    alpha = alpha.resize(size, Image.Resampling.BILINEAR) if alpha is not None else Image.new("L", size, 255)
    rot = layer["rotation"] % 360
    if rot:
        src = src.rotate(-rot, resample=Image.Resampling.BILINEAR, expand=True, fillcolor=255)
        alpha = alpha.rotate(-rot, resample=Image.Resampling.BILINEAR, expand=True, fillcolor=0)
    return np.array(src), np.array(alpha) > 127


def _layer_key(layer, design):
    """Everything that changes how a layer looks, except where it sits."""
    skip = {"x_in", "y_in", "name", "visible"}
    if layer["type"] in ("text", "arctext"):
        skip |= {"w_in", "h_in"}                 # measured from the font, not inputs
    d = {k: v for k, v in layer.items() if k not in skip}
    d["_g"] = [design[f] for f in GLOBAL_FIELDS if f not in ("mirror", "outline", "outline_offset_in")]
    src = layer.get("src")
    if src and os.path.exists(src):
        d["_m"] = os.path.getmtime(src)
    return hashlib.sha1(json.dumps(d, sort_keys=True, default=str).encode()).hexdigest()


class Renderer:
    """
    Renders designs, caching each layer's masks so moving a layer is instant.
    draft=False: the real engraving at the design DPI (photos fully processed and dithered).
    draft=True:  the Layout view at DRAFT_DPI: photos as plain grayscale pictures, everything
                 else drawn normally. Fast, for arranging.
    """

    def __init__(self, limit=40, draft=False):
        self.cache, self.limit, self.draft = {}, limit, draft
        self.path_cache = {}
        self.placed = {}           # layer id -> (x0, y0, cover bool array) from the last render
        self.cuts = {}             # layer id -> cut polylines (canvas inches) from the last render

    def cut_paths(self, layer, design):
        """A Cut layer's vector lines in canvas inches (cached relative to the layer's position,
        so moving it is instant)."""
        import vectors
        key = _layer_key(layer, design)
        hit = self.path_cache.get(key)
        if hit is None:
            paths = vectors.layer_paths(layer, self, design)
            size = (layer["w_in"], layer["h_in"]) if layer["type"] in ("text", "arctext") else None
            rel = [([(x - layer["x_in"], y - layer["y_in"]) for x, y in pts], c) for pts, c in paths]
            hit = (rel, size)
            if len(self.path_cache) >= self.limit:
                self.path_cache.pop(next(iter(self.path_cache)))
            self.path_cache[key] = hit
        rel, size = hit
        if size:
            layer["w_in"], layer["h_in"] = size
        return [([(x + layer["x_in"], y + layer["y_in"]) for x, y in pts], c) for pts, c in rel]

    def _line_mask(self, paths, dpi, width=4):
        """Thick rasterized cut lines, so Cut items can be clicked on the canvas."""
        pts = [p for poly, _ in paths for p in poly]
        if not pts:
            return 0, 0, np.zeros((1, 1), bool)
        xs, ys = [p[0] * dpi for p in pts], [p[1] * dpi for p in pts]
        x0, y0 = int(min(xs)) - width, int(min(ys)) - width
        m = np.zeros((int(max(ys)) - y0 + width + 1, int(max(xs)) - x0 + width + 1), np.uint8)
        for poly, closed in paths:
            arr = np.array([[(x * dpi - x0), (y * dpi - y0)] for x, y in poly], np.int32).reshape(-1, 1, 2)
            cv2.polylines(m, [arr], closed, 1, thickness=width)
        return x0, y0, m.astype(bool)

    def dpi(self, design):
        return min(design["dpi"], DRAFT_DPI) if self.draft else design["dpi"]

    def layer_masks(self, layer, design):
        """(value, cover) arrays for one layer, rotated. value = ink (bool) for the engraving,
        or gray tone (uint8, 0 = black) for the Layout view."""
        key = _layer_key(layer, design)
        if self.draft and layer["type"] == "image":
            key += f"|cut={draft_cutout_ready(layer)}"
        hit = self.cache.get(key)
        if hit is None:
            dpi = self.dpi(design)
            size = None
            if layer["type"] == "image":
                if self.draft:
                    value, cover = render_image_draft(layer, dpi)
                else:
                    ink, cover = render_image(layer, design)
                    value, cover = np.array(ink) >= 128, np.array(cover) >= 128
            else:
                ink, cover = RENDERERS[layer["type"]](layer, dpi)
                if layer["type"] in ("text", "arctext"):
                    size = (ink.width / dpi, ink.height / dpi)     # text size comes from the font
                rot = layer["rotation"] % 360
                if rot:
                    ink = ink.rotate(-rot, resample=Image.Resampling.BICUBIC, expand=True)
                    cover = cover.rotate(-rot, resample=Image.Resampling.BICUBIC, expand=True)
                value, cover = np.array(ink) >= 128, np.array(cover) >= 128
                if self.draft:
                    value = np.where(value, 0, 255).astype(np.uint8)
            hit = (value, cover, size)
            if len(self.cache) >= self.limit:
                self.cache.pop(next(iter(self.cache)))
            self.cache[key] = hit
        if hit[2]:
            layer["w_in"], layer["h_in"] = hit[2]       # for the selection handles
        return hit[0], hit[1]

    def compose(self, design):
        """Whole canvas, before mirror: (burn bool, cover bool) for the engraving, or
        (gray uint8, cover bool) for the Layout view."""
        dpi = self.dpi(design)
        W = _px(design["canvas"]["width_in"], dpi)
        H = _px(design["canvas"]["height_in"], dpi)
        burn = np.full((H, W), 255, np.uint8) if self.draft else np.zeros((H, W), bool)
        cover_all = np.zeros((H, W), bool)
        self.placed, self.cuts = {}, {}
        for layer in design["layers"]:
            if not layer.get("visible", True):
                continue
            if layer["type"] in ("image", "logo", "svg") and not (layer.get("src") and os.path.exists(layer["src"])):
                continue
            if layer.get("cut"):
                # Cut items aren't engraved: they become vector lines (shown red in the preview).
                paths = self.cut_paths(layer, design)
                self.cuts[layer["id"]] = paths
                self.placed[layer["id"]] = self._line_mask(paths, dpi)
                continue
            ink, cover = self.layer_masks(layer, design)
            h, w = ink.shape
            x0 = round(layer["x_in"] * dpi - w / 2)
            y0 = round(layer["y_in"] * dpi - h / 2)
            self.placed[layer["id"]] = (x0, y0, cover)
            # Clip to the canvas.
            cx0, cy0, cx1, cy1 = max(0, x0), max(0, y0), min(W, x0 + w), min(H, y0 + h)
            if cx1 <= cx0 or cy1 <= cy0:
                continue
            sub_ink = ink[cy0 - y0:cy1 - y0, cx0 - x0:cx1 - x0]
            sub_cov = cover[cy0 - y0:cy1 - y0, cx0 - x0:cx1 - x0]
            region = burn[cy0:cy1, cx0:cx1]
            region[sub_cov] = sub_ink[sub_cov]
            cover_all[cy0:cy1, cx0:cx1] |= sub_cov
        return burn, cover_all

    def engraving(self, design):
        """Final 1-bit engraving (mirror applied) and the subject mask for the cut outline."""
        assert not self.draft, "use a full (non-draft) Renderer for the engraving"
        burn, cover = self.compose(design)
        img = Image.fromarray(np.where(burn, 0, 255).astype(np.uint8)).convert("1")
        if design.get("mirror"):
            img, cover = ImageOps.mirror(img), cover[:, ::-1]
        return img, cover

    def hit_test(self, design, x_px, y_px, tolerance=6):
        """Topmost visible layer at a pixel of this renderer's last composite (real shapes, so
        thin text is clickable)."""
        hits = self.hits(design, x_px, y_px, tolerance)
        return hits[0] if hits else None

    def hits(self, design, x_px, y_px, tolerance=6):
        """All visible layers under a pixel, topmost first."""
        found = []
        for layer in reversed(design["layers"]):
            p = self.placed.get(layer["id"])
            if not p or not layer.get("visible", True):
                continue
            x0, y0, cover = p
            h, w = cover.shape
            lx, ly = int(x_px - x0), int(y_px - y0)
            if -tolerance <= lx < w + tolerance and -tolerance <= ly < h + tolerance:
                ys = slice(max(0, ly - tolerance), min(h, ly + tolerance + 1))
                xs = slice(max(0, lx - tolerance), min(w, lx + tolerance + 1))
                if cover[ys, xs].any():
                    found.append(layer)
        return found


CUT_FORMATS = {"none": (), "svg": ("svg",), "dxf": ("dxf",), "svg+dxf": ("svg", "dxf")}

# Length fields (inches / mm / points) that shrink with the design for a test copy.
# DPI, tone settings and dithering stay the same, so the dots burn like the real job.
_SCALED_DESIGN = ("outline_offset_in",)
_SCALED_LAYER = ("x_in", "y_in", "w_in", "h_in", "size_pt", "radius_in", "corner_in", "stroke_mm")


def scaled_design(design, factor):
    """A copy of the design shrunk (or grown) by `factor`, for a small test burn."""
    d = copy.deepcopy(design)
    d["canvas"] = {k: v * factor for k, v in d["canvas"].items()}
    for f in _SCALED_DESIGN:
        if f in d:
            d[f] *= factor
    for layer in d["layers"]:
        for f in _SCALED_LAYER:
            if f in layer:
                layer[f] *= factor
    return d


def all_cut_paths(design, renderer, cover):
    """Every cut line in the design (Cut items + the optional outline), canvas inches.
    Call after renderer.compose()/engraving() so renderer.cuts is current."""
    import vectors
    paths = [p for lid in [l["id"] for l in design["layers"]] for p in renderer.cuts.get(lid, [])]
    if design.get("outline") and cover.any():
        paths += vectors.outline_paths(cover, renderer.dpi(design), design.get("outline_offset_in", 0.1))
    return paths


def save_engraving(design, renderer, out_path):
    """Writes the engraving PNG (if anything is engraved) and the cut-line file(s) chosen in the
    design: <name>_cut.svg / _cut.dxf, plus <name>_combined.svg if enabled. Returns the files."""
    import vectors
    burn, cover = renderer.compose(design)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    written = []
    if burn.any():
        img = Image.fromarray(np.where(burn, 0, 255).astype(np.uint8)).convert("1")
        if design.get("mirror"):
            img = ImageOps.mirror(img)
        img.save(out_path, dpi=(design["dpi"], design["dpi"]))
        written.append(out_path)
    paths = all_cut_paths(design, renderer, cover)
    if paths:
        base, frame = out_path[:-4], design.get("frame", True)
        for fmt in CUT_FORMATS.get(design.get("cut_format", "svg"), ("svg",)):
            f = f"{base}_cut.{fmt}"
            (vectors.write_svg if fmt == "svg" else vectors.write_dxf)(paths, design, f, frame=frame)
            written.append(f)
        if design.get("combined"):
            f = f"{base}_combined.svg"
            vectors.write_svg(paths, design, f, frame=False, png_path=out_path if burn.any() else None)
            written.append(f)
    return written


# ------------------------------------------------------------------------------
# Project files (.lep = zip of project.json + copies of every image used)
# ------------------------------------------------------------------------------

def save_project(design, path):
    d = copy.deepcopy(design)
    with zipfile.ZipFile(path + ".tmp", "w", zipfile.ZIP_DEFLATED) as z:
        added = {}
        for layer in d["layers"]:
            src = layer.get("src")
            if not src:
                continue
            if src not in added:
                if not os.path.exists(src):
                    raise FileNotFoundError(f"Image for layer '{layer['name']}' is missing: {src}")
                with open(src, "rb") as f:
                    data = f.read()
                name = f"assets/{hashlib.sha1(data).hexdigest()[:16]}{os.path.splitext(src)[1].lower()}"
                z.writestr(name, data)
                added[src] = name
            layer["src"] = added[src]
        z.writestr("project.json", json.dumps(d, indent=1))
    os.replace(path + ".tmp", path)


def load_project(path):
    """Opens a .lep. Images are unpacked to the app's data folder (named by content, so
    reopening reuses the same files and their cached cut-outs)."""
    with zipfile.ZipFile(path) as z:
        d = json.loads(z.read("project.json"))
        os.makedirs(PROJECTS_DIR, exist_ok=True)
        for layer in d["layers"]:
            src = layer.get("src")
            if src and src.startswith("assets/"):
                out = os.path.join(PROJECTS_DIR, os.path.basename(src))
                if not os.path.exists(out):
                    with open(out + ".tmp", "wb") as f:
                        f.write(z.read(src))
                    os.replace(out + ".tmp", out)
                layer["src"] = out
    if d.get("version", 1) > DESIGN_VERSION:
        raise ValueError("This project was made with a newer version of Laser Engraver.")
    base = new_design()
    base.update({k: v for k, v in d.items() if k != "layers"})
    base["layers"] = [_with_defaults(l, base) for l in d.get("layers", [])]
    return base


def _with_defaults(layer, design):
    """Fills in fields added in later versions so old projects keep opening."""
    filled = new_layer(layer["type"], design)
    filled["cut"] = False             # items saved before Cut existed were all engraved
    filled.update(layer)
    if layer["type"] == "image":
        settings = dict(new_layer("image", design)["settings"])
        settings.update(layer.get("settings", {}))
        filled["settings"] = settings
    return filled
