# CLAUDE.md: LaserEngraver

Desktop app (tkinter) for preparing photos for laser engraving. See README.md for usage.

- `--LASER ENGRAVING TOOLS--/` holds the user's original scripts. Read-only: don't edit, add, or delete inside without asking.
- `engrave_core.py` style "photo" is a port of `--LASER ENGRAVING TOOLS--/Imag-R Mimic Processing/image_processing.py`. With the same settings and `remove_bg` off, its output must stay **pixel-identical** to the original. If you change the pipeline on purpose, say so and say why. If you refactor, re-run a comparison against the original script (via `render()`) for all four dither modes.
- Intentional differences from the original: settings are passed in, invert/mirror are separate flags (acrylic turns both on), EXIF rotation is applied on load, and Stucki uses numba (falls back to plain Python if numba is missing).
- Style "comic" (`comic()`): bilateral flattening -> tone bands filled with patterns (`PATTERN_SETS`) + adaptive-threshold ink lines. With invert, bands flip but ink stays burned.
- `render()` is the single entry point for both styles; `save_outputs()` writes the PNG plus optional preview JPG and outline SVG. `test_card()` reuses `render()`.
- Cut-out fixes: strokes `(kind, radius, [(x, y), ...])` in source-image pixels, stored per file in `fixes.json` (`DATA_DIR`, keyed by path + mtime). `core.refine_alpha()` applies strokes **sequentially** (`_apply_stroke`, one GrabCut per stroke on top of the previous result) and is **direction-locked**: a Keep stroke may only add (alpha only rises), a Remove only subtracts, and only in changed regions touching that stroke. This is a firm user requirement; keep it. The new stroke's label is pre-spread over nearby same-color pixels (`_similar_region`: fixed ΔE 12, short reach; a wider tolerance leaked in testing). GrabCut at ≤400 px with a **seeded RNG** (preview == saved file). Edges near the change are re-softened with a guided filter, using max/min so softening also respects direction. Prefix results are cached in memory (app `step_cache`) and final results on disk.
- Disk cache `cutout_cache/` under `DATA_DIR`: AI masks and fixed masks keyed by path + mtime + size + `CACHE_VERSION` (+ `stroke_key`). **Bump `CACHE_VERSION` whenever cut-out output changes**, or stale masks will be served. The app warms up the model and prefetches AI cut-outs for listed files in a background thread.
- The user wants to test changes from source (`Laser Engraver.bat`) before a new installer is built. Don't run build.bat unless asked. The app caches the AI cut-out and the refined alpha separately, so slider changes don't re-run either.
- Background removal uses rembg (u2net model in `%USERPROFILE%\.u2net`), imported lazily.
- The user approved small fixes inside `--LASER ENGRAVING TOOLS--` once (README/log text). Still ask before any further edits there.
- Test with the window off-screen (geometry like `1400x900-6000+0`) or headless. Don't pop windows over the user's screen or screen-grab their desktop.
- `engrave_app.py` only handles UI. Keep processing logic in `engrave_core.py` so the CLI and app stay in sync.
- Material profiles live in `materials.json`. Keys starting with `_` are comments. Materials = contrast/brightness/gamma/invert/mirror; style presets (`presets.json`, `core.PRESET_FIELDS`) = photo or comic look settings. Keep the two separate. A built-in "Default" preset per style is generated from `Settings()` defaults, never stored. If you add a new photo or comic setting, add it to `PRESET_FIELDS`.
- `settings.json` and `output/` are git-ignored.
- Packaged with PyInstaller (one-folder) + Inno Setup via `build.bat` → `packaging/build.py`. Outputs `dist/`, `build/`, `installer/` are git-ignored.
- When frozen, `core.FROZEN` is true: user data goes to `%APPDATA%\LaserEngraver`, output defaults to `Documents\Laser Engraver`, bundled read-only files are under `core.HERE` (`sys._MEIPASS`). From source, everything stays in this folder. Keep new file paths going through `core.DATA_DIR` / `core.HERE`.
- `LaserEngraver.exe --selftest <report.txt>` runs photo, comic, and rembg headlessly; build.py runs it and refuses to make an installer if it fails. If you add a library, check it still passes.
- Bump `VERSION` in `engrave_core.py` for each release; the installer reads it. Keep the `AppId` GUID in `installer.iss` unchanged so upgrades replace the old install.
- Run with plain `python` (not `python -I`): packages are in user site-packages.
