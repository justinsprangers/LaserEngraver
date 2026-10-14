# CLAUDE.md: LaserEngraver

Desktop app (tkinter) for preparing photos for laser engraving. See README.md for usage.

- `--LASER ENGRAVING TOOLS--/` holds the user's original scripts. Read-only: don't edit, add, or delete inside without asking.
- `engrave_core.py` style "photo" is a port of `--LASER ENGRAVING TOOLS--/Imag-R Mimic Processing/image_processing.py`. With the same settings and `remove_bg` off, its output must stay **pixel-identical** to the original. If you change the pipeline on purpose, say so and say why. If you refactor, re-run a comparison against the original script (via `render()`) for all four dither modes.
- Intentional differences from the original: settings are passed in, invert/mirror are separate flags (acrylic turns both on), EXIF rotation is applied on load, and Stucki uses numba (falls back to plain Python if numba is missing).
- Style "comic" (`comic()`): bilateral flattening -> tone bands filled with patterns (`PATTERN_SETS`) + adaptive-threshold ink lines. With invert, bands flip but ink stays burned.
- `render()` is the single entry point for both styles; `save_outputs()` writes the PNG plus optional preview JPG and outline SVG. `test_card()` reuses `render()`.
- Background removal uses rembg (u2net model in `%USERPROFILE%\.u2net`), imported lazily.
- The user approved small fixes inside `--LASER ENGRAVING TOOLS--` once (README/log text). Still ask before any further edits there.
- Test with the window off-screen (geometry like `1400x900-6000+0`) or headless. Don't pop windows over the user's screen or screen-grab their desktop.
- `engrave_app.py` only handles UI. Keep processing logic in `engrave_core.py` so the CLI and app stay in sync.
- Material profiles live in `materials.json`. Keys starting with `_` are comments.
- `settings.json` and `output/` are git-ignored.
- Run with plain `python` (not `python -I`): packages are in user site-packages.
