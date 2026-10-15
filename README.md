# Laser Engraver

A desktop app that turns photos into 1-bit engraving files. Two styles:

- **Photo**: the Imag-R-style pipeline from
  `--LASER ENGRAVING TOOLS--/Imag-R Mimic Processing/image_processing.py`. Output matches that
  script pixel for pixel (with background removal off), just faster and with a live preview.
- **Comic**: flat tones filled with halftone dots and crosshatching, plus bold ink outlines.
  Less realistic, more graphic, and much faster to burn.

## Install it (normal use)

Run **`installer\LaserEngraver-Setup-<version>.exe`**. It installs for your user only (no admin
prompt) and adds *Laser Engraver* to the Start menu. Uninstall it from Windows Settings → Apps.
Python isn't needed on the PC it's installed on, so you can copy the Setup file to another PC.

The installed app keeps its data in these places, which updates and reinstalls don't touch:

| What | Where |
|---|---|
| Your materials (`materials.json`) and last-used settings | `%APPDATA%\LaserEngraver` |
| Saved engravings (default) | `Documents\Laser Engraver` |
| Background-removal AI model (downloaded on first use, 170 MB) | `%USERPROFILE%\.u2net` |

The first background removal after installing can take a minute or two (one-time). After that
it's about 2-3 s per photo.

## Change it and make a new installer

1. Edit the code (`engrave_core.py`, `engrave_app.py`) or `materials.json` in this folder.
2. Try the change by double-clicking **`Laser Engraver.bat`**. It runs straight from the source,
   no build needed. (Running from source keeps its own `settings.json` and `materials.json` in this
   folder, separate from the installed app's.)
3. When you're happy, raise `VERSION` near the top of `engrave_core.py` (e.g. 1.0.0 → 1.1.0) and
   double-click **`build.bat`**. After ~3 minutes it self-tests the bundled app and writes
   `installer\LaserEngraver-Setup-<version>.exe`.
4. Run the new Setup to update the installed app in place.

Building needs PyInstaller (`python -m pip install pyinstaller`) and Inno Setup 6
(`winget install JRSoftware.InnoSetup`). Both are installed on this PC.

If the source version won't open, run it from a terminal to see the error:

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

   **Zoom** with the mouse wheel (about 12% per notch, centered on the cursor) or the − / + buttons;
   the label shows the current %. **Fit** shows the whole image, **100%** shows one engraving dot
   per screen pixel (best for judging the dot pattern). Drag to pan (right-drag works in any tool);
   double right-click returns to Fit. Zoomed out, the preview averages the dots to show overall tone.
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

## Fixing the background removal

When **Remove background** is on, a *Fix cut-out* toolbar appears above the preview, along with
the **Cut-out** view: your photo in color, with removed areas tinted red.

1. Pick **Keep ✚** and paint over parts of the subject that were wrongly removed, or pick
   **Remove ✖** and paint over background that was wrongly kept. (Choosing a tool switches to
   the Cut-out view.)
2. Let go of the mouse. It re-evaluates in a second or two.
3. Repeat where needed. **Undo** removes the last stroke; **Clear fixes** removes them all.
   Right-drag pans in any tool; the mouse wheel zooms.

Rules you can rely on:
- **Keep only ever adds** to the subject, and **Remove only ever takes away**, and only in the
  region the stroke touches.
- Each stroke is applied on top of the previous result, so a new stroke never changes an earlier fix.
- Painted pixels always get exactly what you painted.

How it works: the AI (u2net) makes the first cut. Each stroke acts as labeled examples of
"subject" or "background", and its label also spreads a short way across nearby pixels of the same
color. GrabCut learns a color model for each side (Gaussian mixture models) and re-cuts with a
graph cut, so one stroke can fix the whole wrong region, not just the pixels you painted. Edges
around the change are re-softened to follow the photo's real edges. A region in a very different
color (e.g. a white rim next to a black visor) may need its own stroke.

Speed: the AI model loads in the background as soon as Remove background is on (~4 s), and every
image in the list is cut out in the background, so clicking one is near-instant. Results (AI
cut-outs and fixed cut-outs) are saved in `cutout_cache/` (next to `settings.json`), so reopening
an image, Process all and test cards reuse them. The cache keeps the newest 300 files and is safe
to delete.

Fixes are saved per image (`fixes.json`, next to `settings.json`) and used by Save, Process all and
the test card. If the image file changes, its fixes are dropped. Learning happens per image: fixes
on one photo don't teach the AI anything about the next.

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
| `Laser Engraver.bat` | Runs the app from source. |
| `build.bat` | Builds the installer. |
| `packaging/` | Build recipe: `build.py`, PyInstaller `LaserEngraver.spec`, Inno Setup `installer.iss`, `icon.ico`. |
| `dist/`, `build/`, `installer/` | Build outputs (not in git). |
| `engrave_app.py` | The window (tkinter). |
| `engrave_core.py` | All image processing. Change the pipeline here. |
| `materials.json` | Material profiles. |
| `settings.json` | Remembers your last settings and file list (created automatically, not in git). |
| `output/` | Default save folder (not in git). |
| `--LASER ENGRAVING TOOLS--/` | The original scripts, kept unchanged for reference. |
