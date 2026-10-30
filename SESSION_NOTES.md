# LaserEngraver: session notes

## Status
- **Desktop app** (v1.11.0, from source; installer not rebuilt yet): Photos mode (photo/comic styles, four dither modes, AI background removal with direction-locked fix strokes, crop/rotate, job estimate) and Design mode (layered layouts, `.lep` projects).
- **v1.11 Design mode = file prep without Inkscape:** vector files (SVG, PDF, AI, DXF) at real size, a job per color (engrave / score / cut / blank / ignore), Break apart / Split by color, SVG text converted with the installed fonts, Score output for every layer, weld / outer outline / offset, smooth curves, one export file per job plus an all-in-one SVG / PDF.
- **Web version** (`web/`, GitHub Pages): photo/comic + background fix tools; no Design mode.

## Next steps
- Try real files in the laser programs: does the all-in-one PDF/SVG bring in the image and the red/blue layers in LightBurn, Ruby, xTool, and does LaserGRBL's Append line up with the frame? Then update the README table.
- Rebuild the installer (`build.bat`) once tested from source. A scratch PyInstaller bundle passed the self-test with ezdxf + shapely included.
- Manually check the web tutorial and start-screen buttons in a real browser (Ctrl+F5 once).

## Decisions & gotchas
- See `CLAUDE.md` for the pixel-identical rule, cache-version bumps, vector-file pipeline and testing constraints.
- Frame color moved from blue to green: blue = score (Ruby / xTool / LightBurn convention).
- MuPDF alone mis-reads SVGs (no CSS classes, no display:none, px at 72 DPI, built-in fonts), hence `vecfile._prepare_svg`.
- Old projects keep their output: `_with_defaults` converts `cut` → `op`, old SVG layers keep page-sized boxes and threshold jobs. Verified against the v1.10 renderer: photos/text/logos/shapes pixel-identical, SVG strokes differ only at edges/corners.
- New dependencies: shapely, ezdxf (requirements.txt, PyInstaller spec collects ezdxf data).
- Public repo: keep personal photos, settings, and private scripts out.

## Log
- 2026-10-29: v1.11.0 vector-file import, score jobs, weld/offset, per-job and all-in-one exports
- 2026-10-21: Web tutorial in ToolTrace's style; fixed stale cached files (b28b7d5)
- 2026-10-20: v1.10.0; segmented-button UI on desktop
- 2026-10-20: Web version phases 1 and 2; GitHub Pages hosting
- 2026-10-20: Split into its own public repo (MIT)
