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
    # Design mode: text (fonts), arc text, shapes, a photo layer and an SVG (PyMuPDF), plus a
    # project file round trip.
    t = time.time()
    try:
        import design as D
        svg = os.path.join(os.path.dirname(report_path), "selftest.svg")
        with open(svg, "w") as f:
            f.write('<svg xmlns="http://www.w3.org/2000/svg" width="1in" height="1in" viewBox="0 0 10 10">'
                    '<circle cx="5" cy="5" r="4" fill="black"/></svg>')
        d = D.new_design("wood", 3, 3)
        d["layers"] += [D.new_layer("shape", d, shape="ellipse"), D.new_layer("text", d),
                        D.new_layer("arctext", d), D.new_layer("svg", d, src=svg, w_in=0.5),
                        D.new_layer("image", d, src=path, w_in=1.0)]
        img, _ = D.Renderer().engraving(d)
        # Cut lines: shape + text outlines (fontTools) + SVG paths, written as SVG and DXF.
        d["layers"][1]["cut"] = d["layers"][3]["cut"] = True
        d["cut_format"] = "svg+dxf"
        files = D.save_engraving(d, D.Renderer(), os.path.join(os.path.dirname(report_path), "selftest_design.png"))
        if not any(f.endswith("_cut.dxf") for f in files):
            raise RuntimeError(f"cut files missing: {files}")
        proj = os.path.join(os.path.dirname(report_path), "selftest.lep")
        D.save_project(d, proj)
        D.load_project(proj)
        lines.append(f"OK    design ({len(D.fonts())} fonts, project file)  {time.time() - t:.1f} s")
    except Exception as e:
        ok = False
        lines.append(f"FAIL  design: {e!r}")
    lines.append(f"fast stucki (numba): {core.FAST_STUCKI}")
    ok = ok and core.FAST_STUCKI
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines + ["PASS" if ok else "FAIL"]) + "\n")
    return 0 if ok else 1
