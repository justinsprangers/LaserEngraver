"""
shortcuts.py: keyboard shortcuts: the list of actions, matching key presses, and the
Shortcuts window where you can see and change them (saved in settings.json).
"""

import tkinter as tk
from tkinter import messagebox, ttk

# (action id, description, default key, group)
ACTIONS = [
    ("tool_keep", "Smart keep tool", "K", "Background fix tools"),
    ("tool_remove", "Smart remove tool", "R", "Background fix tools"),
    ("brush_keep", "Brush keep tool", "B", "Background fix tools"),
    ("brush_erase", "Brush erase tool", "E", "Background fix tools"),
    ("tool_pan", "Pan tool", "P", "Background fix tools"),
    ("brush_smaller", "Smaller brush", "[", "Background fix tools"),
    ("brush_bigger", "Bigger brush", "]", "Background fix tools"),
    ("pan_hold", "Pan while held (drag with the mouse)", "Space", "View"),
    ("compare", "Compare before / after", "C", "View"),
    ("zoom_fit", "Zoom to fit", "Ctrl+0", "View"),
    ("zoom_100", "Zoom to 100%", "Ctrl+1", "View"),
    ("undo", "Undo (fix stroke in Photos, last change in Design)", "Ctrl+Z", "Editing"),
    ("redo", "Redo (Design)", "Ctrl+Y", "Editing"),
    ("save", "Save engraving (Photos) / save project (Design)", "Ctrl+S", "Editing"),
    ("duplicate", "Duplicate item", "Ctrl+D", "Design"),
    ("delete", "Delete item", "Delete", "Design"),
    ("nudge_left", "Nudge left (hold Shift for bigger steps)", "Left", "Design"),
    ("nudge_right", "Nudge right", "Right", "Design"),
    ("nudge_up", "Nudge up", "Up", "Design"),
    ("nudge_down", "Nudge down", "Down", "Design"),
]
DEFAULTS = {a: k for a, _, k, _ in ACTIONS}
LABELS = {a: d for a, d, _, _ in ACTIONS}
FIXED_HELP = [("Esc", "Pan tool (in addition to the key above)"), ("Mouse wheel", "Zoom"), ("Right-drag", "Pan"), ("Double right-click", "Zoom to fit"),
              ("Shift while rotating", "Snap to 15°"), ("Alt while moving / resizing", "No snapping"),
              ("Ctrl while resizing", "Resize from the other anchor (corner / center)"),
              ("Shift while resizing a shape", "Keep proportions"), ("Ctrl+click", "Pick the item underneath")]

_NAMES = {"bracketleft": "[", "bracketright": "]", "space": "Space", "Escape": "Esc", "Return": "Enter",
          "BackSpace": "Backspace", "minus": "-", "equal": "=", "comma": ",", "period": ".", "slash": "/",
          "semicolon": ";", "apostrophe": "'", "grave": "`", "backslash": "\\", "Prior": "PageUp",
          "Next": "PageDown"}
_MODIFIER_KEYS = {"Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R", "Caps_Lock",
                  "Win_L", "Win_R", "Num_Lock", "App"}
_MODS = (("Ctrl", 0x4), ("Alt", 0x20000), ("Shift", 0x1))


def from_event(e, ignore_shift=False):
    """Key event -> text like 'K', 'Ctrl+Z', 'Shift+Left', 'Space'. None for lone modifiers."""
    if e.keysym in _MODIFIER_KEYS:
        return None
    name = _NAMES.get(e.keysym, e.keysym)
    if len(name) == 1:
        name = name.upper()
    mods = [m for m, bit in _MODS if e.state & bit and not (m == "Shift" and ignore_shift)]
    return "+".join(mods + [name])


def load(saved):
    """Keymap from settings (unknown actions dropped, missing ones get defaults)."""
    keymap = dict(DEFAULTS)
    for a, k in (saved or {}).items():
        if a in keymap and isinstance(k, str):
            keymap[a] = k
    return keymap


class ShortcutsWindow:
    """Lists every shortcut; select one and press Change (or double-click) to set a new key."""

    def __init__(self, root, keymap, on_change):
        self.keymap, self.on_change = dict(keymap), on_change
        self.capturing = None
        w = self.win = tk.Toplevel(root)
        w.title("Keyboard shortcuts")
        w.transient(root)
        w.geometry(f"+{root.winfo_rootx() + 120}+{root.winfo_rooty() + 80}")
        frame = ttk.Frame(w, padding=14)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Keyboard shortcuts", style="Title.TLabel").pack(anchor="w")
        ttk.Label(frame, text="Select a shortcut and click Change (or double-click it), then press the "
                              "new key or combination.", style="Hint.TLabel").pack(anchor="w", pady=(2, 8))
        tree = self.tree = ttk.Treeview(frame, columns=("key",), height=20, selectmode="browse")
        tree.heading("#0", text="Action")
        tree.heading("key", text="Shortcut")
        tree.column("#0", width=380)
        tree.column("key", width=130, anchor="center")
        tree.pack(fill="both", expand=True)
        tree.bind("<Double-1>", lambda e: self.start_capture())
        self.groups = {}
        for a, desc, _, group in ACTIONS:
            if group not in self.groups:
                self.groups[group] = tree.insert("", "end", text=group, open=True)
            tree.insert(self.groups[group], "end", iid=a, text=desc, values=(keymap[a],))
        fixed = tree.insert("", "end", text="Mouse and modifiers (fixed)", open=False)
        for key, desc in FIXED_HELP:
            tree.insert(fixed, "end", text=desc, values=(key,))
        self.msg = ttk.Label(frame, text="", style="Hint.TLabel")
        self.msg.pack(anchor="w", pady=(8, 0))
        btns = ttk.Frame(frame)
        btns.pack(fill="x", pady=(8, 0))
        ttk.Button(btns, text="Change…", command=self.start_capture).pack(side="left")
        ttk.Button(btns, text="Reset selected", command=self.reset_selected).pack(side="left", padx=6)
        ttk.Button(btns, text="Reset all", command=self.reset_all).pack(side="left")
        ttk.Button(btns, text="Close", style="Accent.TButton", command=w.destroy).pack(side="right")
        w.bind("<KeyPress>", self.on_key)

    def selected_action(self):
        sel = self.tree.selection()
        return sel[0] if sel and sel[0] in DEFAULTS else None

    def start_capture(self):
        a = self.selected_action()
        if not a:
            self.msg.configure(text="Select a shortcut first.")
            return
        self.capturing = a
        self.msg.configure(text=f"Press the new key for “{LABELS[a]}”…  (Esc cancels)")
        self.win.focus_force()

    def on_key(self, e):
        if not self.capturing:
            return
        if e.keysym == "Escape":
            self.capturing = None
            self.msg.configure(text="Cancelled.")
            return "break"
        key = from_event(e)
        if key is None:
            return "break"                      # waiting for the actual key after a modifier
        a, self.capturing = self.capturing, None
        other = next((x for x, k in self.keymap.items() if k == key and x != a), None)
        if other and not messagebox.askyesno(
                "Shortcut in use", f"{key} is used for “{LABELS[other]}”.\nMove it to “{LABELS[a]}”?",
                parent=self.win):
            self.msg.configure(text="Not changed.")
            return "break"
        if other:
            self.set(other, "")
        self.set(a, key)
        self.msg.configure(text=f"“{LABELS[a]}” is now {key}." + (f" “{LABELS[other]}” has no shortcut now."
                                                                   if other else ""))
        return "break"

    def set(self, action, key):
        self.keymap[action] = key
        self.tree.set(action, "key", key or "(none)")
        self.on_change(dict(self.keymap))

    def reset_selected(self):
        a = self.selected_action()
        if a:
            self.set(a, DEFAULTS[a])

    def reset_all(self):
        for a in DEFAULTS:
            self.set(a, DEFAULTS[a])
        self.msg.configure(text="All shortcuts reset to defaults.")
