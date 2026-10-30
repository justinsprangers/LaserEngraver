"""
vectors.py: cut and score lines for Design mode, and the SVG / DXF / PDF writers.

Any layer can be engraved (raster), scored (a light vector line) or cut. Its lines are
polylines in canvas inches:
    shapes      their outline (rectangle with rounded corners, ellipse, line)
    text        letter outlines (fontTools), laid out the same way for preview and export
    arc text    letter outlines placed along the circle
    vector file each color's own job (vecfile.py): line colors along their path, fill colors
                along their outline
    logo/photo  the traced outline of the artwork / cut-out
Optional per layer: weld overlapping shapes, keep only the outer outline, offset (shapely).
Vector items are left out of the engraving PNG. The writers produce files in millimetres with
cut lines in pure red (#FF0000), score lines in pure blue (#0000FF) and an optional green canvas
frame for lining the PNG up in software that doesn't keep file positions.
"""

import base64
import math

import cv2
import numpy as np

CUT_RGB = "#ff0000"
SCORE_RGB = "#0000ff"
FRAME_RGB = "#00ff00"
KINDS = ("score", "cut")              # file order: scores before cuts (laser order)
COLORS = {"cut": CUT_RGB, "score": SCORE_RGB}
TOL_IN = 0.0008                       # curve flattening tolerance for vector output (~0.02 mm)
HAIRLINE_MM = 0.0254                  # line width in written files (0.001 in "hairline")


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


def _center_groups(groups):
    """Shifts groups of local polylines so their bounding box is centered on 0,0.
    Returns (groups, w, h)."""
    l, t, r, b = _bbox([p for g, _ in groups for p in g])
    mx, my = (l + r) / 2, (t + b) / 2
    return [([([(x - mx, y - my) for x, y in pts], c) for pts, c in g], eo) for g, eo in groups], r - l, b - t


def _ellipse(w, h, n=None):
    n = n or max(48, int(math.pi * (w + h) / 0.02))        # ~0.5 mm segments
    return [(w / 2 * math.cos(2 * math.pi * i / n), h / 2 * math.sin(2 * math.pi * i / n)) for i in range(n)]


def _arc_steps(r):
    """Segments per quarter circle so the line stays within TOL_IN of the arc."""
    if r <= TOL_IN:
        return 1
    return max(4, math.ceil((math.pi / 2) / (2 * math.acos(max(-1.0, 1 - TOL_IN / r)))))


def _rounded_rect(w, h, r):
    r = max(0.0, min(r, w / 2, h / 2))
    if r <= 0:
        return [(-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)]
    pts, n = [], _arc_steps(r)
    for (cx, cy, a0) in ((w / 2 - r, -h / 2 + r, -90), (w / 2 - r, h / 2 - r, 0),
                         (-w / 2 + r, h / 2 - r, 90), (-w / 2 + r, -h / 2 + r, 180)):
        for i in range(n + 1):
            a = math.radians(a0 + 90 * i / n)
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


def path_length_mm(vecs):
    """Total length of every vector line (cut + score), mm."""
    return sum(math.dist(p[i], p[(i + 1) % len(p)]) if c or i + 1 < len(p) else 0
               for paths in vecs.values() for p, c in paths for i in range(len(p))) * 25.4


# ------------------------------------------------------------------------------
# Weld / outer outline / offset (shapely)
# ------------------------------------------------------------------------------

def _group_area(rings, evenodd):
    """One shape's filled area from its closed rings. Even-odd: rings alternate filled/hole.
    Nonzero (fonts, most drawings): rings turning the same way as the biggest one fill, the
    others cut holes."""
    from shapely.geometry import Polygon
    from shapely.validation import make_valid
    from shapely.ops import unary_union
    polys = []
    for pts in rings:
        if len(pts) < 3:
            continue
        p = Polygon(pts)
        if not p.is_valid:
            p = make_valid(p)
        if not p.is_empty and p.area > 1e-12:
            polys.append((p, _signed(pts)))
    if not polys:
        return None
    if evenodd:
        out = polys[0][0]
        for p, _ in polys[1:]:
            out = out.symmetric_difference(p)
        return out
    ref = max(polys, key=lambda q: abs(q[1]))[1] > 0
    pos = unary_union([p for p, s in polys if (s > 0) == ref])
    neg = [p for p, s in polys if (s > 0) != ref]
    return pos.difference(unary_union(neg)) if neg else pos


def _signed(pts):
    return 0.5 * sum(pts[i][0] * pts[i - 1][1] - pts[i - 1][0] * pts[i][1] for i in range(len(pts)))


def polygons(geom):
    """The Polygons inside any shapely geometry (Polygon, MultiPolygon, collection)."""
    from shapely.geometry import Polygon, MultiPolygon, GeometryCollection
    if isinstance(geom, Polygon):
        return [geom]
    if isinstance(geom, (MultiPolygon, GeometryCollection)):
        return [p for g in geom.geoms for p in polygons(g)]
    return []


def shape_ops(groups, weld=False, outer=False, offset_in=0.0):
    """groups = [(polylines, evenodd), ...], one per shape (letter, drawing item...).
    Returns plain polylines. Weld merges overlapping shapes into one outline; outer keeps only
    the outside edge (no holes); offset grows (+) or shrinks (-) the shapes. Outer and offset
    also weld. Open lines pass through unchanged."""
    flat = [p for g, _ in groups for p in g]
    if not (weld or outer or offset_in):
        return flat
    from shapely.ops import unary_union
    areas, opens = [], []
    for g, evenodd in groups:
        rings = [pts for pts, c in g if c and len(pts) >= 3]
        opens += [(pts, c) for pts, c in g if not c or len(pts) < 3]
        a = _group_area(rings, evenodd)
        if a is not None and not a.is_empty:
            areas.append(a)
    if not areas:
        return flat
    merged = unary_union(areas)
    if offset_in:
        merged = merged.buffer(offset_in, quad_segs=max(8, _arc_steps(abs(offset_in))), join_style="round")
    out = []
    for poly in polygons(merged):
        out.append((list(poly.exterior.coords)[:-1], True))
        if not outer:
            out += [(list(r.coords)[:-1], True) for r in poly.interiors]
    return out + opens


def layer_ops(layer):
    """The layer's weld / outer / offset choices as shape_ops() keyword arguments."""
    return {"weld": bool(layer.get("weld")), "outer": bool(layer.get("outer")),
            "offset_in": float(layer.get("offset_mm", 0.0) or 0.0) / 25.4}


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


def _cubic_steps(p0, p1, p2, p3, tol):
    m = max(math.hypot(p0[0] - 2 * p1[0] + p2[0], p0[1] - 2 * p1[1] + p2[1]),
            math.hypot(p1[0] - 2 * p2[0] + p3[0], p1[1] - 2 * p2[1] + p3[1]))
    return min(500, max(1, math.ceil(math.sqrt(0.75 * m / tol))))      # Wang's formula


class _FlattenPen:
    """fontTools pen that turns glyph outlines into polylines (font units), curves split
    until they're within `tol` font units of the real curve."""

    def __init__(self, tol):
        self.polys, self.cur, self.tol = [], [], tol

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
            n = _cubic_steps(p0, p1, p2, p3, self.tol)
            for i in range(1, n + 1):
                t = i / n
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
            dd = math.hypot(prev[0] - 2 * c[0] + nxt[0], prev[1] - 2 * c[1] + nxt[1])
            n = min(500, max(1, math.ceil(math.sqrt(dd / (4 * self.tol)))))
            for k in range(1, n + 1):
                t = k / n
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


_GLYPHS = {}


def _glyph_polys(gs, name, tol=1.0, font=None):
    """A glyph's outline polylines (font units). Pass the TTFont to cache the result."""
    key = (id(font), name, round(tol, 4)) if font is not None else None
    if key in _GLYPHS:
        return _GLYPHS[key]
    from fontTools.pens.recordingPen import DecomposingRecordingPen
    rec = DecomposingRecordingPen(gs)            # resolves composite glyphs (é, Å, ...)
    gs[name].draw(rec)
    pen = _FlattenPen(tol)
    rec.replay(pen)
    pen._close()
    if key:
        if len(_GLYPHS) > 5000:
            _GLYPHS.clear()
        _GLYPHS[key] = pen.polys
    return pen.polys


def text_paths(layer):
    """Letter outlines of a text layer, local inches centered on 0,0, one group per letter.
    Returns (groups, w, h)."""
    font, k, gs, cmap, hmtx, hhea = _glyphs(layer)
    tol = TOL_IN / k
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
    groups = []
    for (glyphs, y0), w in zip(laid, widths):
        dx = {"left": 0, "right": full - w}.get(layer.get("align", "center"), (full - w) / 2)
        for name, x in glyphs:
            g = [([(dx + x + px * k, y0 - py * k) for px, py in pts], c)      # font y is up
                 for pts, c in _glyph_polys(gs, name, tol, font)]
            if g:
                groups.append((g, False))
    return _center_groups(groups) if groups else ([], 0, 0)


def arctext_paths(layer):
    """Letter outlines along a circle, local inches centered on the circle's center, one group
    per letter. Returns (groups, w, h)."""
    font, k, gs, cmap, hmtx, hhea = _glyphs(layer)
    tol = TOL_IN / k
    track = layer.get("tracking", 0.0) * layer["size_pt"] / 72
    r = max(0.01, layer["radius_in"])
    names = [cmap.get(ord(ch), ".notdef") for ch in (layer["text"] or " ")]
    adv = [hmtx[n][0] * k + track for n in names]
    span = sum(adv) / r
    top = layer.get("arc", "top") == "top"
    theta0 = -math.pi / 2 - span / 2 if top else math.pi / 2 + span / 2
    groups, cum = [], 0.0
    for name, a in zip(names, adv):
        mid = (cum + a / 2) / r
        th = theta0 + mid if top else theta0 - mid
        cum += a
        turn = th + (math.pi / 2 if top else -math.pi / 2)
        ct, st = math.cos(turn), math.sin(turn)
        bx, by = r * math.cos(th), r * math.sin(th)          # baseline point on the circle
        half = (a - track) / 2
        g = []
        for pts, c in _glyph_polys(gs, name, tol, font):
            out = []
            for px, py in pts:
                lx, ly = px * k - half, -py * k                # glyph coords, baseline-centered
                out.append((bx + lx * ct - ly * st, by + lx * st + ly * ct))
            g.append((out, c))
        if g:
            groups.append((g, False))
    R = r + (hhea.ascent - hhea.descent) * k
    return groups, 2 * R, 2 * R


# ------------------------------------------------------------------------------
# Per-layer vector lines
# ------------------------------------------------------------------------------

def file_vectors(layer):
    """A vector-file layer's cut and score lines: {"cut": groups, "score": groups}, local
    inches centered on 0,0 (groups as in shape_ops). Fill colors give their outlines, line
    colors their paths."""
    import design as D
    import vecfile as V
    doc = V.load(layer["src"])
    jobs = D.vector_jobs(layer, doc)
    l, t, r, b = D.vector_box(layer, doc)
    sx, sy = layer["w_in"] / (r - l), layer["h_in"] / (b - t)
    cx, cy = (l + r) / 2, (t + b) / 2
    tol = TOL_IN / max(sx, sy, 1e-9)
    out = {"cut": [], "score": []}
    for i in V.part_ids(doc, layer.get("parts")):
        it = doc["items"][i]
        for key in V.item_keys(it):
            job = jobs.get(key)
            if job not in out:
                continue
            fill = key.startswith("f:")
            g = [([((x - cx) * sx, (y - cy) * sy) for x, y in V.flatten(sub, tol)], fill or sub[2])
                 for sub in it["subs"]]
            out[job].append((g, it["evenodd"]))
    return out


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


def layer_vectors(layer, renderer, design):
    """{"cut": polylines, "score": polylines} for one layer, canvas inches (kinds with no lines
    are left out). Text/arc text also update the layer's size."""
    t = layer["type"]
    ops = layer_ops(layer)
    if t == "svg":
        out = {}
        for kind, groups in file_vectors(layer).items():
            if groups:
                out[kind] = _place(shape_ops(groups, **ops), layer)
        return out
    op = layer.get("op", "engrave")
    if op not in COLORS:
        return {}
    if t == "shape":
        w, h, kind = layer["w_in"], layer["h_in"], layer.get("shape", "rect")
        if kind == "line":
            groups = [([([(-w / 2, 0), (w / 2, 0)], False)], False)]
        elif kind == "ellipse":
            groups = [([(_ellipse(w, h), True)], False)]
        else:
            groups = [([(_rounded_rect(w, h, layer.get("corner_in", 0.0)), True)], False)]
        return {op: _place(shape_ops(groups, **ops), layer)}
    if t in ("text", "arctext"):
        groups, w, h = text_paths(layer) if t == "text" else arctext_paths(layer)
        layer["w_in"], layer["h_in"] = max(w, 0.01), max(h, 0.01)
        return {op: _place(shape_ops(groups, **ops), layer)}
    # logo / photo: trace the artwork's outline from its rendered mask (already placed)
    _, cover = renderer.layer_masks(layer, design)
    dpi = renderer.dpi(design)
    h, w = cover.shape
    x0, y0 = round(layer["x_in"] * dpi - w / 2), round(layer["y_in"] * dpi - h / 2)
    rings = mask_paths(cover, x0, y0, dpi, holes=(t == "logo"))
    return {op: shape_ops([(rings, True)], **ops)}


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
# Writers (millimetres; red cut lines, blue score lines; optional green frame = the canvas)
# ------------------------------------------------------------------------------

def _mm(paths, design):
    """Canvas inches -> mm, mirrored like the PNG when the design is mirrored."""
    W = design["canvas"]["width_in"]
    mir = design.get("mirror")
    return [([(((W - x) if mir else x) * 25.4, y * 25.4) for x, y in pts], c) for pts, c in paths]


def write_svg(vecs, design, out_path, frame=False, png_path=None):
    """vecs = {"cut": polylines, "score": polylines}. With png_path, the engraving image is
    embedded underneath (the combined file)."""
    W, H = design["canvas"]["width_in"] * 25.4, design["canvas"]["height_in"] * 25.4
    sw = HAIRLINE_MM
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
             f'width="{W:.3f}mm" height="{H:.3f}mm" viewBox="0 0 {W:.3f} {H:.3f}">']
    if png_path:                                 # combined file: engraving image under the lines
        with open(png_path, "rb") as f:
            data = base64.b64encode(f.read()).decode()
        parts.append(f'  <image id="engrave" x="0" y="0" width="{W:.3f}" height="{H:.3f}" '
                     f'preserveAspectRatio="none" xlink:href="data:image/png;base64,{data}" '
                     f'href="data:image/png;base64,{data}"/>')
    if frame:
        parts.append(f'  <rect id="frame" x="0" y="0" width="{W:.3f}" height="{H:.3f}" fill="none" '
                     f'stroke="{FRAME_RGB}" stroke-width="{sw}"/>')
    for kind in KINDS:
        paths = vecs.get(kind) or []
        if not paths:
            continue
        parts.append(f'  <g id="{kind}" fill="none" stroke="{COLORS[kind]}" stroke-width="{sw}">')
        for pts, closed in _mm(paths, design):
            d = "M " + " L ".join(f"{x:.4f},{y:.4f}" for x, y in pts) + (" Z" if closed else "")
            parts.append(f'    <path d="{d}"/>')
        parts.append("  </g>")
    parts.append("</svg>")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(parts) + "\n")


def write_dxf(vecs, design, out_path, frame=False):
    """DXF R12 (the most widely readable version): POLYLINEs in mm, Y up. Layers CUT (red),
    SCORE (blue) and FRAME (green)."""
    H = design["canvas"]["height_in"] * 25.4
    out = ["0", "SECTION", "2", "HEADER", "9", "$ACADVER", "1", "AC1009",
           "9", "$INSUNITS", "70", "4", "0", "ENDSEC",
           "0", "SECTION", "2", "TABLES", "0", "TABLE", "2", "LAYER", "70", "3"]
    for name, color in (("CUT", 1), ("SCORE", 5), ("FRAME", 3)):
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
        poly([(0, 0), (W, 0), (W, H), (0, H)], True, "FRAME", 3)
    for kind, (layer, color) in (("score", ("SCORE", 5)), ("cut", ("CUT", 1))):
        for pts, closed in _mm(vecs.get(kind) or [], design):
            poly(pts, closed, layer, color)
    out += ["0", "ENDSEC", "0", "EOF"]
    with open(out_path, "w", encoding="ascii") as f:
        f.write("\n".join(out) + "\n")


def write_pdf(vecs, design, out_path, png_path=None):
    """One PDF page the size of the canvas: the engraving image (if any) with the score and cut
    lines on top as hairlines, all already lined up (Trotec Ruby reads PDF natively)."""
    try:
        import pymupdf
    except ImportError:                      # e.g. the web version (no PyMuPDF build for it)
        return _write_pdf_plain(vecs, design, out_path, png_path)
    W, H = design["canvas"]["width_in"] * 72, design["canvas"]["height_in"] * 72
    doc = pymupdf.open()
    page = doc.new_page(width=W, height=H)
    if png_path:
        page.insert_image(page.rect, filename=png_path, keep_proportion=False)
    k = 72 / 25.4
    width = HAIRLINE_MM * k
    for kind in KINDS:
        color = tuple(int(COLORS[kind][i:i + 2], 16) / 255 for i in (1, 3, 5))
        paths = _mm(vecs.get(kind) or [], design)
        if not paths:
            continue
        shape = page.new_shape()
        for pts, closed in paths:
            if len(pts) < 2:
                continue
            shape.draw_polyline([pymupdf.Point(x * k, y * k) for x, y in pts])
            shape.finish(color=color, width=width, closePath=closed, fill=None)
        shape.commit()
    doc.save(out_path, garbage=3, deflate=True)
    doc.close()


def _write_pdf_plain(vecs, design, out_path, png_path=None):
    """write_pdf without PyMuPDF: the same page written by hand (a 1-bit image + stroked paths)."""
    import zlib
    from PIL import Image
    W, H = design["canvas"]["width_in"] * 72, design["canvas"]["height_in"] * 72
    k = 72 / 25.4
    ops, xobj, objs = [], "", []
    if png_path:
        with Image.open(png_path) as im:
            img = im.convert("1")              # rows packed 8 pixels per byte, 1 = white
        data = zlib.compress(img.tobytes(), 9)
        objs.append(((f"<< /Type /XObject /Subtype /Image /Width {img.width} /Height {img.height} "
                     f"/ColorSpace /DeviceGray /BitsPerComponent 1 /Filter /FlateDecode "
                     f"/Length {len(data)} >>").encode(), data))
        xobj = "/XObject << /Im0 6 0 R >>"
        ops.append(f"q {W:.4f} 0 0 {H:.4f} 0 0 cm /Im0 Do Q")
    ops.append(f"{HAIRLINE_MM * k:.4f} w 1 J 1 j")
    for kind in KINDS:
        paths = _mm(vecs.get(kind) or [], design)
        if not paths:
            continue
        r, g, b = (int(COLORS[kind][i:i + 2], 16) / 255 for i in (1, 3, 5))
        ops.append(f"{r:.3f} {g:.3f} {b:.3f} RG")
        for pts, closed in paths:
            if len(pts) < 2:
                continue
            seg = [f"{x * k:.3f} {H - y * k:.3f} {'l' if i else 'm'}" for i, (x, y) in enumerate(pts)]
            ops.append(" ".join(seg) + (" h S" if closed else " S"))
    content = zlib.compress("\n".join(ops).encode(), 9)
    head = [b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {W:.4f} {H:.4f}] "
             f"/Resources << {xobj} >> /Contents 4 0 R >>").encode(),
            (f"<< /Length {len(content)} /Filter /FlateDecode >>".encode(), content),
            b"<< /Producer (Laser Engraver) >>"]
    out, offsets = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"), []
    for n, obj in enumerate(head + objs, 1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n".encode()
        if isinstance(obj, tuple):
            out += obj[0] + b"\nstream\n" + obj[1] + b"\nendstream"
        else:
            out += obj
        out += b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(offsets) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    out += (f"trailer\n<< /Size {len(offsets) + 1} /Root 1 0 R /Info 5 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n").encode()
    with open(out_path, "wb") as f:
        f.write(out)
