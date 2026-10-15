"""
app_panels.py: The settings tabs (Basic / Photo tuning / Comic tuning): sliders, presets,
materials, crop & rotate, test card.

Part of engrave_app.App (mixed in); `self` is the App, so everything shares its state.
"""

import os

import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

import engrave_core as core
from ui_kit import Tooltip, section
from app_common import DPI_CHOICES, TIPS, CROP_FIELDS


class PanelsMixin:
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
        mb = ttk.Menubutton(sm, text="Manage")
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
