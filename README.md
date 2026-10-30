# Laser Engraver

A desktop app that turns photos into 1-bit engraving files. Two styles:

- **Photo**: an Imag-R-style pipeline, ported from my original processing script. Output
  matches that script pixel for pixel (with background removal off), just faster and with a
  live preview.
- **Comic**: flat tones filled with halftone dots and crosshatching, plus bold ink outlines.
  Less realistic, more graphic, and much faster to burn.

There are two versions that share the same processing code (`engrave_core.py`):

- **Desktop app** (Windows): everything, including background-removal fixes and Design mode.
- **Web version** (`web/`): runs in any modern browser with nothing to install, for sharing
  (e.g. a makerspace). Photo and comic styles, AI background removal with the same fix tools
  (smart keep / remove, brush keep / erase), presets, materials and PNG / outline downloads.
  No Design mode yet. Photos are processed on the viewer's own computer, never uploaded.

## Web version

Open `web/index.html` through a web server (it can't run from a double-clicked file, because
browsers block the worker and model files there). On GitHub Pages it's served from this repo's
root; the root `index.html` forwards to `web/`.

To try it locally:

```bash
python -m http.server 8800
```

then open http://localhost:8800/web/.

How it works:

- `web/worker.js` loads [Pyodide](https://pyodide.org) (Python in WebAssembly) with NumPy,
  OpenCV and Pillow, then runs this folder's own `engrave_core.py` through `web/engine.py`.
  Any change to `engrave_core.py` or `materials.json` reaches the web version on the next push.
- Stucki dithering is a small JavaScript loop in `worker.js` (numba doesn't run in browsers),
  with the same math. Photo-style output is pixel-identical to the desktop app for PNG photos;
  JPEGs decode very slightly differently in the browser, so their dots can differ. Comic style
  can differ in about 0.01% of pixels on images over 1200 px, from OpenCV's browser build.
- Background removal runs the same u2net model with onnxruntime-web. The model is stored in
  `web/models/` in pieces (see its README), downloaded once and cached by the browser.
  Masks match the desktop's to within 1 level out of 255. About 7 s per photo.
- Fix strokes use the desktop's format and `core.refine_alpha`, so they behave the same. They're
  kept per photo until it's closed or the page is reloaded (photos aren't stored).
- **Tutorial** (`web/tour.js`, same style as ToolTrace's): starts by itself on a first visit;
  the **Tutorial** button (top right), Help, or the start screen replay it. It spotlights each
  control in the order you'd use it (the page stays usable underneath) and can load a sample
  photo (`web/samples/`), which it closes again at the end. To change the wording or order, edit
  the `STEPS` list in `tour.js`.
- After changing `style.css`, `app.js`, `tour.js` or `worker.js`, bump the `?v=` number in
  `web/index.html` (all three links), so browsers load the new files instead of a cached copy.
- Presets, your own materials, settings and theme are saved in the browser (localStorage). The
  built-in materials (`materials.json`) can't be renamed, deleted or overwritten in the web
  version, so a shared computer always has the originals; **Save as…** makes a personal copy.
- The interface (`web/index.html`, `app.js`, `style.css`) is separate from the desktop's, so
  new interface features have to be added to each version.
- **Design mode (in progress):** the browser already runs the desktop's design engine
  (`design.py`, `vectors.py`) through `web/engine.py`; the Design-mode screens come next.
  Fonts: browsers can't hand Python your installed fonts, so the web version uses the
  open-licence fonts in `web/fonts/` (made by `python web/tools/make_fonts.py`; add a family
  to its list and run it again). Arial, Times New Roman and Courier New in desktop projects use
  Arimo, Tinos and Cousine, which have the same letter widths.
  `python web/tools/compare_design.py design.json` renders a web design on the desktop to
  check both give the same files.

First visit downloads about 20 MB (Python + libraries, from the jsDelivr CDN); the AI model
(176 MB) downloads the first time someone removes a background.

## Install it (normal use)

Download **`LaserEngraver-Setup-<version>.exe`** from the
[Releases page](https://github.com/justinsprangers/LaserEngraver/releases/latest) and run it.
It installs for your user only (no admin prompt) and adds *Laser Engraver* to the Start menu.
Uninstall it from Windows Settings → Apps. Python isn't needed on the PC it's installed on.

The installer isn't code-signed, so Windows SmartScreen may say it "protected your PC" the
first time: click **More info → Run anyway**. Running a newer Setup updates the app in place.

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
5. To share it, push the code, then on GitHub: **Releases → Draft a new release**, tag
   `v<version>`, and attach the Setup file. (Installers aren't committed to the repo; they're
   too big and are rebuilt from the code.)

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

Both are remembered between sessions. Your settings and any unsaved design are autosaved every
minute, so a crash or power cut loses at most a minute of work; they come back on the next start. The app also sizes itself for the monitor it's on
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

## Design mode (layouts with photos, vector files, text and shapes)

Switch **Photos | Design** at the top left. Design mode lays out several items on one canvas and
exports files your laser software opens directly (engraving image + cut and score lines), so you
don't need Inkscape to set up a job.

1. **Design** tab: set the canvas size (your material), material, DPI, invert, mirror, and
   which files to export.
2. **Add** (Layers panel): Photo, Logo / clip art (PNG), Vector file (SVG, PDF, AI, DXF), Text,
   Arc text, Rectangle, Circle / ellipse, Line.
   Shapes are drawn like in Inkscape: pick one, then drag corner to corner on the canvas (a
   line: end to end). **Ctrl** = square / circle (lines in 15° steps), **Shift** = draw out
   from the center, **Alt** = no snapping, **Esc** = cancel. A plain click makes a 1-inch one.
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
   - Logo: dark threshold, or engrave the light parts instead. Transparent areas let lower
     layers show through.
   - Vector file: see [Vector files](#vector-files-svg-pdf-ai-dxf) below.
   - Text: text, font (type to filter), style, size in points, alignment, line spacing,
     outline-only.
   - Arc text: text along a circle, either over the top (reads clockwise) or along the bottom
     (upright). Put a top and a bottom arc at the same position with the same radius for a
     round badge.
   - Shapes: filled or outlined, line width, rounded corners.
5. **Engrave, score or cut:** every item has **Output: Engrave / Score / Cut** at the top of
   the Layer tab (vector files choose per color instead). Shapes start as Cut; everything else
   starts as Engrave. Score is a light line on the surface (blue); Cut goes through (red). Score
   and cut items are drawn as blue / red lines on the canvas and left out of the engraving:
   - shapes follow their outline (fill is ignored);
   - text and arc text follow the letter outlines;
   - logos and photos follow their traced outline (for photos, the background-removed shape).

   For score / cut items, **Score / cut lines** on the Layer tab offers:
   - **Weld overlapping shapes**: overlapping letters or shapes become one outline, so the laser
     doesn't cut through the middle (on by default for text);
   - **Outer outline only**: just the outside edge, e.g. a cut-out around a logo;
   - **Offset (mm)**: grows (+) or shrinks (−) the lines, for a margin around text / a logo or
     for kerf compensation (about half your laser's cut width). Offset and outer outline also
     weld; open lines (not closed shapes) aren't offset.
6. **Save engraving** writes:
   - `<name>.png`: the 1-bit engraving (if anything is engraved);
   - `<name>_cut.svg` / `.dxf` and `<name>_score.svg` / `.dxf`: one vector file per job, as
     chosen under **Cut / score files** on the Design tab. Millimetres, cut lines pure red
     (#FF0000), score lines pure blue (#0000FF), as hairlines. DXF is the old R12 format for the
     widest compatibility (layers CUT, SCORE, FRAME);
   - optional **all-in-one file** `<name>_combined.svg` / `.pdf`: the engraving image with every
     score and cut line on top, already lined up.

   **Lining up separate files:** the PNG is exactly the canvas size, and with **Add alignment
   frame** on, the cut and score files have a green rectangle the size of the canvas. Import
   them, align the PNG's corner with the green frame's corner, then delete or disable the frame.

   **Which files for which laser program** (from their documentation; not all tested here):

   | Program | Use | Then |
   |---|---|---|
   | LightBurn | All-in-one SVG or PDF | Each color comes in as its own layer: set the image layer to Image mode (dithering off / Threshold, line interval = your DPI), blue to score, red to cut. |
   | Trotec Ruby | All-in-one PDF | Map red to cut and blue to score in the material settings; the image engraves. |
   | xTool (XCS / xTool Studio) | All-in-one SVG or PDF, or PNG + cut / score SVGs | Each color becomes a layer; SVG lines arrive as Score: set red to Cut. |
   | LaserGRBL | PNG, then the cut / score SVGs | It runs one job per file: load the PNG (engrave), then use File → Append for each SVG with its own power and speed. Use the frame to line them up. |

   If a program doesn't bring in the image from the all-in-one file, use the PNG + cut / score
   files instead.

   **Cut outline around everything engraved** adds a cut line around the engraved artwork,
   pushed out by the offset. Mirror is applied to the engraving and the cut lines together, so
   text reads correctly from the front of back-engraved acrylic.

   **Test copy:** set **Test copy size (%)** on the Design tab and click **Save test copy** to
   save the whole design shrunk down (files end in `_test`), to try on scrap before a big job.
   DPI, material and photo settings stay the same, so the dots burn like the real thing.

   **In your laser software,** engrave the PNG at the same resolution it was saved at (254 DPI
   = 100 lines/cm = 0.1 mm line interval) and turn the software's own dithering off (use a
   plain black-and-white / threshold mode). If the software resamples or re-dithers the
   already-dithered image, you get blotchy, boxy patches.

### Vector files (SVG, PDF, AI, DXF)

**Add → Vector file…** opens SVG, PDF, Adobe Illustrator (.ai, saved PDF-compatible, the
default) and DXF files.

- **Real size:** the file comes in at the size it was drawn (SVG width/height in mm, in, pt or
  px at 96 per inch, like Inkscape; Illustrator SVGs at 72 per inch; PDF page size; DXF units).
  If the file's page is the same size as the canvas, the drawing lands where it sits on the
  page. **1:1 size** goes back to the file's size; **Canvas = file page** makes the canvas the
  file's page size, for ready-made cut sheets. A file that states no size (only a viewBox) is
  scaled to fit like a logo.
- **What the laser does with each color:** every line color and fill color in the file gets a
  job: **Engrave**, **Score**, **Cut**, **Blank** (not burned, and hides anything under it, like
  white parts of a logo) or **Ignore**. Defaults follow the usual laser convention: red lines
  cut, blue lines score, hairlines (thinner than 0.1 mm) cut, dark fills and thick lines engrave,
  light colors are blank; every DXF line cuts. A fill color set to Score or Cut follows its
  outline. Change any of them in the list.
- **Engraved line width (mm):** width for line colors set to Engrave (0 = as drawn).
- **Break apart** makes every shape its own layer (move, delete, or set each separately);
  **Split by color** makes one layer per color. Ctrl+Z undoes either.
- **Text** inside SVGs is converted with your installed fonts (bold / italic, alignment,
  multi-line). PDF text and SVG text in fonts you don't have are converted with a stand-in font
  (the Layer tab says so). DXF text uses ezdxf's fonts.
- **Not imported** (the Layer tab lists what applies to the file): pictures inside a vector
  file (add them as Photo layers), clipping masks (the hidden parts come in too), text along a
  path, old Inkscape flowed text, DXF points. Dashed lines come in solid. Only the first page
  of a multi-page PDF is used.

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
| `engrave_app.py` | The window (tkinter): layout, settings, files, preview pipeline, saving, autosave. |
| `app_panels.py` | Settings tabs: sliders, presets, materials, crop & rotate, test card. |
| `app_preview.py` | Preview canvas: drawing, zoom / pan, compare, mouse handling, brush outline. |
| `app_fixtools.py` | Background-removal fix toolbar and tools, AI model choice. |
| `app_keys.py` | Keyboard shortcut handling and UI size. |
| `app_common.py` | Constants shared by the app files. |
| `selftest.py` | Self-test the build runs on the finished exe. |
| `engrave_core.py` | All image processing. Change the pipeline here. |
| `design.py` | Design mode engine: layers, text/arc text/shapes/logos/vector-file rendering, compositing, export, project files. |
| `design_ui.py` | Design mode screens: layers panel, Design/Layer tabs, color jobs, drag handles, undo. |
| `vecfile.py` | Reads vector files (SVG, PDF, AI, DXF) at their real size, with each line / fill color as a group. |
| `vectors.py` | Score / cut lines (shapes, letter outlines, vector files, traced outlines), weld / offset, and the SVG / DXF / PDF writers. |
| `shortcuts.py` | Keyboard shortcut list, key matching, and the Shortcuts window. |
| `ui_kit.py` | Look, DPI scaling and shared widgets: the theme, tooltips, pinned-tab scrolling pages. |
| `materials.json` | Material profiles. |
| `settings.json` | Remembers your last settings and file list (created automatically, not in git). |
| `output/` | Default save folder (not in git). |
| `index.html` | Forwards to the web version (for GitHub Pages). |
| `web/` | The web version: page (`index.html`, `app.js`, `style.css`), `worker.js` (Pyodide + AI model), `engine.py` (glue to `engrave_core`), `models/` (u2net in pieces), `tools/split_model.py`. |

## License

MIT. See [LICENSE](LICENSE).
