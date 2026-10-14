# CLAUDE.md: LaserEngraver

Desktop app (tkinter) for preparing photos for laser engraving. See README.md for usage.

- `--LASER ENGRAVING TOOLS--/` holds the user's original scripts. Read-only: don't edit, add, or delete inside without asking.
- `engrave_core.py` is a port of `--LASER ENGRAVING TOOLS--/Imag-R Mimic Processing/image_processing.py`. With the same settings its output must stay **pixel-identical** to the original. If you change the pipeline on purpose, say so and say why. If you refactor, re-run a comparison against the original script for all four dither modes.
- Intentional differences from the original: settings are passed in, invert/mirror are separate flags (acrylic turns both on), EXIF rotation is applied on load, and Stucki uses numba (falls back to plain Python if numba is missing).
- `engrave_app.py` only handles UI. Keep processing logic in `engrave_core.py` so the CLI and app stay in sync.
- Material profiles live in `materials.json`. Keys starting with `_` are comments.
- `settings.json` and `output/` are git-ignored.
- Run with plain `python` (not `python -I`): packages are in user site-packages.
