# Laser Engraver

A desktop app that turns photos into 1-bit engraving files using the Imag-R-style pipeline from
`--LASER ENGRAVING TOOLS--/Imag-R Mimic Processing/image_processing.py`. Its output matches that
script pixel for pixel. It just adds a window, a live preview, and faster dithering.

## Start it

Double-click **`Laser Engraver.bat`**. The first time, it installs any missing packages.

If nothing opens, run it from a terminal to see the error:

```
python engrave_app.py
```

## Using it

1. **Add images…** or **Add folder…**. Click a file in the list to preview it.
2. **Basic** tab: pick the material, width in inches, DPI, and dither mode. Choosing a material
   loads its contrast, brightness, gamma, invert, and mirror settings. You can then adjust them.
3. Look at the preview:
   - **Original**: the photo in grayscale.
   - **Adjusted**: the grayscale image just before dithering.
   - **Engraving**: the actual 1-bit file. Black = laser fires.
   - **Material look**: the engraving in rough material colors, flipped back to how it reads from
     the front if Mirror is on.

   Set zoom to **100%** (or use the mouse wheel) to see the real dot pattern; drag to pan. "Fit"
   averages the dots, so it shows overall tone.
4. **Save this engraving** writes the current image. **Process all** runs every image in the list
   with the current settings. Files go to the **Save to** folder (default: `output/`).

The line under the preview shows output size. It turns orange when the photo has fewer pixels than
the engraving needs, meaning it will be upscaled and lose some sharpness.

### Output files

`<name>_<material>_<width>in_<dpi>dpi_<dither>.png`: a 1-bit PNG with the DPI stored in the file,
so laser software that reads DPI imports it at the right physical size. Tick *Also save side-by-side
preview JPG* to get a grayscale-vs-dithered comparison too.

## Settings reference

| Setting | What it does |
|---|---|
| DPI | Match your laser's line interval: 254 = 0.10 mm, 318 = 0.08 mm, 423 = 0.06 mm. |
| Dither | **stucki**: most detail (acrylic, glass). **floyd-steinberg**: general photos. **bayer**: regular grid, good on wood grain. **none**: hard cutoff at 50%. |
| Contrast / Brightness | 1.0 = unchanged. |
| Gamma | > 1 lightens midtones (less burn), < 1 darkens. |
| Invert | Engrave the negative. Use it when the laser makes *light* marks (clear or black acrylic, slate, anodized metal). |
| Mirror | Flip left-right, for engraving the back of clear acrylic so it reads correctly from the front. |
| Shadow recovery | Lifts dark regions so their texture isn't lost in solid black. |
| CLAHE | Local contrast boost that brings out hair, fabric, and skin texture. |
| Sharpening | Unsharp mask applied before dithering. |

## Materials

Profiles live in `materials.json`. To add one, tune the sliders and click **Save as material…**,
or edit the file by hand (it explains each field at the top). **Reset to material** reloads the
saved values.

## Command line (optional)

```
python engrave_core.py photo.jpg "C:\path\to\folder" --material wood --width 4 --dpi 318 --dither floyd-steinberg
```

Run `python engrave_core.py -h` for all options.

## Files

| File | Purpose |
|---|---|
| `Laser Engraver.bat` | Launcher. |
| `engrave_app.py` | The window (tkinter). |
| `engrave_core.py` | All image processing. Change the pipeline here. |
| `materials.json` | Material profiles. |
| `settings.json` | Remembers your last settings and file list (created automatically, not in git). |
| `output/` | Default save folder (not in git). |
| `--LASER ENGRAVING TOOLS--/` | The original scripts, kept unchanged for reference. |
