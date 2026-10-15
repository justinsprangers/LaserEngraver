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
2. **Basic** tab: pick the style, material, size (set the **width** or the **height**; the other
   follows the photo), DPI, and dither mode (photo only). **Crop & rotate** turns the photo in
   90° steps, **Straighten** fixes a tilt (±15°, the empty corners are trimmed automatically),
   and **Crop** trims a % from each edge.
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

**Compare (C)** splits the Adjusted / Engraving / Material look view: the original photo is on the
left of the divider. Drag the divider.

**Job time estimate** (bottom right): set your engrave and cut speeds in mm/s. Engraving time
assumes each line is scanned from its first to its last dark pixel, plus 2.5 mm overscan at each
end; cutting time is the total cut-line length at the cut speed. It ignores acceleration and
multiple passes, so real jobs usually take somewhat longer. In Design mode the engraving part
updates when you look at the Design view.

## Theme, screen size and scaling

**⚙ View** (top right) holds:
- **Theme:** Light, Dark, or System (follows your Windows app setting). Dark also darkens the
  window's title bar.
- **UI size:** 80-200%, also Ctrl+= / Ctrl+-. Makes everything bigger or smaller, for example
  on a high-resolution screen that Windows runs at 100%.

Both are remembered between sessions. The app also sizes itself for the monitor it's on
(Windows display scaling) and re-adjusts when you drag it to a monitor with different scaling.

The output folder (**Save to**, with **Change…** and **Open folder**) is at the bottom, above
the Save buttons.

## Keyboard shortcuts

Click **⌨ Shortcuts** (top right) to see every shortcut. To change one, select it, click
**Change…** (or double-click), and press the new key or combination. If that key is already
used, it asks before moving it. **Reset all** restores the defaults below. Changes are saved
in `settings.json`.

| Default keys | Action |
|---|---|
| K / R | Smart keep / smart remove (with Remove background on) |
| B / E | Brush keep / brush erase |
| P or Esc | Pan tool |
| [ / ] | Smaller / bigger brush (a circle under the cursor shows its size) |
| Space + drag | Pan, in any tool or view |
| C | Compare (before/after split) |
| Ctrl+0 / Ctrl+1 | Zoom to fit / 100% |
| Ctrl+Z / Ctrl+Y | Undo the last fix stroke (Photos) / undo, redo (Design) |
| Ctrl+S | Save engraving (Photos mode) / save project (Design mode) |
| Ctrl+D, Delete, arrow keys | Duplicate, delete, nudge the selected item (Design; Shift = bigger nudge) |

Shortcuts are ignored while you're typing in a field.

### Output files

- `<name>_<material>_<width>in_<dpi>dpi_<dither or comic>.png`: a 1-bit PNG with the DPI stored in
  the file, so laser software that reads DPI imports it at the right physical size.
- `..._outline.svg` (if ticked): sized in inches. Its canvas is larger than the PNG by the offset
  on every side, so center the two on each other in your laser software.
- `..._preview.jpg` (if *Also save side-by-side preview JPG* is ticked): adjusted vs. final.

## Design mode (layouts with text, logos and shapes)

Switch **Photos | Design** at the top left. Design mode lays out several items on one canvas and
exports a single engraving, so you don't need Inkscape for the layout step.

1. **Design** tab: set the canvas size (your material), material, DPI, invert, mirror, and
   whether to save a cut outline.
2. **Add** (Layers panel): Photo, Logo / clip art (PNG), SVG, Text, Arc text, Rectangle,
   Circle / ellipse, Line.
3. Arrange on the **Layout** view: it's fast because photos are shown as plain pictures. Switch
   to the **Design** view to see the real engraving; it only renders when you look at it.
   - Drag to move. Edges and centers snap to other items and to the canvas edges and center
     (pink guide lines); hold **Alt** to move freely.
   - Drag a corner square to resize. By default the opposite corner stays put; switch to
     **Resize from: Center** on the Design tab, or hold **Ctrl** while dragging. Edges snap
     while resizing too. Photos, logos, SVGs and text keep their proportions; shapes resize
     freely (Shift keeps proportions).
   - Drag the circle on top to rotate (Shift snaps to 15°). The cursor shows what a drag will do.
   - **Ctrl+click** picks the item underneath, for items stacked on top of each other.
   - Arrow keys nudge (Shift = bigger steps). The mouse wheel zooms, including out past the
     canvas edges.
   - ▲ / ▼ change the stacking order; items higher in the list sit on top.
4. **Layer** tab: exact position, size and rotation, plus settings for the selected item:
   - Photo: crop (% from each edge). Its style (photo / comic), tone, background removal and
     cut-out fixes are on the Basic / Photo tuning / Comic tuning tabs, separately for every photo.
   - Logo / SVG: dark threshold, or engrave the light parts instead. Transparent areas let
     lower layers show through.
   - Text: text, font (type to filter), style, size in points, alignment, line spacing,
     outline-only.
   - Arc text: text along a circle, either over the top (reads clockwise) or along the bottom
     (upright). Put a top and a bottom arc at the same position with the same radius for a
     round badge.
   - Shapes: filled or outlined, line width, rounded corners.
5. **Engrave or cut:** every item has **Output: Engrave / Cut** at the top of the Layer tab.
   Shapes start as Cut; everything else starts as Engrave. Cut items are drawn as red lines on
   the canvas and left out of the engraving:
   - shapes cut along their outline (fill is ignored);
   - text and arc text cut along the letter outlines;
   - SVGs cut along their own paths (SVG `<text>` isn't included; convert text to paths in
     Inkscape first);
   - logos and photos cut along their traced outline (for photos, the background-removed shape).
6. **Save engraving** writes:
   - `<name>.png`: the 1-bit engraving (if anything is engraved);
   - `<name>_cut.svg` and/or `<name>_cut.dxf`: the cut lines, as chosen under **Cut lines
     file** on the Design tab. They're in millimetres with cut lines in pure red (#FF0000),
     which Trotec Ruby, xTool, LaserGRBL and LightBurn can map to a cut layer. DXF is the
     old R12 format for the widest compatibility;
   - optional `<name>_combined.svg`: the engraving embedded under the cuts, already aligned
     (works in LightBurn; not tested in Ruby or xTool).

   **Lining them up:** the PNG is exactly the canvas size, and with **Add alignment frame** on,
   the cut file has a blue rectangle the size of the canvas. Import both, align the PNG's
   corner with the blue frame's corner, then delete or disable the frame.

   **Cut outline around everything engraved** adds a cut line around the engraved artwork,
   pushed out by the offset. Mirror is applied to the engraving and the cut lines together, so
   text reads correctly from the front of back-engraved acrylic.

**Projects:** **Save** / **Save as…** write a `.lep` project file. It's self-contained (it includes
copies of every image and all photo fixes), so it still opens if the originals move, or on
another PC. **Open…** loads one. Your current design is also remembered when you close the app,
even if you haven't saved it. Ctrl+Z / Ctrl+Y undo and redo, Ctrl+D duplicates, Delete removes,
Ctrl+S saves.

## Fixing the background removal

When **Remove background** is on, a toolbar appears above the preview, along with the
**Cut-out** view: your photo in color, with removed areas tinted red.

**AI model**: which AI makes the first cut. Each downloads once (to `%USERPROFILE%\.u2net`) the
first time you pick it. Your fixes stay and are re-applied on top of whichever model you choose.

| Model | Size | Speed (this PC) | Best for |
|---|---|---|---|
| General (u2net) | 170 MB | ~0.5 s | Anything; the original |
| People only (u2net human) | 170 MB | ~0.5 s | Photos of people; poor on pets and objects |
| Sharper edges (IS-Net) | 180 MB | ~1 s | General, finer edges |
| Best quality, lite (BiRefNet) | 220 MB | ~7-17 s | Tricky photos where the others fail |
| Best portraits (BiRefNet) | 970 MB | slowest (not measured) | Hair and portrait detail |

**Fix tools**:

| Tool | Color | What it does |
|---|---|---|
| **Smart keep** | green | Paint on part of the subject that was removed; re-cuts the whole connected region. |
| **Smart remove** | red | Paint on background that was kept; re-cuts the whole connected region. |
| **Brush keep** | blue | Adds exactly the painted pixels, nothing else. For touch-ups. |
| **Brush erase** | orange | Removes exactly the painted pixels, nothing else. For touch-ups. |

1. Pick a tool and paint over the mistake. (Choosing a tool switches to the Cut-out view.)
2. Let go of the mouse. Smart tools re-evaluate in about a second; brushes are instant.
3. Repeat where needed. Typical flow: smart tools for big mistakes, then brushes for the last
   details. **Undo** removes the last stroke; **Clear fixes** removes them all. **Size** sets the
   brush size. Right-drag pans in any tool; the mouse wheel zooms.

Rules you can rely on:
- **Keep tools only ever add** to the subject, and **Remove/erase tools only ever take away**;
  smart tools act only in the region the stroke touches, brushes only on the painted pixels.
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

## Materials and presets

Two kinds of saved settings, kept separate so any look works on any material:

| | What it stores | Where you manage it |
|---|---|---|
| **Material** | Contrast, brightness, gamma, invert, mirror (how the material takes the laser) | Basic tab |
| **Photo preset** | Dither, shadow recovery, CLAHE, sharpening | Top of the Photo tuning tab |
| **Comic preset** | Tone bands, flatten, dot spacing, line settings | Top of the Comic tuning tab |

- **Materials:** pick one from the Material list to load it. Tune the sliders, then use
  **Save as material…** (new name, or overwrite an existing one), **Rename material…** or
  **Delete material**. **Reset to material** reloads the saved values. Names are made
  lowercase with underscores because they go into output filenames.
- **Presets:** pick one from the dropdown to load it. "Preset • changed" means the sliders no
  longer match the selected preset; use **Save as…** to keep them. **Rename…** / **Delete** act on
  the selected preset, and **Revert to preset** reloads it. Each style has a built-in **Default**
  (the original settings) that can't be renamed or deleted.

Materials live in `materials.json` and presets in `presets.json` (installed app:
`%APPDATA%\LaserEngraver`). You can also edit them by hand; `materials.json` explains each field
at the top. Running from source, `materials.json` is the project's own copy, so deleting a
built-in material there removes it from the shipped defaults too (git can bring it back).

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
| `design.py` | Design mode engine: layers, text/arc text/shapes/logos/SVG rendering, compositing, project files. |
| `design_ui.py` | Design mode screens: layers panel, Design/Layer tabs, drag handles, undo. |
| `vectors.py` | Cut lines (shapes, letter outlines, SVG paths, traced outlines) and the SVG/DXF writers. |
| `shortcuts.py` | Keyboard shortcut list, key matching, and the Shortcuts window. |
| `ui_kit.py` | Look, DPI scaling and shared widgets: the theme, tooltips, pinned-tab scrolling pages. |
| `materials.json` | Material profiles. |
| `settings.json` | Remembers your last settings and file list (created automatically, not in git). |
| `output/` | Default save folder (not in git). |
| `--LASER ENGRAVING TOOLS--/` | The original scripts, kept unchanged for reference. |
