"""
compare_design.py: renders a design with the desktop engine and the web version's bundled fonts,
and prints hashes to compare with the browser's export of the same design.

    python web/tools/compare_design.py design.json [asset folder]

design.json is a design from the web version (e.g. JSON.stringify of the page's design). Layer
"src" paths like /app/project_assets/<hash>.jpg are looked up by file name in the asset folder
(default: web/samples, after copying any other assets there). Prints the 1-bit pixel hash and
burn count of the PNG and the SHA-256 of each SVG; the browser side computes the same numbers
(see SESSION_NOTES.md / CLAUDE.md, "Web Design mode").
"""
import hashlib
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, ROOT)

import numpy as np                      # noqa: E402
from PIL import Image                   # noqa: E402

import design as D                      # noqa: E402


def bundled_fonts():
    fonts_dir = os.path.join(ROOT, "web", "fonts")
    with open(os.path.join(fonts_dir, "fonts.json"), encoding="utf-8") as f:
        m = json.load(f)
    found = {fam: {st: os.path.join(fonts_dir, n) for st, n in styles.items()}
             for fam, styles in m["families"].items()}
    for alias, fam in m["aliases"].items():
        found.setdefault(alias, found[fam])
    return found


def main():
    with open(sys.argv[1], encoding="utf-8-sig") as f:
        d = json.load(f)
    assets = sys.argv[2] if len(sys.argv) > 2 else os.path.join(ROOT, "web", "samples")
    by_hash = {}
    for n in os.listdir(assets):
        p = os.path.join(assets, n)
        with open(p, "rb") as f:
            by_hash[hashlib.sha1(f.read()).hexdigest()[:16]] = p
    for layer in d["layers"]:
        if layer.get("src"):
            layer["src"] = by_hash[os.path.splitext(os.path.basename(layer["src"]))[0]]
    D._FONTS = bundled_fonts()
    out = tempfile.mkdtemp()
    written = D.save_engraving(d, D.Renderer(), os.path.join(out, "test.png"))
    for p in written:
        if p.endswith(".png"):
            bits = (np.array(Image.open(p).convert("L")) < 128).astype(np.uint8)
            print(f"png {bits.shape[1]}x{bits.shape[0]} burn={int(bits.sum())} "
                  f"{hashlib.sha256(bits.tobytes()).hexdigest()}")
        else:
            with open(p, "rb") as f:
                print(os.path.basename(p), hashlib.sha256(f.read()).hexdigest())
    print("files in", out)


if __name__ == "__main__":
    main()
