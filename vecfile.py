"""
vecfile.py: reads vector artwork (SVG, PDF, AI, DXF) for Design mode, at its real size.

load(path) returns a document dict (cached by file + modification time):
    {"kind": "svg" | "pdf" | "dxf", "w_in", "h_in" (page size), "sized" (False if the file
     states no physical size), "items": [item, ...] (drawing order), "notes": [str, ...]}
    item = {"stroke": "#rrggbb" | None, "fill": "#rrggbb" | None, "width_in" (line width),
            "cap" / "join" (0 butt/miter, 1 round, 2 square/bevel), "evenodd": bool,
            "subs": [(start, segs, closed), ...], "bbox": (l, t, r, b)}
    segs = [("L", p) | ("C", c1, c2, p), ...]       page inches, origin top-left, y down

Curves stay Bézier curves until flatten(), so cut lines are smooth at any size.

Each line color and each fill color is a "group" ("s:#rrggbb" / "f:#rrggbb") with a job
(JOBS): engrave it, score or cut along it, leave it blank, or ignore it. default_job() picks
the usual laser convention (red lines cut, blue lines score, dark fills engrave).

SVG: MuPDF draws the file, but it doesn't read CSS <style> classes, display:none, or 96-DPI
px sizes, and draws text with built-in fonts. _prepare_svg() fixes all of that first: it
inlines CSS, drops hidden elements, writes the real size into the root, and turns <text> into
outlines with the installed fonts (fontTools), and _fix_for_mupdf() rewrites SVG that MuPDF
misreads (style-only fill rules / opacity, units, currentColor, visibility, nested viewports,
sized symbols, rounded rects). PDF/AI: MuPDF directly. DXF: ezdxf. Without PyMuPDF (the web
version) SVGs are read by _load_svg_plain().
"""

import math
import os
import re
import xml.etree.ElementTree as ET

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
EXTENSIONS = (".svg", ".pdf", ".ai", ".dxf")
JOBS = ("engrave", "score", "cut", "blank", "ignore")
JOB_LABELS = {"engrave": "Engrave", "score": "Score (line)", "cut": "Cut", "blank": "Blank (no burn)",
              "ignore": "Ignore"}
HAIRLINE_IN = 0.1 / 25.4          # lines thinner than 0.1 mm are cut lines by default
_CACHE = {}


# ------------------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------------------

def load(path):
    st = os.stat(path)
    key = (os.path.abspath(path), st.st_mtime, st.st_size)
    doc = _CACHE.get(key)
    if doc is None:
        ext = os.path.splitext(path)[1].lower()
        doc = _load_dxf(path) if ext == ".dxf" else _load_mupdf(path, ext)
        for it in doc["items"]:
            it["bbox"] = _item_bbox(it)
        doc["items"] = [it for it in doc["items"] if it["bbox"] is not None]
        if not doc["items"]:
            doc["notes"].append("No drawable shapes found in this file.")
        doc["notes"] = list(dict.fromkeys(doc["notes"]))
        if len(_CACHE) > 12:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[key] = doc
    return doc


def part_ids(doc, parts):
    n = len(doc["items"])
    return list(range(n)) if parts is None else [i for i in parts if 0 <= i < n]


def box(doc, parts=None, page=False):
    """(l, t, r, b) page inches around the chosen items (or the whole page), never zero-sized."""
    if page:
        l, t, r, b = 0.0, 0.0, doc["w_in"], doc["h_in"]
    else:
        bbs = [doc["items"][i]["bbox"] for i in part_ids(doc, parts)]
        if not bbs:
            return 0.0, 0.0, max(doc["w_in"], 0.01), max(doc["h_in"], 0.01)
        l, t = min(b[0] for b in bbs), min(b[1] for b in bbs)
        r, b = max(b[2] for b in bbs), max(b[3] for b in bbs)
    if r - l < 0.002:
        l, r = (l + r) / 2 - 0.001, (l + r) / 2 + 0.001
    if b - t < 0.002:
        t, b = (t + b) / 2 - 0.001, (t + b) / 2 + 0.001
    return l, t, r, b


def item_keys(it):
    """The color groups an item belongs to (its line color and/or fill color)."""
    keys = []
    if it["fill"]:
        keys.append("f:" + it["fill"])
    if it["stroke"]:
        keys.append("s:" + it["stroke"])
    return keys


def groups(doc, parts=None):
    """[(key, count, label), ...] in drawing order for the chosen items."""
    seen = {}
    for i in part_ids(doc, parts):
        for k in item_keys(doc["items"][i]):
            seen[k] = seen.get(k, 0) + 1
    return [(k, n, group_label(k)) for k, n in seen.items()]


def group_label(key):
    kind, hexc = key.split(":", 1)
    return f"{color_name(hexc)} {'lines' if kind == 's' else 'fill'}"


def default_job(key, doc, legacy=None):
    """The usual laser meaning of a color. legacy = ("cut", ...) or ("engrave", threshold,
    invert) for SVG layers saved before colors had jobs (keeps their old output)."""
    kind, hexc = key.split(":", 1)
    r, g, b = rgb(hexc)
    lum = 0.299 * r + 0.587 * g + 0.114 * b
    if legacy:
        if legacy[0] == "cut":
            return "cut"
        dark = lum < legacy[1]
        return "engrave" if dark != bool(legacy[2]) else "blank"
    red = r >= 200 and g <= 80 and b <= 80
    blue = b >= 200 and r <= 80 and g <= 80
    if hexc == "#00ff00":
        return "ignore"                 # this app's alignment frame (re-imported export)
    if kind == "s":
        if red:
            return "cut"
        if blue:
            return "score"
        if doc["kind"] == "dxf":
            return "cut"
        if lum >= 200:
            return "blank"
        widths = [it["width_in"] for it in doc["items"] if it["stroke"] == hexc]
        return "cut" if widths and max(widths) < HAIRLINE_IN else "engrave"
    return "engrave" if lum < 128 else "blank"


def flatten(sub, tol):
    """A subpath as a polyline (page inches), curves split until within `tol` of the curve."""
    start, segs, _ = sub
    pts, cur = [start], start
    for s in segs:
        if s[0] == "L":
            pts.append(s[1])
            cur = s[1]
            continue
        c1, c2, p = s[1], s[2], s[3]
        m = max(math.hypot(cur[0] - 2 * c1[0] + c2[0], cur[1] - 2 * c1[1] + c2[1]),
                math.hypot(c1[0] - 2 * c2[0] + p[0], c1[1] - 2 * c2[1] + p[1]))
        n = min(2000, max(1, math.ceil(math.sqrt(0.75 * m / max(tol, 1e-9)))))   # Wang's formula
        x0, y0 = cur
        for i in range(1, n + 1):
            t = i / n
            u = 1 - t
            a, b_, c, d = u * u * u, 3 * u * u * t, 3 * u * t * t, t * t * t
            pts.append((a * x0 + b_ * c1[0] + c * c2[0] + d * p[0], a * y0 + b_ * c1[1] + c * c2[1] + d * p[1]))
        cur = p
    return pts


def rgb(hexc):
    return int(hexc[1:3], 16), int(hexc[3:5], 16), int(hexc[5:7], 16)


_NAMES = {"Black": (0, 0, 0), "White": (255, 255, 255), "Red": (255, 0, 0), "Green": (0, 160, 0),
          "Blue": (0, 0, 255), "Yellow": (255, 255, 0), "Cyan": (0, 255, 255), "Magenta": (255, 0, 255),
          "Orange": (255, 140, 0), "Gray": (128, 128, 128), "Light gray": (200, 200, 200),
          "Dark gray": (64, 64, 64), "Brown": (140, 80, 30), "Purple": (128, 0, 128)}


def color_name(hexc):
    c = rgb(hexc)
    name, d = min(((n, sum((a - b) ** 2 for a, b in zip(c, v))) for n, v in _NAMES.items()), key=lambda x: x[1])
    return name if d == 0 else (f"{name}-ish ({hexc})" if d < 1600 else hexc)


# ------------------------------------------------------------------------------
# Geometry helpers
# ------------------------------------------------------------------------------

def _item_bbox(it):
    xs, ys = [], []
    for sub in it["subs"]:
        for x, y in flatten(sub, 0.002):
            xs.append(x)
            ys.append(y)
    if not xs:
        return None
    hw = it["width_in"] / 2 if it["stroke"] else 0.0
    return min(xs) - hw, min(ys) - hw, max(xs) + hw, max(ys) + hw


def _area(pts):
    return 0.5 * sum(pts[i][0] * pts[i - 1][1] - pts[i - 1][0] * pts[i][1] for i in range(len(pts)))


def _hex(c):
    return "#%02x%02x%02x" % tuple(max(0, min(255, round(v * 255))) for v in c[:3])


def _close_enough(a, b, eps=1e-6):
    return abs(a[0] - b[0]) < eps and abs(a[1] - b[1]) < eps


# ------------------------------------------------------------------------------
# PDF / AI / SVG through MuPDF
# ------------------------------------------------------------------------------

def _load_mupdf(path, ext):
    try:
        import pymupdf
    except ImportError:               # the web version: there's no PyMuPDF build for browsers
        if ext != ".svg":
            raise ValueError("PDF and Illustrator files can only be opened in the desktop app here. "
                             "Save the drawing as SVG or DXF instead.")
        with open(path, "rb") as f:
            return _load_svg_plain(f.read())
    notes = []
    if ext == ".svg":
        with open(path, "rb") as f:
            raw = f.read()
        data, w_in, h_in, sized, notes = _prepare_svg(raw, mupdf=True)
        doc = pymupdf.open(stream=data, filetype="svg")
        kind = "svg"
    else:
        try:
            doc = pymupdf.open(path, filetype="pdf")
        except Exception:
            raise ValueError("Couldn't read this file as PDF. Illustrator files need "
                             "'Create PDF Compatible File' turned on (it is by default).")
        kind, sized = "pdf", True
        if doc.page_count > 1:
            notes.append(f"Only the first of {doc.page_count} pages is used.")
    page = doc[0]
    pw, ph = page.rect.width, page.rect.height
    if ext != ".svg":
        w_in, h_in = pw / 72, ph / 72
    sx, sy = w_in / max(pw, 1e-9), h_in / max(ph, 1e-9)
    if page.get_image_info():
        notes.append("Pictures inside this file aren't imported: add them as Photo layers.")
    if page.get_texttrace():
        # Text MuPDF still draws as text (PDF text; SVG text in fonts that aren't installed):
        # re-read the page with the text turned into outlines.
        notes.append("Some text uses fonts that aren't installed here and was converted with a "
                     "stand-in font." if kind == "svg" else "Text was converted to outlines.")
        page = pymupdf.open(stream=page.get_svg_image(text_as_path=True).encode(), filetype="svg")[0]
        sx, sy = w_in / max(page.rect.width, 1e-9), h_in / max(page.rect.height, 1e-9)
    items = []
    for d in page.get_drawings():
        it = _drawing_item(d, sx, sy)
        if it:
            items.append(it)
    return {"kind": kind, "w_in": w_in, "h_in": h_in, "sized": sized, "items": items, "notes": notes}


def _drawing_item(d, sx, sy):
    typ = d.get("type") or ""
    stroke = _hex(d["color"]) if "s" in typ and d.get("color") is not None and \
        (d.get("stroke_opacity") is None or d["stroke_opacity"] > 0) else None
    fill = _hex(d["fill"]) if "f" in typ and d.get("fill") is not None and \
        (d.get("fill_opacity") is None or d["fill_opacity"] > 0) else None
    if not stroke and not fill:
        return None
    tf = lambda p: (p.x * sx, p.y * sy)
    subs, start, segs, cur = [], None, [], None

    def end(closed=False):
        nonlocal start, segs
        if start is not None and segs:
            subs.append((start, segs, closed or _close_enough(start, cur)))
        start, segs = None, []

    for item in d["items"]:
        op = item[0]
        if op in ("re", "qu"):
            end()
            if op == "re":
                rc = item[1]
                pts = [tf(rc.tl), tf(rc.tr), tf(rc.br), tf(rc.bl)]
            else:
                q = item[1]
                pts = [tf(q.ul), tf(q.ur), tf(q.lr), tf(q.ll)]
            subs.append((pts[0], [("L", p) for p in pts[1:]] + [("L", pts[0])], True))
            continue
        a = tf(item[1])
        if cur is None or not _close_enough(a, cur):
            end()
            start = cur = a
        if op == "l":
            cur = tf(item[2])
            segs.append(("L", cur))
        elif op == "c":
            cur = tf(item[4])
            segs.append(("C", tf(item[2]), tf(item[3]), cur))
    end(bool(d.get("closePath")))
    if not subs:
        return None
    if fill and sum(abs(_area(flatten(s, 0.01))) for s in subs) < 1e-7:
        fill = None                     # a plain line: SVG's default black fill paints nothing
        if not stroke:
            return None
    width = (d.get("width") or 0.0) * (sx + sy) / 2 if stroke else 0.0
    cap = d.get("lineCap") or (0,)
    return {"stroke": stroke, "fill": fill, "width_in": width, "evenodd": bool(d.get("even_odd")),
            "subs": subs, "cap": int(max(cap) if isinstance(cap, (tuple, list)) else cap),
            "join": int(d.get("lineJoin") or 0)}


# ------------------------------------------------------------------------------
# SVG preparation (CSS, hidden elements, real size, text -> outlines)
# ------------------------------------------------------------------------------

_UNITS = {"in": 96.0, "mm": 96 / 25.4, "cm": 96 / 2.54, "pt": 96 / 72, "pc": 16.0, "px": 1.0, "": 1.0}


def _length(v, px_per_in=96.0):
    """SVG length -> CSS px (96 per inch), or None for missing / percent values. px_per_in is
    what the file's plain numbers / px mean (72 for Illustrator)."""
    m = re.fullmatch(r"\s*([-+]?[\d.]+(?:e[-+]?\d+)?)\s*(in|mm|cm|pt|pc|px)?\s*", v or "", re.I)
    if not m:
        return None
    unit = (m.group(2) or "").lower()
    return float(m.group(1)) * (96 / px_per_in if unit in ("", "px") else _UNITS[unit])


def _local(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _decls(text):
    out = {}
    for part in (text or "").split(";"):
        if ":" in part:
            k, v = part.split(":", 1)
            out[k.strip().lower()] = v.replace("!important", "").strip()
    return out


def _style_str(d):
    return ";".join(f"{k}:{v}" for k, v in d.items())


def _register_namespaces(raw):
    import io
    try:
        for _, (prefix, uri) in ET.iterparse(io.BytesIO(raw), events=("start-ns",)):
            if uri == SVG_NS or not prefix:
                continue
            try:
                ET.register_namespace(prefix, uri)
            except ValueError:
                pass
    except ET.ParseError:
        pass
    ET.register_namespace("", SVG_NS)
    ET.register_namespace("xlink", XLINK_NS)


def _css_rules(root):
    rules = []
    for el in root.iter():
        if _local(el.tag) != "style":
            continue
        text = re.sub(r"/\*.*?\*/", "", "".join(el.itertext()), flags=re.S)
        text = re.sub(r"@[^{};]*;", "", text)
        for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", text):
            decls = _decls(body)
            for s in sel.split(","):
                s = s.strip()
                m = re.fullmatch(r"([A-Za-z][\w-]*|\*)?((?:[.#][\w-]+)*)", s)
                if not s or not m:
                    continue                         # descendant / attribute selectors: skipped
                tag = m.group(1) if m.group(1) not in (None, "*") else None
                parts = re.findall(r"([.#])([\w-]+)", m.group(2))
                ids = [v for k, v in parts if k == "#"]
                classes = [v for k, v in parts if k == "."]
                rules.append(((len(ids), len(classes), 1 if tag else 0), len(rules), tag, ids, classes, decls))
    rules.sort(key=lambda r: (r[0], r[1]))
    return rules


def _prepare_svg(raw, stand_in=None, as_tree=False, mupdf=False):
    """Returns (svg bytes for MuPDF, width in, height in, sized, notes). stand_in: a font for
    text whose fonts aren't installed (else such text is left for MuPDF to convert); as_tree:
    return the prepared root element instead of bytes; mupdf: also rewrite what MuPDF misreads
    (_fix_for_mupdf)."""
    notes = []
    try:
        _register_namespaces(raw)
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        raise ValueError(f"This SVG file is damaged or isn't an SVG ({e}).")
    if _local(root.tag) != "svg":
        raise ValueError("This file isn't an SVG drawing.")
    # Real size: width/height in CSS units (96 px per inch, like Inkscape and browsers).
    # Adobe Illustrator writes px meaning points (72 per inch).
    px_per_in = 72.0 if b"Adobe Illustrator" in raw[:4000] else 96.0
    if px_per_in == 72:
        notes.append("Made in Adobe Illustrator: sized at 72 px per inch, like Illustrator shows it.")
    vb = [float(v) for v in re.split(r"[\s,]+", root.get("viewBox", "").strip()) if v] or None
    if vb and len(vb) != 4:
        vb = None
    w_px, h_px = _length(root.get("width"), px_per_in), _length(root.get("height"), px_per_in)
    sized = w_px is not None or h_px is not None
    if vb and vb[2] > 0 and vb[3] > 0:
        if w_px is None and h_px is not None:
            w_px = h_px * vb[2] / vb[3]
        elif h_px is None and w_px is not None:
            h_px = w_px * vb[3] / vb[2]
        elif w_px is None:
            w_px, h_px = vb[2], vb[3]
    else:
        w_px, h_px = w_px or 300.0, h_px or 150.0
        u = px_per_in / 96                                    # user units are the file's px
        root.set("viewBox", f"0 0 {w_px * u:g} {h_px * u:g}")  # keeps them when resized
    w_in, h_in = w_px / 96, h_px / 96               # (_length returns 96-DPI px)
    root.set("width", f"{w_in:.6f}in")
    root.set("height", f"{h_in:.6f}in")

    # CSS classes -> inline styles; hidden elements removed.
    rules = _css_rules(root)
    parent = {c: p for p in root.iter() for c in p}
    hidden, clip, images = [], False, False
    for el in root.iter():
        tag = _local(el.tag)
        if rules and tag:
            cls, eid, merged = set((el.get("class") or "").split()), el.get("id"), {}
            for _, _, rtag, ids, classes, decls in rules:
                if (rtag is None or rtag == tag) and all(i == eid for i in ids) and cls.issuperset(classes):
                    merged.update(decls)
            if merged:
                merged.update(_decls(el.get("style")))
                el.set("style", _style_str(merged))
        st = _decls(el.get("style"))
        if (st.get("display") or el.get("display")) == "none" and el is not root:
            hidden.append(el)
        clip = clip or bool(el.get("clip-path") or el.get("mask") or "clip-path" in st or "mask" in st)
        images = images or tag == "image"
    for el in hidden:
        if el in parent:
            parent[el].remove(el)
    if clip:
        notes.append("Clipping masks are ignored: parts hidden by a clip are imported too.")
    if images:
        notes.append("Pictures inside this file aren't imported: add them as Photo layers.")
    parent = {c: p for p in root.iter() for c in p}
    for el in [e for e in root.iter() if _local(e.tag) == "text"]:
        try:
            try:
                done = _text_to_path(el, parent)
            except LookupError:
                if not stand_in:
                    raise
                done = _text_to_path(el, parent, stand_in)
                notes.append(f"Some text uses fonts that aren't available here and was converted with "
                             f"{stand_in}.")
            if not done:
                notes.append("Text on a path isn't supported; it was converted with a stand-in font."
                             if not stand_in else "Text on a path isn't supported and was left out.")
        except Exception:
            pass                         # left as text: MuPDF converts it (see _load_mupdf)
    flow = [e for e in root.iter() if _local(e.tag) == "flowRoot"]
    for el in flow:
        if el in parent:
            parent[el].remove(el)
    if flow:
        notes.append("Flowed text (old Inkscape text boxes) was skipped: convert it to paths first.")
    if mupdf:
        _fix_for_mupdf(root)
    return (root if as_tree else ET.tostring(root)), w_in, h_in, sized, notes


_SHAPES = ("path", "rect", "circle", "ellipse", "line", "polyline", "polygon")
_STYLE_TO_ATTR = ("fill-rule", "clip-rule", "fill-opacity", "stroke-opacity", "opacity", "stroke-width")


def _fix_for_mupdf(root):
    """Rewrites SVG that MuPDF misreads into a form it reads the way browsers / Inkscape draw it
    (and like _load_svg_plain): fill-rule / opacities / stroke-width inside style="" (ignored)
    -> attributes; stroke-width units (dropped) -> px; currentColor and visibility (ignored) ->
    resolved; nested <svg> x/y (ignored) -> a translate; <use> width/height on a <symbol>
    (ignored) -> a sized nested <svg> copy; rounded rects (corners drawn off a true circle) ->
    paths."""
    import copy
    ids = {e.get("id"): e for e in root.iter() if e.get("id")}
    svg = lambda tag: f"{{{SVG_NS}}}{tag}"
    for _ in range(10):                                   # symbols may use other symbols
        parent = {c: p for p in root.iter() for c in p}
        changed = False
        for use in [e for e in root.iter() if _local(e.tag) == "use"]:
            href = use.get(f"{{{XLINK_NS}}}href") or use.get("href") or ""
            ref = ids.get(href[1:]) if href.startswith("#") else None
            box = [float(v) for v in re.split(r"[\s,]+", ref.get("viewBox", "").strip()) if v] \
                if ref is not None and _local(ref.tag) == "symbol" else []
            if len(box) != 4 or (use.get("width") is None and use.get("height") is None) or use not in parent:
                continue
            w = _length(use.get("width")) or _length(ref.get("width")) or box[2]
            h = _length(use.get("height")) or _length(ref.get("height")) or box[3]
            g = ET.Element(svg("g"), {k: v for k, v in use.attrib.items() if k not in
                                      ("x", "y", "width", "height", "href", f"{{{XLINK_NS}}}href")})
            g.set("transform", f"{use.get('transform') or ''} translate({_length(use.get('x')) or 0.0:g},"
                               f"{_length(use.get('y')) or 0.0:g})".strip())
            inner = ET.SubElement(g, svg("svg"), {k: v for k, v in ref.attrib.items() if k not in
                                                  ("id", "x", "y", "width", "height")})
            inner.set("width", f"{w:g}")
            inner.set("height", f"{h:g}")
            for c in ref:
                c = copy.deepcopy(c)
                for e in c.iter():
                    e.attrib.pop("id", None)
                inner.append(c)
            par = parent[use]
            par.insert(list(par).index(use), g)
            par.remove(use)
            changed = True
        if not changed:
            break
    parent = {c: p for p in root.iter() for c in p}
    for el in [e for e in root.iter() if _local(e.tag) == "svg" and e is not root]:
        x, y = _length(el.get("x")) or 0.0, _length(el.get("y")) or 0.0
        if (x or y) and el in parent:
            g = ET.Element(svg("g"), {"transform": f"translate({x:g},{y:g})"})
            par = parent[el]
            par.insert(list(par).index(el), g)
            par.remove(el)
            g.append(el)
            el.attrib.pop("x", None)
            el.attrib.pop("y", None)

    hidden_uses = []

    def walk(el, color, vis):
        if not isinstance(el.tag, str):
            return
        tag, st = _local(el.tag), _decls(el.get("style"))
        c = st.get("color") or el.get("color")
        color = c if c and c not in ("inherit", "currentColor") else color
        v = st.pop("visibility", None) or el.attrib.pop("visibility", None)
        vis = v if v and v != "inherit" else vis
        for k in ("fill", "stroke", "stop-color"):
            if (st.get(k) or "").strip() == "currentColor":
                st[k] = color
            if (el.get(k) or "").strip() == "currentColor":
                el.set(k, color)
        for k in _STYLE_TO_ATTR:                     # style beats the attribute, so this keeps
            if k in st:                              # the cascade the same
                el.set(k, st.pop(k))
        sw = el.get("stroke-width")
        if sw and re.search(r"[a-z]", sw, re.I) and _length(sw) is not None:
            el.set("stroke-width", f"{_length(sw):g}")
        if vis in ("hidden", "collapse"):
            if tag in _SHAPES:
                st.update(fill="none", stroke="none")
            elif tag == "use":
                hidden_uses.append(el)
        if tag == "rect":
            subs = _shape_subs(el, "rect")
            rx, ry = _length(el.get("rx")) or 0.0, _length(el.get("ry")) or 0.0
            if subs and (rx > 0 or ry > 0) and len(subs[0][1]) > 4:
                for k in ("x", "y", "width", "height", "rx", "ry"):
                    el.attrib.pop(k, None)
                el.tag = svg("path")
                el.set("d", _subs_d(subs))
        if st:
            el.set("style", _style_str(st))
        else:
            el.attrib.pop("style", None)
        for ch in el:
            walk(ch, color, vis)

    walk(root, "black", "visible")
    parent = {c: p for p in root.iter() for c in p}
    for el in hidden_uses:
        if el in parent:
            parent[el].remove(el)


def _subs_d(subs):
    f = lambda p: f"{p[0]:.6f},{p[1]:.6f}"
    out = []
    for start, segs, closed in subs:
        out.append("M" + f(start))
        out.extend(("L" + f(s[1])) if s[0] == "L" else f"C{f(s[1])} {f(s[2])} {f(s[3])}" for s in segs)
        if closed:
            out.append("Z")
    return " ".join(out)


# ------------------------------------------------------------------------------
# SVG without MuPDF (the web version): the prepared SVG's shapes read directly
# ------------------------------------------------------------------------------
# Same result format as _load_mupdf: paths, rects, circles, ellipses, lines, polylines and
# polygons with their transforms, <use> / <symbol>, nested <svg>, inherited styles. Not drawn
# (as on the desktop): clipping, masks, pictures, markers. Gradients and patterns are taken as
# one solid color.

_CSS_COLORS = dict(zip(*[iter("""
aliceblue f0f8ff antiquewhite faebd7 aqua 00ffff aquamarine 7fffd4 azure f0ffff beige f5f5dc bisque ffe4c4
black 000000 blanchedalmond ffebcd blue 0000ff blueviolet 8a2be2 brown a52a2a burlywood deb887 cadetblue 5f9ea0
chartreuse 7fff00 chocolate d2691e coral ff7f50 cornflowerblue 6495ed cornsilk fff8dc crimson dc143c cyan 00ffff
darkblue 00008b darkcyan 008b8b darkgoldenrod b8860b darkgray a9a9a9 darkgreen 006400 darkgrey a9a9a9
darkkhaki bdb76b darkmagenta 8b008b darkolivegreen 556b2f darkorange ff8c00 darkorchid 9932cc darkred 8b0000
darksalmon e9967a darkseagreen 8fbc8f darkslateblue 483d8b darkslategray 2f4f4f darkslategrey 2f4f4f
darkturquoise 00ced1 darkviolet 9400d3 deeppink ff1493 deepskyblue 00bfff dimgray 696969 dimgrey 696969
dodgerblue 1e90ff firebrick b22222 floralwhite fffaf0 forestgreen 228b22 fuchsia ff00ff gainsboro dcdcdc
ghostwhite f8f8ff gold ffd700 goldenrod daa520 gray 808080 grey 808080 green 008000 greenyellow adff2f
honeydew f0fff0 hotpink ff69b4 indianred cd5c5c indigo 4b0082 ivory fffff0 khaki f0e68c lavender e6e6fa
lavenderblush fff0f5 lawngreen 7cfc00 lemonchiffon fffacd lightblue add8e6 lightcoral f08080 lightcyan e0ffff
lightgoldenrodyellow fafad2 lightgray d3d3d3 lightgreen 90ee90 lightgrey d3d3d3 lightpink ffb6c1
lightsalmon ffa07a lightseagreen 20b2aa lightskyblue 87cefa lightslategray 778899 lightslategrey 778899
lightsteelblue b0c4de lightyellow ffffe0 lime 00ff00 limegreen 32cd32 linen faf0e6 magenta ff00ff maroon 800000
mediumaquamarine 66cdaa mediumblue 0000cd mediumorchid ba55d3 mediumpurple 9370db mediumseagreen 3cb371
mediumslateblue 7b68ee mediumspringgreen 00fa9a mediumturquoise 48d1cc mediumvioletred c71585
midnightblue 191970 mintcream f5fffa mistyrose ffe4e1 moccasin ffe4b5 navajowhite ffdead navy 000080
oldlace fdf5e6 olive 808000 olivedrab 6b8e23 orange ffa500 orangered ff4500 orchid da70d6 palegoldenrod eee8aa
palegreen 98fb98 paleturquoise afeeee palevioletred db7093 papayawhip ffefd5 peachpuff ffdab9 peru cd853f
pink ffc0cb plum dda0dd powderblue b0e0e6 purple 800080 rebeccapurple 663399 red ff0000 rosybrown bc8f8f
royalblue 4169e1 saddlebrown 8b4513 salmon fa8072 sandybrown f4a460 seagreen 2e8b57 seashell fff5ee
sienna a0522d silver c0c0c0 skyblue 87ceeb slateblue 6a5acd slategray 708090 slategrey 708090 snow fffafa
springgreen 00ff7f steelblue 4682b4 tan d2b48c teal 008080 thistle d8bfd8 tomato ff6347 turquoise 40e0d0
violet ee82ee wheat f5deb3 white ffffff whitesmoke f5f5f5 yellow ffff00 yellowgreen 9acd32
""".split())] * 2))
_INHERITED = ("fill", "stroke", "stroke-width", "fill-rule", "fill-opacity", "stroke-opacity", "visibility",
              "stroke-linecap", "stroke-linejoin", "color")
_NOT_DRAWN = {"defs", "clipPath", "mask", "pattern", "marker", "symbol", "linearGradient", "radialGradient",
              "style", "title", "desc", "metadata", "script", "filter", "foreignObject", "image", "text",
              "font", "font-face", "cursor", "view", "animate", "set"}
_KAPPA = 0.5522847498307936          # cubic Bézier control distance for a quarter circle


def _mat_mul(m, n):
    a, b, c, d, e, f = m
    a2, b2, c2, d2, e2, f2 = n
    return (a * a2 + c * b2, b * a2 + d * b2, a * c2 + c * d2, b * c2 + d * d2,
            a * e2 + c * f2 + e, b * e2 + d * f2 + f)


def _transform(text):
    m = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    for name, args in re.findall(r"(matrix|translate|scale|rotate|skewX|skewY)\s*\(([^)]*)\)", text or ""):
        v = _nums(args)
        if name == "matrix" and len(v) == 6:
            t = tuple(v)
        elif name == "translate" and v:
            t = (1, 0, 0, 1, v[0], v[1] if len(v) > 1 else 0.0)
        elif name == "scale" and v:
            t = (v[0], 0, 0, v[1] if len(v) > 1 else v[0], 0, 0)
        elif name == "rotate" and v:
            a = math.radians(v[0])
            t = (math.cos(a), math.sin(a), -math.sin(a), math.cos(a), 0, 0)
            if len(v) == 3:
                t = _mat_mul(_mat_mul((1, 0, 0, 1, v[1], v[2]), t), (1, 0, 0, 1, -v[1], -v[2]))
        elif name == "skewX" and v:
            t = (1, 0, math.tan(math.radians(v[0])), 1, 0, 0)
        elif name == "skewY" and v:
            t = (1, math.tan(math.radians(v[0])), 0, 1, 0, 0)
        else:
            continue
        m = _mat_mul(m, t)
    return m


def _viewport(vb, x, y, w, h, par):
    """Transform from a viewBox (vx, vy, vw, vh) into the box x, y, w, h (preserveAspectRatio)."""
    vx, vy, vw, vh = vb
    if vw <= 0 or vh <= 0:
        return (1, 0, 0, 1, x, y)
    sx, sy = w / vw, h / vh
    par = (par or "xMidYMid meet").split()
    align = par[0] if par else "xMidYMid"
    if align != "none":
        s = max(sx, sy) if len(par) > 1 and par[1] == "slice" else min(sx, sy)
        ax = {"xMin": 0.0, "xMid": 0.5, "xMax": 1.0}.get(align[:4], 0.5)
        ay = {"YMin": 0.0, "YMid": 0.5, "YMax": 1.0}.get(align[4:], 0.5)
        x += (w - vw * s) * ax
        y += (h - vh * s) * ay
        sx = sy = s
    return (sx, 0, 0, sy, x - vx * sx, y - vy * sy)


def _arc_curves(p0, rx, ry, phi, large, sweep, p1):
    """An SVG elliptical arc as cubic Béziers [(c1, c2, p), ...] (endpoint -> center form)."""
    if rx == 0 or ry == 0 or p0 == p1:
        return [(p0, p1, p1)]
    rx, ry = abs(rx), abs(ry)
    cp, sp = math.cos(phi), math.sin(phi)
    dx, dy = (p0[0] - p1[0]) / 2, (p0[1] - p1[1]) / 2
    x1, y1 = cp * dx + sp * dy, -sp * dx + cp * dy
    lam = x1 * x1 / (rx * rx) + y1 * y1 / (ry * ry)
    if lam > 1:
        rx, ry = rx * math.sqrt(lam), ry * math.sqrt(lam)
    num = rx * rx * ry * ry - rx * rx * y1 * y1 - ry * ry * x1 * x1
    den = rx * rx * y1 * y1 + ry * ry * x1 * x1
    k = math.sqrt(max(0.0, num / den)) if den else 0.0
    if large == sweep:
        k = -k
    cx1, cy1 = k * rx * y1 / ry, -k * ry * x1 / rx
    cx = cp * cx1 - sp * cy1 + (p0[0] + p1[0]) / 2
    cy = sp * cx1 + cp * cy1 + (p0[1] + p1[1]) / 2

    def ang(ux, uy, vx, vy):
        a = math.atan2(ux * vy - uy * vx, ux * vx + uy * vy)
        return a

    t1 = ang(1, 0, (x1 - cx1) / rx, (y1 - cy1) / ry)
    dt = ang((x1 - cx1) / rx, (y1 - cy1) / ry, (-x1 - cx1) / rx, (-y1 - cy1) / ry)
    if not sweep and dt > 0:
        dt -= 2 * math.pi
    elif sweep and dt < 0:
        dt += 2 * math.pi
    n = max(1, math.ceil(abs(dt) / (math.pi / 2) - 1e-9))
    d = dt / n
    alpha = 4 / 3 * math.tan(d / 4)
    out = []

    def pt(t):
        x, y = rx * math.cos(t), ry * math.sin(t)
        return cx + cp * x - sp * y, cy + sp * x + cp * y

    def deriv(t):
        x, y = -rx * math.sin(t), ry * math.cos(t)
        return cp * x - sp * y, sp * x + cp * y

    t = t1
    for i in range(n):
        a, b = pt(t), pt(t + d)
        da, db = deriv(t), deriv(t + d)
        out.append(((a[0] + alpha * da[0], a[1] + alpha * da[1]), (b[0] - alpha * db[0], b[1] - alpha * db[1]),
                    p1 if i == n - 1 else b))
        t += d
    return out


def _path_subs(d):
    """SVG path data -> [(start, segs, closed), ...] in user units."""
    tokens = re.findall(r"[MmLlHhVvCcSsQqTtAaZz]|[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?", d or "")
    subs, segs, start, cur = [], [], None, (0.0, 0.0)
    last_c = last_q = None               # reflected control points for S / T
    i, cmd = 0, None

    def num():
        nonlocal i
        v = float(tokens[i])
        i += 1
        return v

    def flag():                          # arc flags may be written without separators ("011")
        nonlocal i
        t = tokens[i]
        if len(t) > 1 and t[0] in "01" and not t.startswith(("0.", "1.")):
            tokens[i] = t[1:]
            return t[0] == "1"
        i += 1
        return float(t) != 0

    def finish(closed=False):
        nonlocal segs
        if start is not None and segs:
            subs.append((start, segs, closed or _close_enough(start, cur)))
        segs = []

    while i < len(tokens):
        t = tokens[i]
        if t.isalpha():
            cmd = t
            i += 1
            if cmd in "Zz":
                if start is not None and segs and not _close_enough(cur, start):
                    segs.append(("L", start))
                finish(True)
                cur = start if start is not None else cur     # (drawing on starts a subpath here)
                start = None
                last_c = last_q = None
                continue
        if cmd is None:
            i += 1
            continue
        rel = cmd.islower()
        c = cmd.upper()
        try:
            if c == "M":
                finish()
                p = (num(), num())
                cur = (cur[0] + p[0], cur[1] + p[1]) if rel else p
                start = cur
                cmd = "l" if rel else "L"                  # further pairs are line-tos
                last_c = last_q = None
                continue
            if start is None:                              # drawing after Z (or no M yet)
                start = cur
            if c == "L":
                p = (num(), num())
                cur = (cur[0] + p[0], cur[1] + p[1]) if rel else p
                segs.append(("L", cur))
                last_c = last_q = None
            elif c == "H":
                x = num()
                cur = (cur[0] + x if rel else x, cur[1])
                segs.append(("L", cur))
                last_c = last_q = None
            elif c == "V":
                y = num()
                cur = (cur[0], cur[1] + y if rel else y)
                segs.append(("L", cur))
                last_c = last_q = None
            elif c in "CS":
                if c == "C":
                    c1 = (num(), num())
                    if rel:
                        c1 = (cur[0] + c1[0], cur[1] + c1[1])
                else:
                    c1 = (2 * cur[0] - last_c[0], 2 * cur[1] - last_c[1]) if last_c else cur
                c2, p = (num(), num()), (num(), num())
                if rel:
                    c2, p = (cur[0] + c2[0], cur[1] + c2[1]), (cur[0] + p[0], cur[1] + p[1])
                segs.append(("C", c1, c2, p))
                cur, last_c, last_q = p, c2, None
            elif c in "QT":
                if c == "Q":
                    q = (num(), num())
                    if rel:
                        q = (cur[0] + q[0], cur[1] + q[1])
                else:
                    q = (2 * cur[0] - last_q[0], 2 * cur[1] - last_q[1]) if last_q else cur
                p = (num(), num())
                if rel:
                    p = (cur[0] + p[0], cur[1] + p[1])
                segs.append(("C", (cur[0] + 2 / 3 * (q[0] - cur[0]), cur[1] + 2 / 3 * (q[1] - cur[1])),
                             (p[0] + 2 / 3 * (q[0] - p[0]), p[1] + 2 / 3 * (q[1] - p[1])), p))
                cur, last_q, last_c = p, q, None
            elif c == "A":
                rx, ry, rot = num(), num(), num()
                large, sweep = flag(), flag()
                p = (num(), num())
                if rel:
                    p = (cur[0] + p[0], cur[1] + p[1])
                for c1, c2, q in _arc_curves(cur, rx, ry, math.radians(rot), large, sweep, p):
                    segs.append(("C", c1, c2, q))
                cur = p
                last_c = last_q = None
            else:
                i += 1
        except (IndexError, ValueError):
            break                                           # bad data: keep what was read
    finish()
    return subs


def _ellipse_subs(cx, cy, rx, ry):
    kx, ky = rx * _KAPPA, ry * _KAPPA
    p = [(cx + rx, cy), (cx, cy + ry), (cx - rx, cy), (cx, cy - ry)]
    segs = [("C", (cx + rx, cy + ky), (cx + kx, cy + ry), p[1]), ("C", (cx - kx, cy + ry), (cx - rx, cy + ky), p[2]),
            ("C", (cx - rx, cy - ky), (cx - kx, cy - ry), p[3]), ("C", (cx + kx, cy - ry), (cx + rx, cy - ky), p[0])]
    return [(p[0], segs, True)]


def _shape_subs(el, tag):
    g = lambda k, d=0.0: _length(el.get(k)) if _length(el.get(k)) is not None else d
    if tag == "path":
        return _path_subs(el.get("d"))
    if tag == "rect":
        x, y, w, h = g("x"), g("y"), g("width"), g("height")
        if w <= 0 or h <= 0:
            return []
        rx, ry = _length(el.get("rx")), _length(el.get("ry"))
        rx, ry = (rx if rx is not None else ry or 0.0), (ry if ry is not None else rx or 0.0)
        rx, ry = min(max(rx, 0), w / 2), min(max(ry, 0), h / 2)
        if not rx or not ry:
            return [((x, y), [("L", (x + w, y)), ("L", (x + w, y + h)), ("L", (x, y + h)), ("L", (x, y))], True)]
        kx, ky = rx * (1 - _KAPPA), ry * (1 - _KAPPA)
        segs = [("L", (x + w - rx, y)), ("C", (x + w - kx, y), (x + w, y + ky), (x + w, y + ry)),
                ("L", (x + w, y + h - ry)), ("C", (x + w, y + h - ky), (x + w - kx, y + h), (x + w - rx, y + h)),
                ("L", (x + rx, y + h)), ("C", (x + kx, y + h), (x, y + h - ky), (x, y + h - ry)),
                ("L", (x, y + ry)), ("C", (x, y + ky), (x + kx, y), (x + rx, y))]
        return [((x + rx, y), segs, True)]
    if tag == "circle":
        r = g("r")
        return _ellipse_subs(g("cx"), g("cy"), r, r) if r > 0 else []
    if tag == "ellipse":
        rx, ry = g("rx"), g("ry")
        return _ellipse_subs(g("cx"), g("cy"), rx, ry) if rx > 0 and ry > 0 else []
    if tag == "line":
        return [((g("x1"), g("y1")), [("L", (g("x2"), g("y2")))], False)]
    if tag in ("polyline", "polygon"):
        v = _nums(el.get("points"))
        pts = list(zip(v[0::2], v[1::2]))
        if len(pts) < 2:
            return []
        segs = [("L", p) for p in pts[1:]]
        if tag == "polygon" and not _close_enough(pts[0], pts[-1]):
            segs.append(("L", pts[0]))
        return [(pts[0], segs, tag == "polygon" or _close_enough(pts[0], pts[-1]))]
    return []


def _color(v, st, ids):
    """CSS color -> "#rrggbb", None for none / unknown."""
    v = (v or "").strip()
    if not v or v == "none" or v == "transparent":
        return None
    if v == "currentColor":
        return _color(st.get("color", "black"), {}, ids)
    m = re.match(r"url\(\s*['\"]?#([^)'\"]+)['\"]?\s*\)\s*(.*)", v)
    if m:                                           # gradient / pattern: its first stop's color
        ref = ids.get(m.group(1))
        seen = set()
        while ref is not None and id(ref) not in seen:
            seen.add(id(ref))
            stops = [s for s in ref if _local(s.tag) == "stop"]
            if stops:
                s = stops[0]
                return _color(_decls(s.get("style")).get("stop-color") or s.get("stop-color") or "black", {}, ids)
            href = ref.get(f"{{{XLINK_NS}}}href") or ref.get("href") or ""
            ref = ids.get(href[1:]) if href.startswith("#") else None
        return _color(m.group(2), st, ids) if m.group(2) else "#000000"
    if v.startswith("#"):
        h = v[1:]
        if len(h) in (3, 4):
            h = "".join(c * 2 for c in h[:3])
        return "#" + h[:6].lower() if len(h) >= 6 and re.fullmatch(r"[0-9a-fA-F]+", h[:6]) else None
    m = re.match(r"rgba?\(([^)]*)\)", v)
    if m:
        parts = [p.strip() for p in re.split(r"[,\s/]+", m.group(1)) if p.strip()]
        if len(parts) >= 4 and float(parts[3].rstrip("%")) == 0:
            return None
        ch = [float(p[:-1]) / 100 * 255 if p.endswith("%") else float(p) for p in parts[:3]]
        return "#%02x%02x%02x" % tuple(max(0, min(255, round(c))) for c in ch)
    hexc = _CSS_COLORS.get(v.lower())
    return "#" + hexc if hexc else None


def _opacity(v):
    try:
        v = (v or "1").strip()
        return float(v[:-1]) / 100 if v.endswith("%") else float(v)
    except ValueError:
        return 1.0


def _load_svg_plain(raw):
    root, w_in, h_in, sized, notes = _prepare_svg(raw, stand_in="Arial", as_tree=True)
    vb = [float(v) for v in re.split(r"[\s,]+", root.get("viewBox", "").strip()) if v]
    m0 = _viewport(vb, 0, 0, w_in, h_in, root.get("preserveAspectRatio"))
    ids = {e.get("id"): e for e in root.iter() if e.get("id")}
    items, skipped = [], set()

    def style_of(el, inherited):
        st = dict(inherited)
        own = {k: el.get(k) for k in _INHERITED + ("opacity", "display") if el.get(k) is not None}
        own.update({k: v for k, v in _decls(el.get("style")).items()})
        for k, v in own.items():
            if v != "inherit":
                st[k] = v
        st["_opacity"] = inherited.get("_opacity", 1.0) * _opacity(own.get("opacity"))
        return st

    def shape(el, tag, m, st):
        subs = _shape_subs(el, tag)
        if not subs or st.get("visibility") in ("hidden", "collapse"):
            return
        op = st["_opacity"]
        fill = _color(st.get("fill", "black"), st, ids)
        stroke = _color(st.get("stroke", "none"), st, ids)
        if fill and (op * _opacity(st.get("fill-opacity")) <= 0 or tag in ("line",)):
            fill = None
        if stroke and op * _opacity(st.get("stroke-opacity")) <= 0:
            stroke = None
        sw = _length(st.get("stroke-width", "1")) if stroke else 0.0
        sw = 1.0 if sw is None else sw                      # (percent widths: not supported)
        if stroke and not sw:
            stroke = None
        if not stroke and not fill:
            return
        tf = lambda p: (m[0] * p[0] + m[2] * p[1] + m[4], m[1] * p[0] + m[3] * p[1] + m[5])
        subs = [(tf(s), [(q[0],) + tuple(tf(p) for p in q[1:]) for q in segs], c) for s, segs, c in subs]
        subs = [s for s in subs if s[1]]
        if not subs:
            return
        if fill and sum(abs(_area(flatten(s, 0.01))) for s in subs) < 1e-7:
            fill = None                 # a plain line: the default black fill paints nothing
            if not stroke:
                return
        scale = math.sqrt(abs(m[0] * m[3] - m[1] * m[2]))
        caps = {"butt": 0, "round": 1, "square": 2}
        joins = {"miter": 0, "miter-clip": 0, "arcs": 0, "round": 1, "bevel": 2}
        items.append({"stroke": stroke, "fill": fill, "width_in": (sw or 0.0) * scale if stroke else 0.0,
                      "evenodd": (st.get("fill-rule") or "").strip() == "evenodd", "subs": subs,
                      "cap": caps.get((st.get("stroke-linecap") or "butt").strip(), 0),
                      "join": joins.get((st.get("stroke-linejoin") or "miter").strip(), 0)})

    def walk(el, m, inherited, depth=0):
        if depth > 50 or not isinstance(el.tag, str) or not el.tag.startswith(f"{{{SVG_NS}}}"):
            return
        tag = _local(el.tag)
        st = style_of(el, inherited)
        if st.get("display") == "none":
            return
        m = _mat_mul(m, _transform(el.get("transform")))
        if tag in ("g", "a", "switch"):
            for c in el:
                walk(c, m, st, depth + 1)
        elif tag == "svg":                                    # nested viewport
            x, y = _length(el.get("x")) or 0.0, _length(el.get("y")) or 0.0
            box = [float(v) for v in re.split(r"[\s,]+", el.get("viewBox", "").strip()) if v]
            w, h = _length(el.get("width")), _length(el.get("height"))
            if len(box) == 4:
                m = _mat_mul(m, _viewport(box, x, y, w or box[2], h or box[3], el.get("preserveAspectRatio")))
            else:
                m = _mat_mul(m, (1, 0, 0, 1, x, y))
            for c in el:
                walk(c, m, st, depth + 1)
        elif tag == "use":
            href = el.get(f"{{{XLINK_NS}}}href") or el.get("href") or ""
            ref = ids.get(href[1:]) if href.startswith("#") else None
            if ref is None:
                return
            m = _mat_mul(m, (1, 0, 0, 1, _length(el.get("x")) or 0.0, _length(el.get("y")) or 0.0))
            if _local(ref.tag) == "symbol":
                rst = style_of(ref, st)
                box = [float(v) for v in re.split(r"[\s,]+", ref.get("viewBox", "").strip()) if v]
                if len(box) == 4:
                    w = _length(el.get("width")) or _length(ref.get("width")) or box[2]
                    h = _length(el.get("height")) or _length(ref.get("height")) or box[3]
                    m = _mat_mul(m, _viewport(box, 0, 0, w, h, ref.get("preserveAspectRatio")))
                for c in ref:
                    walk(c, m, rst, depth + 1)
            else:
                walk(ref, m, st, depth + 1)
        elif tag in ("path", "rect", "circle", "ellipse", "line", "polyline", "polygon"):
            shape(el, tag, m, st)
        elif tag in ("text", "marker") or (tag not in _NOT_DRAWN and tag != "stop"):
            if tag in ("text", "marker"):
                skipped.add(tag)

    root_style = style_of(root, {"_opacity": 1.0})
    for c in root:
        walk(c, m0, root_style)
    if "text" in skipped:
        notes.append("Some text couldn't be converted to outlines and was left out.")
    if "marker" in skipped or any(_local(e.tag) == "marker" for e in root.iter()):
        notes.append("Line-end markers (arrowheads) aren't imported.")
    if any(_local(e.tag) in ("linearGradient", "radialGradient", "pattern") for e in root.iter()):
        notes.append("Gradients and patterns are taken as one solid color.")
    return {"kind": "svg", "w_in": w_in, "h_in": h_in, "sized": sized, "items": items, "notes": notes}


# --- text -> outlines --------------------------------------------------------

_GENERIC = {"sans-serif": "Arial", "serif": "Times New Roman", "monospace": "Courier New",
            "cursive": "Comic Sans MS", "fantasy": "Impact", "helvetica": "Arial", "times": "Times New Roman"}


def _prop(el, name, parent, default=None):
    """Inherited style property (style attribute beats the presentation attribute)."""
    while el is not None:
        v = _decls(el.get("style")).get(name)
        if v is None:
            v = el.get(name)
        if v not in (None, "inherit"):
            return v
        el = parent.get(el)
    return default


def _font_size(el, parent):
    chain = []
    while el is not None:
        chain.append(el)
        el = parent.get(el)
    size = 16.0
    for e in reversed(chain):
        v = _decls(e.get("style")).get("font-size") or e.get("font-size")
        if not v:
            continue
        v = v.strip()
        if v.endswith("em"):
            size *= float(v[:-2] or 1)
        elif v.endswith("%"):
            size *= float(v[:-1]) / 100
        else:
            px = _length(v)
            size = px if px is not None else size
    return size


def _font_for(el, parent, stand_in=None):
    import design as D
    installed = D.fonts()
    lower = {k.lower(): k for k in installed}
    family = None
    for name in (_prop(el, "font-family", parent, "Arial") or "Arial").split(","):
        name = name.strip().strip("'\"")
        key = lower.get(name.lower()) or lower.get(_GENERIC.get(name.lower(), "").lower())
        if key:
            family = key
            break
    if family is None and stand_in:
        family = lower.get(stand_in.lower())
    if family is None:
        raise LookupError("font not installed")
    weight = (_prop(el, "font-weight", parent, "normal") or "normal").lower()
    bold = weight in ("bold", "bolder") or (weight.isdigit() and int(weight) >= 600)
    italic = (_prop(el, "font-style", parent, "normal") or "").lower() in ("italic", "oblique")
    style = {(False, False): "Regular", (True, False): "Bold", (False, True): "Italic",
             (True, True): "Bold Italic"}[(bold, italic)]
    styles = installed[family]
    if style not in styles and italic and bold and "Bold Oblique" in styles:
        style = "Bold Oblique"
    return D.font_path(family, style), family, style


def _nums(v):
    return [float(x) for x in re.findall(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:e[-+]?\d+)?", v or "")]


def _text_to_path(text_el, parent, stand_in=None):
    """Replaces a <text> element with a <g> of outline <path>s (installed fonts, fontTools).
    Returns False (and leaves it) for text on a path. stand_in: font for missing families."""
    import vectors
    from fontTools.pens.svgPathPen import SVGPathPen
    from fontTools.pens.transformPen import TransformPen
    if any(_local(e.tag) == "textPath" for e in text_el.iter()):
        return False
    preserve = text_el.get("{http://www.w3.org/XML/1998/namespace}space") == "preserve"
    runs = []                                   # (element, string, absolute x, y, dx, dy)

    def walk(el):
        xs, ys, dxs, dys = _nums(el.get("x")), _nums(el.get("y")), _nums(el.get("dx")), _nums(el.get("dy"))
        pos = (xs[0] if xs else None, ys[0] if ys else None, dxs[0] if dxs else 0.0, dys[0] if dys else 0.0)
        if el.text:
            runs.append((el, el.text) + pos)
            pos = (None, None, 0.0, 0.0)
        elif any(v not in (None, 0.0) for v in pos):
            runs.append((el, "") + pos)
        for c in el:
            if _local(c.tag) == "tspan":
                walk(c)
            if c.tail:
                runs.append((el, c.tail, None, None, 0.0, 0.0))

    walk(text_el)
    cleaned = []
    for i, (el, s, *pos) in enumerate(runs):
        s = s.replace("\n", " " if preserve else "").replace("\t", " ").replace("\r", "")
        if not preserve:
            s = re.sub(r" +", " ", s)
        cleaned.append([el, s] + pos)
    if not preserve:
        for r in cleaned:
            if r[1]:
                r[1] = r[1].lstrip()
                break
        for r in reversed(cleaned):
            if r[1]:
                r[1] = r[1].rstrip()
                break

    group = ET.Element(f"{{{SVG_NS}}}g")
    for k, v in text_el.attrib.items():
        if k not in ("x", "y", "dx", "dy", "rotate", "textLength", "lengthAdjust"):
            group.set(k, v)
    pen_x = pen_y = 0.0
    chunks, chunk = [], None                    # chunks for text-anchor: [anchor, [path elements, d parts]]
    for el, s, ax, ay, dx, dy in cleaned:
        if ax is not None or chunk is None:
            chunk = {"anchor": _prop(el, "text-anchor", parent, "start"), "start": ax if ax is not None else pen_x,
                     "paths": []}
            chunks.append(chunk)
        if ax is not None:
            pen_x = ax
        if ay is not None:
            pen_y = ay
        pen_x += dx
        pen_y += dy
        if not s:
            continue
        path, family, style = _font_for(el, parent, stand_in)
        font = vectors._ttfont(path, family, style)
        size = _font_size(el, parent)
        k = size / font["head"].unitsPerEm
        spacing = _length(_prop(el, "letter-spacing", parent, "0") or "0") or 0.0
        cmap, hmtx, gs = font.getBestCmap(), font["hmtx"], font.getGlyphSet()
        d = []
        for ch in s:
            name = cmap.get(ord(ch), ".notdef")
            sp = SVGPathPen(gs, ntos=lambda v: f"{v:.4f}")      # real curves, font y flipped
            gs[name].draw(TransformPen(sp, (k, 0, 0, -k, pen_x, pen_y)))
            d.append(sp.getCommands())
            pen_x += hmtx[name][0] * k + spacing
        chunk["end"] = pen_x
        if d:
            p = ET.Element(f"{{{SVG_NS}}}path")
            styles, e = {}, el
            while e is not None and e is not text_el:      # the tspan chain's own styles
                for kk, vv in e.attrib.items():
                    if kk in ("fill", "stroke", "stroke-width", "opacity", "fill-opacity", "stroke-opacity"):
                        styles.setdefault(kk, vv)
                for kk, vv in _decls(e.get("style")).items():
                    styles.setdefault(kk, vv)
                e = parent.get(e)
            if styles:
                p.set("style", _style_str(styles))
            p.set("d", "".join(d))
            chunk["paths"].append(p)
    for c in chunks:
        width = c.get("end", c["start"]) - c["start"]
        shift = {"middle": -width / 2, "end": -width}.get(c["anchor"], 0.0)
        for p in c["paths"]:
            if shift:
                p.set("transform", f"translate({shift:.4f},0)")
            group.append(p)
    par = parent[text_el]
    idx = list(par).index(text_el)
    par.remove(text_el)
    par.insert(idx, group)
    return True


# ------------------------------------------------------------------------------
# DXF (ezdxf)
# ------------------------------------------------------------------------------

_DXF_UNITS = {1: 1.0, 2: 12.0, 4: 1 / 25.4, 5: 1 / 2.54, 6: 1000 / 25.4, 8: 1e-6, 9: 0.001,
              10: 36.0, 14: 0.1 / 2.54}


def _load_dxf(path):
    from ezdxf import recover, path as ezpath
    from ezdxf.colors import aci2rgb
    try:
        doc, auditor = recover.readfile(path)
    except Exception as e:
        raise ValueError(f"Couldn't read this DXF file ({e}).")
    notes = []
    units = doc.header.get("$INSUNITS", 0)
    k = _DXF_UNITS.get(units)
    if k is None:
        k = 1 / 25.4
        notes.append("The file doesn't say its units: millimetres assumed. Check the size.")
    layers = doc.layers

    def color(e):
        if e.dxf.hasattr("true_color"):
            return "#%02x%02x%02x" % e.rgb
        aci = e.dxf.get("color", 256)
        if aci in (0, 256):
            lay = layers.get(e.dxf.get("layer", "0")) if layers.has_entry(e.dxf.get("layer", "0")) else None
            if lay is not None and lay.dxf.hasattr("true_color"):
                return "#%02x%02x%02x" % lay.rgb
            aci = abs(lay.color) if lay is not None else 7
        if aci in (7, 0, 256):
            return "#000000"                   # ACI 7 is black on a white background
        return "#%02x%02x%02x" % aci2rgb(aci)

    def visible(e):
        name = e.dxf.get("layer", "0")
        if not layers.has_entry(name):
            return True
        lay = layers.get(name)
        return not (lay.is_off() or lay.is_frozen())

    raw, skipped = [], set()

    def add(e, block_color, depth=0):
        t = e.dxftype()
        if not visible(e):
            return
        col = color(e)
        if e.dxf.get("color", 256) == 0 and block_color:
            col = block_color                       # BYBLOCK: the block reference's color
        try:
            if t == "INSERT" or t in ("DIMENSION", "LEADER", "MLEADER", "ACAD_TABLE", "ARC_DIMENSION",
                                      "RADIAL_DIMENSION", "LARGE_RADIAL_DIMENSION"):
                if depth < 20:
                    for v in e.virtual_entities():  # transformed copies of the block's contents
                        add(v, col, depth + 1)
                if t == "INSERT":
                    for a in e.attribs:
                        add(a, col, depth + 1)
                return
            if t in ("TEXT", "MTEXT", "ATTRIB"):
                from ezdxf.addons import text2path
                paths, fill = text2path.make_paths_from_entity(e), True
            elif t in ("HATCH", "MPOLYGON"):
                paths, fill = list(ezpath.from_hatch(e)), True
            elif t in ("SOLID", "TRACE"):
                paths, fill = [ezpath.make_path(e)], True
            elif t in ("POINT", "IMAGE", "VIEWPORT", "WIPEOUT", "OLE2FRAME", "ATTDEF"):
                if t not in ("VIEWPORT", "ATTDEF"):
                    skipped.add(t)
                return
            else:
                paths, fill = [ezpath.make_path(e)], False
        except Exception:
            skipped.add(t)
            return
        raw.append((col, paths, fill))

    for e in doc.modelspace():
        add(e, None)
    if skipped:
        notes.append("Skipped DXF entities: " + ", ".join(sorted(skipped)) + ".")
    # Model space has no page: the page is the drawing's extents.
    items, xs, ys = [], [], []
    for col, paths, fill in raw:
        subs = []
        for p in paths:
            for sp in (p.sub_paths() if p.has_sub_paths else [p]):
                if len(sp) == 0:
                    continue
                start = (sp.start.x, sp.start.y)
                segs, cur = [], start
                for cmd in sp.commands():
                    ct = cmd.type.name
                    end = (cmd.end.x, cmd.end.y)
                    if ct == "LINE_TO":
                        segs.append(("L", end))
                    elif ct == "CURVE3_TO":
                        c = (cmd.ctrl.x, cmd.ctrl.y)
                        segs.append(("C", (cur[0] + 2 / 3 * (c[0] - cur[0]), cur[1] + 2 / 3 * (c[1] - cur[1])),
                                     (end[0] + 2 / 3 * (c[0] - end[0]), end[1] + 2 / 3 * (c[1] - end[1])), end))
                    elif ct == "CURVE4_TO":
                        segs.append(("C", (cmd.ctrl1.x, cmd.ctrl1.y), (cmd.ctrl2.x, cmd.ctrl2.y), end))
                    elif ct == "MOVE_TO":
                        if segs:
                            subs.append((start, segs, _close_enough(start, cur)))
                        start, segs = end, []
                    cur = end
                if segs:
                    subs.append((start, segs, fill or _close_enough(start, cur)))
        if subs:
            for s in subs:
                xs += [s[0][0]] + [q[-1][0] for q in s[1]]
                ys += [s[0][1]] + [q[-1][1] for q in s[1]]
            items.append((col, subs, fill))
    if not xs:
        return {"kind": "dxf", "w_in": 1.0, "h_in": 1.0, "sized": True, "items": [], "notes": notes}
    x0, y1 = min(xs), max(ys)
    w_in, h_in = (max(xs) - x0) * k, (y1 - min(ys)) * k
    tf = lambda p: ((p[0] - x0) * k, (y1 - p[1]) * k)        # DXF y is up
    out = []
    for col, subs, fill in items:
        conv = [(tf(s), [(q[0],) + tuple(tf(p) for p in q[1:]) for q in segs], c) for s, segs, c in subs]
        out.append({"stroke": None if fill else col, "fill": col if fill else None, "width_in": 0.0,
                    "cap": 1, "join": 1, "evenodd": True, "subs": conv})
    return {"kind": "dxf", "w_in": max(w_in, 0.01), "h_in": max(h_in, 0.01), "sized": True,
            "items": out, "notes": notes}
