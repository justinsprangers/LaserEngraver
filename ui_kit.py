"""
ui_kit.py: shared look, scaling and building blocks for the app's windows.

Scaling: the app is per-monitor DPI aware. Scaler sizes everything (fonts, padding, spacing,
checkboxes, scrollbars, canvas handles) for the monitor the window is on, times your own UI size
setting, and re-applies it when the window moves to a monitor with different scaling. Layout
numbers in the code are "base" pixels at 100% (96 DPI); Scaler multiplies them.

The theme is drawn by Tk itself (based on "clam") so every part scales; image-based themes
(e.g. Sun Valley) can't.
"""

import ctypes
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk

ACCENT = "#2563eb"
ACCENT_DARK = "#1d4ed8"
MUTED = "#6b7280"
WARN = "#b45309"
TEXT = "#1f2937"
BG = "#f3f4f6"
FIELD = "#ffffff"
BORDER = "#cbd2dc"
CANVAS_BG = "#cbd5e1"          # preview background: neutral, so light and dark engravings both read
UI_SIZES = (0.8, 0.9, 1.0, 1.1, 1.25, 1.5, 1.75, 2.0)

# Fonts: (family, size in px at 100%)
FONTS = {"TkDefaultFont": ("Segoe UI", 13), "TkTextFont": ("Segoe UI", 13), "TkMenuFont": ("Segoe UI", 13),
         "TkHeadingFont": ("Segoe UI Semibold", 13), "TkCaptionFont": ("Segoe UI", 13),
         "TkSmallCaptionFont": ("Segoe UI", 12), "TkIconFont": ("Segoe UI", 13),
         "TkTooltipFont": ("Segoe UI", 12), "TkFixedFont": ("Consolas", 13),
         "AppSection": ("Segoe UI Semibold", 14), "AppTitle": ("Segoe UI Semibold", 16),
         "AppSmall": ("Segoe UI", 12), "AppBold": ("Segoe UI Semibold", 12)}


def set_dpi_awareness():
    """Call before creating the Tk root. Per-monitor v2 if available (Windows 10 1703+)."""
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass


def window_dpi(root):
    """DPI of the monitor the window is on (96 = 100% Windows scaling)."""
    try:
        hwnd = ctypes.windll.user32.GetAncestor(root.winfo_id(), 2)      # GA_ROOT
        dpi = ctypes.windll.user32.GetDpiForWindow(hwnd)
        if dpi:
            return dpi
    except Exception:
        pass
    try:
        return round(root.winfo_fpixels("1i"))
    except Exception:
        return 96


class Scaler:
    """Applies `factor` = monitor DPI / 96 x the user's UI size to fonts, styles and the layout."""

    def __init__(self, root, user_scale=1.0):
        self.root = root
        self.user_scale = user_scale
        self.dpi = window_dpi(root)
        self.factor = self.dpi / 96 * user_scale
        self.listeners = []           # callables run after the factor changes (e.g. redraw)
        self.skip = set()             # widget paths whose sizes the code manages itself
        self._base, self._set = {}, {}
        self._job = None
        self._fonts = {}              # keep references: Tk deletes a named font when its object dies
        for name, (family, px) in FONTS.items():
            try:
                f = tkfont.nametofont(name)
                f.configure(family=family)
            except tk.TclError:
                f = tkfont.Font(root, name=name, family=family)
            self._fonts[name] = f
        self._apply_fonts_and_styles()
        root.bind("<Configure>", self._maybe_moved, add="+")

    def px(self, n):
        return max(1, int(round(n * self.factor))) if n else 0

    # --- fonts and ttk styles
    def _apply_fonts_and_styles(self):
        for name, (family, px) in FONTS.items():
            self._fonts[name].configure(size=-self.px(px))
        self.root.tk.call("tk", "scaling", self.factor * 96 / 72)
        style_theme(self.root, self.px)

    # --- layout: padding / spacing / pixel lengths of every widget
    def refresh(self, widget=None):
        """Scales widgets built (or re-packed) with base sizes since the last call."""
        self._walk(widget or self.root)

    def _walk(self, w):
        self._scale_widget(w)
        for child in w.winfo_children():
            self._walk(child)

    def _scale_widget(self, w):
        path = str(w)
        if path in self.skip:
            return
        mgr = w.winfo_manager()
        opts = []
        if mgr in ("pack", "grid"):
            info = w.pack_info() if mgr == "pack" else w.grid_info()
            for k in ("padx", "pady", "ipadx", "ipady"):
                if k in info:
                    opts.append((("geo", mgr, k), info[k]))
        for k in ("padding", "wraplength", "length"):
            try:
                if k in w.keys():
                    opts.append((("opt", k), w.cget(k)))
            except tk.TclError:
                pass
        for key, cur in opts:
            cur = _ints(cur)
            if cur is None:
                continue
            slot = (path, key)
            if self._set.get(slot) != cur:          # set by the code (base values) since last time
                self._base[slot] = cur
            scaled = tuple(self.px(v) for v in self._base[slot])
            if scaled != cur:
                value = scaled if len(scaled) > 1 else scaled[0]
                try:
                    if key[0] == "geo":
                        (w.pack_configure if key[1] == "pack" else w.grid_configure)(**{key[2]: value})
                    else:
                        w.configure(**{key[1]: value})
                except tk.TclError:
                    continue
            self._set[slot] = scaled

    # --- monitor / user changes
    def set_user_scale(self, s):
        self.user_scale = s
        self._rescale()

    def _maybe_moved(self, e):
        if e.widget is not self.root:
            return
        if self._job:
            self.root.after_cancel(self._job)
        self._job = self.root.after(150, self._check_dpi)

    def _check_dpi(self):
        self._job = None
        dpi = window_dpi(self.root)
        if dpi != self.dpi:
            self.dpi = dpi
            self._rescale()

    def _rescale(self):
        new = self.dpi / 96 * self.user_scale
        if abs(new - self.factor) < 1e-6:
            return
        self.factor = new
        self._apply_fonts_and_styles()
        self.refresh()
        for fn in self.listeners:
            fn()


S = None                       # the app's Scaler (set by init_scaling)


def init_scaling(root, user_scale=1.0):
    global S
    S = Scaler(root, user_scale)
    return S


def px(n):
    return S.px(n) if S else n


def _ints(v):
    """Tk padding/geometry values (int, tuple, '4 8', Tcl objects) -> tuple of ints, or None."""
    if v in ("", None):
        return None
    if isinstance(v, (int, float)):
        return (int(v),)
    parts = v if isinstance(v, (tuple, list)) else str(v).split()
    try:
        return tuple(int(float(str(p))) for p in parts) or None
    except ValueError:
        return None


def style_theme(root, px):
    """A clean, light theme drawn by Tk (based on 'clam'), sized by px()."""
    st = ttk.Style(root)
    st.theme_use("clam")
    root.configure(background=BG)
    st.configure(".", background=BG, foreground=TEXT, bordercolor=BORDER, lightcolor=BG, darkcolor=BG,
                 troughcolor="#e5e7eb", focuscolor=ACCENT, selectbackground=ACCENT, selectforeground="white",
                 fieldbackground=FIELD, insertcolor=TEXT, font="TkDefaultFont")
    st.configure("TFrame", background=BG)
    st.configure("TLabel", background=BG, foreground=TEXT)
    st.configure("Hint.TLabel", foreground=MUTED, font="AppSmall")
    st.configure("Warn.TLabel", foreground=WARN)
    st.configure("Section.TLabel", foreground=ACCENT, font="AppSection")
    st.configure("Title.TLabel", font="AppTitle")
    st.configure("TLabelframe", background=BG, bordercolor=BORDER, relief="solid", borderwidth=1)
    st.configure("TLabelframe.Label", background=BG, foreground=ACCENT, font="AppSection")
    st.configure("TButton", background=FIELD, foreground=TEXT, bordercolor=BORDER, relief="flat",
                 padding=(px(12), px(5)), lightcolor=FIELD, darkcolor=FIELD, focusthickness=1)
    st.map("TButton", background=[("pressed", "#dbe4ff"), ("active", "#eef2ff"), ("disabled", BG)],
           bordercolor=[("active", ACCENT), ("focus", ACCENT)], foreground=[("disabled", MUTED)])
    st.configure("Big.TButton", padding=(px(16), px(7)))
    st.configure("Accent.TButton", background=ACCENT, foreground="white", bordercolor=ACCENT,
                 lightcolor=ACCENT, darkcolor=ACCENT, padding=(px(18), px(7)), font="AppBold")
    st.map("Accent.TButton", background=[("pressed", "#1e40af"), ("active", ACCENT_DARK)],
           lightcolor=[("active", ACCENT_DARK)], darkcolor=[("active", ACCENT_DARK)],
           bordercolor=[("active", ACCENT_DARK)])
    st.configure("TMenubutton", background=FIELD, bordercolor=BORDER, padding=(px(10), px(4)),
                 arrowsize=px(9), relief="flat")
    st.map("TMenubutton", background=[("active", "#eef2ff")])
    for w in ("TCheckbutton", "TRadiobutton"):
        st.configure(w, background=BG, indicatorbackground=FIELD, indicatorforeground=ACCENT,
                     indicatorsize=px(15), indicatordiameter=px(15), indicatormargin=(0, 0, px(6), 0),
                     upperbordercolor=BORDER, lowerbordercolor=BORDER, padding=(px(2), px(3)))
        st.map(w, indicatorbackground=[("disabled", BG), ("pressed", "#dbe4ff"), ("selected", FIELD)],
               background=[("active", BG)], foreground=[("disabled", MUTED)])
    for w in ("TEntry", "TCombobox", "TSpinbox"):
        st.configure(w, fieldbackground=FIELD, bordercolor=BORDER, lightcolor=FIELD, darkcolor=FIELD,
                     padding=(px(6), px(3)), arrowsize=px(13), arrowcolor=TEXT)
        st.map(w, bordercolor=[("focus", ACCENT)], fieldbackground=[("readonly", FIELD), ("disabled", BG)],
               foreground=[("disabled", MUTED)])
    root.option_add("*TCombobox*Listbox.font", "TkDefaultFont")
    st.configure("TNotebook", background=BG, bordercolor=BORDER, tabmargins=(0, px(2), 0, 0))
    st.configure("TNotebook.Tab", background="#e5e7eb", foreground=MUTED, bordercolor=BORDER,
                 padding=(px(14), px(6)), lightcolor="#e5e7eb")
    st.map("TNotebook.Tab", background=[("selected", BG)], foreground=[("selected", TEXT)],
           lightcolor=[("selected", BG)], expand=[("selected", (0, px(2), 0, 0))])
    st.configure("Vertical.TScrollbar", arrowsize=px(14), background="#e5e7eb", bordercolor=BG,
                 troughcolor=BG, gripcount=0, arrowcolor=MUTED)
    st.configure("Horizontal.TScrollbar", arrowsize=px(14), background="#e5e7eb", bordercolor=BG,
                 troughcolor=BG, gripcount=0, arrowcolor=MUTED)
    st.configure("Horizontal.TScale", sliderlength=px(18), sliderthickness=px(16), troughcolor="#dbe2ea",
                 background=FIELD, bordercolor=BORDER)
    st.map("Horizontal.TScale", background=[("active", "#eef2ff")])
    st.configure("Horizontal.TProgressbar", thickness=px(10), background=ACCENT, troughcolor="#e5e7eb",
                 bordercolor=BORDER)
    st.configure("Treeview", rowheight=px(26), fieldbackground=FIELD, background=FIELD, bordercolor=BORDER)
    st.configure("Treeview.Heading", font="TkHeadingFont", padding=(px(6), px(4)), background="#e5e7eb")
    st.configure("TSeparator", background=BORDER)
    st.configure("TPanedwindow", background=BG)
    st.configure("Sash", sashthickness=px(6), gripcount=0, background=BG)
    root.option_add("*Listbox.font", "TkDefaultFont")
    root.option_add("*Listbox.background", FIELD)
    root.option_add("*Listbox.selectBackground", ACCENT)
    root.option_add("*Listbox.selectForeground", "white")
    root.option_add("*Listbox.activeStyle", "none")
    root.option_add("*Listbox.borderWidth", 0)
    root.option_add("*Listbox.highlightThickness", 1)
    root.option_add("*Listbox.highlightColor", ACCENT)
    root.option_add("*Listbox.highlightBackground", BORDER)
    root.option_add("*Text.font", "TkDefaultFont")
    root.option_add("*Menu.font", "TkMenuFont")


def apply_theme(root):
    """Kept for compatibility: the theme is applied by the Scaler."""
    if S is None:
        init_scaling(root)
    return BG


class Tooltip:
    """Shows a small hint box while the mouse is over a widget."""

    def __init__(self, widget, text):
        self.widget, self.text, self.tip = widget, text, None
        widget.bind("<Enter>", self.show, add="+")
        widget.bind("<Leave>", self.hide, add="+")

    def show(self, _):
        x = self.widget.winfo_rootx() + px(10)
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + px(4)
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{x}+{y}")
        tk.Label(self.tip, text=self.text, background="#fffbe6", foreground=TEXT, relief="solid",
                 borderwidth=1, padx=px(8), pady=px(5), justify="left", font="AppSmall").pack()

    def hide(self, _):
        if self.tip:
            self.tip.destroy()
            self.tip = None


class ScrollTab(ttk.Frame):
    """Notebook page with its own scrollbar, so the tab headers stay pinned at the top while
    long settings scroll. Build the page's content into `.inner`."""

    def __init__(self, nb, padding=(12, 10)):
        super().__init__(nb)
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0, background=BG)
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
