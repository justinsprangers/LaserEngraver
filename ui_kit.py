"""
ui_kit.py: shared look and building blocks for the app's windows.

- apply_theme(): Sun Valley (Windows 11 style, `sv-ttk` package) if installed, else the
  built-in Windows theme, plus the app's own label/button styles.
- Tooltip: hover hint.
- ScrollTab: a notebook page whose content scrolls while the tabs stay pinned at the top.
"""

import tkinter as tk
from tkinter import ttk

ACCENT = "#2563eb"
MUTED = "#6b7280"
WARN = "#b45309"
CANVAS_BG = "#cbd5e1"          # preview background: neutral, so light and dark engravings both read
THEMED = False                 # True once Sun Valley is active


def apply_theme(root):
    """Sets the theme and app styles. Returns the frame background color."""
    global THEMED
    style = ttk.Style(root)
    try:
        import sv_ttk
        sv_ttk.set_theme("light")
        THEMED = True
    except Exception:
        if "vista" in style.theme_names():
            style.theme_use("vista")
    bg = style.lookup("TFrame", "background") or "#f3f3f3"
    style.configure("Hint.TLabel", foreground=MUTED)
    style.configure("Warn.TLabel", foreground=WARN)
    style.configure("Section.TLabel", foreground=ACCENT, font=("Segoe UI Semibold", 10))
    style.configure("Title.TLabel", font=("Segoe UI Semibold", 11))
    style.configure("Big.TButton", padding=(14, 7))
    if not THEMED:                 # Sun Valley already provides an accent button style
        style.configure("Accent.TButton", padding=(14, 7))
    root.option_add("*Listbox.font", ("Segoe UI", 10))
    root.option_add("*Listbox.selectBackground", ACCENT)
    root.option_add("*Listbox.selectForeground", "white")
    root.option_add("*Listbox.activeStyle", "none")
    root.option_add("*Listbox.borderWidth", 0)
    root.option_add("*Listbox.highlightThickness", 1)
    root.option_add("*Listbox.highlightColor", ACCENT)
    root.option_add("*Text.font", ("Segoe UI", 10))
    return bg


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
        tk.Label(self.tip, text=self.text, background="#fffbe6", foreground="#1f2937", relief="solid",
                 borderwidth=1, padx=8, pady=5, justify="left", font=("Segoe UI", 9)).pack()

    def hide(self, _):
        if self.tip:
            self.tip.destroy()
            self.tip = None


class ScrollTab(ttk.Frame):
    """Notebook page with its own scrollbar, so the tab headers stay pinned at the top while
    long settings scroll. Build the page's content into `.inner`."""

    def __init__(self, nb, padding=(12, 10)):
        super().__init__(nb)
        bg = ttk.Style(self).lookup("TFrame", "background") or "#f3f3f3"
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0, background=bg)
        sb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = ttk.Frame(self.canvas, padding=padding)
        win = self.canvas.create_window(0, 0, window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(
            scrollregion=self.canvas.bbox("all"), width=self.inner.winfo_reqwidth()))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(win, width=e.width))
        wheel = lambda e: self.canvas.yview_scroll(-1 if e.delta > 0 else 1, "units")
        self.bind("<Enter>", lambda e: self.canvas.bind_all("<MouseWheel>", wheel))
        self.bind("<Leave>", lambda e: self.canvas.unbind_all("<MouseWheel>"))


def section(parent, title, row=None, **grid):
    """A labeled group box. With `row`, it's gridded (full width); else packed."""
    box = ttk.LabelFrame(parent, text=f" {title} ", padding=(10, 6, 10, 10))
    if row is None:
        box.pack(fill="x", pady=(0, 10))
    else:
        box.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(0, 10), **grid)
    box.columnconfigure(1, weight=1)
    return box
