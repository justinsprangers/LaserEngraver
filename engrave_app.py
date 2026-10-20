"""
engrave_app.py: desktop app for preparing photos for laser engraving.

Double-click "Laser Engraver.bat" (or run: python engrave_app.py).
All image processing lives in engrave_core.py; this file is only the window. The App class is
split by job into mixins (same `self`, shared state):
    engrave_app.py   window layout, settings <-> controls, files, preview pipeline, saving, state
    app_panels.py    settings tabs: sliders, presets, materials, crop & rotate, test card
    app_preview.py   preview canvas: drawing, zoom / pan, compare, mouse routing, brush outline
    app_fixtools.py  background-removal fix tools and AI model choice
    app_keys.py      keyboard shortcuts, UI size
    design_ui.py     Design mode (layout editor), attached as self.dc
"""

import json
import os
import queue
import subprocess
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import numpy as np

import design_ui
import engrave_core as core
import shortcuts
import ui_kit
from app_common import CROP_FIELDS, DEBOUNCE_MS, INT_FIELDS, VIEWS, ZOOM_STEP
from app_fixtools import FixToolsMixin
from app_keys import KeysMixin
from app_panels import PanelsMixin
from app_preview import PreviewMixin
from ui_kit import ScrollTab, Tooltip

STATE_FILE = os.path.join(core.DATA_DIR, "settings.json")
DEFAULT_OUT = core.DEFAULT_OUT
ICON = os.path.join(core.HERE, "icon.ico")
FIXES_FILE = os.path.join(core.DATA_DIR, "fixes.json")


class App(PanelsMixin, PreviewMixin, FixToolsMixin, KeysMixin):
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
        theme = state.get("theme", "Light")
        self.scaler = ui_kit.init_scaling(root, state.get("ui_scale", 1.0), theme if theme in ui_kit.THEMES else "Light")
        self.ui_size = tk.StringVar(value=f"{round(self.scaler.user_scale * 100)}%")
        self.theme = tk.StringVar(value=self.scaler.theme_name)
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
        self.root.after(self.AUTOSAVE_MS, self._autosave)

    # ------------------------------------------------------------------ layout

    def _build(self):
        ui_kit.apply_theme(self.root)
        top = ttk.Frame(self.root, padding=(12, 10, 12, 6))
        top.pack(fill="x")
        self.mode_frame = ttk.Frame(top)
        self.mode_frame.pack(side="left", padx=(0, 4))
        ttk.Label(self.mode_frame, text="Mode", style="Section.TLabel").pack(side="left", padx=(0, 8))
        mode_seg = ui_kit.segmented(self.mode_frame)
        mode_seg.pack(side="left")
        for text, val, tip in (("Photos", "photos", "Process photos one by one or in batches."),
                               ("Design", "design", "Lay out photos, logos, SVGs, text and shapes\n"
                                                    "into one engraving, and save it as a project.")):
            Tooltip(ui_kit.seg_button(mode_seg, text, val, self.mode, self.set_mode), tip)
        ttk.Separator(self.mode_frame, orient="vertical").pack(side="left", fill="y", padx=(12, 12))
        self.file_toolbar = ttk.Frame(top)
        self.file_toolbar.pack(side="left")
        ttk.Button(self.file_toolbar, text="Add images…", command=self.add_images).pack(side="left")
        ttk.Button(self.file_toolbar, text="Add folder…", command=self.add_folder).pack(side="left", padx=6)
        ttk.Button(self.file_toolbar, text="Remove", command=self.remove_selected).pack(side="left")
        ttk.Button(top, text="Help", width=6, command=self.open_help).pack(side="right")
        kb = ttk.Button(top, text="⌨ Shortcuts", command=self.open_shortcuts)
        kb.pack(side="right", padx=(0, 6))
        Tooltip(kb, "See and change the keyboard shortcuts")
        # Display menu: theme + UI size (kept in one button so the toolbar has room).
        vm = ttk.Menubutton(top, text="⚙ View")
        menu = tk.Menu(vm, tearoff=False)
        menu.add_command(label="Theme", state="disabled")
        for t in ui_kit.THEMES:
            menu.add_radiobutton(label=f"   {t}", value=t, variable=self.theme,
                                 command=lambda: ui_kit.set_theme(self.theme.get()))
        menu.add_separator()
        menu.add_command(label="UI size  (Ctrl+= / Ctrl+-)", state="disabled")
        for v in ui_kit.UI_SIZES:
            menu.add_radiobutton(label=f"   {round(v * 100)}%", value=f"{round(v * 100)}%", variable=self.ui_size,
                                 command=lambda v=v: self.set_ui_scale(v))
        vm["menu"] = menu
        vm.pack(side="right", padx=(0, 6))
        Tooltip(vm, "Light / dark theme and UI size. The app also adjusts automatically when\n"
                    "you move it to a monitor with different Windows scaling.")
        # Footer (packed before the body so it can never be squeezed off-screen), two rows:
        #   where files go  |  save buttons, job-time estimate, status
        footer = ttk.Frame(self.root, padding=(12, 6, 12, 8))
        footer.pack(side="bottom", fill="x")
        ttk.Separator(self.root).pack(side="bottom", fill="x")
        out_row = ttk.Frame(footer)
        out_row.pack(fill="x", pady=(0, 6))
        ttk.Label(out_row, text="Save to", style="Section.TLabel").pack(side="left", padx=(0, 8))
        ttk.Entry(out_row, textvariable=self.out_dir, width=20).pack(side="left", fill="x", expand=True)
        ttk.Button(out_row, text="Change…", style="Tool.TButton", command=self.choose_out).pack(side="left", padx=(6, 0))
        ob = ttk.Button(out_row, text="Open folder", style="Tool.TButton", command=self.open_out)
        ob.pack(side="left", padx=(6, 0))
        Tooltip(ob, "Open the output folder")
        self.comparison_cb = ttk.Checkbutton(out_row, text="Also save side-by-side preview JPG",
                                             variable=self.comparison)
        self.comparison_cb.pack(side="left", padx=(16, 0))
        bottom = ttk.Frame(footer)
        bottom.pack(fill="x")
        self.save_btn = ttk.Button(bottom, text="Save this engraving", style="Accent.TButton",
                                   command=self.save_current)
        self.save_btn.pack(side="left")
        self.batch_btn = ttk.Button(bottom, text="Process all", style="Big.TButton",
                                    command=self.process_all)
        self.batch_btn.pack(side="left", padx=8)
        self.progress = ttk.Progressbar(bottom, length=140, mode="determinate")
        self.progress.pack(side="right")
        self.status = ttk.Label(bottom, text="", style="Hint.TLabel")
        self.status.pack(side="right", padx=8)
        # Job-time estimate (rough: constant speed, no acceleration).
        est = ttk.Frame(bottom)
        est.pack(side="right", padx=(8, 8))
        self.estimate_lbl = ttk.Label(est, text="")
        self.estimate_lbl.pack(side="left", padx=(0, 12))
        ttk.Label(est, text="Engrave", style="Hint.TLabel").pack(side="left")
        ttk.Spinbox(est, textvariable=self.engrave_speed, from_=1, to=5000, increment=10, width=5).pack(side="left", padx=3)
        ttk.Label(est, text="Cut", style="Hint.TLabel").pack(side="left", padx=(6, 0))
        ttk.Spinbox(est, textvariable=self.cut_speed, from_=0.5, to=500, increment=1, width=4).pack(side="left", padx=3)
        ttk.Label(est, text="mm/s", style="Hint.TLabel").pack(side="left")
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
        nb = ui_kit.StripNotebook(left)            # tab headers = a segmented button strip
        nb.strip.pack(anchor="w", pady=(10, 6))
        nb.pack(fill="both", expand=True)
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
        self.design_seg = ui_kit.segmented(bar)
        self.layout_radio = ui_kit.seg_button(self.design_seg, "Layout", "Layout", self.view, self.on_view_change)
        self.design_radio = ui_kit.seg_button(self.design_seg, "Design", "Design", self.view, self.on_view_change)
        Tooltip(self.layout_radio, "Fast view for arranging: photos are shown as plain pictures,\n"
                                   "without the engraving processing.")
        Tooltip(self.design_radio, "The real engraving of the whole design (slower to update).")
        self.view_seg = ui_kit.segmented(bar)
        self.view_seg.pack(side="left")
        self.view_radios = [ui_kit.seg_button(self.view_seg, v, v, self.view, self.on_view_change) for v in VIEWS]
        self.compare_cb = ttk.Checkbutton(bar, text="Compare (C)", variable=self.compare, command=self.redraw)
        self.compare_cb.pack(side="right")
        Tooltip(self.compare_cb, "Before/after split on the Adjusted, Engraving and Material look views:\n"
                                 "the original photo on the left of the divider. Drag the divider.")

        self._build_fixbar(right)                  # app_fixtools.py

        cf = ttk.Frame(right)
        cf.pack(fill="both", expand=True)
        self.canvas_frame = cf
        self.canvas = tk.Canvas(cf, background=ui_kit.P["canvas"], highlightthickness=0, cursor="fleur")
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

        self.dc.build(top, nb.strip, nb, bottom_hide=[self.batch_btn, self.comparison_cb])
        self._bind_shortcuts()

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

    # ------------------------------------------------------------- presets

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
        self.dither_hint.configure(foreground=ui_kit.P["muted"] if photo else ui_kit.P["border"])
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

    # ------------------------------------------------------------- zoom/pan
    # Zoom is stored as a multiple of "fit" (zf) plus the view center as a fraction of the
    # image (cu, cv), so switching views keeps you looking at the same spot. Only the visible
    # part of the image is resized each redraw, which keeps zooming smooth on big images.

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

    # --- keyboard shortcuts: one dispatcher, keys from the editable keymap (shortcuts.py)

    SHORTCUTS_HELP = "See ⌨ Shortcuts (top right) for all keys; you can change them there."

    # --- UI size / monitor scaling

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

    AUTOSAVE_MS = 60_000

    def _autosave(self):
        """Saves the app state (including an unsaved design) every minute, so a crash or power
        cut loses at most a minute of work. It's restored automatically on the next start."""
        try:
            self.save_state()
        finally:
            self.root.after(self.AUTOSAVE_MS, self._autosave)

    def on_close(self):
        self.save_state()
        self.root.destroy()

    def save_state(self):
        """Writes settings.json atomically (a crash mid-write can't corrupt it). Skips the write
        when nothing changed."""
        # In Design mode the controls show the selected layer, so save the Photos-mode settings.
        s = (self.dc.photo_snapshot if self.dc.active else None) or self.settings()
        state = {"settings": (s or core.Settings()).to_dict(), "out_dir": self.out_dir.get(),
                 "comparison": self.comparison.get(), "files": self.files,
                 "maximized": self.root.state() == "zoomed",
                 "mode": self.mode.get(), "design_state": self.dc.state(),   # unsaved designs survive
                 "engrave_speed": self._num(self.engrave_speed, 150.0), "cut_speed": self._num(self.cut_speed, 15.0),
                 "shortcuts": {a: k for a, k in self.keymap.items() if k != shortcuts.DEFAULTS[a]},
                 "ui_scale": self.scaler.user_scale, "theme": self.theme.get(),
                 **{f"{st}_preset": v.get() for st, v in self.preset_vars.items()}}
        if self.root.state() == "normal":
            state["geometry"] = self.root.geometry()
        text = json.dumps(state, indent=2)
        if text == getattr(self, "_saved_state", None):
            return
        try:
            os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, STATE_FILE)
            self._saved_state = text
        except OSError:
            pass

    @staticmethod
    def _num(var, default):
        try:
            return float(var.get())
        except (tk.TclError, ValueError):
            return default

    def on_error(self, exc, val, tb):
        messagebox.showerror("Unexpected error", "".join(traceback.format_exception(exc, val, tb))[-1500:])


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--selftest":
        from selftest import selftest
        sys.exit(selftest(sys.argv[2]))
    ui_kit.set_dpi_awareness()       # crisp on every monitor; ui_kit.Scaler handles the sizing
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
