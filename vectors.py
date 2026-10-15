"""
vectors.py: cut lines for Design mode, and SVG / DXF writers.

Any layer can be set to Cut instead of Engrave. Its cut lines are polylines in canvas inches:
    shapes      their outline (rectangle with rounded corners, ellipse, line)
    text        letter outlines (fontTools), laid out the same way for preview and export
    arc text    letter outlines placed along the circle
    SVG         the SVG's own paths (PyMuPDF)
    logo/photo  the traced outline of the artwork / cut-out
Cut items are left out of the engraving PNG. The writers produce files in millimetres with
cut lines in pure red (#FF0000) and an optional blue canvas frame for lining the PNG up in
software that doesn't keep file positions (Trotec Ruby, xTool, LaserGRBL, LightBurn...).
"""

import base64
import io
import math
import os

import cv2
import numpy as np

CUT_RGB = "#ff0000"
FRAME_RGB = "#0000ff"
CURVE_STEPS = 8                      # line segments per font / SVG curve


# ------------------------------------------------------------------------------
# Geometry helpers
# ------------------------------------------------------------------------------

def _place(polys, layer):
    """Local polylines (inches, centered on 0,0) -> canvas inches (rotate, then move)."""
    a = math.radians(layer["rotation"])
    ca, sa = math.cos(a), math.sin(a)
    cx, cy = layer["x_in"], layer["y_in"]
    out = []
    for pts, closed in polys:
        out.append(([(cx + x * ca - y * sa, cy + x * sa + y * ca) for x, y in pts], closed))
    return out


def _bbox(polys):
    xs = [x for pts, _ in polys for x, _ in pts]
    ys = [y for pts, _ in polys for _, y in pts]
    return (min(xs), min(ys), max(xs), max(ys)) if xs else (0, 0, 0, 0)


def _center(polys):
    """Shifts local polylines so their bounding box is centered on 0,0. Returns (polys, w, h)."""
    l, t, r, b = _bbox(polys)
    mx, my = (l + r) / 2, (t + b) / 2
    return [([(x - mx, y - my) for x, y in pts], c) for pts, c in polys], r - l, b - t


def _ellipse(w, h, n=None):
    n = n or max(48, int(math.pi * (w + h) / 0.02))        # ~0.5 mm segments
    return [(w / 2 * math.cos(2 * math.pi * i / n), h / 2 * math.sin(2 * math.pi * i / n)) for i in range(n)]


def _rounded_rect(w, h, r):
    r = max(0.0, min(r, w / 2, h / 2))
    if r <= 0:
        return [(-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)]
    pts, n = [], 12
    for (cx, cy, a0) in ((w / 2 - r, -h / 2 + r, -90), (w / 2 - r, h / 2 - r, 0),
                         (-w / 2 + r, h / 2 - r, 90), (-w / 2 + r, -h / 2 + r, 180)):
        for i in range(n + 1):
            a = math.radians(a0 + 90 * i / n)
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


# ------------------------------------------------------------------------------
# Text outlines (fontTools)
# ------------------------------------------------------------------------------

_TT = {}


def _ttfont(path, family, style):
    from fontTools.ttLib import TTFont, TTCollection
    key = (path, family, style)
    if key not in _TT:
        if path.lower().endswith(".ttc"):
            coll = TTCollection(path)
            font = coll.fonts[0]
            for f in coll.fonts:                 # pick the face matching family + style
                names = f["name"]
                if names.getDebugName(1) == family and (names.getDebugName(2) or "Regular") == style:
                    font = f
                    break
        else:
            font = TTFont(path)
        _TT[key] = font
    return _TT[key]


class _FlattenPen:
    """fontTools pen that turns glyph outlines into polylines (font units)."""

    def __init__(self):
        self.polys, self.cur = [], []

    def moveTo(self, p):
        self._close()
        self.cur = [p]

    def lineTo(self, p):
        self.cur.append(p)

    def curveTo(self, *pts):                     # cubic (possibly several off-curve points)
        start = self.cur[-1]
        ctrl = [start] + list(pts)
        while len(ctrl) >= 4:
            p0, p1, p2, p3 = ctrl[:4]
            for i in range(1, CURVE_STEPS + 1):
                t = i / CURVE_STEPS
                u = 1 - t
                self.cur.append((u ** 3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t ** 3 * p3[0],
                                 u ** 3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t ** 3 * p3[1]))
            ctrl = ctrl[3:]

    def qCurveTo(self, *pts):                    # TrueType quadratic with implied on-points
        start = self.cur[-1]
        if pts[-1] is None:                      # closed contour of only off-curve points
            pts = pts[:-1]
        offs, end = list(pts[:-1]), pts[-1]
        prev = start
        for i, c in enumerate(offs):
            nxt = end if i == len(offs) - 1 else ((c[0] + offs[i + 1][0]) / 2, (c[1] + offs[i + 1][1]) / 2)
            for k in range(1, CURVE_STEPS + 1):
                t = k / CURVE_STEPS
                u = 1 - t
                self.cur.append((u * u * prev[0] + 2 * u * t * c[0] + t * t * nxt[0],
                                 u * u * prev[1] + 2 * u * t * c[1] + t * t * nxt[1]))
            prev = nxt
        if not offs:
            self.cur.append(end)

    def closePath(self):
        self._close()

    endPath = closePath

    def addComponent(self, name, transform):     # composite glyphs are drawn by the glyph set
        pass

    def _close(self):
        if len(self.cur) > 1:
            self.polys.append((self.cur, True))
        self.cur = []


def _glyphs(layer):
    """(font, units->inch scale, glyph set, cmap, advances, metrics) for a text layer."""
    import design as D
    path = D.font_path(layer["font"], layer["style"])
    font = _ttfont(path, layer["font"], layer["style"])
    scale = layer["size_pt"] / 72 / font["head"].unitsPerEm
    hhea = font["hhea"]
    return font, scale, font.getGlyphSet(), font.getBestCmap(), font["hmtx"], hhea


def _glyph_polys(gs, name):
    from fontTools.pens.recordingPen import DecomposingRecordingPen
    rec = DecomposingRecordingPen(gs)            # resolves composite glyphs (é, Å, ...)
    gs[name].draw(rec)
    pen = _FlattenPen()
    rec.replay(pen)
    pen._close()
    return pen.polys


def text_paths(layer):
    """Letter outlines of a text layer, local inches centered on 0,0. Returns (polys, w, h)."""
    font, k, gs, cmap, hmtx, hhea = _glyphs(layer)
    track = layer.get("tracking", 0.0) * layer["size_pt"] / 72
    line_h = (hhea.ascent - hhea.descent + hhea.lineGap) * k * layer.get("line_spacing", 1.1)
    lines = (layer["text"] or " ").split("\n")
    laid, widths = [], []
    for row, line in enumerate(lines):
        x, glyphs = 0.0, []
        for ch in line:
            name = cmap.get(ord(ch), ".notdef")
            glyphs.append((name, x))
            x += hmtx[name][0] * k + track
        widths.append(max(0.0, x - track))
        laid.append((glyphs, row * line_h))
    full = max(widths) if widths else 0
    polys = []
    for (glyphs, y0), w in zip(laid, widths):
        dx = {"left": 0, "right": full - w}.get(layer.get("align", "center"), (full - w) / 2)
        for name, x in glyphs:
            for pts, c in _glyph_polys(gs, name):
                polys.append(([(dx + x + px * k, y0 - py * k) for px, py in pts], c))   # font y is up
    return _center(polys) if polys else ([], 0, 0)


def arctext_paths(layer):
    """Letter outlines along a circle, local inches centered on the circle's center."""
    font, k, gs, cmap, hmtx, hhea = _glyphs(layer)
    track = layer.get("tracking", 0.0) * layer["size_pt"] / 72
    r = max(0.01, layer["radius_in"])
    names = [cmap.get(ord(ch), ".notdef") for ch in (layer["text"] or " ")]
    adv = [hmtx[n][0] * k + track for n in names]
    span = sum(adv) / r
    top = layer.get("arc", "top") == "top"
    theta0 = -math.pi / 2 - span / 2 if top else math.pi / 2 + span / 2
    polys, cum = [], 0.0
    for name, a in zip(names, adv):
        mid = (cum + a / 2) / r
        th = theta0 + mid if top else theta0 - mid
        cum += a
        turn = th + (math.pi / 2 if top else -math.pi / 2)
        ct, st = math.cos(turn), math.sin(turn)
        bx, by = r * math.cos(th), r * math.sin(th)          # baseline point on the circle
        half = (a - track) / 2
        for pts, c in _glyph_polys(gs, name):
            out = []
            for px, py in pts:
                lx, ly = px * k - half, -py * k                # glyph coords, baseline-centered
                out.append((bx + lx * ct - ly * st, by + lx * st + ly * ct))
            polys.append((out, c))
    R = r + (hhea.ascent - hhea.descent) * k
    return polys, 2 * R, 2 * R


# ------------------------------------------------------------------------------
# Per-layer cut paths
# ------------------------------------------------------------------------------

def svg_paths(layer):
    """The SVG's own drawing paths, scaled to the layer size, local inches centered on 0,0."""
    import pymupdf
    page = pymupdf.open(layer["src"])[0]
    pw, ph = page.rect.width, page.rect.height
    sx, sy = layer["w_in"] / max(pw, 1e-6), layer["h_in"] / max(ph, 1e-6)
    tf = lambda p: (p.x * sx - layer["w_in"] / 2, p.y * sy - layer["h_in"] / 2)
    polys = []
    for d in page.get_drawings():
        cur = []
        for item in d["items"]:
            op = item[0]
            if op == "l":
                a, b = tf(item[1]), tf(item[2])
                if not cur or cur[-1] != a:
                    if len(cur) > 1:
                        polys.append((cur, False))
                    cur = [a]
                cur.append(b)
            elif op == "c":
                p0, p1, p2, p3 = (tf(p) for p in item[1:5])
                if not cur or cur[-1] != p0:
                    if len(cur) > 1:
                        polys.append((cur, False))
                    cur = [p0]
                for i in range(1, CURVE_STEPS + 1):
                    t = i / CURVE_STEPS
                    u = 1 - t
                    cur.append((u ** 3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t ** 3 * p3[0],
                                u ** 3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t ** 3 * p3[1]))
            elif op == "re":
                rc = item[1]
                if len(cur) > 1:
                    polys.append((cur, False))
                cur = []
                polys.append(([tf(rc.tl), tf(rc.tr), tf(rc.br), tf(rc.bl)], True))
            elif op == "qu":
                q = item[1]
                if len(cur) > 1:
                    polys.append((cur, False))
                cur = []
                polys.append(([tf(q.ul), tf(q.ur), tf(q.lr), tf(q.ll)], True))
        if len(cur) > 1:
            closed = d.get("closePath") or cur[0] == cur[-1]
            polys.append((cur, bool(closed)))
    return polys


def mask_paths(cover, x0, y0, dpi, holes=True):
    """Traced outline(s) of a placed mask (already rotated), canvas inches."""
    m = cover.astype(np.uint8) * 255
    mode = cv2.RETR_LIST if holes else cv2.RETR_EXTERNAL
    contours, _ = cv2.findContours(m, mode, cv2.CHAIN_APPROX_NONE)
    out = []
    for c in contours:
        if cv2.contourArea(c) < 4:
            continue
        c = cv2.approxPolyDP(c, 0.6, True)
        out.append(([((x0 + x) / dpi, (y0 + y) / dpi) for x, y in c[:, 0, :]], True))
    return out


def layer_paths(layer, renderer, design):
    """Cut lines for one layer in canvas inches. Text/arc text also update the layer's size."""
    t = layer["type"]
    if t == "shape":
        w, h, kind = layer["w_in"], layer["h_in"], layer.get("shape", "rect")
        if kind == "line":
            local = [([(-w / 2, 0), (w / 2, 0)], False)]
        elif kind == "ellipse":
            local = [(_ellipse(w, h), True)]
        else:
            local = [(_rounded_rect(w, h, layer.get("corner_in", 0.0)), True)]
        return _place(local, layer)
    if t in ("text", "arctext"):
        local, w, h = text_paths(layer) if t == "text" else arctext_paths(layer)
        layer["w_in"], layer["h_in"] = max(w, 0.01), max(h, 0.01)
        return _place(local, layer)
    if t == "svg":
        return _place(svg_paths(layer), layer)
    # logo / photo: trace the artwork's outline from its rendered mask
    _, cover = renderer.layer_masks(layer, design)
    dpi = renderer.dpi(design)
    h, w = cover.shape
    x0, y0 = round(layer["x_in"] * dpi - w / 2), round(layer["y_in"] * dpi - h / 2)
    return mask_paths(cover, x0, y0, dpi, holes=(t == "logo"))


def outline_paths(cover, dpi, offset_in):
    """Cut line around everything engraved (the old 'cut outline'), canvas inches."""
    m = cover.astype(np.uint8) * 255
    close = max(3, int(2 / 25.4 * dpi) | 1)
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (close, close)))
    off = int(round(offset_in * dpi))
    pad = abs(off) + 2
    m = cv2.copyMakeBorder(m, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=0)
    if off:
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * abs(off) + 1, 2 * abs(off) + 1))
        m = cv2.dilate(m, k) if off > 0 else cv2.erode(m, k)
    contours, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not contours:
        return []
    c = cv2.approxPolyDP(max(contours, key=cv2.contourArea), max(0.5, dpi / 254 * 0.75), True)
    return [([((x - pad) / dpi, (y - pad) / dpi) for x, y in c[:, 0, :]], True)]


# ------------------------------------------------------------------------------
# Writers (millimetres; red cut lines; optional blue frame = the canvas)
# ------------------------------------------------------------------------------

def _mm(paths, design):
    """Canvas inches -> mm, mirrored like the PNG when the design is mirrored."""
    W = design["canvas"]["width_in"]
    mir = design.get("mirror")
    return [([(((W - x) if mir else x) * 25.4, y * 25.4) for x, y in pts], c) for pts, c in paths]


def write_svg(paths, design, out_path, frame=False, png_path=None):
    W, H = design["canvas"]["width_in"] * 25.4, design["canvas"]["height_in"] * 25.4
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
             f'width="{W:.3f}mm" height="{H:.3f}mm" viewBox="0 0 {W:.3f} {H:.3f}">']
    if png_path:                                 # combined file: engraving image under the cuts
        with open(png_path, "rb") as f:
            data = base64.b64encode(f.read()).decode()
        parts.append(f'  <image x="0" y="0" width="{W:.3f}" height="{H:.3f}" preserveAspectRatio="none" '
                     f'xlink:href="data:image/png;base64,{data}" href="data:image/png;base64,{data}"/>')
    if frame:
        parts.append(f'  <rect x="0" y="0" width="{W:.3f}" height="{H:.3f}" fill="none" '
                     f'stroke="{FRAME_RGB}" stroke-width="0.1"/>')
    for pts, closed in _mm(paths, design):
        d = "M " + " L ".join(f"{x:.3f},{y:.3f}" for x, y in pts) + (" Z" if closed else "")
        parts.append(f'  <path d="{d}" fill="none" stroke="{CUT_RGB}" stroke-width="0.1"/>')
    parts.append("</svg>")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(parts) + "\n")


def write_dxf(paths, design, out_path, frame=False):
    """DXF R12 (the most widely readable version): POLYLINEs in mm, Y up. Red = cut layer,
    blue = FRAME layer."""
    H = design["canvas"]["height_in"] * 25.4
    out = ["0", "SECTION", "2", "HEADER", "9", "$ACADVER", "1", "AC1009",
           "9", "$INSUNITS", "70", "4", "0", "ENDSEC",
           "0", "SECTION", "2", "TABLES", "0", "TABLE", "2", "LAYER", "70", "2"]
    for name, color in (("CUT", 1), ("FRAME", 5)):
        out += ["0", "LAYER", "2", name, "70", "0", "62", str(color), "6", "CONTINUOUS"]
    out += ["0", "ENDTAB", "0", "ENDSEC", "0", "SECTION", "2", "ENTITIES"]

    def poly(pts, closed, layer, color):
        out.extend(["0", "POLYLINE", "8", layer, "62", str(color), "66", "1", "70", "1" if closed else "0",
                    "10", "0.0", "20", "0.0", "30", "0.0"])
        for x, y in pts:
            out.extend(["0", "VERTEX", "8", layer, "10", f"{x:.4f}", "20", f"{H - y:.4f}", "30", "0.0"])
        out.extend(["0", "SEQEND", "8", layer])

    if frame:
        W = design["canvas"]["width_in"] * 25.4
        poly([(0, 0), (W, 0), (W, H), (0, H)], True, "FRAME", 5)
    for pts, closed in _mm(paths, design):
        poly(pts, closed, "CUT", 1)
    out += ["0", "ENDSEC", "0", "EOF"]
    with open(out_path, "w", encoding="ascii") as f:
        f.write("\n".join(out) + "\n")
