# LaserEngraver: session notes

## Status
- **Desktop app** (v1.10.0): Photos mode (photo/comic styles, four dither modes, AI background removal with direction-locked fix strokes, crop/rotate, job estimate) and Design mode (layered layouts, cut vs engrave, SVG/DXF export, `.lep` projects). Windows installer via `build.bat`, distributed through GitHub Releases.
- **Web version** (`web/`, GitHub Pages): phase 1 (photo/comic in the browser via Pyodide) and phase 2 (background fix tools) are done. Photo output is pixel-identical to the desktop app.
- Web guided tour and sample photo added, and the stale-cache bug was fixed (b28b7d5). The tour was checked by layout measurements only, not real clicks: it needs a manual look.

## Next steps
- Manually check the web tutorial and start-screen buttons in a real browser (Ctrl+F5 once).
- Web version: Design mode isn't built yet.

## Decisions & gotchas
- See `CLAUDE.md` for the pixel-identical rule, cache-version bumps, and testing constraints.
- Public repo: keep personal photos, settings, and private scripts out.

## Log
- 2026-10-21: Web tutorial in ToolTrace's style; fixed stale cached files (b28b7d5)
- 2026-10-20: v1.10.0; segmented-button UI on desktop
- 2026-10-20: Web version phases 1 and 2; GitHub Pages hosting
- 2026-10-20: Split into its own public repo (MIT)
