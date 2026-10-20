"""
app_fixtools.py: Background-removal fix tools: strokes (Photos mode: fixes.json; Design mode:
inside the photo layer), AI model choice, prefetching cut-outs, tool switching.

Part of engrave_app.App (mixed in); `self` is the App, so everything shares its state.
"""

import tkinter as tk
from tkinter import messagebox, ttk

import engrave_core as core
from app_common import STROKE_COLORS
from ui_kit import Tooltip, seg_button, segmented


class FixToolsMixin:
    def _build_fixbar(self, right):
        """The background-fix toolbar above the preview (AI model, tools, brush)."""
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
        # Joined buttons like the web version; the selected tool fills with its stroke color.
        styles = {"pan": "Seg", "keep": "Keep.Seg", "remove": "Remove.Seg",
                  "paint_keep": "PaintKeep.Seg", "paint_remove": "PaintRemove.Seg"}
        for row, tools in ((row1, (("Pan", "pan"), ("Smart keep", "keep"), ("Smart remove", "remove"))),
                           (row2, (("Brush keep", "paint_keep"), ("Brush erase", "paint_remove")))):
            seg = segmented(row)
            seg.pack(side="left")
            for label, val in tools:
                rb = seg_button(seg, label, val, self.tool, self.on_tool, style=f"{styles[val]}.Toolbutton")
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

    def _pick_tool(self, tool):
        if tool != "pan" and not self.fixbar.winfo_ismapped():
            return                                 # fix tools only exist with Remove background on
        self.tool.set(tool)
        self.on_tool()
        self._draw_brush()

    def _resize_brush(self, f):
        self.brush.set(int(min(100, max(2, round(self.brush.get() * f)))))
