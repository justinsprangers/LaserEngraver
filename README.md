# Laser Engraver

A desktop app that turns photos into 1-bit engraving files. Two styles:

- **Photo**: the Imag-R-style pipeline from
  `--LASER ENGRAVING TOOLS--/Imag-R Mimic Processing/image_processing.py`. Output matches that
  script pixel for pixel (with background removal off), just faster and with a live preview.
- **Comic**: flat tones filled with halftone dots and crosshatching, plus bold ink outlines.
  Less realistic, more graphic, and much faster to burn.

## Start it

Double-click **`Laser Engraver.bat`**. The first time, it installs any missing packages.

If nothing opens, run it from a terminal to see the error:

```
python engrave_app.py
```

## Using it

1. **Add images…** or **Add folder…**. Click a file in the list to preview it.
2. **Basic** tab: pick the style, material, width in inches, DPI, and dither mode (photo only).
   Choosing a material loads its contrast, brightness, gamma, invert, and mirror settings. You
   can then adjust them. Optional:
   - **Remove background**: AI cut-out of the main subject; the background is left blank.
     Takes 1-3 s per photo (the first run also loads the model).
   - **Also save cut outline (SVG)**: a red cut line around the subject, pushed out by
     *Outline offset*. Best with Remove background on; without it, the "subject" is everything
     that isn't pure white, which for most photos is the whole rectangle.
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

- `<name>_<material>_<width>in_<dpi>dpi_<dither or comic>.png`: a 1-bit PNG with the DPI stored in
  the file, so laser software that reads DPI imports it at the right physical size.
- `..._outline.svg` (if ticked): sized in inches. Its canvas is larger than the PNG by the offset
  on every side, so center the two on each other in your laser software.
- `..._preview.jpg` (if *Also save side-by-side preview JPG* is ticked): adjusted vs. final.

## Test card

**Make test card** (Basic tab) saves a 3×3 grid: one inch of the selected image at true engraving
scale, with contrast varied across columns and gamma down rows, around your current values. Under
each tile there's a black-to-white ramp and its numbers. Burn it on a scrap of the material, pick
the best tile, type its numbers into Contrast and Gamma, then **Save as material…**.

## Comic style

Set Style to **Comic**, then adjust on the **Comic tuning** tab:

| Setting | What it does |
|---|---|
| Tone bands | 2-5. With 4: black, crosshatch, dots, blank. |
| Flatten | Smoothing passes that merge shading into flat regions. |
| Dot spacing (mm) | Size of the dot/hatch grid. Go bigger if dots burn together on your material. |
| Line scale | Bigger = bolder shapes, fewer fine lines. |
| Sensitivity | Higher = fewer, cleaner lines. |
| Thickness | Widens ink lines. |
| Despeckle | Removes small ink specks. |

Contrast, brightness and gamma move the tone-band boundaries. With **Invert** on, shading flips
but the ink lines stay engraved, which gives a scratchboard look on slate or black acrylic.

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
python engrave_core.py pet.jpg --style comic --remove-bg --outline --material wood --width 3
python engrave_core.py photo.jpg --test-card --material maple
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
