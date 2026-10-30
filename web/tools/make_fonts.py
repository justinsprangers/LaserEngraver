"""
make_fonts.py: builds web/fonts/ (the fonts the web version's Design mode offers).

Browsers can't use the computer's installed fonts from Python, so the web version ships a small
set of open-licence fonts from the Google Fonts repository (github.com/google/fonts). This
script downloads them, turns variable fonts into plain Regular / Bold / Italic files (fontTools
instancer), trims them to Latin characters to keep them small, and writes web/fonts/fonts.json
plus each family's licence file.

    python web/tools/make_fonts.py

To add a family: add a line to FAMILIES (its folder in the google/fonts repo and the styles to
make), run the script again, and bump ?v= in web/index.html.
"""
import io
import json
import os
import sys
import urllib.request

from fontTools import subset
from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "fonts")
RAW = "https://raw.githubusercontent.com/google/fonts/main/"

# family: (repo folder, {style: (file in the folder, weight for variable fonts or None)})
FAMILIES = {
    # Same letter widths as Arial / Times New Roman / Courier New, so desktop projects line up.
    "Arimo": ("ofl/arimo", {"Regular": ("Arimo[wght].ttf", 400), "Bold": ("Arimo[wght].ttf", 700),
                            "Italic": ("Arimo-Italic[wght].ttf", 400),
                            "Bold Italic": ("Arimo-Italic[wght].ttf", 700)}),
    "Tinos": ("ofl/tinos", {"Regular": ("Tinos-Regular.ttf", None), "Bold": ("Tinos-Bold.ttf", None),
                            "Italic": ("Tinos-Italic.ttf", None), "Bold Italic": ("Tinos-BoldItalic.ttf", None)}),
    "Cousine": ("ofl/cousine", {"Regular": ("Cousine-Regular.ttf", None), "Bold": ("Cousine-Bold.ttf", None)}),
    "Montserrat": ("ofl/montserrat", {"Regular": ("Montserrat[wght].ttf", 400),
                                      "Bold": ("Montserrat[wght].ttf", 700),
                                      "Black": ("Montserrat[wght].ttf", 900)}),
    "Oswald": ("ofl/oswald", {"Regular": ("Oswald[wght].ttf", 400), "Bold": ("Oswald[wght].ttf", 700)}),
    "Bebas Neue": ("ofl/bebasneue", {"Regular": ("BebasNeue-Regular.ttf", None)}),
    "Playfair Display": ("ofl/playfairdisplay", {"Regular": ("PlayfairDisplay[wght].ttf", 400),
                                                 "Bold": ("PlayfairDisplay[wght].ttf", 700),
                                                 "Italic": ("PlayfairDisplay-Italic[wght].ttf", 400)}),
    "Lora": ("ofl/lora", {"Regular": ("Lora[wght].ttf", 400), "Bold": ("Lora[wght].ttf", 700),
                          "Italic": ("Lora-Italic[wght].ttf", 400)}),
    "Cinzel": ("ofl/cinzel", {"Regular": ("Cinzel[wght].ttf", 400), "Bold": ("Cinzel[wght].ttf", 700)}),
    "Dancing Script": ("ofl/dancingscript", {"Regular": ("DancingScript[wght].ttf", 400),
                                             "Bold": ("DancingScript[wght].ttf", 700)}),
    "Great Vibes": ("ofl/greatvibes", {"Regular": ("GreatVibes-Regular.ttf", None)}),
    "Pacifico": ("ofl/pacifico", {"Regular": ("Pacifico-Regular.ttf", None)}),
    "Lobster": ("ofl/lobster", {"Regular": ("Lobster-Regular.ttf", None)}),
    "Allerta Stencil": ("ofl/allertastencil", {"Regular": ("AllertaStencil-Regular.ttf", None)}),
}
# Families whose google/fonts folder has no licence file: take it from the font's own repo.
LICENSES = {"Tinos": "https://raw.githubusercontent.com/googlefonts/tinos/main/OFL.txt"}
# Desktop font names that map onto a bundled family (design.py's default is Arial).
ALIASES = {"Arial": "Arimo", "Helvetica": "Arimo", "Times New Roman": "Tinos", "Times": "Tinos",
           "Courier New": "Cousine", "Courier": "Cousine"}
# Latin-1, Latin Extended-A, general punctuation, currency, a few symbols.
UNICODES = list(range(0x20, 0x7F)) + list(range(0xA0, 0x180)) + list(range(0x2010, 0x2060)) + \
    [0x20AC, 0x2122, 0x2190, 0x2192, 0x2605, 0x2665, 0x266A, 0xFB01, 0xFB02]


def fetch(path, cache={}):
    if path not in cache:
        print("  download", path)
        with urllib.request.urlopen(RAW + path) as r:
            cache[path] = r.read()
    return cache[path]


def build(folder, file, weight):
    font = TTFont(io.BytesIO(fetch(f"{folder}/{file}")))
    if weight is not None and "fvar" in font:
        font = instancer.instantiateVariableFont(font, {"wght": weight}, updateFontNames=False)
    opts = subset.Options()
    opts.layout_features = ["*"]
    opts.hinting = False                      # outlines only matter here
    opts.name_IDs = ["*"]
    opts.notdef_outline = True
    sub = subset.Subsetter(opts)
    sub.populate(unicodes=UNICODES)
    sub.subset(font)
    buf = io.BytesIO()
    font.save(buf)
    return buf.getvalue()


def main():
    os.makedirs(OUT, exist_ok=True)
    manifest = {"families": {}, "aliases": ALIASES}
    for family, (folder, styles) in FAMILIES.items():
        print(family)
        slug = family.replace(" ", "")
        files = {}
        for style, (file, weight) in styles.items():
            name = f"{slug}-{style.replace(' ', '')}.ttf"
            with open(os.path.join(OUT, name), "wb") as f:
                f.write(build(folder, file, weight))
            files[style] = name
        for lic in ("OFL.txt", "LICENSE.txt"):
            try:
                data = urllib.request.urlopen(LICENSES[family]).read() if family in LICENSES \
                    else fetch(f"{folder}/{lic}")
            except Exception:
                continue
            with open(os.path.join(OUT, f"{slug}-{lic}"), "wb") as f:
                f.write(data)
            break
        else:
            print(f"  WARNING: no licence file found for {family}", file=sys.stderr)
        manifest["families"][family] = files
    with open(os.path.join(OUT, "fonts.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=1)
    total = sum(os.path.getsize(os.path.join(OUT, n)) for n in os.listdir(OUT))
    print(f"done: {OUT} ({total / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
