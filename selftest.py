"""
selftest.py: headless check that the bundled libraries work. build.py runs the built exe
with --selftest <report.txt>; engrave_app.main() dispatches here.
"""

import os

from PIL import Image

import engrave_core as core


def selftest(report_path):
    """Headless check that the bundled libraries work (build.py runs this after building)."""
    import time
    import numpy as np
    lines, ok = [], True
    yy, xx = np.mgrid[0:600, 0:500]
    img = Image.fromarray(((xx + yy) % 256).astype(np.uint8)).convert("RGB")
    path = os.path.join(os.path.dirname(report_path), "selftest_input.png")
    img.save(path)
    fix = [("remove", 20, [(100, 100), (200, 150)])]
    for label, kw, strokes in (("photo/stucki", {}, None), ("comic", {"style": "comic"}, None),
                               ("remove background", {"remove_bg": True}, None),
                               ("cut-out fix (GrabCut)", {"remove_bg": True}, fix)):
        t = time.time()
        try:
            s = core.Settings(width_in=2.0, **kw)
            core.render(*core.load_source(path, s.remove_bg, strokes, s.bg_model), s)
            lines.append(f"OK    {label}  {time.time() - t:.1f} s")
        except Exception as e:
            ok = False
            lines.append(f"FAIL  {label}: {e!r}")
    # Design mode: text (fonts), arc text, shapes, a photo layer and vector files (SVG with CSS and
    # text through PyMuPDF + fontTools, DXF through ezdxf), score/cut lines with weld and offset
    # (shapely), every export format, and a project file round trip.
    t = time.time()
    try:
        import design as D
        import vecfile as V
        here = os.path.dirname(report_path)
        svg = os.path.join(here, "selftest.svg")
        with open(svg, "w") as f:
            f.write('<svg xmlns="http://www.w3.org/2000/svg" width="1in" height="1in" viewBox="0 0 10 10">'
                    '<style>.c{fill:none;stroke:#ff0000;stroke-width:0.01}</style>'
                    '<circle cx="5" cy="5" r="4" fill="black"/><rect class="c" x="1" y="1" width="8" height="8"/>'
                    '<text x="5" y="9" font-family="Arial" font-size="2" text-anchor="middle">Hi</text></svg>')
        import ezdxf
        dxf = os.path.join(here, "selftest.dxf")
        dd = ezdxf.new("R2010")
        dd.header["$INSUNITS"] = 4
        dd.modelspace().add_lwpolyline([(0, 0), (20, 0), (20, 10), (0, 10)], close=True)
        dd.modelspace().add_circle((10, 5), 3, dxfattribs={"color": 5})
        dd.saveas(dxf)
        sdoc, ddoc = V.load(svg), V.load(dxf)
        if [D.vector_jobs({"jobs": {}}, sdoc).get(k) for k in ("s:#ff0000", "f:#000000")] != ["cut", "engrave"]:
            raise RuntimeError(f"SVG colors read wrong: {D.vector_jobs({'jobs': {}}, sdoc)}")
        if abs(ddoc["w_in"] * 25.4 - 20) > 0.01:
            raise RuntimeError(f"DXF size read wrong: {ddoc['w_in'] * 25.4} mm")
        d = D.new_design("wood", 3, 3)
        d["layers"] += [D.new_layer("shape", d, shape="ellipse", offset_mm=1.0),
                        D.new_layer("text", d, op="cut"), D.new_layer("arctext", d, op="score", outer=True),
                        D.new_layer("svg", d, src=svg, w_in=0.5), D.new_layer("svg", d, src=dxf, w_in=0.8),
                        D.new_layer("image", d, src=path, w_in=1.0)]
        img, _ = D.Renderer().engraving(d)
        d["cut_format"], d["combined"] = "svg+dxf", "svg+pdf"
        files = D.save_engraving(d, D.Renderer(), os.path.join(here, "selftest_design.png"))
        for end in ("_cut.dxf", "_score.svg", "_combined.svg", "_combined.pdf"):
            if not any(f.endswith(end) for f in files):
                raise RuntimeError(f"{end} missing: {files}")
        proj = os.path.join(here, "selftest.lep")
        D.save_project(d, proj)
        D.load_project(proj)
        lines.append(f"OK    design ({len(D.fonts())} fonts, vector files, project file)  {time.time() - t:.1f} s")
    except Exception as e:
        ok = False
        lines.append(f"FAIL  design: {e!r}")
    lines.append(f"fast stucki (numba): {core.FAST_STUCKI}")
    ok = ok and core.FAST_STUCKI
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines + ["PASS" if ok else "FAIL"]) + "\n")
    return 0 if ok else 1
