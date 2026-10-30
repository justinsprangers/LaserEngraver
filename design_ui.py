"""
design_ui.py: Design mode (the layout editor) for engrave_app.App.

App owns the window and the photo pipeline; this controller adds the Layers panel, the Design
and Layer tabs, the drag/resize/rotate handles on the Design view, undo, and project files.
Image layers reuse the app's normal tabs and cut-out fix tools: selecting one loads its
settings into the app's controls, and edits flow back into the layer (sync_from_vars).
"""

import copy
import math
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import numpy as np
from PIL import Image, ImageOps

import design as D
import engrave_core as core
import vecfile as V
import vectors
from ui_kit import P, ScrollTab, section
from ui_kit import px as ui_px   # (named so it can't clash with local x/y variables)

HANDLE = 6            # half-size of the corner handles, screen px
ROT_OFFSET = 28       # rotation handle distance above the box, screen px
SNAP_PX = 6           # snap edges/centers within this many screen px (hold Alt to turn off)
ACCENT = "#2563eb"
CANVAS_PRESETS = {"4 × 4 in (coaster)": (4, 4), "6 × 4 in": (6, 4), "8 × 8 in": (8, 8),
                  "8 × 10 in": (8, 10), "12 × 12 in": (12, 12)}
ASPECT_LOCKED = ("image", "logo", "svg")
ART_TYPES = [("Images", " ".join("*" + e for e in core.IMAGE_EXTENSIONS)), ("All files", "*.*")]
VECTOR_TYPES = [("Vector files", " ".join("*" + e for e in V.EXTENSIONS)), ("SVG", "*.svg"),
                ("PDF / Illustrator", "*.pdf *.ai"), ("DXF (CAD)", "*.dxf"), ("All files", "*.*")]
OP_LABELS = {"engrave": "Engrave", "score": "Score", "cut": "Cut"}
LINE_COLORS = {"cut": "#ef4444", "score": "#3b82f6"}    # preview colors for cut / score lines
SPLIT_WARN = 150         # Break apart asks first above this many pieces


class DesignController:
    def __init__(self, app, tooltip_cls):
        self.app, self.Tooltip = app, tooltip_cls
        self.design = D.new_design(app.vars["material"].get() if app.vars["material"].get()
                                   in app.materials else next(iter(app.materials)))
        self.renderer = D.Renderer()                 # the real engraving (Design view, export)
        self.draft = D.Renderer(draft=True)          # fast Layout view
        self.executor = ThreadPoolExecutor(max_workers=1)     # composites run off the UI thread
        self.gens = {"Layout": 0, "Design": 0}       # per-view render generation (drop stale results)
        self.cut_preview = {"Layout": {}, "Design": {}}   # {cut/score: lines (canvas inches)} per view
        self.stale = {"Layout": True, "Design": True}
        self.active = False
        self.selected = None          # layer id
        self.project_path = None
        self.dirty = False
        self.undo_stack, self.redo_stack = [], []
        self.last_snapshot = None
        self._commit_job = None
        self.drag = None
        self.handles = {}
        self._last_live = 0.0
        self._guard = False           # stops field <-> layer feedback loops
        self.fields = {}
        self.photo_snapshot = None    # photo-mode settings, restored when leaving Design mode
        self._saved_scale_from = "corner"

    # ================================================================ building

    def build(self, top, left_holder_before, nb, bottom_hide):
        """Creates (unpacked) widgets. Called once from App._build."""
        # --- project buttons (replace Add images / Add folder / Remove)
        self.toolbar = ttk.Frame(top)
        for text, cmd, tip in (("New", self.new_design, "Start a new, empty design"),
                               ("Open…", self.open_project, "Open a project (.lep)"),
                               ("Save", self.save_project, "Save the project (Ctrl+S)"),
                               ("Save as…", lambda: self.save_project(as_new=True), "Save the project under a new name")):
            b = ttk.Button(self.toolbar, text=text, width=len(text) + 2, command=cmd)
            b.pack(side="left", padx=(0, 4))
            self.Tooltip(b, tip)
        ttk.Label(self.toolbar, text="project", style="Hint.TLabel").pack(side="left")
        self.bottom_hide = bottom_hide

        # --- layers panel (replaces the image list)
        self.layers_frame = ttk.LabelFrame(left_holder_before.master, text=" Layers (top first) ",
                                           padding=(8, 4, 8, 8))
        self.left_before = left_holder_before
        lb_row = ttk.Frame(self.layers_frame)
        lb_row.pack(fill="both", expand=True)
        self.layer_list = tk.Listbox(lb_row, height=7, activestyle="none", exportselection=False)
        sb = ttk.Scrollbar(lb_row, command=self.layer_list.yview)
        self.layer_list.configure(yscrollcommand=sb.set)
        self.layer_list.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.layer_list.bind("<<ListboxSelect>>", self._on_list_select)
        btns = ttk.Frame(self.layers_frame)
        btns.pack(fill="x", pady=(4, 0))
        add = ttk.Menubutton(btns, text="Add")
        menu = tk.Menu(add, tearoff=False)
        menu.add_command(label="Photo…", command=lambda: self.add_art("image"))
        menu.add_command(label="Logo / clip art (PNG)…", command=lambda: self.add_art("logo"))
        menu.add_command(label="Vector file (SVG, PDF, AI, DXF)…", command=self.add_vector)
        menu.add_separator()
        menu.add_command(label="Text", command=lambda: self.add_simple("text"))
        menu.add_command(label="Arc text", command=lambda: self.add_simple("arctext"))
        menu.add_separator()
        menu.add_command(label="Rectangle", command=lambda: self.add_simple("shape", shape="rect"))
        menu.add_command(label="Circle / ellipse", command=lambda: self.add_simple("shape", shape="ellipse"))
        menu.add_command(label="Line", command=lambda: self.add_simple("shape", shape="line", h_in=0.05))
        add["menu"] = menu
        add.pack(side="left")
        for text, cmd, tip in (("▲", lambda: self.move_layer(+1), "Bring forward"),
                               ("▼", lambda: self.move_layer(-1), "Send backward"),
                               ("Copy", self.duplicate_layer, "Duplicate (Ctrl+D)"),
                               ("Hide", self.toggle_visible, "Hide / show"),
                               ("Delete", self.delete_layer, "Delete (Del)")):
            b = ttk.Button(btns, text=text, style="Tool.TButton", command=cmd)
            b.pack(side="left", padx=(4, 0))
            self.Tooltip(b, tip)

        # --- Design + Layer tabs (added to the notebook while active)
        self.nb = nb
        self.design_tab = ScrollTab(nb)          # tabs stay pinned; pages scroll on their own
        self.layer_tab = ScrollTab(nb)
        self.scale_from = tk.StringVar(value=self._saved_scale_from)
        self._build_design_tab(self.design_tab.inner)

        # Keys are handled by App's shortcut dispatcher (customizable, see shortcuts.py).

    def _build_design_tab(self, f):
        app = self.app
        f.columnconfigure(0, weight=1)
        # --- Canvas
        cv = section(f, "Canvas (your material)", row=0)
        self.cw_var = tk.DoubleVar(value=self.design["canvas"]["width_in"])
        self.ch_var = tk.DoubleVar(value=self.design["canvas"]["height_in"])
        for r, (label, var) in enumerate((("Width (in)", self.cw_var), ("Height (in)", self.ch_var))):
            ttk.Label(cv, text=label).grid(row=r, column=0, sticky="w", pady=3, padx=(0, 10))
            ttk.Spinbox(cv, textvariable=var, from_=0.5, to=48, increment=0.25, width=8).grid(row=r, column=1, sticky="w")
            var.trace_add("write", lambda *_: self._canvas_size_changed())
        ttk.Label(cv, text="Preset").grid(row=2, column=0, sticky="w", pady=3)
        pc = ttk.Combobox(cv, values=list(CANVAS_PRESETS), state="readonly", width=18)
        pc.grid(row=2, column=1, sticky="w")
        pc.bind("<<ComboboxSelected>>", lambda e: self._apply_canvas_preset(pc.get()))
        ttk.Label(cv, text="Resize from").grid(row=3, column=0, sticky="w", pady=3)
        rf = ttk.Frame(cv)
        rf.grid(row=3, column=1, columnspan=2, sticky="w")
        for text, val in (("Opposite corner", "corner"), ("Center", "center")):
            rb = ttk.Radiobutton(rf, text=text, value=val, variable=self.scale_from)
            rb.pack(side="left", padx=(0, 10))
            self.Tooltip(rb, "What stays put when you drag a corner handle.\n"
                             "Hold Ctrl while dragging to use the other one.")

        # --- Material & output (whole design)
        mo = section(f, "Material & output", row=1)
        ttk.Label(mo, text="Material").grid(row=0, column=0, sticky="w", pady=3, padx=(0, 10))
        self.design_mat = ttk.Combobox(mo, textvariable=app.vars["material"], state="readonly",
                                       values=sorted(app.materials), width=18)
        self.design_mat.grid(row=0, column=1, sticky="w")
        ttk.Label(mo, text="DPI").grid(row=1, column=0, sticky="w", pady=3)
        ttk.Combobox(mo, textvariable=app.vars["dpi"], values=("254", "318", "423", "508"), width=8).grid(
            row=1, column=1, sticky="w")
        ttk.Checkbutton(mo, text="Invert (laser makes light marks)", variable=app.vars["invert"]).grid(
            row=2, column=0, columnspan=3, sticky="w", pady=(4, 0))
        ttk.Checkbutton(mo, text="Mirror (engrave from the back)", variable=app.vars["mirror"]).grid(
            row=3, column=0, columnspan=3, sticky="w", pady=(2, 0))

        # --- Export
        f_ = section(f, "Export", row=2)
        ttk.Label(f_, text="Cut / score files").grid(row=0, column=0, sticky="w", pady=3, padx=(0, 10))
        self.cut_fmt = tk.StringVar(value=self._fmt_label(self.design.get("cut_format", "svg")))
        cf = ttk.Combobox(f_, textvariable=self.cut_fmt, state="readonly", width=12,
                          values=list(self.FMT_LABELS.values()))
        cf.grid(row=0, column=1, sticky="w")
        cf.bind("<<ComboboxSelected>>", lambda e: self._set_design_opt(
            "cut_format", next(k for k, v in self.FMT_LABELS.items() if v == self.cut_fmt.get())))
        self.Tooltip(cf, "One vector file per job, in mm: _cut (red lines) and _score (blue lines).\n"
                         "SVG: xTool, LaserGRBL, LightBurn, Trotec Ruby. DXF: Trotec Ruby, LightBurn, xTool.\n"
                         "Separate files suit LaserGRBL, which runs one power setting per file.")
        self.frame_var = tk.BooleanVar(value=self.design.get("frame", True))
        cb = ttk.Checkbutton(f_, text="Add alignment frame ⓘ", variable=self.frame_var,
                             command=lambda: self._set_design_opt("frame", self.frame_var.get()))
        cb.grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 0))
        self.Tooltip(cb, "Adds a green rectangle the size of the canvas to the cut and score files.\n"
                         "The engraving PNG is the same size: line their corners up in your\n"
                         "laser software, then delete or disable the green frame.")
        ttk.Label(f_, text="All-in-one file").grid(row=2, column=0, sticky="w", pady=3)
        self.combined_var = tk.StringVar(value=self._comb_label(self.design.get("combined", "none")))
        cc = ttk.Combobox(f_, textvariable=self.combined_var, state="readonly", width=12,
                          values=list(self.COMB_LABELS.values()))
        cc.grid(row=2, column=1, sticky="w")
        cc.bind("<<ComboboxSelected>>", lambda e: self._set_design_opt(
            "combined", next(k for k, v in self.COMB_LABELS.items() if v == self.combined_var.get())))
        self.Tooltip(cc, "One file with the engraving image and every score / cut line, already\n"
                         "lined up (_combined.svg / .pdf): open it and assign the colors.\n"
                         "PDF: Trotec Ruby, LightBurn, xTool. SVG: LightBurn, xTool.\n"
                         "Not tested on every program: if the image doesn't come in, use the\n"
                         "PNG + cut / score files instead.")
        cb = ttk.Checkbutton(f_, text="Cut outline around everything engraved", variable=app.vars["outline"])
        cb.grid(row=3, column=0, columnspan=3, sticky="w", pady=(2, 0))
        ttk.Label(f_, text="Outline offset (in)").grid(row=4, column=0, sticky="w", pady=3)
        ttk.Spinbox(f_, textvariable=app.vars["outline_offset_in"], from_=0, to=1, increment=0.02,
                    width=8).grid(row=4, column=1, sticky="w")
        ttk.Label(f_, text="Test copy size (%)").grid(row=5, column=0, sticky="w", pady=(8, 3))
        self.test_pct = tk.StringVar(value=f"{self.design.get('test_pct', 25):g}")
        sp = ttk.Spinbox(f_, textvariable=self.test_pct, from_=5, to=100, increment=5, width=8)
        sp.grid(row=5, column=1, sticky="w", pady=(8, 3))
        self.test_size = ttk.Label(f_, style="Hint.TLabel")
        self.test_size.grid(row=6, column=0, columnspan=2, sticky="w")
        b = ttk.Button(f_, text="Save test copy", command=self.export_test)
        b.grid(row=7, column=0, columnspan=2, sticky="w", pady=(2, 0))
        self.Tooltip(b, "Saves a shrunken copy of the whole design (same DPI, material and\n"
                        "settings, so it burns like the real job) to try on scrap first.\n"
                        "Files end in _test.")
        self.test_pct.trace_add("write", lambda *a: self._test_pct_changed())
        self._update_test_size()
        r = 3
        ttk.Label(f, text="Material, DPI, invert and mirror apply to the whole design. Each photo "
                          "layer has its own style and settings: select it and use the Basic / "
                          "Photo / Comic tabs.\n\n"
                          "Arrange on the Layout view (fast; photos shown unprocessed), check the "
                          "result on the Design view (the real engraving).\n\n"
                          "Drag to move: edges and centers snap to other items and the canvas "
                          "(hold Alt to move freely). Ctrl+click picks the item underneath. "
                          "Corner squares resize, the circle on top "
                          "rotates (Shift snaps to 15°). Arrow keys nudge (Shift = bigger steps). "
                          "Mouse wheel zooms, including out past the canvas. All keys: ⌨ Shortcuts "
                          "(top right).",
                  style="Hint.TLabel", justify="left", wraplength=320).grid(
            row=r, column=0, columnspan=3, sticky="w", pady=(2, 0))

    FMT_LABELS = {"none": "None", "svg": "SVG", "dxf": "DXF", "svg+dxf": "SVG + DXF"}
    COMB_LABELS = {"none": "None", "svg": "SVG", "pdf": "PDF", "svg+pdf": "SVG + PDF"}

    def _fmt_label(self, key):
        return self.FMT_LABELS.get(key, "SVG")

    def _comb_label(self, key):
        key = {True: "svg", False: "none"}.get(key, key)        # (a checkbox before v1.11)
        return self.COMB_LABELS.get(key, "None")

    def _set_design_opt(self, key, value):
        if self.design.get(key) != value:
            self.design[key] = value
            self.changed(recomposite=False)

    # ================================================================ mode switch

    def enter(self):
        app = self.app
        self.active = True
        self.photo_snapshot = app.settings()
        app.set_estimate(None, self.design["dpi"], 0.0)   # the estimate now covers the design
        app.file_toolbar.pack_forget()
        self.toolbar.pack(side="left", after=app.mode_frame)
        app.images_frame.pack_forget()
        self.layers_frame.pack(fill="both", expand=False, before=self.left_before)
        app.photo_only.grid_remove()               # layers have their own crop / rotation / size
        for w in app.size_rows[2:]:                # (height row: layers are sized by width)
            w.grid_remove()
        self.nb.insert(0, self.layer_tab, text="Layer")
        self.nb.insert(0, self.design_tab, text="Design")
        for w in self.bottom_hide:
            w.pack_forget()
        app.save_btn.configure(text="Save engraving")
        app.design_seg.pack(side="left", padx=(0, 12), before=app.view_seg)
        self._load_globals_into_vars()
        if self.last_snapshot is None:
            self.last_snapshot = copy.deepcopy(self.design)
        layer = self.layer_by_id(self.selected) or (self.design["layers"][-1] if self.design["layers"] else None)
        self.select(layer["id"] if layer else None)
        app.view.set("Layout")
        app.zoom_fit()
        self.refresh_list()
        self.update_title()
        self.stale = {"Layout": True, "Design": True}
        self.request_composite()
        self.nb.select(self.design_tab if not self.design["layers"] else self.layer_tab)
        app.scaler.refresh()                       # widgets just packed with base sizes

    def leave(self):
        app = self.app
        self.active = False
        app.set_estimate(None, 0, 0.0)
        self.toolbar.pack_forget()
        app.file_toolbar.pack(side="left", after=app.mode_frame)
        self.layers_frame.pack_forget()
        app.images_frame.pack(fill="both", expand=False, before=self.left_before)
        app.photo_only.grid()
        for w in app.size_rows[2:]:
            w.grid()
        self.nb.forget(self.design_tab)
        self.nb.forget(self.layer_tab)
        for tab in (app.basic_tab, app.fine_tab, app.comic_tab):
            self.nb.add(tab)
        self.nb.select(app.basic_tab)
        for w in self.bottom_hide:
            w.pack(side="left", padx=6)
        app.save_btn.configure(text="Save this engraving")
        app.design_seg.pack_forget()
        if app.view.get() in ("Layout", "Design"):
            app.view.set("Engraving")
        if self.photo_snapshot:
            app.load_settings_into_vars(self.photo_snapshot)
        app.views.pop("Design", None)
        app.views.pop("Layout", None)
        self.update_title()
        app.scaler.refresh()
        app.on_change()

    # ================================================================ helpers

    def layer_by_id(self, lid):
        return next((l for l in self.design["layers"] if l["id"] == lid), None)

    def sel_layer(self):
        return self.layer_by_id(self.selected)

    def selected_image(self):
        l = self.sel_layer()
        return l if l and l["type"] == "image" else None

    def current_src(self):
        l = self.selected_image()
        return l["src"] if l and l.get("src") and os.path.exists(l["src"]) else None

    def strokes(self):
        l = self.selected_image()
        return l.setdefault("strokes", []) if l else []

    def _label(self, layer):
        kind = D.TYPE_LABELS[layer["type"]]
        if layer["type"] in ("text", "arctext"):
            detail = layer["text"].splitlines()[0][:24] if layer["text"] else ""
        elif layer["type"] == "shape":
            detail = {"rect": "rectangle", "ellipse": "ellipse", "line": "line"}[layer.get("shape", "rect")]
        else:
            # The file's own name (project files store images under content hashes).
            named = layer.get("name") not in (None, "", kind)
            detail = layer["name"] if named else (os.path.basename(layer.get("src", "")) or "(no file)")
            if layer["type"] == "svg" and layer.get("parts") is not None:
                n = len(layer["parts"])
                detail += f" ({n} piece{'s' if n != 1 else ''})"
        op = self._ops_text(layer)
        return (f"{kind}: {detail}" + (f"  [{op}]" if op else "")
                + ("" if layer.get("visible", True) else "  (hidden)"))

    def _ops_text(self, layer):
        """'Cut', 'Score', 'Engrave + Cut'... (empty for plain engraving)."""
        if layer["type"] != "svg":
            op = layer.get("op", "engrave")
            return "" if op == "engrave" else OP_LABELS[op]
        try:
            doc = V.load(layer["src"])
            jobs = D.vector_jobs(layer, doc)
            used = {jobs.get(k) for k, _, _ in V.groups(doc, layer.get("parts"))}
        except Exception:
            return ""
        names = [OP_LABELS[o] for o in ("engrave", "score", "cut") if o in used]
        return "" if names in ([], ["Engrave"]) else " + ".join(names)

    def refresh_list(self):
        lb = self.layer_list
        lb.delete(0, "end")
        layers = list(reversed(self.design["layers"]))
        for l in layers:
            lb.insert("end", self._label(l))
        if self.selected:
            for i, l in enumerate(layers):
                if l["id"] == self.selected:
                    lb.selection_set(i)
                    lb.see(i)
        title = getattr(self, "title_label", None)
        if title is not None and title.winfo_exists() and self.sel_layer():
            title.configure(text=self._label(self.sel_layer()))

    def update_title(self):
        app = self.app
        base = f"Laser Engraver {core.VERSION}"
        if self.active:
            name = os.path.basename(self.project_path) if self.project_path else "Untitled design"
            base += f"  ·  {name}{' *' if self.dirty else ''}"
        app.root.title(base)

    def _load_globals_into_vars(self):
        app = self.app
        app.suspend_traces = True
        try:
            d = self.design
            if d["material"] in app.materials:
                app.vars["material"].set(d["material"])
            app.vars["dpi"].set(str(d["dpi"]))
            for k in ("invert", "mirror", "outline"):
                app.vars[k].set(bool(d.get(k)))
            app.vars["outline_offset_in"].set(d.get("outline_offset_in", 0.1))
            self._guard = True
            self.cw_var.set(d["canvas"]["width_in"])
            self.ch_var.set(d["canvas"]["height_in"])
            self.cut_fmt.set(self._fmt_label(d.get("cut_format", "svg")))
            self.frame_var.set(d.get("frame", True))
            self.combined_var.set(self._comb_label(d.get("combined", "none")))
            self.test_pct.set(f"{d.get('test_pct', 25):g}")
        finally:
            self._guard = False
            app.suspend_traces = False

    # ================================================================ selection

    def _on_list_select(self, _):
        sel = self.layer_list.curselection()
        if sel:
            layers = list(reversed(self.design["layers"]))
            self.select(layers[sel[0]]["id"])

    def select(self, lid):
        app = self.app
        self.selected = lid
        layer = self.sel_layer()
        image_tabs = (app.basic_tab, app.fine_tab, app.comic_tab)
        if layer and layer["type"] == "image":
            for tab in image_tabs:
                self.nb.add(tab)
            d = dict(layer["settings"])
            d.update({f: self.design[f] for f in D.GLOBAL_FIELDS if f in self.design})
            d["width_in"] = layer["w_in"]
            app.load_settings_into_vars(core.Settings.from_dict(d))
        else:
            for tab in image_tabs:
                self.nb.hide(tab)
            if app.tool.get() != "pan":
                app.tool.set("pan")
        self._build_layer_tab()
        self.refresh_list()
        app.on_change()            # refreshes the per-layer views and fix toolbar
        app.request_redraw()

    # ================================================================ layer tab

    def _build_layer_tab(self):
        f = self.layer_tab.inner
        for w in f.winfo_children():
            w.destroy()
        self.fields = {}
        f.columnconfigure(1, weight=1)
        layer = self.sel_layer()
        if not layer:
            ttk.Label(f, text="No layer selected.\n\nUse Add above to add a photo, logo, vector\n"
                              "file (SVG, PDF, DXF), text, arc text or shape,\nor click an item "
                              "on the Design view.",
                      style="Hint.TLabel", justify="left").grid(row=0, column=0, sticky="w")
            return
        self.title_label = ttk.Label(f, text=self._label(layer), style="Title.TLabel", wraplength=320)
        self.title_label.grid(row=0, column=0, columnspan=3, sticky="w")
        if layer["type"] != "svg":                 # vector files choose a job per color instead
            out = ttk.Frame(f)
            out.grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 2))
            ttk.Label(out, text="Output:").pack(side="left")
            op_var = tk.StringVar(value=layer.get("op", "engrave"))
            for text, val in (("Engrave", "engrave"), ("Score", "score"), ("Cut ⓘ", "cut")):
                rb = ttk.Radiobutton(out, text=text, value=val, variable=op_var)
                rb.pack(side="left", padx=(6, 0))
            self.Tooltip(rb, "Engrave: burned into the engraving image.\n"
                             "Score: a light vector line (blue, _score file). Cut: a vector line\n"
                             "through the material (red, _cut file). Shapes follow their outline,\n"
                             "text its letter outlines, logos and photos their traced outline.")
            op_var.trace_add("write", lambda *_: self._guard or self._set_op(op_var.get()))
        r = 2
        r = self._num(f, r, "X center (in)", "x_in", 0.05)
        r = self._num(f, r, "Y center (in)", "y_in", 0.05)
        if layer["type"] not in ("text", "arctext"):
            r = self._num(f, r, "Width (in)", "w_in", 0.05, lo=0.02)
            r = self._num(f, r, "Height (in)", "h_in", 0.05, lo=0.02)
        r = self._num(f, r, "Rotation (°)", "rotation", 1, lo=-360, hi=360)
        t = layer["type"]
        ttk.Separator(f).grid(row=r, column=0, columnspan=3, sticky="ew", pady=6)
        r += 1
        if t == "image":
            ttk.Label(f, text="Crop (% from each edge)").grid(row=r, column=0, columnspan=3, sticky="w")
            r += 1
            for i, side in enumerate(("Left", "Top", "Right", "Bottom")):
                r = self._crop(f, r, side, i)
            ttk.Label(f, text="Style, tone and background removal for this photo\n"
                              "are on the Basic / Photo / Comic tabs.",
                      style="Hint.TLabel", justify="left").grid(row=r, column=0, columnspan=3, sticky="w", pady=(6, 0))
        elif t == "svg":
            r = self._vector_panel(f, r, layer)
        elif t == "logo":
            r = self._num(f, r, "Dark threshold (0-255)", "threshold", 5, lo=0, hi=255, integer=True)
            r = self._check(f, r, "Engrave the light parts instead", "invert_art")
            ttk.Label(f, text="Opaque pixels darker than the threshold are engraved;\n"
                              "transparent areas let lower layers show through.",
                      style="Hint.TLabel", justify="left").grid(row=r, column=0, columnspan=3, sticky="w", pady=(6, 0))
        elif t in ("text", "arctext"):
            r = self._text_box(f, r, multiline=(t == "text"))
            r = self._font(f, r)
            r = self._num(f, r, "Size (pt)", "size_pt", 1, lo=4, hi=400)
            if t == "text":
                r = self._choice(f, r, "Align", "align", ("left", "center", "right"))
                r = self._num(f, r, "Line spacing", "line_spacing", 0.05, lo=0.6, hi=3)
            else:
                r = self._num(f, r, "Circle radius (in)", "radius_in", 0.05, lo=0.1)
                r = self._choice(f, r, "Position", "arc", ("top", "bottom"))
                r = self._num(f, r, "Letter spacing", "tracking", 0.02, lo=-0.3, hi=2)
            r = self._check(f, r, "Outline only", "outline")
            r = self._num(f, r, "Outline width (mm)", "stroke_mm", 0.1, lo=0.1, hi=5)
        elif t == "shape":
            r = self._choice(f, r, "Shape", "shape", ("rect", "ellipse", "line"))
            r = self._check(f, r, "Filled", "fill")
            r = self._num(f, r, "Line width (mm)", "stroke_mm", 0.1, lo=0.1, hi=20)
            r = self._num(f, r, "Corner radius (in)", "corner_in", 0.05, lo=0, hi=10)
        if t == "svg" or layer.get("op", "engrave") != "engrave":
            r = self._line_options(f, r, layer)
        self.app.scaler.refresh(f)                 # new widgets: scale their spacing

    def _set_op(self, op):
        self._set("op", op)
        self.app.root.after_idle(self._build_layer_tab)      # show / hide the line options

    def _line_options(self, f, r, layer):
        """Weld / outer outline / offset for the layer's score and cut lines."""
        ttk.Separator(f).grid(row=r, column=0, columnspan=3, sticky="ew", pady=6)
        ttk.Label(f, text="Score / cut lines", style="Section.TLabel").grid(row=r + 1, column=0, columnspan=3, sticky="w")
        r += 2
        if layer["type"] != "shape":
            r = self._check(f, r, "Weld overlapping shapes ⓘ", "weld")
            self.Tooltip(f.grid_slaves(row=r - 1, column=0)[0],
                         "Merges shapes that overlap (touching letters, stacked shapes)\n"
                         "into one outline, so the laser doesn't cut through the middle.")
        r = self._check(f, r, "Outer outline only (no holes) ⓘ", "outer")
        self.Tooltip(f.grid_slaves(row=r - 1, column=0)[0],
                     "Keeps only the outside edge: a sticker / cut-out outline around\n"
                     "the whole thing, with the insides of letters left alone.")
        r = self._num(f, r, "Offset (mm) ⓘ", "offset_mm", 0.5, lo=-50, hi=50)
        self.Tooltip(f.grid_slaves(row=r - 1, column=0)[0],
                     "Pushes the lines out (+) or in (−): a margin around text or a logo,\n"
                     "or kerf compensation (about half the laser's cut width).\n"
                     "Offset and Outer outline also weld. Open lines aren't offset.")
        return r

    # ---------------------------------------------------------------- vector files

    def _vector_panel(self, f, r, layer):
        """File info, size buttons, a job per color, engraved line width, split buttons."""
        try:
            doc = V.load(layer["src"])
        except Exception as e:
            ttk.Label(f, text=f"Couldn't read the file:\n{e}", style="Warn.TLabel", wraplength=320,
                      justify="left").grid(row=r, column=0, columnspan=3, sticky="w")
            return r + 1
        n = len(V.part_ids(doc, layer.get("parts")))
        w, h = D.vector_size(layer, doc)
        info = (f"{os.path.basename(layer['src'])}\n{n} shape{'s' if n != 1 else ''}, "
                f"{w * 25.4:.1f} × {h * 25.4:.1f} mm at 1:1"
                + (f" (now {layer['w_in'] / w * 100:.0f}%)" if abs(layer["w_in"] / w - 1) > 0.001 else ""))
        ttk.Label(f, text=info, style="Hint.TLabel", justify="left", wraplength=320).grid(
            row=r, column=0, columnspan=3, sticky="w")
        r += 1
        for note in doc["notes"] + ([] if doc["sized"] else
                                    ["The file doesn't state a physical size: set the width here."]):
            ttk.Label(f, text="• " + note, style="Warn.TLabel", wraplength=320, justify="left").grid(
                row=r, column=0, columnspan=3, sticky="w")
            r += 1
        row = ttk.Frame(f)
        row.grid(row=r, column=0, columnspan=3, sticky="w", pady=(4, 2))
        b = ttk.Button(row, text="1:1 size", command=self.vector_real_size)
        b.pack(side="left")
        self.Tooltip(b, "Back to the size saved in the file (no rotation).")
        if layer.get("parts") is None:
            b = ttk.Button(row, text="Canvas = file page", command=self.vector_canvas_to_page)
            b.pack(side="left", padx=(4, 0))
            self.Tooltip(b, "Makes the canvas the size of the file's page and puts the drawing\n"
                            "where it sits on that page (1:1), e.g. for a ready-made cut sheet.")
        r += 1
        ttk.Label(f, text="What the laser does with each color ⓘ", style="Section.TLabel").grid(
            row=r, column=0, columnspan=3, sticky="w", pady=(8, 2))
        self.Tooltip(f.grid_slaves(row=r, column=0)[0],
                     "Engrave: burned into the image. Score: a light line along it (blue).\n"
                     "Cut: cut along it (red). Blank: not burned, and hides anything under it.\n"
                     "Ignore: left out. Fill colors score / cut along their outline.\n"
                     "Defaults: red lines cut, blue lines score, hairlines (< 0.1 mm) cut,\n"
                     "dark fills and thick lines engrave, light colors are blank.")
        r += 1
        jobs = D.vector_jobs(layer, doc)
        labels = [V.JOB_LABELS[j] for j in V.JOBS]
        for key, count, label in V.groups(doc, layer.get("parts")):
            line = ttk.Frame(f)
            line.grid(row=r, column=0, columnspan=3, sticky="ew", pady=1)
            sw = tk.Canvas(line, width=ui_px(22), height=ui_px(14), highlightthickness=0, bg=P["bg"])
            color = key[2:]
            if key.startswith("s:"):
                sw.create_line(ui_px(2), ui_px(7), ui_px(20), ui_px(7), fill=color, width=ui_px(3))
            else:
                sw.create_rectangle(ui_px(2), ui_px(1), ui_px(20), ui_px(13), fill=color, outline=P["border"])
            sw.pack(side="left")
            ttk.Label(line, text=f"{label} ({count})").pack(side="left", padx=(6, 0))
            var = tk.StringVar(value=V.JOB_LABELS[jobs.get(key, "engrave")])
            cb = ttk.Combobox(line, textvariable=var, values=labels, state="readonly", width=14)
            cb.pack(side="right")
            cb.bind("<<ComboboxSelected>>", lambda e, k=key, v=var: self._set_job(k, v.get()))
            r += 1
        r = self._num(f, r, "Engraved line width (mm) ⓘ", "line_mm", 0.05, lo=0, hi=20)
        self.Tooltip(f.grid_slaves(row=r - 1, column=0)[0],
                     "Width for line colors set to Engrave. 0 = as drawn in the file\n"
                     "(hairlines still burn one dot wide).")
        row = ttk.Frame(f)
        row.grid(row=r, column=0, columnspan=3, sticky="w", pady=(6, 0))
        b = ttk.Button(row, text="Break apart", command=lambda: self.split_vector("item"))
        b.pack(side="left")
        self.Tooltip(b, "Makes every shape in the file its own layer, so you can move, delete\n"
                        "or set each one separately. Ctrl+Z undoes it.")
        b = ttk.Button(row, text="Split by color", command=lambda: self.split_vector("color"))
        b.pack(side="left", padx=(4, 0))
        self.Tooltip(b, "One layer per color.")
        return r + 1

    def _set_job(self, key, label):
        layer = self.sel_layer()
        if not layer:
            return
        job = next(j for j, l in V.JOB_LABELS.items() if l == label)
        layer.setdefault("jobs", {})[key] = job
        self.refresh_list()
        self.changed()

    def vector_real_size(self):
        layer = self.sel_layer()
        if not layer or layer["type"] != "svg":
            return
        layer["w_in"], layer["h_in"] = D.vector_size(layer, V.load(layer["src"]))
        layer["rotation"] = 0.0
        self._build_layer_tab()
        self.changed()

    def vector_canvas_to_page(self):
        layer = self.sel_layer()
        if not layer or layer["type"] != "svg":
            return
        doc = V.load(layer["src"])
        l, t, r, b = D.vector_box(layer, doc)
        layer.update(w_in=r - l, h_in=b - t, rotation=0.0, x_in=(l + r) / 2, y_in=(t + b) / 2)
        self.design["canvas"] = {"width_in": round(doc["w_in"], 4), "height_in": round(doc["h_in"], 4)}
        self._guard = True
        self.cw_var.set(self.design["canvas"]["width_in"])
        self.ch_var.set(self.design["canvas"]["height_in"])
        self._guard = False
        self.app.zoom_fit()
        self._build_layer_tab()
        self.changed()

    def split_vector(self, by):
        """Break apart (one layer per shape) or split by color (one layer per color group).
        Each new layer shows its shapes exactly where they were."""
        layer = self.sel_layer()
        if not layer or layer["type"] != "svg":
            return
        doc = V.load(layer["src"])
        ids = V.part_ids(doc, layer.get("parts"))
        buckets = {}
        for i in ids:
            key = i if by == "item" else (V.item_keys(doc["items"][i]) or ["?"])[0]
            buckets.setdefault(key, []).append(i)
        pieces = list(buckets.values())
        if len(pieces) < 2:
            messagebox.showinfo("Nothing to split", "This layer has only one "
                                + ("shape." if by == "item" else "color."))
            return
        if len(pieces) > SPLIT_WARN and not messagebox.askyesno(
                "Many pieces", f"This makes {len(pieces)} layers. Continue?"):
            return
        l0, t0, r0, b0 = D.vector_box(layer, doc)
        sx, sy = layer["w_in"] / (r0 - l0), layer["h_in"] / (b0 - t0)
        a = math.radians(layer["rotation"])
        ca, sa = math.cos(a), math.sin(a)
        new = []
        for part in pieces:
            nl = copy.deepcopy(layer)
            nl.update(id=uuid.uuid4().hex[:10], parts=part, box="content")
            l, t, r, b = V.box(doc, part)
            dx, dy = ((l + r) / 2 - (l0 + r0) / 2) * sx, ((t + b) / 2 - (t0 + b0) / 2) * sy
            nl.update(w_in=(r - l) * sx, h_in=(b - t) * sy,
                      x_in=layer["x_in"] + dx * ca - dy * sa, y_in=layer["y_in"] + dx * sa + dy * ca)
            if by == "color":
                color = V.group_label((V.item_keys(doc["items"][part[0]]) or ["f:#000000"])[0])
                nl["name"] = f"{layer.get('name') or os.path.basename(layer['src'])} – {color}"
            new.append(nl)
        layers = self.design["layers"]
        i = layers.index(layer)
        layers[i:i + 1] = new
        self.select(new[-1]["id"])
        self.changed()
        self.app.set_status(f"Split into {len(new)} layers (Ctrl+Z to undo)")

    def _num(self, f, r, label, key, step, lo=-1000.0, hi=1000.0, integer=False):
        layer = self.sel_layer()
        var = tk.DoubleVar(value=round(float(layer.get(key, 0)), 4))
        ttk.Label(f, text=label).grid(row=r, column=0, sticky="w", pady=2)
        ttk.Spinbox(f, textvariable=var, from_=lo, to=hi, increment=step, width=9).grid(row=r, column=1, sticky="w")

        def write(*_):
            if self._guard:
                return
            try:
                v = float(var.get())
            except (tk.TclError, ValueError):
                return
            v = min(hi, max(lo, v))
            self._set(key, int(round(v)) if integer else v)
        var.trace_add("write", write)
        self.fields[key] = var
        return r + 1

    def _check(self, f, r, label, key):
        var = tk.BooleanVar(value=bool(self.sel_layer().get(key)))
        ttk.Checkbutton(f, text=label, variable=var).grid(row=r, column=0, columnspan=3, sticky="w", pady=2)
        var.trace_add("write", lambda *_: self._guard or self._set(key, bool(var.get())))
        self.fields[key] = var
        return r + 1

    def _choice(self, f, r, label, key, values):
        var = tk.StringVar(value=self.sel_layer().get(key, values[0]))
        ttk.Label(f, text=label).grid(row=r, column=0, sticky="w", pady=2)
        ttk.Combobox(f, textvariable=var, values=values, state="readonly", width=10).grid(row=r, column=1, sticky="w")
        var.trace_add("write", lambda *_: self._guard or self._set(key, var.get()))
        self.fields[key] = var
        return r + 1

    def _crop(self, f, r, side, i):
        layer = self.sel_layer()
        crop = layer.setdefault("crop", [0.0, 0.0, 1.0, 1.0])
        pct = crop[i] * 100 if i < 2 else (1 - crop[i]) * 100
        var = tk.DoubleVar(value=round(pct, 1))
        ttk.Label(f, text=f"  {side}").grid(row=r, column=0, sticky="w")
        ttk.Spinbox(f, textvariable=var, from_=0, to=95, increment=1, width=9).grid(row=r, column=1, sticky="w")

        def write(*_):
            if self._guard:
                return
            try:
                v = min(95.0, max(0.0, float(var.get()))) / 100
            except (tk.TclError, ValueError):
                return
            c = list(layer["crop"])
            c[i] = v if i < 2 else 1 - v
            if c[2] - c[0] < 0.05 or c[3] - c[1] < 0.05:
                return
            layer["crop"] = c
            D.fit_to_source(layer)                # height follows the new aspect ratio
            self._refresh_fields()
            self.changed()
        var.trace_add("write", write)
        return r + 1

    def _text_box(self, f, r, multiline):
        layer = self.sel_layer()
        ttk.Label(f, text="Text").grid(row=r, column=0, sticky="nw", pady=2)
        if multiline:
            box = tk.Text(f, height=3, width=24, wrap="word", undo=True)
            box.insert("1.0", layer["text"])
            box.grid(row=r, column=1, columnspan=2, sticky="ew")
            box.bind("<KeyRelease>", lambda e: self._set_text(box.get("1.0", "end-1c")))
        else:
            var = tk.StringVar(value=layer["text"])
            ttk.Entry(f, textvariable=var, width=26).grid(row=r, column=1, columnspan=2, sticky="ew")
            var.trace_add("write", lambda *_: self._set_text(var.get()))
        return r + 1

    def _set_text(self, text):
        layer = self.sel_layer()
        if layer and layer.get("text") != text:
            layer["text"] = text
            self.refresh_list()
            self.changed()

    def _font(self, f, r):
        layer = self.sel_layer()
        families = list(D.fonts())
        fam = tk.StringVar(value=layer["font"])
        sty = tk.StringVar(value=layer["style"])
        ttk.Label(f, text="Font").grid(row=r, column=0, sticky="w", pady=2)
        fc = ttk.Combobox(f, textvariable=fam, values=families, width=22)
        fc.grid(row=r, column=1, columnspan=2, sticky="ew")
        ttk.Label(f, text="Style").grid(row=r + 1, column=0, sticky="w", pady=2)
        sc = ttk.Combobox(f, textvariable=sty, state="readonly", width=14,
                          values=list(D.fonts().get(layer["font"], {"Regular": None})))
        sc.grid(row=r + 1, column=1, sticky="w")

        def family_changed(*_):
            name = fam.get()
            if name not in D.fonts():
                # Typing filters the list; only a real family is applied.
                fc.configure(values=[x for x in families if name.lower() in x.lower()] or families)
                return
            styles = list(D.fonts()[name])
            sc.configure(values=styles)
            if sty.get() not in styles:
                self._guard = True
                sty.set("Regular" if "Regular" in styles else styles[0])
                self._guard = False
            self._set("font", name, also={"style": sty.get()})
        fam.trace_add("write", family_changed)
        sty.trace_add("write", lambda *_: self._guard or self._set("style", sty.get()))
        self.Tooltip(fc, "Type to filter the installed fonts, then pick one.")
        return r + 2

    def _set(self, key, value, also=None):
        layer = self.sel_layer()
        if not layer:
            return
        layer[key] = value
        for k, v in (also or {}).items():
            layer[k] = v
        if layer["type"] in ASPECT_LOCKED and key in ("w_in", "h_in"):
            aspect = D.source_aspect(layer)
            if key == "w_in":
                layer["h_in"] = layer["w_in"] * aspect
            else:
                layer["w_in"] = layer["h_in"] / max(aspect, 1e-6)
            self._refresh_fields()
        if layer["type"] == "image" and key == "w_in":
            self.app.suspend_traces = True
            self.app.vars["width_in"].set(round(layer["w_in"], 3))
            self.app.suspend_traces = False
        if key in ("shape", "op"):
            self.refresh_list()
        self.changed()

    def _refresh_fields(self):
        """Pushes layer values into the Layer tab (after drags, crops, undo)."""
        layer = self.sel_layer()
        if not layer:
            return
        self._guard = True
        try:
            for key, var in self.fields.items():
                if key in layer:
                    v = layer[key]
                    var.set(round(v, 3) if isinstance(v, float) else v)
        finally:
            self._guard = False

    # ================================================================ changes / undo

    def changed(self, recomposite=True):
        self.dirty = True
        self.update_title()
        self._update_test_size()
        if recomposite:
            self.request_composite()
        if self._commit_job:
            self.app.root.after_cancel(self._commit_job)
        self._commit_job = self.app.root.after(500, self._commit)

    def _commit(self):
        """Records an undo step (edits within half a second count as one step)."""
        self._commit_job = None
        if self.last_snapshot is not None and self.last_snapshot != self.design:
            self.undo_stack.append(self.last_snapshot)
            del self.undo_stack[:-50]
            self.redo_stack.clear()
        self.last_snapshot = copy.deepcopy(self.design)

    def undo(self):
        if self._commit_job:
            self.app.root.after_cancel(self._commit_job)
            self._commit()
        if not self.undo_stack:
            return
        self.redo_stack.append(copy.deepcopy(self.design))
        self._restore(self.undo_stack.pop())

    def redo(self):
        if not self.redo_stack:
            return
        self.undo_stack.append(copy.deepcopy(self.design))
        self._restore(self.redo_stack.pop())

    def _restore(self, snap):
        self.design = copy.deepcopy(snap)
        self.last_snapshot = copy.deepcopy(snap)
        self._load_globals_into_vars()
        self.select(self.selected if self.layer_by_id(self.selected) else None)
        self.dirty = True
        self.update_title()
        self.request_composite()

    def sync_from_vars(self):
        """App controls -> design (globals) and the selected photo layer (its own settings)."""
        s = self.app.settings()
        if s is None:
            return
        changed = False
        for f in D.GLOBAL_FIELDS:
            v = getattr(s, f)
            if self.design.get(f) != v:
                self.design[f] = v
                changed = True
        layer = self.selected_image()
        if layer:
            for f in D.LAYER_SETTING_FIELDS:
                v = getattr(s, f)
                if layer["settings"].get(f) != v:
                    layer["settings"][f] = v
                    changed = True
            if abs(s.width_in - layer["w_in"]) > 1e-4:
                layer["w_in"] = s.width_in
                D.fit_to_source(layer)
                self._refresh_fields()
                changed = True
        if changed:
            self.changed()

    def strokes_changed(self):
        self.changed()

    # ================================================================ add / remove / order

    def _add(self, layer):
        self.design["layers"].append(layer)
        self.select(layer["id"])
        self.nb.select(self.layer_tab)
        self.changed()

    def add_art(self, kind):
        path = filedialog.askopenfilename(title=f"Choose {D.TYPE_LABELS[kind].lower()}", filetypes=ART_TYPES)
        if not path:
            return
        c = self.design["canvas"]
        layer = D.new_layer(kind, self.design, src=os.path.abspath(path), name=os.path.basename(path),
                            w_in=round(min(c["width_in"], c["height_in"]) * (0.6 if kind == "image" else 0.35), 2))
        if kind == "image":
            s = self.app.settings() or core.Settings()
            layer["settings"].update({f: getattr(s, f) for f in D.LAYER_SETTING_FIELDS
                                      if f not in ("contrast", "brightness", "gamma")})
        self._add(layer)

    def add_vector(self):
        """A vector file at its real size. If its page is the canvas size, the drawing goes where
        it sits on the page; otherwise it's centered."""
        path = filedialog.askopenfilename(title="Choose a vector file", filetypes=VECTOR_TYPES)
        if not path:
            return
        try:
            doc = V.load(path)
        except Exception as e:
            messagebox.showerror("Couldn't open the file", str(e))
            return
        if not doc["items"]:
            messagebox.showinfo("Nothing to add", "No drawable shapes were found in this file.")
            return
        c = self.design["canvas"]
        layer = D.new_layer("svg", self.design, src=os.path.abspath(path))
        w, h = D.vector_size(layer, doc)
        if not doc["sized"]:                         # no physical size: fit it like a logo
            f = min(c["width_in"], c["height_in"]) * 0.35 / max(w, h)
            w, h = w * f, h * f
        layer.update(w_in=w, h_in=h, name=os.path.basename(path))
        same_page = (abs(doc["w_in"] - c["width_in"]) < 0.02 * c["width_in"]
                     and abs(doc["h_in"] - c["height_in"]) < 0.02 * c["height_in"])
        if doc["sized"] and same_page:
            l, t, r, b = D.vector_box(layer, doc)
            layer.update(x_in=(l + r) / 2, y_in=(t + b) / 2)
        self._add(layer)
        msg = f"Added {os.path.basename(path)} at {w * 25.4:.1f} × {h * 25.4:.1f} mm"
        if w > c["width_in"] + 1e-6 or h > c["height_in"] + 1e-6:
            msg += " (bigger than the canvas)"
        self.app.set_status(msg)

    def add_simple(self, kind, **kw):
        self._add(D.new_layer(kind, self.design, **kw))

    def delete_layer(self):
        layer = self.sel_layer()
        if not layer:
            return
        layers = self.design["layers"]
        i = layers.index(layer)
        layers.remove(layer)
        nxt = layers[min(i, len(layers) - 1)]["id"] if layers else None
        self.select(nxt)
        self.changed()

    def duplicate_layer(self):
        layer = self.sel_layer()
        if not layer:
            return
        dup = copy.deepcopy(layer)
        dup["id"] = uuid.uuid4().hex[:10]
        dup["x_in"] += 0.15
        dup["y_in"] += 0.15
        layers = self.design["layers"]
        layers.insert(layers.index(layer) + 1, dup)
        self.select(dup["id"])
        self.changed()

    def move_layer(self, step):
        layer = self.sel_layer()
        if not layer:
            return
        layers = self.design["layers"]
        i = layers.index(layer)
        j = min(len(layers) - 1, max(0, i + step))
        if i != j:
            layers.insert(j, layers.pop(i))
            self.refresh_list()
            self.changed()

    def toggle_visible(self):
        layer = self.sel_layer()
        if layer:
            layer["visible"] = not layer.get("visible", True)
            self.refresh_list()
            self.changed()

    def nudge(self, dx, dy, e):
        layer = self.sel_layer()
        if not layer:
            return
        step = 0.1 if e.state & 0x1 else 0.01          # Shift = bigger steps
        layer["x_in"] += dx * step
        layer["y_in"] += dy * step
        self._refresh_fields()
        self.app.request_redraw()
        self.changed()

    def _canvas_size_changed(self):
        if self._guard:
            return
        try:
            w, h = float(self.cw_var.get()), float(self.ch_var.get())
        except (tk.TclError, ValueError):
            return
        if w < 0.25 or h < 0.25:
            return
        self.design["canvas"] = {"width_in": w, "height_in": h}
        self.app.zoom_fit()
        self.changed()

    def _apply_canvas_preset(self, name):
        w, h = CANVAS_PRESETS[name]
        self._guard = True
        self.cw_var.set(w)
        self.ch_var.set(h)
        self._guard = False
        self._canvas_size_changed()

    # ================================================================ rendering

    def _shown(self):
        """Which design view is on screen ("Layout" or "Design"), or None."""
        v = self.app.view.get()
        return v if v in ("Layout", "Design") else None

    def request_composite(self):
        """Re-renders the design view that's on screen; the other one is marked out of date
        and rendered when you switch to it. (Photo layer views don't need a composite.)"""
        self.stale = {"Layout": True, "Design": True}
        which = self._shown()
        if which:
            self._render(which)

    def on_view_change(self):
        which = self._shown()
        if which and (self.stale.get(which) or which not in self.app.views):
            self._render(which)

    def _render(self, which):
        self.gens[which] += 1
        self.executor.submit(self._composite_job, which, self.gens[which], copy.deepcopy(self.design))

    def _composite_job(self, which, gen, snap):
        if gen != self.gens[which]:
            return                                   # a newer request is queued
        try:
            t = time.time()
            r = self.draft if which == "Layout" else self.renderer
            if which == "Layout":
                gray, cover = r.compose(snap)
                view, fill = Image.fromarray(gray), None
            else:
                burn, cover = r.compose(snap)
                look = self.app.materials.get(snap["material"], {}).get("look", ["#ffffff", "#000000"])
                img = Image.fromarray(np.where(burn, 0, 255).astype(np.uint8))
                view, fill = ImageOps.colorize(img, black=look[1], white=look[0]), float(burn.mean())
            cuts = D.all_vectors(snap, r, cover)              # red / blue lines on the preview
            sizes = {l["id"]: (l["w_in"], l["h_in"]) for l in snap["layers"] if l["type"] in ("text", "arctext")}
            stats = core.raster_stats(burn) if which == "Design" else None   # for the time estimate
            self.app.results.put(("design", which, gen, view, sizes, fill, time.time() - t, cuts, stats))
        except Exception as e:
            self.app.results.put(("design_error", which, gen, f"Couldn't render the design: {e}"))

    def on_result(self, msg):
        """Called by App._poll for ("design", ...) messages."""
        app = self.app
        if msg[0] == "design_error":
            if msg[2] == self.gens[msg[1]]:
                app.info.configure(text=msg[3], style="Warn.TLabel")
            return
        _, which, gen, view, sizes, fill, secs, cuts, stats = msg
        if gen != self.gens[which] or not self.active:
            return
        self.stale[which] = False
        self.cut_preview[which] = cuts
        cut_mm = vectors.path_length_mm(cuts)
        if stats is None:                       # Layout view: keep the last engraving estimate
            stats = app.estimate[0] if app.estimate else None
        app.set_estimate(stats, self.design["dpi"], cut_mm)
        for lid, (w, h) in sizes.items():
            layer = self.layer_by_id(lid)
            if layer and not (self.drag and self.drag["id"] == lid and self.drag["mode"] == "scale"):
                layer["w_in"], layer["h_in"] = w, h
        app.views[which] = view
        if which == "Layout":
            app.view_factor["Layout"] = self.design["dpi"] / self.draft.dpi(self.design)
        d = self.design
        n = len(d["layers"])
        text = (f"Canvas {d['canvas']['width_in']:g} × {d['canvas']['height_in']:g} in @ {d['dpi']} DPI  ·  "
                f"{n} layer{'s' if n != 1 else ''}")
        if which == "Design":
            text += f"  ·  laser fires on {fill * 100:.0f}% of the area"
            text += "  ·  mirrored on export" if d.get("mirror") else ""
        else:
            text += "  ·  Layout view: photos shown unprocessed (Design view shows the engraving)"
        if self._shown() == which:
            app.info.configure(style="TLabel", text=text)
            app.request_redraw()

    # ================================================================ canvas overlay + mouse

    def _screen(self):
        """(screen px per design px, canvas x/y of the design origin, design DPI)."""
        s_view, ox, oy, factor = self.app.disp          # factor = design px per view px
        return s_view / factor, ox, oy, self.design["dpi"]

    def _view_renderer(self):
        return self.draft if self._shown() == "Layout" else self.renderer

    def draw_overlay(self):
        app, c = self.app, self.app.canvas
        self.handles = {}
        if not app.disp:
            return
        s, ox, oy, dpi = self._screen()
        W, H = self.design["canvas"]["width_in"] * dpi, self.design["canvas"]["height_in"] * dpi
        c.create_rectangle(ox, oy, ox + W * s, oy + H * s, outline="#94a3b8", dash=(4, 3))
        k = dpi * s                                   # inches -> screen
        for kind, paths in (self.cut_preview.get(self._shown()) or {}).items():
            for pts, closed in paths:
                flat = [v for x, y in pts for v in (ox + x * k, oy + y * k)]
                if closed and len(pts) > 2:
                    flat += flat[:2]
                if len(flat) >= 4:
                    c.create_line(*flat, fill=LINE_COLORS[kind], width=1.5)
        if self.drag and self.drag.get("snap"):
            for kind, v in self.drag["snap"]:
                if kind == "x":
                    c.create_line(ox + v * s, oy, ox + v * s, oy + H * s, fill="#ec4899", dash=(2, 2))
                else:
                    c.create_line(ox, oy + v * s, ox + W * s, oy + v * s, fill="#ec4899", dash=(2, 2))
        layer = self.sel_layer()
        if not layer:
            return
        pts = [(ox + x * s, oy + y * s) for x, y in D.corners_px(layer, dpi)]
        c.create_polygon(*[v for p in pts for v in p], outline=ACCENT, fill="", width=2)
        for x, y in pts:
            hs = ui_px(HANDLE)
            c.create_rectangle(x - hs, y - hs, x + hs, y + hs, fill="white", outline=ACCENT)
        tmx, tmy = (pts[0][0] + pts[1][0]) / 2, (pts[0][1] + pts[1][1]) / 2
        cx, cy = sum(p[0] for p in pts) / 4, sum(p[1] for p in pts) / 4
        dx, dy = tmx - cx, tmy - cy
        n = math.hypot(dx, dy) or 1
        rx, ry = tmx + dx / n * ui_px(ROT_OFFSET), tmy + dy / n * ui_px(ROT_OFFSET)
        c.create_line(tmx, tmy, rx, ry, fill=ACCENT)
        hs = ui_px(HANDLE)
        c.create_oval(rx - hs, ry - hs, rx + hs, ry + hs, fill="white", outline=ACCENT)
        self.handles = {"corners": pts, "rot": (rx, ry), "center": (cx, cy)}

    @staticmethod
    def _box(layer):
        """Axis-aligned bounds of a (possibly rotated) layer, in inches: (left, top, right, bottom)."""
        pts = D.corners_px(layer, 1.0)
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        return min(xs), min(ys), max(xs), max(ys)

    def _snap_targets(self, layer):
        """x and y positions (inches) things can snap to: canvas edges/center and the edges and
        centers of every other visible item."""
        cw, ch = self.design["canvas"]["width_in"], self.design["canvas"]["height_in"]
        xs, ys = [0.0, cw / 2, cw], [0.0, ch / 2, ch]
        for other in self.design["layers"]:
            if other["id"] == layer["id"] or not other.get("visible", True):
                continue
            l, t, r, b = self._box(other)
            xs += [l, (l + r) / 2, r]
            ys += [t, (t + b) / 2, b]
        return xs, ys

    def _snap(self, layer, tol):
        """Snap the moving layer's edges/center to the canvas and other items' edges/centers.
        Returns (dx, dy, guides) where guides are ("x" | "y", position in design px)."""
        dpi = self.design["dpi"]
        xs, ys = self._snap_targets(layer)
        l, t, r, b = self._box(layer)
        guides, shift = [], [0.0, 0.0]
        for axis, mine, targets in ((0, (l, (l + r) / 2, r), xs), (1, (t, (t + b) / 2, b), ys)):
            best = None
            for m in mine:
                for v in targets:
                    if abs(v - m) < tol and (best is None or abs(v - m) < abs(best[0])):
                        best = (v - m, v)
            if best:
                shift[axis] = best[0]
                guides.append(("x" if axis == 0 else "y", best[1] * dpi))
        return shift[0], shift[1], guides

    def _inside(self, layer, x, y):
        s, ox, oy, dpi = self._screen()
        pts = [(ox + px * s, oy + py * s) for px, py in D.corners_px(layer, dpi)]
        inside = False
        for i in range(4):
            (x1, y1), (x2, y2) = pts[i], pts[(i + 1) % 4]
            if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / ((y2 - y1) or 1e-9) + x1:
                inside = not inside
        return inside

    def on_press(self, e):
        app = self.app
        app.canvas.focus_set()
        if not app.disp:
            return
        layer = self.sel_layer()
        h = self.handles
        base = None
        if layer:
            base = {"id": layer["id"], "x0": e.x, "y0": e.y, "layer": copy.deepcopy(layer)}
            if h and math.hypot(e.x - h["rot"][0], e.y - h["rot"][1]) <= ui_px(HANDLE + 3):
                cx, cy = h["center"]
                self.drag = dict(base, mode="rotate", a0=math.atan2(e.y - cy, e.x - cx))
                return
            if h:
                for i, (x, y) in enumerate(h["corners"]):
                    if abs(e.x - x) <= ui_px(HANDLE + 3) and abs(e.y - y) <= ui_px(HANDLE + 3):
                        self.drag = dict(base, mode="scale", corner=i)
                        return
        s, ox, oy, dpi = self._screen()
        # The topmost item under the cursor wins; empty parts of the selected item's box
        # (e.g. between letters) still drag the selected item.
        s_view = app.disp[0]                         # hit-testing works in the shown view's pixels
        hits = self._view_renderer().hits(self.design, (e.x - ox) / s_view, (e.y - oy) / s_view)
        hit = hits[0] if hits else None
        if e.state & 0x4 and hits:                   # Ctrl+click: cycle to the item underneath
            ids = [h["id"] for h in hits]
            hit = hits[(ids.index(self.selected) + 1) % len(hits)] if self.selected in ids else hits[0]
        if hit is None and layer and self._inside(layer, e.x, e.y):
            hit = layer
        if hit is None:
            self.select(None)
            self.drag = None
            app.pan_start(e)
            return
        if hit["id"] != self.selected:
            self.select(hit["id"])
        self.drag = {"id": hit["id"], "x0": e.x, "y0": e.y, "layer": copy.deepcopy(hit), "mode": "move"}

    def on_drag(self, e):
        app = self.app
        if not self.drag:
            app.pan_move(e)
            return
        layer = self.layer_by_id(self.drag["id"])
        if not layer:
            return
        s, ox, oy, dpi = self._screen()
        start = self.drag["layer"]
        mode = self.drag["mode"]
        shift = e.state & 0x1
        if mode == "move":
            dx, dy = (e.x - self.drag["x0"]) / s / dpi, (e.y - self.drag["y0"]) / s / dpi
            if shift:                                   # constrain to one axis
                dx, dy = (dx, 0) if abs(dx) > abs(dy) else (0, dy)
            layer["x_in"], layer["y_in"] = start["x_in"] + dx, start["y_in"] + dy
            snaps = []
            if not e.state & 0x20000:                   # hold Alt to move without snapping
                ddx, ddy, snaps = self._snap(layer, ui_px(SNAP_PX) / s / dpi)
                layer["x_in"] += ddx
                layer["y_in"] += ddy
            self.drag["snap"] = snaps
        elif mode == "rotate":
            cx, cy = ox + start["x_in"] * dpi * s, oy + start["y_in"] * dpi * s
            a = math.atan2(e.y - cy, e.x - cx)
            rot = start["rotation"] + math.degrees(a - self.drag["a0"])
            if shift:
                rot = round(rot / 15) * 15
            layer["rotation"] = round(rot % 360, 2)
        elif mode == "scale":
            self._scale(layer, start, e, s, ox, oy, dpi)
        self._refresh_fields()
        # Live preview while dragging; resizing/rotating a photo re-renders on release (slow).
        cheap = mode == "move" or layer["type"] != "image"
        if cheap and time.time() - self._last_live > 0.08:
            self._last_live = time.time()
            self.request_composite()
        app.request_redraw()

    def _scale(self, layer, start, e, s, ox, oy, dpi):
        """Corner-handle resize. From the opposite corner (default) or from the center (setting,
        or hold Ctrl to switch). Photos/logos/SVG/text keep their proportions; shapes resize
        freely (Shift keeps proportions). Edges snap to other items and the canvas (Alt = off)."""
        from_center = (self.scale_from.get() == "center") != bool(e.state & 0x4)
        sx, sy = ((-1, -1), (1, -1), (1, 1), (-1, 1))[self.drag["corner"]]
        w0, h0 = start["w_in"], start["h_in"]
        a = math.radians(start["rotation"])
        ux, uy = (math.cos(a), math.sin(a)), (-math.sin(a), math.cos(a))
        if from_center:
            px, py = start["x_in"], start["y_in"]
            c0 = (sx * w0 / 2, sy * h0 / 2)            # dragged corner, relative to the fixed point
        else:
            ax, ay = -sx * w0 / 2, -sy * h0 / 2         # opposite corner stays put
            px = start["x_in"] + ax * ux[0] + ay * uy[0]
            py = start["y_in"] + ax * ux[1] + ay * uy[1]
            c0 = (sx * w0, sy * h0)
        mx, my = (e.x - ox) / s / dpi, (e.y - oy) / s / dpi
        lx = (mx - px) * ux[0] + (my - py) * ux[1]
        ly = (mx - px) * uy[0] + (my - py) * uy[1]
        locked = layer["type"] != "shape" or bool(e.state & 0x1)
        line = layer["type"] == "shape" and layer.get("shape") == "line"
        if locked:
            f = (lx * c0[0] + ly * c0[1]) / max(c0[0] ** 2 + c0[1] ** 2, 1e-9)
            fx = fy = f
        else:
            fx = lx / c0[0] if c0[0] else 1.0
            fy = ly / c0[1] if c0[1] and not line else 1.0
        guides = []
        if abs(start["rotation"] % 360) < 0.01 and not e.state & 0x20000:
            # Snap the moving edges (only for unrotated items, where edges are well defined).
            xs, ys = self._snap_targets(layer)
            tol = ui_px(SNAP_PX) / s / dpi
            best = {}
            for axis, p, c, fcur, targets in ((0, px, c0[0], fx, xs), (1, py, c0[1], fy, ys)):
                if not c or (axis == 1 and line):
                    continue
                edge = p + c * fcur
                for t in targets:
                    if abs(t - edge) < tol and (axis not in best or abs(t - edge) < best[axis][0]):
                        best[axis] = (abs(t - edge), (t - p) / c, t)
            if locked and best:
                axis = min(best, key=lambda k: best[k][0])
                fx = fy = best[axis][1]
                guides.append(("x" if axis == 0 else "y", best[axis][2] * dpi))
            else:
                for axis, (_, fnew, t) in best.items():
                    if axis == 0:
                        fx = fnew
                    else:
                        fy = fnew
                    guides.append(("x" if axis == 0 else "y", t * dpi))
        minf = 0.05 / max(w0, h0, 1e-6)
        fx, fy = max(fx, minf), max(fy, minf)
        layer["w_in"], layer["h_in"] = w0 * fx, (h0 if line else h0 * fy)
        cx, cy = c0[0] * fx / 2, c0[1] * (1 if line else fy) / 2
        if from_center:
            layer["x_in"], layer["y_in"] = start["x_in"], start["y_in"]
        else:
            layer["x_in"] = px + cx * ux[0] + cy * uy[0]
            layer["y_in"] = py + cx * ux[1] + cy * uy[1]
        if layer["type"] in ("text", "arctext"):
            layer["size_pt"] = max(4.0, start["size_pt"] * fx)
            if layer["type"] == "arctext":
                layer["radius_in"] = max(0.1, start["radius_in"] * fx)
        self.drag["snap"] = guides

    def on_motion(self, e):
        """Cursor feedback: resize arrows on corners, rotate on the top circle, move on items."""
        app = self.app
        if self.drag or not app.disp:
            return
        cur, h = "arrow", self.handles
        if h:
            if math.hypot(e.x - h["rot"][0], e.y - h["rot"][1]) <= ui_px(HANDLE + 3):
                cur = "exchange"
            else:
                cx, cy = h["center"]
                for x, y in h["corners"]:
                    if abs(e.x - x) <= ui_px(HANDLE + 3) and abs(e.y - y) <= ui_px(HANDLE + 3):
                        cur = "size_nw_se" if (x - cx) * (y - cy) > 0 else "size_ne_sw"
                        break
        if cur == "arrow":
            layer = self.sel_layer()
            s, ox, oy, dpi = self._screen()
            s_view = app.disp[0]
            if (layer and self._inside(layer, e.x, e.y)) or \
                    self._view_renderer().hit_test(self.design, (e.x - ox) / s_view, (e.y - oy) / s_view):
                cur = "fleur"
        if str(app.canvas.cget("cursor")) != cur:
            app.canvas.configure(cursor=cur)

    def on_release(self, e):
        if not self.drag:
            return
        layer = self.layer_by_id(self.drag["id"])
        moved = layer and layer != self.drag["layer"]
        if layer and layer["type"] == "image" and self.drag["mode"] in ("scale",):
            self.app.suspend_traces = True
            self.app.vars["width_in"].set(round(layer["w_in"], 3))
            self.app.suspend_traces = False
        self.drag = None
        if moved:
            self.changed()
        self.app.request_redraw()

    # ================================================================ files

    def confirm_discard(self):
        """True if it's OK to throw away the current design (saving first if asked)."""
        if not self.dirty or not self.design["layers"]:
            return True
        ans = messagebox.askyesnocancel("Save design?", "Save changes to the current design first?")
        if ans is None:
            return False
        return self.save_project() if ans else True

    def new_design(self):
        if not self.confirm_discard():
            return
        mat = self.design.get("material", "wood")
        self.design = D.new_design(mat, self.design["canvas"]["width_in"], self.design["canvas"]["height_in"],
                                   self.design["dpi"])
        self.project_path, self.dirty = None, False
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.last_snapshot = copy.deepcopy(self.design)
        self._load_globals_into_vars()
        self.select(None)
        self.update_title()
        self.request_composite()
        self.nb.select(self.design_tab)

    def open_project(self, path=None):
        if not path:
            if not self.confirm_discard():
                return
            path = filedialog.askopenfilename(title="Open project",
                                              filetypes=[("Laser Engraver project", "*" + D.PROJECT_EXT)])
            if not path:
                return
        try:
            self.design = D.load_project(path)
        except Exception as e:
            messagebox.showerror("Couldn't open project", str(e))
            return
        self.project_path, self.dirty = path, False
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.last_snapshot = copy.deepcopy(self.design)
        self._load_globals_into_vars()
        self.select(self.design["layers"][-1]["id"] if self.design["layers"] else None)
        self.app.zoom_fit()
        self.update_title()
        self.request_composite()
        self.app.set_status(f"Opened {os.path.basename(path)}")

    def save_project(self, as_new=False):
        path = self.project_path
        if as_new or not path:
            path = filedialog.asksaveasfilename(
                title="Save project", defaultextension=D.PROJECT_EXT,
                filetypes=[("Laser Engraver project", "*" + D.PROJECT_EXT)],
                initialfile=os.path.basename(path) if path else "design" + D.PROJECT_EXT)
            if not path:
                return False
        try:
            D.save_project(self.design, path)
        except Exception as e:
            messagebox.showerror("Couldn't save project", str(e))
            return False
        self.project_path, self.dirty = path, False
        self.update_title()
        self.app.set_status(f"Saved project {os.path.basename(path)}")
        return True

    def _test_factor(self):
        try:
            pct = float(self.test_pct.get())
        except (tk.TclError, ValueError):
            return None
        return pct / 100 if 1 <= pct <= 100 else None

    def _test_pct_changed(self):
        f = self._test_factor()
        if f is not None and not self._guard:
            self._set_design_opt("test_pct", round(f * 100, 2))
        self._update_test_size()

    def _update_test_size(self):
        if not hasattr(self, "test_size"):
            return
        f = self._test_factor()
        c = self.design["canvas"]
        self.test_size.configure(text=f"Test copy: {c['width_in'] * f:.2f} × {c['height_in'] * f:.2f} in"
                                 if f else "Enter a size from 1 to 100%")

    def export_test(self):
        f = self._test_factor()
        if f is None:
            messagebox.showinfo("Test copy", "Enter a test copy size from 1 to 100%.")
            return
        self.export(factor=f)

    def export(self, factor=None):
        """Saves the engraving and cut files. With `factor`, saves a shrunken test copy instead
        (its own renderer, so the real design's cached layers are kept)."""
        app = self.app
        if not self.design["layers"]:
            messagebox.showinfo("Empty design", "Add something to the design first (Add).")
            return
        d = D.scaled_design(self.design, factor) if factor else copy.deepcopy(self.design)
        name = os.path.splitext(os.path.basename(self.project_path))[0] if self.project_path else "design"
        w, h = round(d["canvas"]["width_in"], 2), round(d["canvas"]["height_in"], 2)
        out = os.path.join(app.out_dir.get(), f"{name}_{d['material']}_{w:g}x{h:g}in_{d['dpi']}dpi"
                                               f"{'_test' if factor else ''}.png")
        snap, renderer = d, D.Renderer() if factor else self.renderer
        app.set_status("Saving test copy…" if factor else "Saving engraving…")

        def job():
            try:
                files = D.save_engraving(snap, renderer, out)
                names = ", ".join(os.path.basename(f) for f in files) or "nothing (design is empty)"
                app.results.put(("status", f"Saved {names}"))
            except Exception as e:
                app.results.put(("status", f"Save failed: {e}"))
        self.executor.submit(job)

    # ================================================================ app state

    def state(self):
        return {"design": self.design, "project": self.project_path, "dirty": self.dirty,
                "scale_from": self.scale_from.get()}

    def restore_state(self, st):
        if st.get("design"):
            try:
                base = D.new_design()
                base.update({k: v for k, v in st["design"].items() if k != "layers"})
                base["layers"] = [D._with_defaults(l, base) for l in st["design"].get("layers", [])]
                self.design = base
            except Exception:
                pass
        self.project_path = st.get("project")
        self.dirty = bool(st.get("dirty"))
        self._saved_scale_from = st.get("scale_from", "corner")
