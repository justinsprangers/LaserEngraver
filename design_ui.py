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

HANDLE = 6            # half-size of the corner handles, screen px
ROT_OFFSET = 28       # rotation handle distance above the box, screen px
SNAP_PX = 6           # snap edges/centers within this many screen px (hold Alt to turn off)
ACCENT = "#2563eb"
CANVAS_PRESETS = {"4 × 4 in (coaster)": (4, 4), "6 × 4 in": (6, 4), "8 × 8 in": (8, 8),
                  "8 × 10 in": (8, 10), "12 × 12 in": (12, 12)}
ASPECT_LOCKED = ("image", "logo", "svg")
ART_TYPES = [("Images", " ".join("*" + e for e in core.IMAGE_EXTENSIONS)), ("All files", "*.*")]


class DesignController:
    def __init__(self, app, tooltip_cls):
        self.app, self.Tooltip = app, tooltip_cls
        self.design = D.new_design(app.vars["material"].get() if app.vars["material"].get()
                                   in app.materials else next(iter(app.materials)))
        self.renderer = D.Renderer()                 # the real engraving (Design view, export)
        self.draft = D.Renderer(draft=True)          # fast Layout view
        self.executor = ThreadPoolExecutor(max_workers=1)     # composites run off the UI thread
        self.gens = {"Layout": 0, "Design": 0}       # per-view render generation (drop stale results)
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

    # ================================================================ building

    def build(self, top, left_holder_before, nb, bottom_hide):
        """Creates (unpacked) widgets. Called once from App._build."""
        app = self.app
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
        self.layers_frame = ttk.LabelFrame(left_holder_before.master, text="Layers (top first)", padding=4)
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
        add = ttk.Menubutton(btns, text="Add ▾")
        menu = tk.Menu(add, tearoff=False)
        menu.add_command(label="Photo…", command=lambda: self.add_art("image"))
        menu.add_command(label="Logo / clip art (PNG)…", command=lambda: self.add_art("logo"))
        menu.add_command(label="SVG…", command=lambda: self.add_art("svg"))
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
            b = ttk.Button(btns, text=text, width=6 if len(text) > 2 else 3, command=cmd)
            b.pack(side="left", padx=(4, 0))
            self.Tooltip(b, tip)

        # --- Design + Layer tabs (added to the notebook while active)
        self.nb = nb
        self.design_tab = ttk.Frame(nb, padding=8)
        self.layer_tab = ttk.Frame(nb, padding=8)
        self._build_design_tab(self.design_tab)

        # --- keys (only act in Design mode and when not typing in a field)
        root = app.root
        for seq, fn in (("<Delete>", lambda e: self.delete_layer()),
                        ("<Control-z>", lambda e: self.undo()), ("<Control-y>", lambda e: self.redo()),
                        ("<Control-Z>", lambda e: self.undo()), ("<Control-Y>", lambda e: self.redo()),
                        ("<Control-s>", lambda e: self.save_project()),
                        ("<Control-d>", lambda e: self.duplicate_layer()),
                        ("<Left>", lambda e: self.nudge(-1, 0, e)), ("<Right>", lambda e: self.nudge(1, 0, e)),
                        ("<Up>", lambda e: self.nudge(0, -1, e)), ("<Down>", lambda e: self.nudge(0, 1, e))):
            root.bind_all(seq, lambda e, fn=fn: self._key(e, fn), add="+")

    def _build_design_tab(self, f):
        app = self.app
        f.columnconfigure(1, weight=1)
        ttk.Label(f, text="Canvas = the material you're engraving.", style="Hint.TLabel").grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 6))
        self.cw_var = tk.DoubleVar(value=self.design["canvas"]["width_in"])
        self.ch_var = tk.DoubleVar(value=self.design["canvas"]["height_in"])
        for r, (label, var) in enumerate((("Width (in)", self.cw_var), ("Height (in)", self.ch_var)), start=1):
            ttk.Label(f, text=label).grid(row=r, column=0, sticky="w", pady=3)
            ttk.Spinbox(f, textvariable=var, from_=0.5, to=48, increment=0.25, width=8).grid(row=r, column=1, sticky="w")
            var.trace_add("write", lambda *_: self._canvas_size_changed())
        ttk.Label(f, text="Preset").grid(row=3, column=0, sticky="w", pady=3)
        pc = ttk.Combobox(f, values=list(CANVAS_PRESETS), state="readonly", width=18)
        pc.grid(row=3, column=1, sticky="w")
        pc.bind("<<ComboboxSelected>>", lambda e: self._apply_canvas_preset(pc.get()))
        ttk.Separator(f).grid(row=4, column=0, columnspan=3, sticky="ew", pady=8)
        r = 5
        ttk.Label(f, text="Material").grid(row=r, column=0, sticky="w", pady=3)
        self.design_mat = ttk.Combobox(f, textvariable=app.vars["material"], state="readonly",
                                       values=sorted(app.materials), width=18)
        self.design_mat.grid(row=r, column=1, sticky="w")
        r += 1
        ttk.Label(f, text="DPI").grid(row=r, column=0, sticky="w", pady=3)
        ttk.Combobox(f, textvariable=app.vars["dpi"], values=("254", "318", "423", "508"), width=8).grid(
            row=r, column=1, sticky="w")
        r += 1
        ttk.Checkbutton(f, text="Invert (laser makes light marks)", variable=app.vars["invert"]).grid(
            row=r, column=0, columnspan=3, sticky="w")
        r += 1
        ttk.Checkbutton(f, text="Mirror (engrave from the back)", variable=app.vars["mirror"]).grid(
            row=r, column=0, columnspan=3, sticky="w")
        r += 1
        ttk.Checkbutton(f, text="Also save cut outline (SVG)", variable=app.vars["outline"]).grid(
            row=r, column=0, columnspan=3, sticky="w")
        r += 1
        ttk.Label(f, text="Material, DPI, invert and mirror apply to the whole design. Each photo "
                          "layer has its own style and settings: select it and use the Basic / "
                          "Photo / Comic tabs.\n\n"
                          "Arrange on the Layout view (fast; photos shown unprocessed), check the "
                          "result on the Design view (the real engraving).\n\n"
                          "Drag to move: edges and centers snap to other items and the canvas "
                          "(hold Alt to move freely). Ctrl+click picks the item underneath. "
                          "Corner squares resize, the circle on top "
                          "rotates (Shift snaps to 15°). Arrow keys nudge (Shift = bigger steps). "
                          "Mouse wheel zooms, including out past the canvas. Ctrl+Z / Ctrl+Y undo / "
                          "redo, Ctrl+D duplicates, Delete removes, Ctrl+S saves the project.",
                  style="Hint.TLabel", justify="left", wraplength=300).grid(
            row=r, column=0, columnspan=3, sticky="w", pady=(10, 0))

    # ================================================================ mode switch

    def enter(self):
        app = self.app
        self.active = True
        self.photo_snapshot = app.settings()
        app.file_toolbar.pack_forget()
        self.toolbar.pack(side="left", after=app.mode_frame)
        app.images_frame.pack_forget()
        self.layers_frame.pack(fill="both", expand=False, before=self.left_before)
        self.nb.insert(0, self.layer_tab, text="Layer")
        self.nb.insert(0, self.design_tab, text="Design")
        for w in self.bottom_hide:
            w.pack_forget()
        app.save_btn.configure(text="Save engraving")
        app.layout_radio.pack(side="left", padx=(0, 8), before=app.view_radios[0])
        app.design_radio.pack(side="left", padx=(0, 8), before=app.view_radios[0])
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

    def leave(self):
        app = self.app
        self.active = False
        self.toolbar.pack_forget()
        app.file_toolbar.pack(side="left", after=app.mode_frame)
        self.layers_frame.pack_forget()
        app.images_frame.pack(fill="both", expand=False, before=self.left_before)
        self.nb.forget(self.design_tab)
        self.nb.forget(self.layer_tab)
        for tab in (app.basic_tab, app.fine_tab, app.comic_tab):
            self.nb.add(tab)
        self.nb.select(app.basic_tab)
        for w in self.bottom_hide:
            w.pack(side="left", padx=6)
        app.save_btn.configure(text="Save this engraving")
        app.layout_radio.pack_forget()
        app.design_radio.pack_forget()
        if app.view.get() in ("Layout", "Design"):
            app.view.set("Engraving")
        if self.photo_snapshot:
            app.load_settings_into_vars(self.photo_snapshot)
        app.views.pop("Design", None)
        app.views.pop("Layout", None)
        self.update_title()
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
            detail = os.path.basename(layer.get("src", "")) or "(no file)"
        return f"{kind}: {detail}" + ("" if layer.get("visible", True) else "  (hidden)")

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

    def update_title(self):
        app = self.app
        base = f"Laser Engraver {core.VERSION}"
        if self.active:
            name = os.path.basename(self.project_path) if self.project_path else "Untitled design"
            base += f"  ·  {name}{' *' if self.dirty else ''}"
        app.root.title(base)

    def _key(self, e, fn):
        if not self.active:
            return
        focus = self.app.root.focus_get()
        if isinstance(focus, (tk.Entry, ttk.Entry, tk.Text, ttk.Spinbox, ttk.Combobox)):
            if not (e.state & 0x4 and e.keysym.lower() in ("z", "y", "s", "d")):   # Ctrl shortcuts still work
                return
        fn(e)
        return "break"

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
        f = self.layer_tab
        for w in f.winfo_children():
            w.destroy()
        self.fields = {}
        f.columnconfigure(1, weight=1)
        layer = self.sel_layer()
        if not layer:
            ttk.Label(f, text="No layer selected.\n\nUse Add ▾ above to add a photo, logo, SVG,\n"
                              "text, arc text or shape, or click an item\non the Design view.",
                      style="Hint.TLabel", justify="left").grid(row=0, column=0, sticky="w")
            return
        ttk.Label(f, text=self._label(layer), font=("", 9, "bold")).grid(row=0, column=0, columnspan=3, sticky="w")
        r = 1
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
        elif t in ("logo", "svg"):
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
        if key in ("shape",):
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
        types = [("SVG files", "*.svg")] if kind == "svg" else ART_TYPES
        path = filedialog.askopenfilename(title=f"Choose {D.TYPE_LABELS[kind].lower()}", filetypes=types)
        if not path:
            return
        c = self.design["canvas"]
        layer = D.new_layer(kind, self.design, src=os.path.abspath(path),
                            w_in=round(min(c["width_in"], c["height_in"]) * (0.6 if kind == "image" else 0.35), 2))
        if kind == "image":
            s = self.app.settings() or core.Settings()
            layer["settings"].update({f: getattr(s, f) for f in D.LAYER_SETTING_FIELDS
                                      if f not in ("contrast", "brightness", "gamma")})
        self._add(layer)

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
            if which == "Layout":
                gray, _ = self.draft.compose(snap)
                view, fill = Image.fromarray(gray), None
            else:
                burn, _ = self.renderer.compose(snap)
                look = self.app.materials.get(snap["material"], {}).get("look", ["#ffffff", "#000000"])
                img = Image.fromarray(np.where(burn, 0, 255).astype(np.uint8))
                view, fill = ImageOps.colorize(img, black=look[1], white=look[0]), float(burn.mean())
            sizes = {l["id"]: (l["w_in"], l["h_in"]) for l in snap["layers"] if l["type"] in ("text", "arctext")}
            self.app.results.put(("design", which, gen, view, sizes, fill, time.time() - t))
        except Exception as e:
            self.app.results.put(("design_error", which, gen, f"Couldn't render the design: {e}"))

    def on_result(self, msg):
        """Called by App._poll for ("design", ...) messages."""
        app = self.app
        if msg[0] == "design_error":
            if msg[2] == self.gens[msg[1]]:
                app.info.configure(text=msg[3], style="Warn.TLabel")
            return
        _, which, gen, view, sizes, fill, secs = msg
        if gen != self.gens[which] or not self.active:
            return
        self.stale[which] = False
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
            c.create_rectangle(x - HANDLE, y - HANDLE, x + HANDLE, y + HANDLE, fill="white", outline=ACCENT)
        tmx, tmy = (pts[0][0] + pts[1][0]) / 2, (pts[0][1] + pts[1][1]) / 2
        cx, cy = sum(p[0] for p in pts) / 4, sum(p[1] for p in pts) / 4
        dx, dy = tmx - cx, tmy - cy
        n = math.hypot(dx, dy) or 1
        rx, ry = tmx + dx / n * ROT_OFFSET, tmy + dy / n * ROT_OFFSET
        c.create_line(tmx, tmy, rx, ry, fill=ACCENT)
        c.create_oval(rx - HANDLE, ry - HANDLE, rx + HANDLE, ry + HANDLE, fill="white", outline=ACCENT)
        self.handles = {"corners": pts, "rot": (rx, ry), "center": (cx, cy)}

    @staticmethod
    def _box(layer):
        """Axis-aligned bounds of a (possibly rotated) layer, in inches: (left, top, right, bottom)."""
        pts = D.corners_px(layer, 1.0)
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        return min(xs), min(ys), max(xs), max(ys)

    def _snap(self, layer, tol):
        """Snap the moving layer's edges/center to the canvas and other items' edges/centers.
        Returns (dx, dy, guides) where guides are ("x" | "y", position in design px)."""
        dpi = self.design["dpi"]
        cw, ch = self.design["canvas"]["width_in"], self.design["canvas"]["height_in"]
        xs, ys = [0.0, cw / 2, cw], [0.0, ch / 2, ch]
        for other in self.design["layers"]:
            if other["id"] == layer["id"] or not other.get("visible", True):
                continue
            l, t, r, b = self._box(other)
            xs += [l, (l + r) / 2, r]
            ys += [t, (t + b) / 2, b]
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
            if h and math.hypot(e.x - h["rot"][0], e.y - h["rot"][1]) <= HANDLE + 3:
                cx, cy = h["center"]
                self.drag = dict(base, mode="rotate", a0=math.atan2(e.y - cy, e.x - cx))
                return
            if h:
                for x, y in h["corners"]:
                    if abs(e.x - x) <= HANDLE + 3 and abs(e.y - y) <= HANDLE + 3:
                        cx, cy = h["center"]
                        self.drag = dict(base, mode="scale", d0=math.hypot(e.x - cx, e.y - cy) or 1)
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
                ddx, ddy, snaps = self._snap(layer, SNAP_PX / s / dpi)
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
            cx, cy = ox + start["x_in"] * dpi * s, oy + start["y_in"] * dpi * s
            if layer["type"] == "shape" and not shift:
                # Free resize: distance from the center along the shape's own axes.
                a = math.radians(start["rotation"])
                vx, vy = e.x - cx, e.y - cy
                lx, ly = vx * math.cos(a) + vy * math.sin(a), -vx * math.sin(a) + vy * math.cos(a)
                layer["w_in"] = max(0.05, 2 * abs(lx) / s / dpi)
                if layer.get("shape") != "line":
                    layer["h_in"] = max(0.05, 2 * abs(ly) / s / dpi)
            else:
                f = max(0.02, math.hypot(e.x - cx, e.y - cy) / self.drag["d0"])
                layer["w_in"], layer["h_in"] = start["w_in"] * f, start["h_in"] * f
                if layer["type"] in ("text", "arctext"):
                    layer["size_pt"] = max(4.0, start["size_pt"] * f)
                    if layer["type"] == "arctext":
                        layer["radius_in"] = max(0.1, start["radius_in"] * f)
        self._refresh_fields()
        # Live preview while dragging; resizing/rotating a photo re-renders on release (slow).
        cheap = mode == "move" or layer["type"] != "image"
        if cheap and time.time() - self._last_live > 0.08:
            self._last_live = time.time()
            self.request_composite()
        app.request_redraw()

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
        app = self.app
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

    def export(self):
        app = self.app
        if not self.design["layers"]:
            messagebox.showinfo("Empty design", "Add something to the design first (Add ▾).")
            return
        d = self.design
        name = os.path.splitext(os.path.basename(self.project_path))[0] if self.project_path else "design"
        out = os.path.join(app.out_dir.get(), f"{name}_{d['material']}_{d['canvas']['width_in']:g}x"
                                               f"{d['canvas']['height_in']:g}in_{d['dpi']}dpi.png")
        snap = copy.deepcopy(d)
        app.set_status("Saving engraving…")

        def job():
            try:
                D.save_engraving(snap, self.renderer, out)
                app.results.put(("status", f"Saved {os.path.basename(out)}"))
            except Exception as e:
                app.results.put(("status", f"Save failed: {e}"))
        self.executor.submit(job)

    # ================================================================ app state

    def state(self):
        return {"design": self.design, "project": self.project_path, "dirty": self.dirty}

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
