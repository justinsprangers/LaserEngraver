"""
app_preview.py: The preview canvas: drawing views, zoom / pan, compare split, mouse routing
(Design view clicks go to design_ui), brush outline.

Part of engrave_app.App (mixed in); `self` is the App, so everything shares its state.
"""

import math

from PIL import Image, ImageOps, ImageTk

from app_common import ZOOM_STEP, VIEWS, STROKE_COLORS


class PreviewMixin:
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

    def _toggle_compare(self):
        self.compare.set(not self.compare.get())
        self.redraw()

    def _set_space(self, down):
        if down != self._space:
            self._space = down
            self.canvas.configure(cursor="fleur" if down else ("crosshair" if self._drawing() else "arrow"))

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
