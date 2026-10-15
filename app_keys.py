"""
app_keys.py: Keyboard shortcuts (one dispatcher over the editable keymap in shortcuts.py),
UI size, and fitting the settings column after a rescale.

Part of engrave_app.App (mixed in); `self` is the App, so everything shares its state.
"""

import tkinter as tk
from tkinter import ttk

import shortcuts
import ui_kit


class KeysMixin:
    def _bind_shortcuts(self):
        self.root.bind_all("<KeyPress>", self._on_key, add="+")
        self.root.bind_all("<KeyRelease>", self._on_key_up, add="+")

    def set_keymap(self, keymap):
        self.keymap = keymap
        self.key_actions = {k: a for a, k in keymap.items() if k}

    def open_shortcuts(self):
        w = shortcuts.ShortcutsWindow(self.root, self.keymap, self.set_keymap)
        ui_kit.adopt(w.win)                        # scale + theme the new window

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
        """After a UI size, monitor or theme change."""
        self._fit_sash()
        self.update_labels()
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
