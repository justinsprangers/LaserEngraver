"""
engrave_app.py: desktop app for preparing photos for laser engraving.

Double-click "Laser Engraver.bat" (or run: python engrave_app.py).
All image processing lives in engrave_core.py; this file is only the window.
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
from tkinter import filedialog, messagebox, simpledialog, ttk

from PIL import Image, ImageOps, ImageTk

import engrave_core as core

STATE_FILE = os.path.join(core.DATA_DIR, "settings.json")
DEFAULT_OUT = core.DEFAULT_OUT
ICON = os.path.join(core.HERE, "icon.ico")
DPI_CHOICES = ("254", "318", "423", "508")   # 0.10, 0.08, 0.06, 0.05 mm line interval
ZOOMS = ("Fit", "25%", "50%", "100%", "200%", "400%")
VIEWS = ("Original", "Cut-out", "Adjusted", "Engraving", "Material look")
FIXES_FILE = os.path.join(core.DATA_DIR, "fixes.json")
STROKE_COLORS = {"keep": "#22c55e", "remove": "#ef4444"}
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
INT_FIELDS = ("shadow_radius", "clahe_grid", "sharpen_percent", "sharpen_threshold", "comic_levels",
              "comic_smooth", "comic_line_block", "comic_line_weight", "comic_despeckle")


class Tooltip:
    """Shows a small hint box while the mouse is over a widget."""

    def __init__(self, widget, text):
        self.widget, self.text, self.tip = widget, text, None
        widget.bind("<Enter>", self.show, add="+")
        widget.bind("<Leave>", self.hide, add="+")

    def show(self, _):
        x = self.widget.winfo_rootx() + 10
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{x}+{y}")
        tk.Label(self.tip, text=self.text, background="#ffffe0", relief="solid",
                 borderwidth=1, padx=6, pady=3, justify="left").pack()

    def hide(self, _):
        if self.tip:
            self.tip.destroy()
            self.tip = None


class App:
    def __init__(self, root):
        self.root = root
        self.materials = core.load_materials()
        self.files = []
        self.src_cache = {}         # path -> grayscale source (no background removal)
        self.ai_cache = {}          # path -> (RGB, AI alpha)
        self.refined = {}           # path -> (strokes key, grayscale on white, corrected alpha)
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
        self.zoom = tk.StringVar(value="Fit")

        self.vars = {}
        for name, value in s.to_dict().items():
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

        self._build()
        root.update_idletasks()
        # Size the settings column to its contents (works at any Windows display scaling).
        root.after(100, lambda: self.body.sashpos(0, self.left.winfo_reqwidth() + 8))
        for f in state.get("files", []):
            if os.path.isfile(f):
                self._add_file(f)
        if self.files:
            self.listbox.selection_set(0)
        self.on_change()
        self.root.after(50, self._poll)

    # ------------------------------------------------------------------ layout

    def _build(self):
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Hint.TLabel", foreground="#666")
        style.configure("Warn.TLabel", foreground="#b45309")
        style.configure("Big.TButton", padding=(10, 6))

        top = ttk.Frame(self.root, padding=(8, 8, 8, 4))
        top.pack(fill="x")
        ttk.Button(top, text="Add images…", command=self.add_images).pack(side="left")
        ttk.Button(top, text="Add folder…", command=self.add_folder).pack(side="left", padx=4)
        ttk.Button(top, text="Remove", command=self.remove_selected).pack(side="left")
        ttk.Button(top, text="Help", command=self.open_help).pack(side="right")
        ttk.Button(top, text="Open", command=self.open_out).pack(side="right", padx=(4, 12))
        ttk.Button(top, text="Change…", command=self.choose_out).pack(side="right")
        ttk.Entry(top, textvariable=self.out_dir, width=46).pack(side="right", padx=4)
        ttk.Label(top, text="Save to:").pack(side="right")

        # Bottom bar is packed before the body so it can never be squeezed off-screen.
        bottom = ttk.Frame(self.root, padding=8)
        bottom.pack(side="bottom", fill="x")
        self.save_btn = ttk.Button(bottom, text="Save this engraving", style="Big.TButton",
                                   command=self.save_current)
        self.save_btn.pack(side="left")
        self.batch_btn = ttk.Button(bottom, text="Process all", style="Big.TButton",
                                    command=self.process_all)
        self.batch_btn.pack(side="left", padx=6)
        ttk.Checkbutton(bottom, text="Also save side-by-side preview JPG",
                        variable=self.comparison).pack(side="left", padx=8)
        self.progress = ttk.Progressbar(bottom, length=180, mode="determinate")
        self.progress.pack(side="right")
        self.status = ttk.Label(bottom, text="", style="Hint.TLabel")
        self.status.pack(side="right", padx=8)

        body = ttk.PanedWindow(self.root, orient="horizontal")
        body.pack(fill="both", expand=True, padx=8)
        self.body = body

        left = ttk.Frame(body, width=340)
        body.add(left, weight=0)
        right = ttk.Frame(body)
        body.add(right, weight=1)

        # --- file list
        lf = ttk.LabelFrame(left, text="Images", padding=4)
        lf.pack(fill="both", expand=False)
        self.listbox = tk.Listbox(lf, height=5, activestyle="none", exportselection=False)
        sb = ttk.Scrollbar(lf, command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=sb.set)
        self.listbox.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.listbox.bind("<<ListboxSelect>>", lambda e: self.on_change())
        self.listbox.bind("<Delete>", lambda e: self.remove_selected())

        # --- settings tabs
        # Settings scroll if the window is too short to show them all.
        holder = ttk.Frame(left)
        holder.pack(fill="both", expand=True, pady=(6, 0))
        sc = tk.Canvas(holder, highlightthickness=0, borderwidth=0)
        ssb = ttk.Scrollbar(holder, orient="vertical", command=sc.yview)
        sc.configure(yscrollcommand=ssb.set)
        ssb.pack(side="right", fill="y")
        sc.pack(side="left", fill="both", expand=True)
        nb = ttk.Notebook(sc)
        win = sc.create_window(0, 0, window=nb, anchor="nw")
        nb.bind("<Configure>", lambda e: sc.configure(scrollregion=sc.bbox("all"),
                                                      width=nb.winfo_reqwidth()))
        sc.bind("<Configure>", lambda e: sc.itemconfigure(win, width=e.width))
        wheel = lambda e: sc.yview_scroll(-1 if e.delta > 0 else 1, "units")
        holder.bind("<Enter>", lambda e: sc.bind_all("<MouseWheel>", wheel))
        holder.bind("<Leave>", lambda e: sc.unbind_all("<MouseWheel>"))
        self.left = left
        basic = ttk.Frame(nb, padding=8)
        fine = ttk.Frame(nb, padding=8)
        comic = ttk.Frame(nb, padding=8)
        nb.add(basic, text="Basic")
        nb.add(fine, text="Photo tuning")
        nb.add(comic, text="Comic tuning")
        self.nb, self.fine_tab, self.comic_tab = nb, fine, comic
        self._build_basic(basic)
        self._build_fine(fine)
        self._build_comic(comic)

        # --- preview
        bar = ttk.Frame(right)
        bar.pack(fill="x", pady=(0, 4))
        for v in VIEWS:
            ttk.Radiobutton(bar, text=v, value=v, variable=self.view,
                            command=self.redraw).pack(side="left", padx=(0, 8))
        zc = ttk.Combobox(bar, textvariable=self.zoom, values=ZOOMS, width=6, state="readonly")
        zc.pack(side="right")
        zc.bind("<<ComboboxSelected>>", lambda e: self.redraw())
        ttk.Label(bar, text="Zoom").pack(side="right", padx=4)

        # Fix tools: shown only while Remove background is on.
        self.fixbar = ttk.Frame(right)
        ttk.Label(self.fixbar, text="Fix cut-out:").pack(side="left")
        for label, val in (("Pan", "pan"), ("Keep ✚", "keep"), ("Remove ✖", "remove")):
            ttk.Radiobutton(self.fixbar, text=label, value=val, variable=self.tool,
                            command=self.on_tool).pack(side="left", padx=(6, 0))
        ttk.Label(self.fixbar, text="Brush").pack(side="left", padx=(14, 4))
        ttk.Scale(self.fixbar, from_=6, to=100, variable=self.brush, length=110).pack(side="left")
        ttk.Button(self.fixbar, text="Undo", command=self.undo_stroke).pack(side="left", padx=(14, 4))
        ttk.Button(self.fixbar, text="Clear fixes", command=self.clear_fixes).pack(side="left")
        self.fix_hint = ttk.Label(self.fixbar, text="", style="Hint.TLabel")
        self.fix_hint.pack(side="left", padx=10)
        Tooltip(self.fix_hint, "Keep: paint on parts of the subject that were wrongly removed.\n"
                               "Remove: paint on background that was wrongly kept.\n"
                               "When you let go, it re-learns subject vs. background colors from\n"
                               "your strokes and re-cuts the whole wrong region, not just the paint.\n"
                               "Right-drag pans in any tool. Fixes are saved per image.")

        cf = ttk.Frame(right)
        cf.pack(fill="both", expand=True)
        self.canvas_frame = cf
        self.canvas = tk.Canvas(cf, background="#808080", highlightthickness=0, cursor="fleur")
        xs = ttk.Scrollbar(cf, orient="horizontal", command=self.canvas.xview)
        ys = ttk.Scrollbar(cf, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(xscrollcommand=xs.set, yscrollcommand=ys.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        xs.grid(row=1, column=0, sticky="ew")
        cf.rowconfigure(0, weight=1)
        cf.columnconfigure(0, weight=1)
        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.canvas.bind("<ButtonPress-3>", lambda e: self.canvas.scan_mark(e.x, e.y))
        self.canvas.bind("<B3-Motion>", lambda e: self.canvas.scan_dragto(e.x, e.y, gain=1))
        self.canvas.bind("<Configure>", lambda e: self.zoom.get() == "Fit" and self.redraw())
        self.canvas.bind("<MouseWheel>", self.on_wheel)

        self.info = ttk.Label(right, text="", anchor="w", wraplength=900)
        self.info.pack(fill="x", pady=(4, 0))
        self.info.bind("<Configure>", lambda e: self.info.configure(wraplength=max(200, e.width)))

    def _build_basic(self, f):
        f.columnconfigure(1, weight=1)
        r = 0
        ttk.Label(f, text="Style").grid(row=r, column=0, sticky="w", pady=3)
        sf = ttk.Frame(f)
        sf.grid(row=r, column=1, columnspan=2, sticky="w")
        ttk.Radiobutton(sf, text="Photo", value="photo", variable=self.vars["style"]).pack(side="left")
        ttk.Radiobutton(sf, text="Comic", value="comic", variable=self.vars["style"]).pack(side="left", padx=10)
        r += 1
        ttk.Label(f, text="Material").grid(row=r, column=0, sticky="w", pady=3)
        self.mat_combo = ttk.Combobox(f, textvariable=self.vars["material"], state="readonly",
                                      values=sorted(self.materials))
        self.mat_combo.grid(row=r, column=1, columnspan=2, sticky="ew")
        r += 1
        ttk.Label(f, text="Width (in)").grid(row=r, column=0, sticky="w", pady=3)
        ttk.Spinbox(f, textvariable=self.vars["width_in"], from_=0.25, to=48, increment=0.25,
                    width=8).grid(row=r, column=1, sticky="w")
        self.height_lbl = ttk.Label(f, text="", style="Hint.TLabel")
        self.height_lbl.grid(row=r, column=2, sticky="w")
        r += 1
        ttk.Label(f, text="DPI").grid(row=r, column=0, sticky="w", pady=3)
        ttk.Combobox(f, textvariable=self.vars["dpi"], values=DPI_CHOICES,
                     width=8).grid(row=r, column=1, sticky="w")
        self.dpi_lbl = ttk.Label(f, text="", style="Hint.TLabel")
        self.dpi_lbl.grid(row=r, column=2, sticky="w")
        r += 1
        ttk.Label(f, text="Dither").grid(row=r, column=0, sticky="w", pady=3)
        self.dither_combo = ttk.Combobox(f, textvariable=self.vars["dither"], values=core.DITHER_MODES,
                                         state="readonly", width=16)
        self.dither_combo.grid(row=r, column=1, columnspan=2, sticky="w")
        r += 1
        self.dither_hint = ttk.Label(f, text="stucki: most detail (acrylic, glass)\n"
                                             "floyd-steinberg: photos, portraits\n"
                                             "bayer: wood grain, geometric\n"
                                             "none: hard cutoff, no shading",
                                     style="Hint.TLabel", justify="left")
        self.dither_hint.grid(row=r, column=0, columnspan=3, sticky="w", pady=(0, 4))
        r += 1
        cb = ttk.Checkbutton(f, text="Remove background (AI, ~1-3 s per photo) ⓘ",
                             variable=self.vars["remove_bg"])
        cb.grid(row=r, column=0, columnspan=3, sticky="w")
        Tooltip(cb, "Cuts out the main subject and makes the background blank,\n"
                    "so the laser skips it. Also gives the cut outline a real silhouette.")
        r += 1
        ttk.Checkbutton(f, text="Also save cut outline (SVG)", variable=self.vars["outline"]).grid(
            row=r, column=0, columnspan=3, sticky="w")
        r += 1
        r = self._slider(f, r, "outline_offset_in", 0, 0.5, 0.01, label="Outline offset (in)")
        ttk.Separator(f).grid(row=r, column=0, columnspan=3, sticky="ew", pady=4)
        r += 1
        ttk.Separator(f).grid(row=r, column=0, columnspan=3, sticky="ew", pady=4)
        r += 1
        for name, lo, hi in (("contrast", 0.5, 2.5), ("brightness", 0.5, 2.0), ("gamma", 0.4, 3.0)):
            r = self._slider(f, r, name, lo, hi, 0.05)
        ttk.Checkbutton(f, text="Invert (laser makes light marks)",
                        variable=self.vars["invert"]).grid(row=r, column=0, columnspan=3, sticky="w")
        r += 1
        ttk.Checkbutton(f, text="Mirror (engrave from the back)",
                        variable=self.vars["mirror"]).grid(row=r, column=0, columnspan=3, sticky="w")
        r += 1
        bf = ttk.Frame(f)
        bf.grid(row=r, column=0, columnspan=3, sticky="w", pady=(10, 0))
        ttk.Button(bf, text="Reset to material", command=self.on_material).pack(side="left")
        ttk.Button(bf, text="Save as material…", command=self.save_as_material).pack(side="left", padx=4)
        tc = ttk.Button(f, text="Make test card ⓘ", command=self.make_test_card)
        tc.grid(row=r + 1, column=0, columnspan=3, sticky="w", pady=(6, 0))
        Tooltip(tc, "Saves a 3x3 grid of this image at different contrast (columns)\n"
                    "and gamma (rows). Burn it on scrap, then type the numbers under\n"
                    "the best tile into Contrast and Gamma.")

    def _build_comic(self, f):
        f.columnconfigure(1, weight=1)
        r = 0
        ttk.Label(f, text="Used when Style = Comic.", style="Hint.TLabel").grid(
            row=r, column=0, columnspan=3, sticky="w", pady=(0, 4))
        r += 1
        ttk.Label(f, text="Tones", font=("", 9, "bold")).grid(row=r, column=0, columnspan=3, sticky="w")
        r += 1
        r = self._slider(f, r, "comic_levels", 2, 5, 1, label="Tone bands")
        r = self._slider(f, r, "comic_smooth", 0, 10, 1, label="Flatten")
        r = self._slider(f, r, "comic_dot_mm", 0.3, 3, 0.05, label="Dot spacing (mm)")
        ttk.Label(f, text="Ink lines", font=("", 9, "bold")).grid(row=r, column=0, columnspan=3, sticky="w", pady=(8, 0))
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
        ttk.Button(f, text="Reset comic to defaults", command=self.reset_comic).grid(
            row=r, column=0, columnspan=3, sticky="w", pady=(10, 0))

    def _build_fine(self, f):
        f.columnconfigure(1, weight=1)
        r = 0
        ttk.Label(f, text="Shadow detail recovery", font=("", 9, "bold")).grid(row=r, column=0, columnspan=3, sticky="w")
        r += 1
        r = self._slider(f, r, "shadow_amount", 0, 0.6, 0.01)
        r = self._slider(f, r, "shadow_radius", 1, 60, 1)
        ttk.Label(f, text="Micro-texture (CLAHE)", font=("", 9, "bold")).grid(row=r, column=0, columnspan=3, sticky="w", pady=(8, 0))
        r += 1
        ttk.Checkbutton(f, text="Enabled", variable=self.vars["use_clahe"]).grid(row=r, column=0, columnspan=3, sticky="w")
        r += 1
        r = self._slider(f, r, "clahe_clip", 0.5, 5, 0.1)
        r = self._slider(f, r, "clahe_grid", 2, 32, 1)
        ttk.Label(f, text="Sharpening", font=("", 9, "bold")).grid(row=r, column=0, columnspan=3, sticky="w", pady=(8, 0))
        r += 1
        r = self._slider(f, r, "sharpen_radius", 0, 8, 0.5)
        r = self._slider(f, r, "sharpen_percent", 0, 400, 5)
        r = self._slider(f, r, "sharpen_threshold", 0, 20, 1)
        ttk.Button(f, text="Reset fine-tune to defaults", command=self.reset_fine).grid(
            row=r, column=0, columnspan=3, sticky="w", pady=(10, 0))

    def _slider(self, f, r, name, lo, hi, step, label=None):
        var = self.vars[name]
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
            for k in INT_FIELDS:
                d[k] = int(round(d[k]))
            d["clahe_grid"] = max(1, d["clahe_grid"])
            d["comic_levels"] = min(5, max(2, d["comic_levels"]))
            s = core.Settings.from_dict(d)
        except (tk.TclError, ValueError):
            return None
        if s.width_in <= 0 or not 50 <= s.dpi <= 1200 or s.material not in self.materials:
            return None
        return s

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

    def reset_fine(self):
        d = core.Settings()
        self.suspend_traces = True
        for k in ("shadow_amount", "shadow_radius", "use_clahe", "clahe_clip", "clahe_grid",
                  "sharpen_radius", "sharpen_percent", "sharpen_threshold"):
            self.vars[k].set(getattr(d, k))
        self.suspend_traces = False
        self.on_change()

    def reset_comic(self):
        d = core.Settings()
        self.suspend_traces = True
        for k in self.vars:
            if k.startswith("comic_"):
                self.vars[k].set(getattr(d, k))
        self.suspend_traces = False
        self.on_change()

    def make_test_card(self):
        path, s = self.current_file(), self.settings()
        if path is None or s is None:
            messagebox.showinfo("No image", "Select an image first. The card uses its center.")
            return
        self.set_status("Making test card…")
        self.root.update_idletasks()
        try:
            out = core.save_test_card(path, self.out_dir.get(), s, self.fixes.get(path))
        except Exception as e:
            messagebox.showerror("Test card failed", str(e))
            return
        self.set_status(f"Saved {os.path.basename(out)}")
        os.startfile(out)

    def save_as_material(self):
        s = self.settings()
        if s is None:
            return
        name = simpledialog.askstring("Save as material",
                                      "Name for this material profile (e.g. maple, black_acrylic):",
                                      initialvalue=s.material, parent=self.root)
        if not name:
            return
        name = name.strip().lower().replace(" ", "_")
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
        sel = self.listbox.curselection()
        return self.files[sel[0]] if sel else None

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
        self.gen += 1
        if self.pending:
            self.root.after_cancel(self.pending)
        self.pending = self.root.after(DEBOUNCE_MS, self.start_preview)
        self.update_labels()

    def update_labels(self):
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
        path = self.current_file()
        src = self.src_cache.get(path) or (self.ai_cache.get(path) or (None,))[0]
        if src is None:
            self.height_lbl.configure(text="")
            return
        w, h = s.output_size(src.width, src.height)
        self.height_lbl.configure(text=f"→ {h / s.dpi:.2f} in tall")

    def start_preview(self):
        self.pending = None
        path, s = self.current_file(), self.settings()
        if path is None:
            self.views = {}
            self.redraw()
            self.info.configure(text="Add images to get started (Add images… or Add folder…).",
                                style="TLabel")
            return
        if s is None:
            return
        gen = self.gen
        strokes = list(self.fixes.get(path, []))
        if not s.remove_bg:
            msg = "Updating preview…"
        elif path not in self.ai_cache:
            msg = "Removing background…"
        elif self.refined.get(path, (None,))[0] != repr(strokes):
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
                if path not in self.ai_cache:
                    self._remember(self.ai_cache, path, core.ai_cutout(path), limit=4)
                rgb, a0 = self.ai_cache[path]
                key = repr(strokes)
                ref = self.refined.get(path)
                if ref is None or ref[0] != key:
                    alpha = core.refine_alpha(rgb, a0, strokes)
                    ref = (key, core.compose_on_white(rgb, alpha), alpha)
                    self._remember(self.refined, path, ref, limit=4)
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
            gray, result, mask = core.render(src, alpha, s)
            self.results.put(("preview", gen, path, s, src, gray, result, mask, cutout))
        except Exception as e:
            self.results.put(("error", gen, f"Could not process {os.path.basename(path)}:\n{e}"))

    def _poll(self):
        try:
            while True:
                msg = self.results.get_nowait()
                kind = msg[0]
                if kind == "preview":
                    _, gen, path, s, src, gray, result, mask, cutout = msg
                    if gen == self.gen:
                        self.last_mask = mask
                        self.show_preview(path, s, src, gray, result, cutout)
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
        look = self.materials.get(s.material, {}).get("look", ["#ffffff", "#000000"])
        # Black pixels in the output = laser fires. Paint them in the burn color.
        material_look = ImageOps.colorize(result.convert("L"), black=look[1], white=look[0])
        if s.mirror:
            material_look = ImageOps.mirror(material_look)   # show it as seen from the front
        self.views = {"Original": src, "Adjusted": gray, "Engraving": result,
                      "Material look": material_look}
        self.view_factor = {}
        if cutout:
            self.views["Cut-out"], self.view_factor["Cut-out"] = cutout
        else:
            self.views["Cut-out"] = src      # nothing removed; just show the photo
        self.update_labels()
        w, h = result.size
        eff = src.width / s.width_in
        text = (f"Output {w} × {h} px  ·  {s.width_in:g} × {h / s.dpi:.2f} in @ {s.dpi} DPI  ·  "
                f"source {src.width} × {src.height} px")
        if eff < s.dpi:
            text += f"  ·  ⚠ source is only {eff:.0f} DPI at this size; it will be upscaled (softer detail)"
            self.info.configure(text=text, style="Warn.TLabel")
        else:
            self.info.configure(text=text, style="TLabel")
        self.set_status("")
        self.redraw()

    def redraw(self):
        self.canvas.delete("all")
        img = self.views.get(self.view.get())
        if img is None:
            return
        cw, ch = max(self.canvas.winfo_width(), 50), max(self.canvas.winfo_height(), 50)
        z = self.zoom.get()
        if z == "Fit":
            scale = min(cw / img.width, ch / img.height, 1.0)
        else:
            scale = int(z[:-1]) / 100
        dw, dh = max(1, round(img.width * scale)), max(1, round(img.height * scale))
        disp = img.convert("L") if img.mode == "1" else img
        if scale < 1:
            # Averaging (not nearest) so a shrunk dither shows its true tone, not moiré.
            disp = disp.resize((dw, dh), Image.Resampling.BOX)
        elif scale > 1:
            disp = disp.resize((dw, dh), Image.Resampling.NEAREST)
        self.photo = ImageTk.PhotoImage(disp)
        x0, y0 = max(0, (cw - dw) // 2), max(0, (ch - dh) // 2)
        self.canvas.create_image(x0, y0, image=self.photo, anchor="nw")
        self.canvas.configure(scrollregion=(0, 0, max(cw, dw), max(ch, dh)))
        self.disp = (scale, x0, y0, self.view_factor.get(self.view.get(), 1.0))
        if self.view.get() == "Cut-out" and self.vars["remove_bg"].get():
            self._draw_strokes()

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

    def update_fixbar(self):
        on = self.vars["remove_bg"].get()
        if on and not self.fixbar.winfo_ismapped():
            self.fixbar.pack(fill="x", pady=(0, 4), before=self.canvas_frame)
        elif not on and self.fixbar.winfo_ismapped():
            self.fixbar.pack_forget()
            self.tool.set("pan")
        n = len(self.fixes.get(self.current_file(), []))
        self.fix_hint.configure(text=f"{n} fix{'es' if n != 1 else ''} on this image ⓘ")
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

    def on_press(self, e):
        if not self._drawing():
            self.canvas.scan_mark(e.x, e.y)
            return
        cx, cy = self.canvas.canvasx(e.x), self.canvas.canvasy(e.y)
        kind = self.tool.get()
        w = self.brush.get()
        item = self.canvas.create_line(cx, cy, cx + 0.1, cy + 0.1, fill=STROKE_COLORS[kind], width=w,
                                       capstyle="round", joinstyle="round", stipple="gray50")
        self.live = (kind, [cx, cy], [self._to_src(cx, cy)], item)

    def on_drag(self, e):
        if not self.live:
            if not self._drawing():
                self.canvas.scan_dragto(e.x, e.y, gain=1)
            return
        cx, cy = self.canvas.canvasx(e.x), self.canvas.canvasy(e.y)
        kind, cpts, spts, item = self.live
        if abs(cx - cpts[-2]) + abs(cy - cpts[-1]) < 3:
            return
        cpts += [cx, cy]
        spts.append(self._to_src(cx, cy))
        self.canvas.coords(item, *cpts)

    def on_release(self, e):
        if not self.live:
            return
        kind, _, spts, _ = self.live
        self.live = None
        scale, _, _, factor = self.disp
        radius = round(self.brush.get() / 2 / scale * factor, 1)
        stroke = (kind, radius, [(round(x, 1), round(y, 1)) for x, y in spts])
        self.fixes.setdefault(self.current_file(), []).append(stroke)
        self._fixes_changed()

    def undo_stroke(self):
        strokes = self.fixes.get(self.current_file())
        if strokes:
            strokes.pop()
            self._fixes_changed()

    def clear_fixes(self):
        path = self.current_file()
        if self.fixes.get(path) and messagebox.askyesno(
                "Clear fixes", "Remove all background fixes on this image?"):
            self.fixes.pop(path, None)
            self._fixes_changed()

    def _fixes_changed(self):
        self._save_fixes()
        self.update_fixbar()
        self.on_change()

    def _draw_strokes(self):
        """Overlays saved strokes on the Cut-out view so you can see what you've marked."""
        scale, x0, y0, factor = self.disp
        for kind, radius, pts in self.fixes.get(self.current_file(), []):
            c = [v for x, y in pts for v in (x0 + x / factor * scale, y0 + y / factor * scale)]
            if len(c) == 2:
                c += [c[0] + 0.1, c[1] + 0.1]
            self.canvas.create_line(*c, fill=STROKE_COLORS[kind], width=max(2, 2 * radius / factor * scale),
                                    capstyle="round", joinstyle="round", stipple="gray25")

    def on_wheel(self, e):
        i = ZOOMS.index(self.zoom.get())
        i = min(i + 1, len(ZOOMS) - 1) if e.delta > 0 else max(i - 1, 0)
        self.zoom.set(ZOOMS[i])
        self.redraw()

    # ---------------------------------------------------------------- saving

    def save_current(self):
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

    def _load_state(self):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def on_close(self):
        s = self.settings()
        state = {"settings": (s or core.Settings()).to_dict(), "out_dir": self.out_dir.get(),
                 "comparison": self.comparison.get(), "files": self.files,
                 "maximized": self.root.state() == "zoomed"}
        if self.root.state() == "normal":
            state["geometry"] = self.root.geometry()
        try:
            os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
        except OSError:
            pass
        self.root.destroy()

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
            core.render(*core.load_source(path, s.remove_bg, strokes), s)
            lines.append(f"OK    {label}  {time.time() - t:.1f} s")
        except Exception as e:
            ok = False
            lines.append(f"FAIL  {label}: {e!r}")
    lines.append(f"fast stucki (numba): {core.FAST_STUCKI}")
    ok = ok and core.FAST_STUCKI
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines + ["PASS" if ok else "FAIL"]) + "\n")
    return 0 if ok else 1


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--selftest":
        sys.exit(selftest(sys.argv[2]))
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)   # crisp text on high-DPI screens
    except Exception:
        pass
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
