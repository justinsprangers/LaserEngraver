"""
app_common.py: constants shared by engrave_app and its mixin modules.
"""

DPI_CHOICES = ("254", "318", "423", "508")   # 0.10, 0.08, 0.06, 0.05 mm line interval
ZOOM_STEP = 1.12          # per mouse-wheel notch
VIEWS = ("Original", "Cut-out", "Adjusted", "Engraving", "Material look")
STROKE_COLORS = {"keep": "#22c55e", "remove": "#ef4444",            # smart (re-cuts the region)
                 "paint_keep": "#38bdf8", "paint_remove": "#f59e0b"}  # plain brush (exact)
DEBOUNCE_MS = 300
TIPS = {
    "contrast": "1.0 = unchanged. Higher pushes tones apart.",
    "brightness": "1.0 = unchanged. Higher = less laser firing.",
    "gamma": ">1 lightens midtones, <1 darkens them.",
    "shadow_amount": "How much to lift dark areas so their texture survives.",
    "shadow_radius": "Blur size (px) used to find dark regions.",
    "clahe_clip": "Local contrast strength. Higher = more texture, more noise.",
    "clahe_grid": "Number of tiles across. Higher = more local.",
    "sharpen_radius": "Unsharp-mask radius in px.",
    "sharpen_percent": "Unsharp-mask strength.",
    "sharpen_threshold": "Only sharpen edges stronger than this.",
    "comic_levels": "Number of tone bands. 4 = black, crosshatch, dots, blank.",
    "comic_smooth": "Flattening passes. More = smoother, more cartoon-like regions.",
    "comic_dot_mm": "Spacing of halftone dots and hatch lines, in mm.\nGo bigger if fine dots burn together.",
    "comic_line_block": "Area (px) used to find ink lines. Bigger = bolder shapes, fewer fine lines.",
    "comic_line_c": "Line sensitivity. Higher = fewer, cleaner lines.",
    "comic_line_weight": "Thicken ink lines by this many pixels.",
    "comic_despeckle": "Remove ink specks smaller than this many pixels.",
    "outline_offset_in": "How far outside the subject the cut line runs.",
}
CROP_FIELDS = ("crop_l", "crop_t", "crop_r", "crop_b")   # fractions in Settings, % in the UI
INT_FIELDS = ("shadow_radius", "clahe_grid", "sharpen_percent", "sharpen_threshold", "comic_levels",
              "comic_smooth", "comic_line_block", "comic_line_weight", "comic_despeckle")
