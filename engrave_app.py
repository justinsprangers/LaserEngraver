"""
engrave_app.py: desktop app for preparing photos for laser engraving.

Double-click "Laser Engraver.bat" (or run: python engrave_app.py).
All image processing lives in engrave_core.py; this file is only the window.
"""

import json
import math
import os
import queue
import subprocess
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor

import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

import numpy as np
from PIL import Image, ImageOps, ImageTk

import design_ui
import engrave_core as core
import shortcuts
import ui_kit
from ui_kit import ScrollTab, Tooltip, section

STATE_FILE = os.path.join(core.DATA_DIR, "settings.json")
DEFAULT_OUT = core.DEFAULT_OUT
ICON = os.path.join(core.HERE, "icon.ico")
DPI_CHOICES = ("254", "318", "423", "508")   # 0.10, 0.08, 0.06, 0.05 mm line interval
ZOOM_STEP = 1.12          # per mouse-wheel notch
VIEWS = ("Original", "Cut-out", "Adjusted", "Engraving", "Material look")
FIXES_FILE = os.path.join(core.DATA_DIR, "fixes.json")
STROKE_COLORS = {"keep": "#22c55e", "remove": "#ef4444",            # smart (re-cuts the region)
                 "paint_keep": "#38bdf8", "paint_remove": "#f59e0b"}  # plain brush (exact)
DEBOUNCE_MS = 300

TIPS = {
    "contrast": "1.0 = unchanged. Higher pushes tones apart.",
    "brightness": "1.0 = unchanged. Higher = less laser firing.",
    "gamma": ">1 lightens midtones, <1 darkens them.",
    "shadow_amount": "How much to lift dark areas so their texture survives.",
    "shadow_radius": "Blur size (px) used to find dark regions.",
    "clahe_clip": "Local contrast strength. Higher = more texture, more noise.",
    "clahe_grid": "Number of tiles across. Higher = more local.",
    "sharpen_radius": "Unsharp-mask radius in px.",
    "sharpen_percent": "Unsharp-mask strength.",
    "sharpen_threshold": "Only sharpen edges stronger than this.",
    "comic_levels": "Number of tone bands. 4 = black, crosshatch, dots, blank.",
    "comic_smooth": "Flattening passes. More = smoother, more cartoon-like regions.",
    "comic_dot_mm": "Spacing of halftone dots and hatch lines, in mm.\nGo bigger if fine dots burn together.",
    "comic_line_block": "Area (px) used to find ink lines. Bigger = bolder shapes, fewer fine lines.",
    "comic_line_c": "Line sensitivity. Higher = fewer, cleaner lines.",
    "comic_line_weight": "Thicken ink lines by this many pixels.",
    "comic_despeckle": "Remove ink specks smaller than this many pixels.",
    "outline_offset_in": "How far outside the subject the cut line runs.",
}
CROP_FIELDS = ("crop_l", "crop_t", "crop_r", "crop_b")   # fractions in Settings, % in the UI
INT_FIELDS = ("shadow_radius", "clahe_grid", "sharpen_percent", "sharpen_threshold", "comic_levels",
              "comic_smooth", "comic_line_block", "comic_line_weight", "comic_despeckle")


class App:
    def __init__(self, root):
        self.root = root
        self.materials = core.load_materials()
        self.files = []
        self.src_cache = {}         # path -> grayscale source (no background removal)
        self.ai_cache = {}          # (path, model) -> (RGB, AI alpha)
        self.refined = {}           # (path, model) -> (strokes key, grayscale on white, corrected alpha)
        self.step_cache = {}        # (path, model) -> {stroke-prefix key: alpha}: one new stroke = one step
        self.prefetcher = ThreadPoolExecutor(max_workers=1)   # AI cut-outs for the rest of the list
        self.prefetched = set()
        self.fixes = self._load_fixes()   # path -> list of feedback strokes
        self.tool = tk.StringVar(value="pan")
        self.brush = tk.IntVar(value=24)
        self.live = None            # stroke being drawn: (kind, [canvas points], [src points], item)
        self.disp = None            # (scale, x0, y0, view->source factor) of the drawn image
        self.view_factor = {}       # view name -> source pixels per view pixel (Cut-out is downsized)
        self.views = {}             # view name -> PIL image for the current file
        self.gen = 0                # bumps on every change; stale results are dropped
        self.pending = None
        self.busy_batch = False
        self.results = queue.Queue()
        self.worker = ThreadPoolExecutor(max_workers=1)
        self.photo = None
        self.suspend_traces = False

        state = self._load_state()
        s = core.Settings.from_dict(state.get("settings", {}))
        if s.material not in self.materials:
            s.material = next(iter(self.materials))
            s.apply_material(self.materials)
        self.out_dir = tk.StringVar(value=state.get("out_dir", DEFAULT_OUT))
        self.comparison = tk.BooleanVar(value=state.get("comparison", False))
        self.view = tk.StringVar(value="Engraving")
        self.preset_vars = {st: tk.StringVar(value=state.get(f"{st}_preset", core.DEFAULT_PRESET))
                            for st in core.PRESET_FIELDS}
        self.preset_combos = {}
        self.zf, self.cu, self.cv = 1.0, 0.5, 0.5   # zoom (x fit) and view center (fraction)
        self._redraw_pending = False
        self._pan_from = None
        self._shown_path = None
        self.last_src_size = {}     # path -> (w, h) of the photo after crop/rotate
        self.estimate = None        # (raster stats, dpi, cut length mm) for the job-time estimate
        self.compare = tk.BooleanVar(value=False)    # before/after split view
        self.split = 0.5            # divider position (fraction of the image width)
        self._split_drag = False
        self._space = False         # Space held = pan
        self._mouse = None          # last pointer position on the canvas (for the brush outline)
        self.set_keymap(shortcuts.load(state.get("shortcuts")))
        self.engrave_speed = tk.DoubleVar(value=state.get("engrave_speed", 150.0))
        self.cut_speed = tk.DoubleVar(value=state.get("cut_speed", 15.0))

        self.vars = {}
        for name, value in s.to_dict().items():
            if name in CROP_FIELDS:
                value = round(value * 100, 2)          # crop is shown in percent
            if isinstance(value, bool):
                v = tk.BooleanVar(value=value)
            elif isinstance(value, (int, float)) and name != "dpi":
                v = tk.DoubleVar(value=value)
            else:
                v = tk.StringVar(value=str(value))
            v.trace_add("write", lambda *_: self.on_change())
            self.vars[name] = v
        self.vars["material"].trace_add("write", lambda *_: self.on_material())

        root.title(f"Laser Engraver {core.VERSION}")
        if os.path.exists(ICON):
            root.iconbitmap(default=ICON)
        if state.get("geometry"):
            root.geometry(state["geometry"])
        if state.get("maximized", True):
            root.state("zoomed")
        root.minsize(980, 640)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.report_callback_exception = self.on_error

        self.mode = tk.StringVar(value="photos")
        self.dc = design_ui.DesignController(self, Tooltip)   # Design mode (layout editor)
        self.dc.restore_state(state.get("design_state", {}))
        # Sizes everything for the monitor the window is on x the UI size setting, and follows
        # the window to monitors with different scaling.
        self.scaler = ui_kit.init_scaling(root, state.get("ui_scale", 1.0))
        self.ui_size = tk.StringVar(value=f"{round(self.scaler.user_scale * 100)}%")
        self._build()
        self._sync_straighten()
        self.scaler.skip.add(str(self.info))      # its wrap width follows its own size
        self.scaler.refresh()
        self.scaler.listeners.append(self._after_rescale)
        root.update_idletasks()
        # Size the settings column to its contents (works at any Windows display scaling).
        root.after(300, self._fit_sash)
        for f in state.get("files", []):
            if os.path.isfile(f):
                self._add_file(f)
        if self.files:
            self.listbox.selection_set(0)
        if state.get("mode") == "design":
            self.mode.set("design")
            self.dc.enter()
        self.on_change()
        self.root.after(50, self._poll)

    # ------------------------------------------------------------------ layout

    def _build(self):
        ui_kit.apply_theme(self.root)
        top = ttk.Frame(self.root, padding=(12, 10, 12, 6))
        top.pack(fill="x")
        self.mode_frame = ttk.Frame(top)
        self.mode_frame.pack(side="left", padx=(0, 4))
        ttk.Label(self.mode_frame, text="Mode", style="Section.TLabel").pack(side="left", padx=(0, 8))
        for text, val, tip in (("Photos", "photos", "Process photos one by one or in batches."),
                               ("Design", "design", "Lay out photos, logos, SVGs, text and shapes\n"
                                                    "into one engraving, and save it as a project.")):
            rb = ttk.Radiobutton(self.mode_frame, text=text, value=val, variable=self.mode,
                                 command=self.set_mode)
            rb.pack(side="left", padx=(0, 8))
            Tooltip(rb, tip)
        ttk.Separator(self.mode_frame, orient="vertical").pack(side="left", fill="y", padx=(6, 12))
        self.file_toolbar = ttk.Frame(top)
        self.file_toolbar.pack(side="left")
        ttk.Button(self.file_toolbar, text="Add images…", command=self.add_images).pack(side="left")
        ttk.Button(self.file_toolbar, text="Add folder…", command=self.add_folder).pack(side="left", padx=6)
        ttk.Button(self.file_toolbar, text="Remove", command=self.remove_selected).pack(side="left")
        ttk.Button(top, text="Help", width=6, command=self.open_help).pack(side="right")
        kb = ttk.Button(top, text="⌨ Shortcuts", command=self.open_shortcuts)
        kb.pack(side="right", padx=(0, 6))
        Tooltip(kb, "See and change the keyboard shortcuts")
        us = ttk.Combobox(top, textvariable=self.ui_size, state="readonly", width=5,
                          values=[f"{round(v * 100)}%" for v in ui_kit.UI_SIZES])
        us.pack(side="right", padx=(0, 10))
        us.bind("<<ComboboxSelected>>", lambda e: self.set_ui_scale(int(self.ui_size.get()[:-1]) / 100))
        Tooltip(us, "UI size: makes everything bigger or smaller (also Ctrl+= / Ctrl+-).\n"
                    "The app also adjusts automatically when you move it to a monitor\n"
                    "with different Windows scaling.")
        ttk.Label(top, text="UI size", style="Hint.TLabel").pack(side="right", padx=(0, 4))
        ttk.Separator(top, orient="vertical").pack(side="right", fill="y", padx=10)
        ob = ttk.Button(top, text="Open", width=6, command=self.open_out)
        ob.pack(side="right", padx=(6, 0))
        Tooltip(ob, "Open the output folder")
        ttk.Button(top, text="Change…", width=9, command=self.choose_out).pack(side="right")
        ttk.Entry(top, textvariable=self.out_dir, width=16).pack(side="right", padx=6, fill="x", expand=True)
        ttk.Label(top, text="Save to", style="Section.TLabel").pack(side="right", padx=(12, 0))

        # Bottom bar is packed before the body so it can never be squeezed off-screen.
        bottom = ttk.Frame(self.root, padding=(12, 8))
        bottom.pack(side="bottom", fill="x")
        ttk.Separator(self.root).pack(side="bottom", fill="x")
        self.save_btn = ttk.Button(bottom, text="Save this engraving", style="Accent.TButton",
                                   command=self.save_current)
        self.save_btn.pack(side="left")
        self.batch_btn = ttk.Button(bottom, text="Process all", style="Big.TButton",
                                    command=self.process_all)
        self.batch_btn.pack(side="left", padx=8)
        self.comparison_cb = ttk.Checkbutton(bottom, text="Also save side-by-side preview JPG",
                                             variable=self.comparison)
        self.comparison_cb.pack(side="left", padx=8)
        self.progress = ttk.Progressbar(bottom, length=140, mode="determinate")
        self.progress.pack(side="right")
        self.status = ttk.Label(bottom, text="", style="Hint.TLabel")
        self.status.pack(side="right", padx=8)
        # Job-time estimate (rough: constant speed, no acceleration).
        est = ttk.Frame(bottom)
        est.pack(side="right", padx=(8, 8))
        ttk.Label(est, text="Speed mm/s: engrave").pack(side="left")
        ttk.Spinbox(est, textvariable=self.engrave_speed, from_=1, to=5000, increment=10, width=5).pack(side="left", padx=2)
        ttk.Label(est, text="cut").pack(side="left", padx=(4, 0))
        ttk.Spinbox(est, textvariable=self.cut_speed, from_=0.5, to=500, increment=1, width=4).pack(side="left", padx=2)
        self.estimate_lbl = ttk.Label(est, text="", width=26)
        self.estimate_lbl.pack(side="left", padx=(6, 0))
        Tooltip(self.estimate_lbl, "Rough job time. Engraving: each line is scanned from its first to its\n"
                                   "last dark pixel, plus 2.5 mm overscan at each end, at the engrave\n"
                                   "speed. Cutting: total cut-line length at the cut speed. Ignores\n"
                                   "acceleration and multiple passes, so real jobs usually take longer.")
        for v in (self.engrave_speed, self.cut_speed):
            v.trace_add("write", lambda *_: self.update_estimate())

        ttk.Separator(self.root).pack(fill="x")
        body = ttk.PanedWindow(self.root, orient="horizontal")
        body.pack(fill="both", expand=True, padx=12, pady=(10, 6))
        self.body = body

        left = ttk.Frame(body, width=340)
        body.add(left, weight=0)
        right = ttk.Frame(body, padding=(10, 0, 0, 0))
        body.add(right, weight=1)

        # --- file list
        lf = ttk.LabelFrame(left, text=" Images ", padding=(8, 4, 8, 8))
        lf.pack(fill="both", expand=False)
        self.images_frame = lf
        self.listbox = tk.Listbox(lf, height=5, exportselection=False)
        sb = ttk.Scrollbar(lf, command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=sb.set)
        self.listbox.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.listbox.bind("<<ListboxSelect>>", lambda e: self.on_change())
        self.listbox.bind("<Delete>", lambda e: self.remove_selected())

        # --- settings tabs: the tab headers stay pinned; each page scrolls on its own
        nb = ttk.Notebook(left)
        nb.pack(fill="both", expand=True, pady=(10, 0))
        self.left = left
        basic, fine, comic = ScrollTab(nb), ScrollTab(nb), ScrollTab(nb)
        nb.add(basic, text="Basic")
        nb.add(fine, text="Photo tuning")
        nb.add(comic, text="Comic tuning")
        self.nb, self.basic_tab, self.fine_tab, self.comic_tab = nb, basic, fine, comic
        self._build_basic(basic.inner)
        self._build_fine(fine.inner)
        self._build_comic(comic.inner)

        # --- preview
        bar = ttk.Frame(right)
        bar.pack(fill="x", pady=(0, 6))
        ttk.Label(bar, text="View", style="Section.TLabel").pack(side="left", padx=(0, 10))
        # Shown in Design mode only: Layout (fast, photos unprocessed) and Design (the engraving).
        self.layout_radio = ttk.Radiobutton(bar, text="Layout", value="Layout", variable=self.view,
                                            command=self.on_view_change)
        self.design_radio = ttk.Radiobutton(bar, text="Design", value="Design", variable=self.view,
                                            command=self.on_view_change)
        Tooltip(self.layout_radio, "Fast view for arranging: photos are shown as plain pictures,\n"
                                   "without the engraving processing.")
        Tooltip(self.design_radio, "The real engraving of the whole design (slower to update).")
        self.view_radios = []
        for v in VIEWS:
            rb = ttk.Radiobutton(bar, text=v, value=v, variable=self.view, command=self.on_view_change)
            rb.pack(side="left", padx=(0, 8))
            self.view_radios.append(rb)
        self.compare_cb = ttk.Checkbutton(bar, text="Compare (C)", variable=self.compare, command=self.redraw)
        self.compare_cb.pack(side="right")
        Tooltip(self.compare_cb, "Before/after split on the Adjusted, Engraving and Material look views:\n"
                                 "the original photo on the left of the divider. Drag the divider.")

        # Fix tools: shown only while Remove background is on. Two rows: model + tools, then brush.
        self.fixbar = ttk.Frame(right)
        row0 = ttk.Frame(self.fixbar)
        row0.pack(fill="x")
        ttk.Label(row0, text="AI model:").pack(side="left")
        self.model_label = tk.StringVar()
        mc = ttk.Combobox(row0, textvariable=self.model_label, state="readonly", width=28,
                          values=[core.BG_MODELS[m][0] for m in core.BG_MODELS])
        mc.pack(side="left", padx=(4, 0))
        mc.bind("<<ComboboxSelected>>", lambda e: self.on_model())
        Tooltip(mc, "Which AI makes the first cut. Try a few on a tricky photo and keep the best.\n"
                    "Each model downloads once (to %USERPROFILE%\\.u2net) the first time you pick it.\n"
                    "Your fix strokes stay and are re-applied on top of the new model's cut.")
        self.model_note = ttk.Label(row0, text="", style="Hint.TLabel")
        self.model_note.pack(side="left", padx=8)
        row1 = ttk.Frame(self.fixbar)
        row1.pack(fill="x", pady=(4, 0))
        row2 = ttk.Frame(self.fixbar)
        row2.pack(fill="x", pady=(4, 0))
        tool_tips = {
            "keep": "Smart keep: paint on part of the subject that was removed.\n"
                    "It re-cuts the whole connected region (e.g. a missing arm).",
            "remove": "Smart remove: paint on background that was kept.\n"
                      "It re-cuts the whole connected region (e.g. a lamp post).",
            "paint_keep": "Brush keep: adds exactly what you paint, nothing else.\n"
                          "For touch-ups after the smart tools.",
            "paint_remove": "Brush erase: removes exactly what you paint, nothing else.\n"
                            "For touch-ups after the smart tools.",
        }
        # Row 1: pan + smart tools + undo/clear. Row 2: plain brushes + size + fix count.
        for row, tools in ((row1, (("Pan", "pan"), ("Smart keep", "keep"), ("Smart remove", "remove"))),
                           (row2, (("Brush keep", "paint_keep"), ("Brush erase", "paint_remove")))):
            for label, val in tools:
                rb = ttk.Radiobutton(row, text=label, value=val, variable=self.tool, command=self.on_tool)
                rb.pack(side="left", padx=(0, 6))
                if val in tool_tips:
                    Tooltip(rb, tool_tips[val])
        ttk.Button(row1, text="Undo", width=6, command=self.undo_stroke).pack(side="left", padx=(8, 4))
        ttk.Button(row1, text="Clear fixes", width=10, command=self.clear_fixes).pack(side="left")
        ttk.Label(row2, text="Size").pack(side="left", padx=(8, 0))
        ttk.Scale(row2, from_=2, to=100, variable=self.brush, length=120).pack(side="left", padx=4)
        self.fix_hint = ttk.Label(row2, text="", style="Hint.TLabel")
        self.fix_hint.pack(side="left", padx=10)
        Tooltip(self.fix_hint, "Smart keep / remove (green / red): paint on a mistake and it re-cuts\n"
                               "the whole wrong region. Keep only adds, Remove only takes away.\n"
                               "Brush keep / erase (blue / orange): changes exactly the painted pixels,\n"
                               "for touch-ups. Strokes apply in order, so brush after smart tools.\n"
                               "Right-drag pans in any tool. Fixes are saved per image.\n\n"
                               "Shortcuts:\n" + self.SHORTCUTS_HELP)

        cf = ttk.Frame(right)
        cf.pack(fill="both", expand=True)
        self.canvas_frame = cf
        self.canvas = tk.Canvas(cf, background=ui_kit.CANVAS_BG, highlightthickness=0, cursor="fleur")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.canvas.bind("<Motion>", self.on_motion)
        self.canvas.bind("<Leave>", lambda e: (setattr(self, "_mouse", None), self.canvas.delete("brush")))
        self.brush.trace_add("write", lambda *_: self._draw_brush())
        self.canvas.bind("<ButtonPress-3>", self.pan_start)
        self.canvas.bind("<B3-Motion>", self.pan_move)
        self.canvas.bind("<Configure>", lambda e: self.request_redraw())
        self.canvas.bind("<MouseWheel>", self.on_wheel)
        self.canvas.bind("<Double-Button-3>", lambda e: self.zoom_fit())

        # Under the preview: zoom controls on the right, output info on the left.
        under = ttk.Frame(right)
        under.pack(fill="x", pady=(4, 0))
        zf = ttk.Frame(under)
        zf.pack(side="right", anchor="n")
        ttk.Button(zf, text="−", width=2, command=lambda: self.zoom_by(ZOOM_STEP ** -2)).pack(side="left")
        self.zoom_lbl = ttk.Label(zf, text="", width=6, anchor="center")
        self.zoom_lbl.pack(side="left")
        ttk.Button(zf, text="+", width=2, command=lambda: self.zoom_by(ZOOM_STEP ** 2)).pack(side="left")
        ttk.Button(zf, text="Fit", width=4, command=self.zoom_fit).pack(side="left", padx=4)
        ttk.Button(zf, text="100%", width=5, command=self.zoom_actual).pack(side="left")
        self.info = ttk.Label(under, text="", anchor="w", wraplength=600)
        self.info.pack(side="left", fill="x", expand=True)
        self.info.bind("<Configure>", lambda e: self.info.configure(wraplength=max(200, e.width)))

        self.dc.build(top, nb, nb, bottom_hide=[self.batch_btn, self.comparison_cb])
        self._bind_shortcuts()

    def _build_basic(self, f):
        f.columnconfigure(0, weight=1)
        # --- Style & material
        sm = section(f, "Style & material", row=0)
        ttk.Label(sm, text="Style").grid(row=0, column=0, sticky="w", pady=4)
        sf = ttk.Frame(sm)
        sf.grid(row=0, column=1, columnspan=2, sticky="w")
        ttk.Radiobutton(sf, text="Photo", value="photo", variable=self.vars["style"]).pack(side="left")
        ttk.Radiobutton(sf, text="Comic", value="comic", variable=self.vars["style"]).pack(side="left", padx=12)
        ttk.Label(sm, text="Material").grid(row=1, column=0, sticky="w", pady=4, padx=(0, 10))
        self.mat_combo = ttk.Combobox(sm, textvariable=self.vars["material"], state="readonly",
                                      values=sorted(self.materials), width=16)
        self.mat_combo.grid(row=1, column=1, sticky="ew")
        mb = ttk.Menubutton(sm, text="Manage ▾")
        mm = tk.Menu(mb, tearoff=False)
        mm.add_command(label="Reset to saved material settings", command=self.on_material)
        mm.add_command(label="Save as material…", command=self.save_as_material)
        mm.add_command(label="Rename material…", command=self.rename_material)
        mm.add_command(label="Delete material", command=self.delete_material)
        mb["menu"] = mm
        mb.grid(row=1, column=2, sticky="e", padx=(8, 0))
        Tooltip(mb, "Save the tone settings below as a material, or reset / rename / delete one.")

        # --- Size & resolution: set the width or the height; the other follows the photo.
        sz = section(f, "Size & resolution", row=1)
        self.size_spins = {}
        for i, (key, label) in enumerate((("width", "Width (in)"), ("height", "Height (in)"))):
            rb = ttk.Radiobutton(sz, text=label, value=key, variable=self.vars["size_by"])
            rb.grid(row=i, column=0, sticky="w", pady=3, padx=(0, 10))
            sp = ttk.Spinbox(sz, textvariable=self.vars[f"{key}_in"], from_=0.25, to=48, increment=0.25, width=8)
            sp.grid(row=i, column=1, sticky="w")
            self.size_spins[key] = sp
            if key == "width":
                self.height_lbl = ttk.Label(sz, text="", style="Hint.TLabel")
                self.height_lbl.grid(row=0, column=2, rowspan=2, sticky="w", padx=(8, 0))
                self.size_rows = [rb, sp]
            else:
                self.size_rows += [rb, sp]
        ttk.Label(sz, text="DPI").grid(row=2, column=0, sticky="w", pady=3)
        ttk.Combobox(sz, textvariable=self.vars["dpi"], values=DPI_CHOICES, width=8).grid(row=2, column=1, sticky="w")
        self.dpi_lbl = ttk.Label(sz, text="", style="Hint.TLabel")
        self.dpi_lbl.grid(row=2, column=2, sticky="w", padx=(8, 0))
        ttk.Label(sz, text="Dither").grid(row=3, column=0, sticky="w", pady=3)
        self.dither_combo = ttk.Combobox(sz, textvariable=self.vars["dither"], values=core.DITHER_MODES,
                                         state="readonly", width=16)
        self.dither_combo.grid(row=3, column=1, columnspan=2, sticky="w")
        self.dither_hint = ttk.Label(sz, text="stucki: most detail (acrylic, glass)  ·  floyd-steinberg: "
                                              "photos, portraits  ·  bayer: wood grain, geometric  ·  "
                                              "none: hard cutoff", style="Hint.TLabel", justify="left",
                                     wraplength=300)
        self.dither_hint.grid(row=4, column=0, columnspan=3, sticky="w", pady=(4, 0))

        # --- Background
        bg = section(f, "Background", row=2)
        cb = ttk.Checkbutton(bg, text="Remove background (AI) ⓘ", variable=self.vars["remove_bg"])
        cb.grid(row=0, column=0, columnspan=3, sticky="w")
        Tooltip(cb, "Cuts out the main subject and makes the background blank,\n"
                    "so the laser skips it. Also gives the cut outline a real silhouette.\n"
                    "About 1 s per photo once the AI model has loaded (~4 s, in the background).\n"
                    "Paint fixes with the Fix cut-out toolbar above the preview.")

        # --- Tone (saved per material)
        tn = section(f, "Tone", row=3)
        r = 0
        for name, lo, hi in (("contrast", 0.5, 2.5), ("brightness", 0.5, 2.0), ("gamma", 0.4, 3.0)):
            r = self._slider(tn, r, name, lo, hi, 0.05)
        ttk.Checkbutton(tn, text="Invert (laser makes light marks)",
                        variable=self.vars["invert"]).grid(row=r, column=0, columnspan=3, sticky="w", pady=(4, 0))
        ttk.Checkbutton(tn, text="Mirror (engrave from the back)",
                        variable=self.vars["mirror"]).grid(row=r + 1, column=0, columnspan=3, sticky="w", pady=(2, 0))

        # --- Crop & rotate (Photos mode only; design layers have their own crop and rotation)
        self.photo_only = section(f, "Crop & rotate", row=4)
        rot = ttk.Frame(self.photo_only)
        rot.grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Button(rot, text="⟲ 90°", width=6, command=lambda: self.turn(-90)).pack(side="left")
        ttk.Button(rot, text="⟳ 90°", width=6, command=lambda: self.turn(90)).pack(side="left", padx=4)
        self.rot_lbl = ttk.Label(rot, text="", style="Hint.TLabel")
        self.rot_lbl.pack(side="left", padx=6)
        self.straighten = tk.DoubleVar(value=0.0)
        r2 = self._slider(self.photo_only, 1, "straighten", -15, 15, 0.25, label="Straighten (°)", var=self.straighten)
        self.straighten.trace_add("write", lambda *_: self._apply_straighten())
        ttk.Label(self.photo_only, text="Crop (% from each edge)").grid(row=r2, column=0, columnspan=3, sticky="w")
        cr = ttk.Frame(self.photo_only)
        cr.grid(row=r2 + 1, column=0, columnspan=3, sticky="w")
        for i, (key, text) in enumerate((("crop_l", "L"), ("crop_t", "T"), ("crop_r", "R"), ("crop_b", "B"))):
            ttk.Label(cr, text=text).pack(side="left", padx=(0 if i == 0 else 6, 2))
            ttk.Spinbox(cr, textvariable=self.vars[key], from_=0, to=90, increment=1, width=4).pack(side="left")
        ttk.Button(self.photo_only, text="Reset crop & rotate", command=self.reset_crop).grid(
            row=r2 + 2, column=0, columnspan=3, sticky="w", pady=(8, 0))

        # --- Extras
        ex = section(f, "Extras", row=5)
        ttk.Checkbutton(ex, text="Also save cut outline (SVG)", variable=self.vars["outline"]).grid(
            row=0, column=0, columnspan=3, sticky="w")
        r = self._slider(ex, 1, "outline_offset_in", 0, 0.5, 0.01, label="Outline offset (in)")
        tc = ttk.Button(ex, text="Make test card ⓘ", command=self.make_test_card)
        tc.grid(row=r, column=0, columnspan=3, sticky="w", pady=(8, 0))
        Tooltip(tc, "Saves a 3x3 grid of this image at different contrast (columns)\n"
                    "and gamma (rows). Burn it on scrap, then type the numbers under\n"
                    "the best tile into Contrast and Gamma.")

    def _build_comic(self, f):
        f.columnconfigure(1, weight=1)
        self._preset_bar(f, "comic")
        r = 1
        ttk.Label(f, text="Used when Style = Comic.", style="Hint.TLabel").grid(
            row=r, column=0, columnspan=3, sticky="w", pady=(0, 4))
        r += 1
        ttk.Label(f, text="Tones", style="Section.TLabel").grid(row=r, column=0, columnspan=3, sticky="w", pady=(4, 2))
        r += 1
        r = self._slider(f, r, "comic_levels", 2, 5, 1, label="Tone bands")
        r = self._slider(f, r, "comic_smooth", 0, 10, 1, label="Flatten")
        r = self._slider(f, r, "comic_dot_mm", 0.3, 3, 0.05, label="Dot spacing (mm)")
        ttk.Label(f, text="Ink lines", style="Section.TLabel").grid(row=r, column=0, columnspan=3, sticky="w", pady=(12, 2))
        r += 1
        r = self._slider(f, r, "comic_line_block", 3, 61, 2, label="Line scale")
        r = self._slider(f, r, "comic_line_c", 0, 20, 0.5, label="Sensitivity")
        r = self._slider(f, r, "comic_line_weight", 0, 4, 1, label="Thickness")
        r = self._slider(f, r, "comic_despeckle", 0, 300, 5, label="Despeckle")
        ttk.Label(f, text="Contrast, brightness, gamma, invert and mirror\n"
                          "on the Basic tab apply to comic too. With Invert on,\n"
                          "shading flips but ink lines stay engraved\n"
                          "(a scratchboard look on slate or black acrylic).",
                  style="Hint.TLabel", justify="left").grid(row=r, column=0, columnspan=3, sticky="w", pady=(8, 0))
        r += 1
        ttk.Button(f, text="Revert to preset", command=lambda: self.apply_preset("comic")).grid(
            row=r, column=0, columnspan=3, sticky="w", pady=(10, 0))

    def _preset_bar(self, f, style):
        """Preset dropdown + Save as / Rename / Delete, at the top of a tuning tab."""
        box = ttk.LabelFrame(f, text="Preset", padding=6)
        box.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 8))
        combo = ttk.Combobox(box, textvariable=self.preset_vars[style], state="readonly", width=20)
        combo.pack(fill="x")
        Tooltip(box, "Pick a saved look to load its settings. \"• changed\" means the sliders\n"
                     "differ from the selected preset: use Save as… to keep them.\n"
                     "Material settings (contrast, gamma, ...) are saved separately.")
        combo.bind("<<ComboboxSelected>>", lambda e: self.apply_preset(style))
        btns = ttk.Frame(box)
        btns.pack(fill="x", pady=(4, 0))
        ttk.Button(btns, text="Save as…", command=lambda: self.save_preset(style)).pack(side="left")
        ttk.Button(btns, text="Rename…", command=lambda: self.rename_preset(style)).pack(side="left", padx=4)
        ttk.Button(btns, text="Delete", command=lambda: self.delete_preset(style)).pack(side="left")
        self.preset_combos[style] = combo
        self.refresh_presets(style)

    def _build_fine(self, f):
        f.columnconfigure(1, weight=1)
        self._preset_bar(f, "photo")
        r = 1
        ttk.Label(f, text="Dither (also on Basic tab)").grid(row=r, column=0, sticky="w", pady=3)
        ttk.Combobox(f, textvariable=self.vars["dither"], values=core.DITHER_MODES,
                     state="readonly", width=16).grid(row=r, column=1, columnspan=2, sticky="w")
        r += 1
        ttk.Label(f, text="Shadow detail recovery", style="Section.TLabel").grid(row=r, column=0, columnspan=3, sticky="w", pady=(12, 2))
        r += 1
        r = self._slider(f, r, "shadow_amount", 0, 0.6, 0.01)
        r = self._slider(f, r, "shadow_radius", 1, 60, 1)
        ttk.Label(f, text="Micro-texture (CLAHE)", style="Section.TLabel").grid(row=r, column=0, columnspan=3, sticky="w", pady=(12, 2))
        r += 1
        ttk.Checkbutton(f, text="Enabled", variable=self.vars["use_clahe"]).grid(row=r, column=0, columnspan=3, sticky="w")
        r += 1
        r = self._slider(f, r, "clahe_clip", 0.5, 5, 0.1)
        r = self._slider(f, r, "clahe_grid", 2, 32, 1)
        ttk.Label(f, text="Sharpening", style="Section.TLabel").grid(row=r, column=0, columnspan=3, sticky="w", pady=(12, 2))
        r += 1
        r = self._slider(f, r, "sharpen_radius", 0, 8, 0.5)
        r = self._slider(f, r, "sharpen_percent", 0, 400, 5)
        r = self._slider(f, r, "sharpen_threshold", 0, 20, 1)
        ttk.Button(f, text="Revert to preset", command=lambda: self.apply_preset("photo")).grid(
            row=r, column=0, columnspan=3, sticky="w", pady=(10, 0))

    def _slider(self, f, r, name, lo, hi, step, label=None, var=None):
        var = var if var is not None else self.vars[name]
        if label is None:
            label = name.replace("_", " ").replace("clahe ", "").replace("sharpen ", "").replace("shadow ", "")
            label = label.capitalize()
        lbl = ttk.Label(f, text=label + (" ⓘ" if name in TIPS else ""))
        lbl.grid(row=r, column=0, sticky="w", pady=3)
        shown = tk.StringVar()

        def snap(val):
            v = round(lo + round((float(val) - lo) / step) * step, 4)
            if var.get() != v:
                var.set(v)

        def refresh(*_):
            try:
                shown.set(f"{var.get():g}")
            except tk.TclError:
                pass

        scale = ttk.Scale(f, from_=lo, to=hi, variable=var, command=snap)
        scale.grid(row=r, column=1, sticky="ew", padx=4)
        e = ttk.Entry(f, textvariable=shown, width=6)
        e.grid(row=r, column=2, sticky="e")
        e.bind("<Return>", lambda ev: self._entry_to_var(shown, var))
        e.bind("<FocusOut>", lambda ev: self._entry_to_var(shown, var))
        var.trace_add("write", refresh)
        refresh()
        if name in TIPS:
            Tooltip(lbl, TIPS[name])
            Tooltip(scale, TIPS[name])
        return r + 1

    @staticmethod
    def _entry_to_var(shown, var):
        try:
            var.set(float(shown.get()))
        except ValueError:
            shown.set(f"{var.get():g}")

    # ------------------------------------------------------------ settings

    def settings(self):
        """Current UI values as a Settings object, or None if a field is mid-edit/invalid."""
        try:
            d = {k: v.get() for k, v in self.vars.items()}
            d["dpi"] = int(float(d["dpi"]))
            d["width_in"] = round(float(d["width_in"]), 3)
            d["height_in"] = round(float(d["height_in"]), 3)
            for k in CROP_FIELDS:
                d[k] = min(0.9, max(0.0, float(d[k]) / 100))
            for k in INT_FIELDS:
                d[k] = int(round(d[k]))
            d["clahe_grid"] = max(1, d["clahe_grid"])
            d["comic_levels"] = min(5, max(2, d["comic_levels"]))
            if d["bg_model"] not in core.BG_MODELS:
                d["bg_model"] = core.DEFAULT_BG_MODEL
            s = core.Settings.from_dict(d)
        except (tk.TclError, ValueError):
            return None
        if s.width_in <= 0 or s.height_in <= 0 or not 50 <= s.dpi <= 1200 or s.material not in self.materials:
            return None
        return s

    # --- crop & rotate (Photos mode)

    def turn(self, delta):
        rot = float(self.vars["rotate"].get())
        self.vars["rotate"].set(round((rot + delta) % 360, 3))

    def _apply_straighten(self):
        if self.suspend_traces:
            return
        rot = float(self.vars["rotate"].get())
        base = 90 * round(rot / 90)                  # the quarter turn, without the fine angle
        self.vars["rotate"].set(round((base + self.straighten.get()) % 360, 3))

    def _sync_straighten(self):
        """Shows the fine part of the rotation on the Straighten slider."""
        rot = float(self.vars["rotate"].get())
        fine = round(rot - 90 * round(rot / 90), 3)
        if abs(self.straighten.get() - fine) > 1e-6:
            was, self.suspend_traces = self.suspend_traces, True
            self.straighten.set(fine)
            self.suspend_traces = was

    def reset_crop(self):
        self.suspend_traces = True
        for k in CROP_FIELDS + ("rotate",):
            self.vars[k].set(0.0)
        self.straighten.set(0.0)
        self.suspend_traces = False
        self.on_change()

    def on_material(self):
        s = self.settings()
        if s is None:
            return
        s.apply_material(self.materials)
        self.suspend_traces = True
        for k in ("contrast", "brightness", "gamma", "invert", "mirror"):
            self.vars[k].set(getattr(s, k))
        self.suspend_traces = False
        self.on_change()

    # ------------------------------------------------------------- presets

    def refresh_presets(self, style, select=None):
        names = list(core.load_presets(style))
        self.preset_combos[style].configure(values=names)
        if select:
            self.preset_vars[style].set(select)
        elif self.preset_vars[style].get() not in names:
            self.preset_vars[style].set(core.DEFAULT_PRESET)
        self.update_preset_state()

    def apply_preset(self, style):
        values = core.load_presets(style).get(self.preset_vars[style].get())
        if not values:
            return
        self.suspend_traces = True
        for k, v in values.items():
            if k in self.vars:
                self.vars[k].set(v)
        self.suspend_traces = False
        self.on_change()

    def update_preset_state(self):
        """Marks a preset as changed when the sliders no longer match it."""
        s = self.settings()
        for style, combo in self.preset_combos.items():
            values = core.load_presets(style).get(self.preset_vars[style].get(), {})
            changed = s is not None and any(
                abs(float(getattr(s, k)) - float(v)) > 1e-6 if isinstance(v, (int, float)) else getattr(s, k) != v
                for k, v in values.items())
            combo.master.configure(text="Preset • changed" if changed else "Preset")

    def _ask_name(self, title, prompt, initial=""):
        name = simpledialog.askstring(title, prompt, initialvalue=initial, parent=self.root)
        return name.strip() if name and name.strip() else None

    def save_preset(self, style):
        s = self.settings()
        if s is None:
            return
        current = self.preset_vars[style].get()
        name = self._ask_name(f"Save {style} preset",
                              f"Name for these {style} settings (e.g. Portrait, Bold lines):",
                              "" if current == core.DEFAULT_PRESET else current)
        if not name:
            return
        if name == core.DEFAULT_PRESET:
            messagebox.showinfo("Name taken", f"'{core.DEFAULT_PRESET}' is built in. Pick another name.")
            return
        if name in core.load_presets(style) and not messagebox.askyesno(
                "Overwrite?", f"Preset '{name}' already exists. Replace it with the current settings?"):
            return
        core.save_preset(style, name, s)
        self.refresh_presets(style, select=name)
        self.set_status(f"Saved {style} preset '{name}'")

    def rename_preset(self, style):
        old = self.preset_vars[style].get()
        if old == core.DEFAULT_PRESET:
            messagebox.showinfo("Built-in preset", "The Default preset can't be renamed.")
            return
        new = self._ask_name("Rename preset", f"New name for '{old}':", old)
        if not new or new == old:
            return
        if new == core.DEFAULT_PRESET or new in core.load_presets(style):
            messagebox.showinfo("Name taken", f"There's already a preset called '{new}'.")
            return
        core.rename_preset(style, old, new)
        self.refresh_presets(style, select=new)

    def delete_preset(self, style):
        name = self.preset_vars[style].get()
        if name == core.DEFAULT_PRESET:
            messagebox.showinfo("Built-in preset", "The Default preset can't be deleted.")
            return
        if messagebox.askyesno("Delete preset", f"Delete the {style} preset '{name}'?\n"
                                                "Your current slider values stay as they are."):
            core.delete_preset(style, name)
            self.refresh_presets(style, select=core.DEFAULT_PRESET)
            self.set_status(f"Deleted {style} preset '{name}'")

    def rename_material(self):
        old = self.vars["material"].get()
        text = self._ask_name("Rename material", f"New name for '{old}':", old)
        new = core.material_name(text or "")
        if not new or new == old:
            return
        if new in self.materials:
            messagebox.showinfo("Name taken", f"There's already a material called '{new}'.")
            return
        core.rename_material(old, new)
        self.materials = core.load_materials()
        self.mat_combo.configure(values=sorted(self.materials))
        self.suspend_traces = True          # same settings, new name: don't reload the profile
        self.vars["material"].set(new)
        self.suspend_traces = False
        self.on_change()
        self.set_status(f"Renamed material '{old}' to '{new}'")

    def delete_material(self):
        name = self.vars["material"].get()
        if len(self.materials) <= 1:
            messagebox.showinfo("Last material", "You need at least one material.")
            return
        if not messagebox.askyesno("Delete material", f"Delete the material '{name}'?"):
            return
        core.delete_material(name)
        self.materials = core.load_materials()
        self.mat_combo.configure(values=sorted(self.materials))
        self.vars["material"].set(sorted(self.materials)[0])     # loads that material's profile
        self.set_status(f"Deleted material '{name}'")

    def make_test_card(self):
        path, s = self.current_file(), self.settings()
        if path is None or s is None:
            messagebox.showinfo("No image", "Select an image first. The card uses its center.")
            return
        self.set_status("Making test card…")
        self.root.update_idletasks()
        try:
            out = core.save_test_card(path, self.out_dir.get(), s, self.strokes_for(path) or None)
        except Exception as e:
            messagebox.showerror("Test card failed", str(e))
            return
        self.set_status(f"Saved {os.path.basename(out)}")
        os.startfile(out)

    def save_as_material(self):
        s = self.settings()
        if s is None:
            return
        name = core.material_name(self._ask_name(
            "Save as material", "Name for this material profile (e.g. maple, black acrylic):",
            s.material) or "")
        if not name:
            return
        if name in self.materials and not messagebox.askyesno(
                "Overwrite?", f"'{name}' already exists. Overwrite its settings?"):
            return
        look = self.materials.get(s.material, {}).get("look", ["#d9b38c", "#5a3a22"])
        profile = {"contrast": s.contrast, "brightness": s.brightness, "gamma": s.gamma,
                   "invert": s.invert, "mirror": s.mirror, "look": look}
        core.save_material(name, profile)
        self.materials = core.load_materials()
        self.mat_combo.configure(values=sorted(self.materials))
        self.vars["material"].set(name)
        self.set_status(f"Saved material '{name}' to materials.json")

    # ----------------------------------------------------------------- files

    def _add_file(self, path):
        path = os.path.abspath(path)
        if path not in self.files:
            self.files.append(path)
            self.listbox.insert("end", os.path.basename(path))

    def add_images(self):
        paths = filedialog.askopenfilenames(
            title="Choose images",
            filetypes=[("Images", " ".join("*" + e for e in core.IMAGE_EXTENSIONS)), ("All files", "*.*")])
        self._add_many(paths)

    def add_folder(self):
        folder = filedialog.askdirectory(title="Choose a folder of images")
        if folder:
            self._add_many(core.list_images(folder))

    def _add_many(self, paths):
        had = len(self.files)
        for p in paths:
            self._add_file(p)
        if paths and not self.listbox.curselection():
            self.listbox.selection_set(had if had < len(self.files) else 0)
        self.on_change()

    def remove_selected(self):
        sel = self.listbox.curselection()
        if not sel:
            return
        i = sel[0]
        self.listbox.delete(i)
        self.files.pop(i)
        if self.files:
            self.listbox.selection_set(min(i, len(self.files) - 1))
        self.on_change()

    def current_file(self):
        if self.dc.active:
            return self.dc.current_src()           # the selected photo layer, if any
        sel = self.listbox.curselection()
        return self.files[sel[0]] if sel else None

    def set_mode(self):
        if self.mode.get() == "design" and not self.dc.active:
            self.dc.enter()
        elif self.mode.get() == "photos" and self.dc.active:
            self.dc.leave()

    def load_settings_into_vars(self, s):
        """Puts a Settings object into the controls without triggering a preview per field."""
        self.suspend_traces = True
        try:
            for name, value in s.to_dict().items():
                if name in self.vars:
                    if name in CROP_FIELDS:
                        value = round(value * 100, 2)
                    self.vars[name].set(str(value) if name == "dpi" else value)
        finally:
            self.suspend_traces = False
        self._sync_straighten()

    # Fix strokes live in fixes.json per photo in Photos mode, and inside each photo layer in
    # Design mode (so the same photo can be used twice with different fixes).
    def strokes_for(self, path):
        if self.dc.active:
            return list(self.dc.strokes())
        return list(self.fixes.get(path, []))

    def _strokes_mut(self, path):
        if self.dc.active:
            return self.dc.strokes()
        return self.fixes.setdefault(path, [])

    def choose_out(self):
        d = filedialog.askdirectory(title="Save engravings to", initialdir=self.out_dir.get())
        if d:
            self.out_dir.set(d)

    def open_out(self):
        os.makedirs(self.out_dir.get(), exist_ok=True)
        os.startfile(self.out_dir.get())

    def open_help(self):
        readme = os.path.join(core.HERE, "README.md")
        try:
            os.startfile(readme)
        except OSError:                      # no app registered for .md files
            subprocess.Popen(["notepad.exe", readme])

    # -------------------------------------------------------------- preview

    def on_change(self):
        if self.suspend_traces:
            return
        if self.dc.active:
            self.dc.sync_from_vars()               # controls -> design / selected photo layer
        self.gen += 1
        if self.pending:
            self.root.after_cancel(self.pending)
        self.pending = self.root.after(DEBOUNCE_MS, self.start_preview)
        self.update_labels()

    def update_view_buttons(self):
        """In Design mode the photo views (Original ... Material look) only make sense with a
        photo layer selected; grey them out otherwise."""
        ok = not self.dc.active or self.current_file() is not None
        for rb in self.view_radios:
            rb.state(["!disabled"] if ok else ["disabled"])
        self.compare_cb.state(["!disabled"] if ok else ["disabled"])
        if not ok and self.view.get() in VIEWS:
            self.view.set("Layout")
            self.on_view_change()

    def update_labels(self):
        self.update_view_buttons()
        s = self.settings()
        n = len(self.files)
        self.batch_btn.configure(text=f"Process all ({n})" if n else "Process all")
        if s is None:
            return
        self.dpi_lbl.configure(text=f"{25.4 / s.dpi:.3f} mm line interval")
        photo = s.style == "photo"
        self.dither_combo.state(["!disabled"] if photo else ["disabled"])
        self.dither_hint.configure(foreground="#666" if photo else "#bbb")
        self.update_fixbar()
        self.update_preset_state()
        by_h = s.size_by == "height"
        self.size_spins["width"].state(["disabled"] if by_h else ["!disabled"])
        self.size_spins["height"].state(["!disabled"] if by_h else ["disabled"])
        self.rot_lbl.configure(text=f"rotated {s.rotate:g}°" if s.rotate else "")
        path = self.current_file()
        size = self.last_src_size.get(path)       # after crop/rotate, from the last preview
        if size is None:
            self.height_lbl.configure(text="")
            return
        w_in, h_in = s.size_in(*size)
        self.height_lbl.configure(text=f"→ {w_in:.2f} in wide" if by_h else f"→ {h_in:.2f} in tall")

    def start_preview(self):
        self.pending = None
        path, s = self.current_file(), self.settings()
        if path is None:
            if self.dc.active:                     # no photo layer selected: keep the design views
                self.views = {k: v for k, v in self.views.items() if k in ("Layout", "Design")}
                self.redraw()
                return
            self.views = {}
            self.redraw()
            self.info.configure(text="Add images to get started (Add images… or Add folder…).",
                                style="TLabel")
            return
        if s is None:
            return
        gen = self.gen
        strokes = self.strokes_for(path)
        key = (path, s.bg_model)
        if not s.remove_bg:
            msg = "Updating preview…"
        elif not core.model_downloaded(s.bg_model):
            msg = "Downloading the AI model (one-time, can take a few minutes)…"
        elif key not in self.ai_cache:
            msg = "Removing background…"
        elif self.refined.get(key, (None,))[0] != core.stroke_key(strokes):
            msg = "Re-evaluating cut-out from your fixes…"
        else:
            msg = "Updating preview…"
        self.set_status(msg)
        self.worker.submit(self._preview_job, gen, path, s, strokes)

    @staticmethod
    def _remember(cache, key, value, limit=6):
        if len(cache) >= limit and key not in cache:     # big photos are ~12 MB each in memory
            cache.pop(next(iter(cache)))
        cache[key] = value

    def _preview_job(self, gen, path, s, strokes):
        try:
            cutout = None
            if s.remove_bg:
                ck = (path, s.bg_model)                      # caches are per image AND model
                if ck not in self.ai_cache:
                    self._remember(self.ai_cache, ck, core.ai_cutout(path, s.bg_model), limit=4)
                rgb, a0 = self.ai_cache[ck]
                key = core.stroke_key(strokes)
                ref = self.refined.get(ck)
                if ref is None or ref[0] != key:
                    steps = self.step_cache.setdefault(ck, {})
                    if len(self.step_cache) > 4:            # only keep step results for a few images
                        self.step_cache.pop(next(k for k in self.step_cache if k != ck))
                    alpha = core.refined_cutout(path, rgb, a0, strokes, steps, s.bg_model)
                    ref = (key, core.compose_on_white(rgb, alpha), alpha)
                    self._remember(self.refined, ck, ref, limit=4)
                _, src, alpha = ref
                cutout = self._cutout_view(rgb, alpha)
            else:
                src = self.src_cache.get(path)
                if src is None:
                    src = core.load_image(path)
                    self._remember(self.src_cache, path, src)
                alpha = None
            if gen != self.gen:
                return
            if not self.dc.active:                 # Photos mode crop / rotate
                src, alpha = core.prepare(src, alpha, s)
            gray, result, mask = core.render(src, alpha, s)
            stats = core.raster_stats(np.array(result.convert("L")) < 128)
            self.results.put(("preview", gen, path, s, src, gray, result, mask, cutout, stats))
        except Exception as e:
            self.results.put(("error", gen, f"Could not process {os.path.basename(path)}:\n{e}"))

    def _poll(self):
        try:
            while True:
                msg = self.results.get_nowait()
                kind = msg[0]
                if kind == "preview":
                    _, gen, path, s, src, gray, result, mask, cutout, stats = msg
                    if gen == self.gen:
                        self.last_mask = mask
                        self.last_src_size[path] = src.size
                        if not self.dc.active:     # in Design mode the estimate covers the design
                            self.set_estimate(stats, s.dpi, 0.0)
                        self.show_preview(path, s, src, gray, result, cutout)
                elif kind in ("design", "design_error"):
                    self.dc.on_result(msg)
                elif kind == "status":
                    self.set_status(msg[1])
                elif kind == "error":
                    if msg[1] == self.gen:
                        self.set_status("")
                        self.info.configure(text=msg[2], style="Warn.TLabel")
                elif kind == "batch":
                    _, done, total, name, err = msg
                    self.progress.configure(maximum=total, value=done)
                    self.set_status(f"{done}/{total}: {name}" + (f"  (FAILED: {err})" if err else ""))
                elif kind == "batch_done":
                    _, ok, total, failed = msg
                    self.busy_batch = False
                    self.batch_btn.state(["!disabled"])
                    text = f"Done: {ok}/{total} saved to {self.out_dir.get()}"
                    self.set_status(text)
                    if failed:
                        messagebox.showwarning("Some files failed", "\n".join(failed))
        except queue.Empty:
            pass
        self.root.after(50, self._poll)

    @staticmethod
    def _cutout_view(rgb, alpha, max_side=1600):
        """The photo in color with removed areas tinted red. Returns (image, view->source factor)."""
        k = min(1.0, max_side / max(rgb.size))
        size = (max(1, round(rgb.width * k)), max(1, round(rgb.height * k)))
        im = rgb.resize(size, Image.Resampling.BILINEAR)
        a = alpha.resize(size, Image.Resampling.BILINEAR)
        tinted = Image.blend(im, Image.new("RGB", size, (190, 20, 20)), 0.6)
        return Image.composite(im, tinted, a), rgb.width / size[0]

    def show_preview(self, path, s, src, gray, result, cutout=None):
        self.last = (self.gen, path, s, gray, result)
        if path != self._shown_path and not self._design_view():     # new image: zoom to fit
            self._shown_path = path
            self.zf, self.cu, self.cv = 1.0, 0.5, 0.5
        look = self.materials.get(s.material, {}).get("look", ["#ffffff", "#000000"])
        # Black pixels in the output = laser fires. Paint them in the burn color.
        material_look = ImageOps.colorize(result.convert("L"), black=look[1], white=look[0])
        if s.mirror:
            material_look = ImageOps.mirror(material_look)   # show it as seen from the front
        keep = {k: v for k, v in self.views.items() if k in ("Layout", "Design")}
        self.views = {"Original": src, "Adjusted": gray, "Engraving": result,
                      "Material look": material_look, **keep}
        self.view_factor = {k: v for k, v in self.view_factor.items() if k == "Layout"}
        if cutout:
            self.views["Cut-out"], self.view_factor["Cut-out"] = cutout
        else:
            self.views["Cut-out"] = src      # nothing removed; just show the photo
        self.update_labels()
        if self.dc.active:                   # the info line describes the whole design
            self.set_status("")
            self.redraw()
            return
        w, h = result.size
        w_in, h_in = w / s.dpi, h / s.dpi
        eff = src.width / w_in
        text = (f"Output {w} × {h} px  ·  {w_in:.2f} × {h_in:.2f} in @ {s.dpi} DPI  ·  "
                f"source {src.width} × {src.height} px")
        if eff < s.dpi:
            text += f"  ·  ⚠ source is only {eff:.0f} DPI at this size; it will be upscaled (softer detail)"
            self.info.configure(text=text, style="Warn.TLabel")
        else:
            self.info.configure(text=text, style="TLabel")
        self.set_status("")
        self.redraw()

    # ------------------------------------------------------------- zoom/pan
    # Zoom is stored as a multiple of "fit" (zf) plus the view center as a fraction of the
    # image (cu, cv), so switching views keeps you looking at the same spot. Only the visible
    # part of the image is resized each redraw, which keeps zooming smooth on big images.

    def _geometry(self, img):
        cw, ch = max(self.canvas.winfo_width(), 50), max(self.canvas.winfo_height(), 50)
        fit = min(cw / img.width, ch / img.height)
        return cw, ch, fit, fit * self.zf

    def request_redraw(self):
        if not self._redraw_pending:
            self._redraw_pending = True
            self.root.after_idle(self.redraw)

    def redraw(self):
        self._redraw_pending = False
        self.canvas.delete("all")
        img = self.views.get(self.view.get())
        if img is None:
            self.zoom_lbl.configure(text="")
            self.disp = None                       # nothing drawn: ignore clicks until it is
            return
        cw, ch, fit, scale = self._geometry(img)
        if self.zf <= 1.0:
            self.cu, self.cv = 0.5, 0.5
        ox = cw / 2 - self.cu * img.width * scale        # canvas x of image x=0
        oy = ch / 2 - self.cv * img.height * scale
        # Visible part of the image, in image pixels.
        l, t = max(0, int((0 - ox) / scale)), max(0, int((0 - oy) / scale))
        r = min(img.width, math.ceil((cw - ox) / scale))
        b = min(img.height, math.ceil((ch - oy) / scale))
        if r > l and b > t:
            crop = img.crop((l, t, r, b))
            if crop.mode == "1":
                crop = crop.convert("L")
            size = (max(1, round((r - l) * scale)), max(1, round((b - t) * scale)))
            if scale < 1:
                # Averaging (not nearest) so a shrunk dither shows its true tone, not moiré.
                method = Image.Resampling.BOX
            elif self.view.get() in ("Engraving", "Material look"):
                method = Image.Resampling.NEAREST        # show the real laser dots
            else:
                method = Image.Resampling.BILINEAR
            shown = crop.resize(size, method)
            split_x = None
            if self._comparing():
                shown, split_x = self._with_original(shown, img, (l, t, r, b), size, ox + l * scale, scale, ox)
            self.photo = ImageTk.PhotoImage(shown)
            self.canvas.create_image(round(ox + l * scale), round(oy + t * scale),
                                     image=self.photo, anchor="nw")
            if split_x is not None:
                y0, y1 = max(0, oy), min(ch, oy + img.height * scale)
                self.canvas.create_line(split_x, y0, split_x, y1, fill="#f8fafc", width=3)
                self.canvas.create_line(split_x, y0, split_x, y1, fill="#2563eb", width=1)
                for text, anchor, dx in (("Original", "ne", -6), (self.view.get(), "nw", 6)):
                    self.canvas.create_text(split_x + dx, y0 + 6, text=text, anchor=anchor,
                                            fill="#2563eb", font="AppBold")
        self.disp = (scale, ox, oy, self.view_factor.get(self.view.get(), 1.0))
        self.zoom_lbl.configure(text=f"{scale * 100:.0f}%")
        if self.view.get() == "Cut-out" and self.vars["remove_bg"].get():
            self._draw_strokes()
        if self._design_view():
            self.dc.draw_overlay()
        self._draw_brush()

    def _comparing(self):
        return (self.compare.get() and self.view.get() in ("Adjusted", "Engraving", "Material look")
                and "Original" in self.views)

    def _with_original(self, shown, img, box, size, x_left, scale, ox):
        """Puts the original photo left of the divider (same framing as the shown view)."""
        orig = self.views["Original"]
        last = getattr(self, "last", None)
        if last and last[2].mirror and self.view.get() in ("Adjusted", "Engraving"):
            orig = ImageOps.mirror(orig)               # those views are mirrored too
        l, t, r, b = box
        fx, fy = orig.width / img.width, orig.height / img.height
        o = orig.crop((round(l * fx), round(t * fy), round(r * fx), round(b * fy))).resize(size, Image.Resampling.BILINEAR)
        mode = "RGB" if "RGB" in (shown.mode, o.mode) else "L"
        shown, o = shown.convert(mode), o.convert(mode)
        split_x = ox + self.split * img.width * scale  # canvas x of the divider
        cut = int(round(split_x - x_left))
        if cut > 0:
            shown.paste(o.crop((0, 0, min(cut, size[0]), size[1])), (0, 0))
        return shown, split_x

    def zoom_by(self, factor, x=None, y=None):
        """Zoom by factor, keeping the image point under (x, y) (default: center) still."""
        img = self.views.get(self.view.get())
        if img is None:
            return
        cw, ch, fit, scale = self._geometry(img)
        # Design views can zoom out past the canvas (to 20% of fit) to see it with some margin.
        min_zf = 0.2 if self._design_view() else 1.0
        new_zf = min(max(self.zf * factor, min_zf), max(1.0, 16 / fit))   # .. 1600%
        if new_zf == self.zf or self.disp is None:
            return
        x = cw / 2 if x is None else x
        y = ch / 2 if y is None else y
        _, ox, oy, _ = self.disp
        px, py = (x - ox) / scale, (y - oy) / scale       # image point under the cursor
        new_scale = fit * new_zf
        self.cu = min(1, max(0, (px - (x - cw / 2) / new_scale) / img.width))
        self.cv = min(1, max(0, (py - (y - ch / 2) / new_scale) / img.height))
        self.zf = new_zf
        self.request_redraw()

    def zoom_fit(self):
        self.zf, self.cu, self.cv = 1.0, 0.5, 0.5
        self.request_redraw()

    def zoom_actual(self):
        img = self.views.get(self.view.get())
        if img is not None:
            self.zoom_by(1 / (self._geometry(img)[2] * self.zf))

    def on_wheel(self, e):
        self.zoom_by(ZOOM_STEP ** (e.delta / 120), e.x, e.y)

    def pan_start(self, e):
        self._pan_from = (e.x, e.y, self.cu, self.cv)

    def pan_move(self, e):
        img = self.views.get(self.view.get())
        if img is None or self.zf <= 1.0 or not self._pan_from:
            return
        x0, y0, cu, cv = self._pan_from
        scale = self._geometry(img)[3]
        self.cu = min(1, max(0, cu - (e.x - x0) / scale / img.width))
        self.cv = min(1, max(0, cv - (e.y - y0) / scale / img.height))
        self.request_redraw()

    # ------------------------------------------------------- cut-out fixes

    def _load_fixes(self):
        """Saved strokes, dropping any whose image file changed since."""
        try:
            with open(FIXES_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return {}
        out = {}
        for path, entry in data.items():
            try:
                if os.path.getmtime(path) == entry["mtime"]:
                    out[path] = [tuple(st) for st in entry["strokes"]]
            except (OSError, KeyError, TypeError):
                pass
        return out

    def _save_fixes(self):
        data = {}
        for path, strokes in self.fixes.items():
            if strokes and os.path.exists(path):
                data[path] = {"mtime": os.path.getmtime(path), "strokes": strokes}
        try:
            os.makedirs(os.path.dirname(FIXES_FILE), exist_ok=True)
            with open(FIXES_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f)
        except OSError:
            pass

    def prefetch_cutouts(self):
        """With Remove background on, load the AI model and cut out every listed image in the
        background, so each is ready (from the disk cache) by the time you click it."""
        if not self.vars["remove_bg"].get() or self.dc.active:   # designs have few photos
            return
        model = self.vars["bg_model"].get()
        if model not in core.BG_MODELS:
            return
        if ("warm", model) not in self.prefetched:
            self.prefetched.add(("warm", model))
            self.prefetcher.submit(self._warm, model)
        self.prefetched.add((self.current_file(), model))   # the preview handles the selected one
        for f in self.files:
            if (f, model) not in self.prefetched:
                self.prefetched.add((f, model))
                self.prefetcher.submit(self._prefetch_one, f, model)

    @staticmethod
    def _warm(model):
        try:
            core.warm_up(model)
        except Exception:
            pass                          # e.g. no internet for a download; the preview reports it

    @staticmethod
    def _prefetch_one(path, model):
        try:
            core.ai_cutout(path, model)   # result lands in the disk cache
        except Exception:
            pass                          # the preview will report real errors

    def on_model(self):
        label = self.model_label.get()
        model = next((m for m, v in core.BG_MODELS.items() if v[0] == label), core.DEFAULT_BG_MODEL)
        self.vars["bg_model"].set(model)          # triggers the preview update
        self.update_model_note()

    def update_model_note(self):
        model = self.vars["bg_model"].get()
        label, size, speed = core.BG_MODELS.get(model, core.BG_MODELS[core.DEFAULT_BG_MODEL])
        if self.model_label.get() != label:
            self.model_label.set(label)
        self.model_note.configure(text=f"{speed}" if core.model_downloaded(model)
                                  else f"{speed}, {size} download on first use")

    def update_fixbar(self):
        on = self.vars["remove_bg"].get()
        self.update_model_note()
        if on:
            self.prefetch_cutouts()
        if on and not self.fixbar.winfo_ismapped():
            self.fixbar.pack(fill="x", pady=(0, 4), before=self.canvas_frame)
            self.scaler.refresh(self.fixbar)
        elif not on and self.fixbar.winfo_ismapped():
            self.fixbar.pack_forget()
            self.tool.set("pan")
        n = len(self.strokes_for(self.current_file()))
        self.fix_hint.configure(text=f"{n} fix{'es' if n != 1 else ''} ⓘ")
        self.canvas.configure(cursor="fleur" if self.tool.get() == "pan" else "crosshair")

    def on_tool(self):
        if self.tool.get() != "pan" and self.view.get() != "Cut-out":
            self.view.set("Cut-out")
            self.redraw()
        self.update_fixbar()

    def _drawing(self):
        return (self.tool.get() != "pan" and self.view.get() == "Cut-out" and self.disp
                and self.vars["remove_bg"].get() and self.current_file())

    def _to_src(self, cx, cy):
        scale, x0, y0, factor = self.disp
        return ((cx - x0) / scale * factor, (cy - y0) / scale * factor)

    def _design_view(self):
        return self.dc.active and self.view.get() in ("Layout", "Design")

    def on_view_change(self):
        if self.dc.active:
            self.dc.on_view_change()               # renders that view if it's out of date
        self.redraw()

    def on_motion(self, e):
        self._mouse = (e.x, e.y)
        if self._design_view():
            self.dc.on_motion(e)
        self._draw_brush()

    def _draw_brush(self):
        """Circle under the cursor showing the brush size while a fix tool is active."""
        self.canvas.delete("brush")
        if not (self._mouse and self._drawing()):
            return
        r = max(1.0, self.brush.get() / 2)
        x, y = self._mouse
        self.canvas.create_oval(x - r - 1, y - r - 1, x + r + 1, y + r + 1, outline="white", width=1, tags="brush")
        self.canvas.create_oval(x - r, y - r, x + r, y + r, outline=STROKE_COLORS[self.tool.get()],
                                width=1.5, tags="brush")

    # --- keyboard shortcuts: one dispatcher, keys from the editable keymap (shortcuts.py)

    SHORTCUTS_HELP = "See ⌨ Shortcuts (top right) for all keys; you can change them there."

    def _bind_shortcuts(self):
        self.root.bind_all("<KeyPress>", self._on_key, add="+")
        self.root.bind_all("<KeyRelease>", self._on_key_up, add="+")

    def set_keymap(self, keymap):
        self.keymap = keymap
        self.key_actions = {k: a for a, k in keymap.items() if k}

    def open_shortcuts(self):
        w = shortcuts.ShortcutsWindow(self.root, self.keymap, self.set_keymap)
        self.scaler.refresh(w.win)

    # --- UI size / monitor scaling

    def set_ui_scale(self, s):
        s = min(max(s, ui_kit.UI_SIZES[0]), ui_kit.UI_SIZES[-1])
        self.ui_size.set(f"{round(s * 100)}%")
        self.scaler.set_user_scale(s)

    def step_ui_scale(self, step):
        sizes = ui_kit.UI_SIZES
        cur = self.scaler.user_scale
        i = min(range(len(sizes)), key=lambda k: abs(sizes[k] - cur))
        self.set_ui_scale(sizes[min(len(sizes) - 1, max(0, i + step))])

    def _after_rescale(self):
        self._fit_sash()
        self.request_redraw()

    def _fit_sash(self):
        self.root.update_idletasks()
        self.body.sashpos(0, self.left.winfo_reqwidth() + ui_kit.px(8))

    def _on_key(self, e):
        key = shortcuts.from_event(e)
        if key is None:
            return
        action = self.key_actions.get(key)
        if action is None and key == "Esc":
            action = "tool_pan"                    # Esc always returns to the Pan tool
        if action is None:                         # Shift+arrow = bigger nudge
            loose = self.key_actions.get(shortcuts.from_event(e, ignore_shift=True))
            if loose and loose.startswith("nudge_"):
                action = loose
        if action is None:
            return
        focus = self.root.focus_get()
        if isinstance(focus, tk.Toplevel) or (focus and str(focus).startswith(".!toplevel")):
            return                                 # dialogs (e.g. the Shortcuts window) handle their own keys
        typing = isinstance(focus, (tk.Entry, ttk.Entry, tk.Text, ttk.Spinbox, ttk.Combobox))
        if typing and "Ctrl+" not in key:
            return                                 # typing in a field
        return "break" if self._run_action(action, e) else None

    def _on_key_up(self, e):
        held = self.keymap.get("pan_hold")
        if held and shortcuts.from_event(e, ignore_shift=True) in (held, held.replace("Shift+", "")):
            self._set_space(False)

    def _run_action(self, action, e):
        """Performs a shortcut. Returns True if it did something."""
        dc = self.dc
        tools = {"tool_keep": "keep", "tool_remove": "remove", "brush_keep": "paint_keep",
                 "brush_erase": "paint_remove", "tool_pan": "pan"}
        if action in tools:
            self._pick_tool(tools[action])
        elif action == "brush_smaller":
            self._resize_brush(1 / 1.2)
        elif action == "brush_bigger":
            self._resize_brush(1.2)
        elif action == "pan_hold":
            self._set_space(True)
        elif action == "compare":
            self._toggle_compare()
        elif action == "ui_bigger":
            self.step_ui_scale(+1)
        elif action == "ui_smaller":
            self.step_ui_scale(-1)
        elif action == "zoom_fit":
            self.zoom_fit()
        elif action == "zoom_100":
            self.zoom_actual()
        elif action == "undo":
            dc.undo() if dc.active else self.undo_stroke()
        elif action == "redo":
            if not dc.active:
                return False
            dc.redo()
        elif action == "save":
            dc.save_project() if dc.active else self.save_current()
        elif action in ("duplicate", "delete") or action.startswith("nudge_"):
            if not dc.active:
                return False
            if action == "duplicate":
                dc.duplicate_layer()
            elif action == "delete":
                dc.delete_layer()
            else:
                dx, dy = {"nudge_left": (-1, 0), "nudge_right": (1, 0),
                          "nudge_up": (0, -1), "nudge_down": (0, 1)}[action]
                dc.nudge(dx, dy, e)
        else:
            return False
        return True

    def _pick_tool(self, tool):
        if tool != "pan" and not self.fixbar.winfo_ismapped():
            return                                 # fix tools only exist with Remove background on
        self.tool.set(tool)
        self.on_tool()
        self._draw_brush()

    def _resize_brush(self, f):
        self.brush.set(int(min(100, max(2, round(self.brush.get() * f)))))

    def _toggle_compare(self):
        self.compare.set(not self.compare.get())
        self.redraw()

    def _set_space(self, down):
        if down != self._space:
            self._space = down
            self.canvas.configure(cursor="fleur" if down else ("crosshair" if self._drawing() else "arrow"))

    def _split_x(self):
        img = self.views.get(self.view.get())
        if not (img and self.disp):
            return None
        scale, ox, _, _ = self.disp
        return ox + self.split * img.width * scale

    def on_press(self, e):
        if self._space:                            # Space + drag pans in any view or tool
            self.pan_start(e)
            return
        if self._design_view():
            self.dc.on_press(e)
            return
        sx = self._split_x() if self._comparing() else None
        if sx is not None and abs(e.x - sx) <= 10:
            self._split_drag = True                # dragging the before/after divider
            return
        if not self._drawing():
            self.pan_start(e)
            return
        cx, cy = self.canvas.canvasx(e.x), self.canvas.canvasy(e.y)
        kind = self.tool.get()
        w = self.brush.get()
        item = self.canvas.create_line(cx, cy, cx + 0.1, cy + 0.1, fill=STROKE_COLORS[kind], width=w,
                                       capstyle="round", joinstyle="round", stipple="gray50")
        self.live = (kind, [cx, cy], [self._to_src(cx, cy)], item)

    def on_drag(self, e):
        self._mouse = (e.x, e.y)
        if self._space:
            self.pan_move(e)
            return
        if self._split_drag:
            img = self.views.get(self.view.get())
            if img and self.disp:
                scale, ox, _, _ = self.disp
                self.split = min(1.0, max(0.0, (e.x - ox) / (img.width * scale)))
                self.request_redraw()
            return
        if self._design_view():
            self.dc.on_drag(e)
            return
        if not self.live:
            if not self._drawing():
                self.pan_move(e)
            return
        cx, cy = self.canvas.canvasx(e.x), self.canvas.canvasy(e.y)
        kind, cpts, spts, item = self.live
        if abs(cx - cpts[-2]) + abs(cy - cpts[-1]) < 3:
            return
        cpts += [cx, cy]
        spts.append(self._to_src(cx, cy))
        self.canvas.coords(item, *cpts)
        self._draw_brush()

    def on_release(self, e):
        if self._split_drag:
            self._split_drag = False
            return
        if self._space:
            return
        if self._design_view():
            self.dc.on_release(e)
            return
        if not self.live:
            return
        kind, _, spts, _ = self.live
        self.live = None
        scale, _, _, factor = self.disp
        radius = round(self.brush.get() / 2 / scale * factor, 1)
        stroke = (kind, radius, [(round(x, 1), round(y, 1)) for x, y in spts])
        self._strokes_mut(self.current_file()).append(stroke)
        self._fixes_changed()

    def undo_stroke(self):
        strokes = self._strokes_mut(self.current_file())
        if strokes:
            strokes.pop()
            self._fixes_changed()

    def clear_fixes(self):
        path = self.current_file()
        if self.strokes_for(path) and messagebox.askyesno(
                "Clear fixes", "Remove all background fixes on this image?"):
            self._strokes_mut(path).clear()
            self._fixes_changed()

    def _fixes_changed(self):
        if self.dc.active:
            self.dc.strokes_changed()          # strokes are saved inside the design
        else:
            self._save_fixes()
        self.update_fixbar()
        self.on_change()

    def _draw_strokes(self):
        """Overlays saved strokes on the Cut-out view so you can see what you've marked."""
        scale, x0, y0, factor = self.disp
        for kind, radius, pts in self.strokes_for(self.current_file()):
            c = [v for x, y in pts for v in (x0 + x / factor * scale, y0 + y / factor * scale)]
            if len(c) == 2:
                c += [c[0] + 0.1, c[1] + 0.1]
            self.canvas.create_line(*c, fill=STROKE_COLORS[kind], width=max(2, 2 * radius / factor * scale),
                                    capstyle="round", joinstyle="round", stipple="gray25")

    # ---------------------------------------------------------------- saving

    def save_current(self):
        if self.dc.active:
            self.dc.export()                   # the whole design as one engraving
            return
        path, s = self.current_file(), self.settings()
        if path is None or s is None:
            messagebox.showinfo("Nothing to save", "Add an image and check the settings first.")
            return
        try:
            last = getattr(self, "last", None)
            if last and last[0] == self.gen and last[1] == path:
                # Preview is up to date: save exactly what's on screen, no reprocessing.
                _, _, s, gray, result = last
                out = core.save_outputs(path, self.out_dir.get(), s, gray, result,
                                        self.last_mask, self.comparison.get())
            else:
                out = core.process_file(path, self.out_dir.get(), s, self.comparison.get(),
                                        self.fixes.get(path))
        except Exception as e:
            messagebox.showerror("Save failed", str(e))
            return
        self.set_status(f"Saved {os.path.basename(out)}")

    def process_all(self):
        s = self.settings()
        if not self.files or s is None or self.busy_batch:
            return
        files, out, comp = list(self.files), self.out_dir.get(), self.comparison.get()
        self.busy_batch = True
        self.batch_btn.state(["disabled"])
        self.progress.configure(maximum=len(files), value=0)
        threading.Thread(target=self._batch_job, args=(files, out, s, comp), daemon=True).start()

    def _batch_job(self, files, out, s, comp):
        done, ok, failed = 0, 0, []
        workers = max(1, min(4, (os.cpu_count() or 2) // 2))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(core.process_file, f, out, s, comp, list(self.fixes.get(f, []))): f
                       for f in files}
            for fut in futures:
                name = os.path.basename(futures[fut])
                err = None
                try:
                    fut.result()
                    ok += 1
                except Exception as e:
                    err = str(e)
                    failed.append(f"{name}: {e}")
                done += 1
                self.results.put(("batch", done, len(files), name, err))
        self.results.put(("batch_done", ok, len(files), failed))

    # ----------------------------------------------------------------- misc

    def set_status(self, text):
        self.status.configure(text=text)

    # ----------------------------------------------------------- estimate

    def set_estimate(self, stats, dpi, cut_mm):
        self.estimate = (stats, dpi, cut_mm)
        self.update_estimate()

    def update_estimate(self):
        if not self.estimate or not (self.estimate[0] or self.estimate[2]):
            self.estimate_lbl.configure(text="")
            return
        stats, dpi, cut_mm = self.estimate
        try:
            ev, cv = float(self.engrave_speed.get()), float(self.cut_speed.get())
        except (tk.TclError, ValueError):
            return
        parts = []
        if stats and stats[1]:
            parts.append(f"engrave ≈ {core.format_duration(core.estimate_seconds(stats, dpi, ev))}")
        if cut_mm and cv > 0:
            parts.append(f"cut ≈ {core.format_duration(cut_mm / cv)}")
        self.estimate_lbl.configure(text="  ·  ".join(parts) or "nothing to burn")

    def _load_state(self):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def on_close(self):
        # In Design mode the controls show the selected layer, so save the Photos-mode settings.
        s = (self.dc.photo_snapshot if self.dc.active else None) or self.settings()
        state = {"settings": (s or core.Settings()).to_dict(), "out_dir": self.out_dir.get(),
                 "comparison": self.comparison.get(), "files": self.files,
                 "maximized": self.root.state() == "zoomed",
                 "mode": self.mode.get(), "design_state": self.dc.state(),   # unsaved designs survive
                 "engrave_speed": self._num(self.engrave_speed, 150.0), "cut_speed": self._num(self.cut_speed, 15.0),
                 "shortcuts": {a: k for a, k in self.keymap.items() if k != shortcuts.DEFAULTS[a]},
                 "ui_scale": self.scaler.user_scale,
                 **{f"{st}_preset": v.get() for st, v in self.preset_vars.items()}}
        if self.root.state() == "normal":
            state["geometry"] = self.root.geometry()
        try:
            os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
        except OSError:
            pass
        self.root.destroy()

    @staticmethod
    def _num(var, default):
        try:
            return float(var.get())
        except (tk.TclError, ValueError):
            return default

    def on_error(self, exc, val, tb):
        messagebox.showerror("Unexpected error", "".join(traceback.format_exception(exc, val, tb))[-1500:])


def selftest(report_path):
    """Headless check that the bundled libraries work (build.py runs this after building)."""
    import time
    import numpy as np
    lines, ok = [], True
    yy, xx = np.mgrid[0:600, 0:500]
    img = Image.fromarray(((xx + yy) % 256).astype(np.uint8)).convert("RGB")
    path = os.path.join(os.path.dirname(report_path), "selftest_input.png")
    img.save(path)
    fix = [("remove", 20, [(100, 100), (200, 150)])]
    for label, kw, strokes in (("photo/stucki", {}, None), ("comic", {"style": "comic"}, None),
                               ("remove background", {"remove_bg": True}, None),
                               ("cut-out fix (GrabCut)", {"remove_bg": True}, fix)):
        t = time.time()
        try:
            s = core.Settings(width_in=2.0, **kw)
            core.render(*core.load_source(path, s.remove_bg, strokes, s.bg_model), s)
            lines.append(f"OK    {label}  {time.time() - t:.1f} s")
        except Exception as e:
            ok = False
            lines.append(f"FAIL  {label}: {e!r}")
    # Design mode: text (fonts), arc text, shapes, a photo layer and an SVG (PyMuPDF), plus a
    # project file round trip.
    t = time.time()
    try:
        import design as D
        svg = os.path.join(os.path.dirname(report_path), "selftest.svg")
        with open(svg, "w") as f:
            f.write('<svg xmlns="http://www.w3.org/2000/svg" width="1in" height="1in" viewBox="0 0 10 10">'
                    '<circle cx="5" cy="5" r="4" fill="black"/></svg>')
        d = D.new_design("wood", 3, 3)
        d["layers"] += [D.new_layer("shape", d, shape="ellipse"), D.new_layer("text", d),
                        D.new_layer("arctext", d), D.new_layer("svg", d, src=svg, w_in=0.5),
                        D.new_layer("image", d, src=path, w_in=1.0)]
        img, _ = D.Renderer().engraving(d)
        # Cut lines: shape + text outlines (fontTools) + SVG paths, written as SVG and DXF.
        d["layers"][1]["cut"] = d["layers"][3]["cut"] = True
        d["cut_format"] = "svg+dxf"
        files = D.save_engraving(d, D.Renderer(), os.path.join(os.path.dirname(report_path), "selftest_design.png"))
        if not any(f.endswith("_cut.dxf") for f in files):
            raise RuntimeError(f"cut files missing: {files}")
        proj = os.path.join(os.path.dirname(report_path), "selftest.lep")
        D.save_project(d, proj)
        D.load_project(proj)
        lines.append(f"OK    design ({len(D.fonts())} fonts, project file)  {time.time() - t:.1f} s")
    except Exception as e:
        ok = False
        lines.append(f"FAIL  design: {e!r}")
    lines.append(f"fast stucki (numba): {core.FAST_STUCKI}")
    ok = ok and core.FAST_STUCKI
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines + ["PASS" if ok else "FAIL"]) + "\n")
    return 0 if ok else 1


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--selftest":
        sys.exit(selftest(sys.argv[2]))
    ui_kit.set_dpi_awareness()       # crisp on every monitor; ui_kit.Scaler handles the sizing
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
