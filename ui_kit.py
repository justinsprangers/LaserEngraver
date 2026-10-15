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

PALETTES = {
    "light": dict(bg="#f3f4f6", field="#ffffff", text="#1f2937", muted="#6b7280", border="#cbd2dc",
                  accent="#2563eb", accent_hover="#1d4ed8", accent_press="#1e40af", hover="#eef2ff",
                  press="#dbe4ff", tab="#e5e7eb", trough="#e5e7eb", scale_trough="#dbe2ea",
                  warn="#b45309", canvas="#cbd5e1", tip_bg="#fffbe6", tip_fg="#1f2937"),
    "dark": dict(bg="#1e2023", field="#2a2d32", text="#e5e7eb", muted="#9ca3af", border="#41464e",
                 accent="#3b82f6", accent_hover="#2563eb", accent_press="#1d4ed8", hover="#323a4a",
                 press="#1e3a8a", tab="#26292d", trough="#2f3338", scale_trough="#3a3f46",
                 warn="#f59e0b", canvas="#2e3238", tip_bg="#2a2d32", tip_fg="#e5e7eb"),
}
THEMES = ("Light", "Dark", "System")
P = dict(PALETTES["light"])    # the active palette (ui_kit.P[...]); changed by set_theme()
ACCENT = "#2563eb"             # fixed accent for drawings that look fine on both themes
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

    def __init__(self, root, user_scale=1.0, theme="Light"):
        self.root = root
        self.user_scale = user_scale
        self.theme_name = theme
        resolved = system_theme() if theme.lower() == "system" else theme.lower()
        P.clear()
        P.update(PALETTES.get(resolved, PALETTES["light"]))
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
        self._relayout_text(self.root)
        for fn in self.listeners:
            fn()

    def retheme(self):
        """Palette changed: restyle everything and tell listeners (e.g. redraw the preview)."""
        style_theme(self.root, self.px)
        self.dark_title_bars()
        self._relayout_text(self.root)
        for fn in self.listeners:
            fn()

    def is_dark(self):
        return P["bg"] == PALETTES["dark"]["bg"]

    def dark_title_bars(self, win=None):
        for w in ([win] if win else [self.root] + [t for t in _toplevels(self.root)]):
            dark_title_bar(w, self.is_dark())

    def _relayout_text(self, w):
        """ttk entries/comboboxes/spinboxes cache where their text is drawn and don't redo it when
        a named font changes size (the text ends up clipped). Re-setting the font forces it."""
        if w.winfo_class() in ("TEntry", "TCombobox", "TSpinbox"):
            try:
                w.configure(font=w.cget("font") or "TkTextFont")
            except tk.TclError:
                pass
        for child in w.winfo_children():
            self._relayout_text(child)


S = None                       # the app's Scaler (set by init_scaling)


def init_scaling(root, user_scale=1.0, theme="Light"):
    global S
    S = Scaler(root, user_scale, theme)
    root.after(50, S.dark_title_bars)     # needs the window to exist on screen
    return S


def _toplevels(w):
    for child in w.winfo_children():
        if child.winfo_class() == "Toplevel":
            yield child
        yield from _toplevels(child)


def adopt(win):
    """Call after creating a dialog window: scales its spacing and applies the theme."""
    if S:
        S.refresh(win)
        recolor(win)
        win.after(50, lambda: S.dark_title_bars(win))


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
    """A clean theme drawn by Tk (based on 'clam') in the active palette P, sized by px()."""
    st = ttk.Style(root)
    st.theme_use("clam")
    bg, field, text, muted, border = P["bg"], P["field"], P["text"], P["muted"], P["border"]
    accent, hover, press, tab = P["accent"], P["hover"], P["press"], P["tab"]
    root.configure(background=bg)
    st.configure(".", background=bg, foreground=text, bordercolor=border, lightcolor=bg, darkcolor=bg,
                 troughcolor=P["trough"], focuscolor=accent, selectbackground=accent, selectforeground="white",
                 fieldbackground=field, insertcolor=text, font="TkDefaultFont")
    st.map(".", foreground=[("disabled", muted)])
    st.configure("TFrame", background=bg)
    st.configure("TLabel", background=bg, foreground=text)
    st.configure("Hint.TLabel", foreground=muted, font="AppSmall")
    st.configure("Warn.TLabel", foreground=P["warn"])
    st.configure("Section.TLabel", foreground=accent, font="AppSection")
    st.configure("Title.TLabel", font="AppTitle")
    st.configure("TLabelframe", background=bg, bordercolor=border, relief="solid", borderwidth=1)
    st.configure("TLabelframe.Label", background=bg, foreground=accent, font="AppSection")
    st.configure("TButton", background=field, foreground=text, bordercolor=border, relief="flat",
                 padding=(px(12), px(5)), lightcolor=field, darkcolor=field, focusthickness=1)
    st.map("TButton", background=[("pressed", press), ("active", hover), ("disabled", bg)],
           bordercolor=[("active", accent), ("focus", accent)], foreground=[("disabled", muted)])
    st.configure("Big.TButton", padding=(px(16), px(7)))
    st.configure("Tool.TButton", padding=(px(8), px(4)), width=-3)   # compact (min 3 chars, not 11)
    st.configure("Accent.TButton", background=accent, foreground="white", bordercolor=accent,
                 lightcolor=accent, darkcolor=accent, padding=(px(18), px(7)), font="AppBold")
    st.map("Accent.TButton", background=[("pressed", P["accent_press"]), ("active", P["accent_hover"])],
           lightcolor=[("active", P["accent_hover"])], darkcolor=[("active", P["accent_hover"])],
           bordercolor=[("active", P["accent_hover"])])
    st.configure("TMenubutton", background=field, foreground=text, bordercolor=border, padding=(px(10), px(4)),
                 arrowsize=px(9), arrowcolor=text, relief="flat")
    st.map("TMenubutton", background=[("active", hover)])
    for w in ("TCheckbutton", "TRadiobutton"):
        st.configure(w, background=bg, foreground=text, indicatorbackground=field, indicatorforeground=accent,
                     indicatorsize=px(15), indicatordiameter=px(15), indicatormargin=(0, 0, px(6), 0),
                     upperbordercolor=border, lowerbordercolor=border, padding=(px(2), px(3)))
        st.map(w, indicatorbackground=[("disabled", bg), ("pressed", press), ("selected", field)],
               background=[("active", bg)], foreground=[("disabled", muted)])
    for w in ("TEntry", "TCombobox", "TSpinbox"):
        st.configure(w, fieldbackground=field, foreground=text, bordercolor=border, lightcolor=field,
                     darkcolor=field, padding=(px(6), px(3)), arrowsize=px(13), arrowcolor=text,
                     background=field, insertcolor=text, selectbackground=accent, selectforeground="white")
        st.map(w, bordercolor=[("focus", accent)], fieldbackground=[("readonly", field), ("disabled", bg)],
               foreground=[("disabled", muted), ("readonly", text)], background=[("active", hover)],
               selectbackground=[("readonly", field)], selectforeground=[("readonly", text)])
    st.configure("TNotebook", background=bg, bordercolor=border, tabmargins=(0, px(2), 0, 0))
    st.configure("TNotebook.Tab", background=tab, foreground=muted, bordercolor=border,
                 padding=(px(14), px(6)), lightcolor=tab)
    st.map("TNotebook.Tab", background=[("selected", bg)], foreground=[("selected", text)],
           lightcolor=[("selected", bg)], expand=[("selected", (0, px(2), 0, 0))])
    for o in ("Vertical", "Horizontal"):
        st.configure(f"{o}.TScrollbar", arrowsize=px(14), background=tab, bordercolor=bg, troughcolor=bg,
                     gripcount=0, arrowcolor=muted, lightcolor=tab, darkcolor=tab)
        st.map(f"{o}.TScrollbar", background=[("active", hover)])
    st.configure("Horizontal.TScale", sliderlength=px(18), sliderthickness=px(16), troughcolor=P["scale_trough"],
                 background=field, bordercolor=border, lightcolor=field, darkcolor=field)
    st.map("Horizontal.TScale", background=[("active", hover)])
    st.configure("Horizontal.TProgressbar", thickness=px(10), background=accent, troughcolor=P["trough"],
                 bordercolor=border, lightcolor=accent, darkcolor=accent)
    st.configure("Treeview", rowheight=px(26), fieldbackground=field, background=field, foreground=text,
                 bordercolor=border)
    st.map("Treeview", background=[("selected", accent)], foreground=[("selected", "white")])
    st.configure("Treeview.Heading", font="TkHeadingFont", padding=(px(6), px(4)), background=tab,
                 foreground=text, bordercolor=border)
    st.configure("TSeparator", background=border)
    st.configure("TPanedwindow", background=bg)
    st.configure("Sash", sashthickness=px(6), gripcount=0, background=bg)
    # Plain Tk widgets (lists, text boxes, menus) created from now on:
    for pattern, value in (("*Listbox.font", "TkDefaultFont"), ("*Listbox.background", field),
                           ("*Listbox.foreground", text), ("*Listbox.selectBackground", accent),
                           ("*Listbox.selectForeground", "white"), ("*Listbox.activeStyle", "none"),
                           ("*Listbox.borderWidth", 0), ("*Listbox.highlightThickness", 1),
                           ("*Listbox.highlightColor", accent), ("*Listbox.highlightBackground", border),
                           ("*TCombobox*Listbox.font", "TkDefaultFont"),
                           ("*Text.font", "TkDefaultFont"), ("*Text.background", field),
                           ("*Text.foreground", text), ("*Text.insertBackground", text),
                           ("*Menu.font", "TkMenuFont"), ("*Menu.background", field), ("*Menu.foreground", text),
                           ("*Menu.activeBackground", accent), ("*Menu.activeForeground", "white"),
                           # small built-in dialogs (e.g. "Save as material…") are plain Tk widgets
                           ("*Toplevel.background", bg), ("*Frame.background", bg),
                           ("*Label.background", bg), ("*Label.foreground", text),
                           ("*Entry.background", field), ("*Entry.foreground", text),
                           ("*Entry.insertBackground", text), ("*Button.background", field),
                           ("*Button.foreground", text), ("*Button.activeBackground", hover),
                           ("*Button.activeForeground", text)):
        root.option_add(pattern, value)
    recolor(root)


def recolor(w):
    """Applies the palette to plain Tk widgets that already exist (ttk ones follow the style)."""
    cls = w.winfo_class()
    try:
        if cls == "Listbox":
            w.configure(background=P["field"], foreground=P["text"], selectbackground=P["accent"],
                        selectforeground="white", highlightbackground=P["border"], highlightcolor=P["accent"])
        elif cls == "Text":
            w.configure(background=P["field"], foreground=P["text"], insertbackground=P["text"],
                        highlightbackground=P["border"], highlightcolor=P["accent"])
        elif cls == "Menu":
            w.configure(background=P["field"], foreground=P["text"], activebackground=P["accent"],
                        activeforeground="white")
        elif cls == "Canvas":
            # Settings pages blend with the window; the preview canvas uses the preview color.
            w.configure(background=P["bg"] if isinstance(w.master, ScrollTab) else P["canvas"])
        elif cls in ("Tk", "Toplevel"):
            w.configure(background=P["bg"])
    except tk.TclError:
        pass
    for child in w.winfo_children():
        recolor(child)


def system_theme():
    """'dark' if Windows apps are set to dark mode, else 'light'."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return "light" if winreg.QueryValueEx(k, "AppsUseLightTheme")[0] else "dark"
    except OSError:
        return "light"


def dark_title_bar(win, dark):
    """Dark or light Windows title bar (Windows 10 20H1+ / 11; ignored elsewhere)."""
    try:
        hwnd = ctypes.windll.user32.GetAncestor(win.winfo_id(), 2)
        value = ctypes.c_int(1 if dark else 0)
        for attr in (20, 19):            # DWMWA_USE_IMMERSIVE_DARK_MODE (newer, older builds)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(value),
                                                         ctypes.sizeof(value)) == 0:
                break
    except Exception:
        pass


def set_theme(name):
    """'Light', 'Dark' or 'System'. Re-themes every open window."""
    resolved = system_theme() if name.lower() == "system" else name.lower()
    P.clear()
    P.update(PALETTES.get(resolved, PALETTES["light"]))
    if S:
        S.theme_name = name
        S.retheme()
    return resolved


def apply_theme(root):
    """Kept for compatibility: the theme is applied by the Scaler."""
    if S is None:
        init_scaling(root)
    return P["bg"]


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
        tk.Label(self.tip, text=self.text, background=P["tip_bg"], foreground=P["tip_fg"], relief="solid",
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
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0, background=P["bg"])
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
