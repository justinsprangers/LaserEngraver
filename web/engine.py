"""
engine.py: glue between the web page and engrave_core, run inside Pyodide (in worker.js).

The processing itself is the desktop app's engrave_core.py, unchanged, so the web version's
output matches the desktop's. Only two things are swapped for browser versions:
  - Stucki dithering calls a JavaScript loop (numba doesn't run in the browser);
  - background removal runs the u2net model through onnxruntime-web (worker.js); the pre/post
    processing here copies rembg's U2netSession so the mask matches the desktop's.
"""
import io
import json

import numpy as np
from PIL import Image, ImageOps

import engrave_core as core
from js import stuckiJS
from pyodide.ffi import to_js


def _stucki(arr):
    h, w = arr.shape
    out = stuckiJS(to_js(memoryview(np.ascontiguousarray(arr).ravel())), w, h)
    arr[:] = np.frombuffer(out.to_py(), dtype=np.float64).reshape(h, w)


core._stucki_fast = _stucki
core.FAST_STUCKI = True

_files = {}            # id -> {"name", "path", "gray", "rgb", "alpha"}


def _js(d):
    from js import Object
    return to_js(d, dict_converter=Object.fromEntries)


def _pixels(img):
    """8-bit grayscale pixels for the page to draw."""
    a = np.ascontiguousarray(np.array(img.convert("L"), dtype=np.uint8))
    return to_js(memoryview(a.ravel()))


def open_file(fid, name, data):
    path = f"/files/{fid}"
    with open(path, "wb") as f:
        f.write(bytes(data.to_py()))
    gray = core.load_image(path)          # raises if it isn't an image
    _files[fid] = {"name": name, "path": path, "gray": gray, "rgb": None, "alpha": None}
    return _js({"w": gray.width, "h": gray.height})


def close_file(fid):
    _files.pop(fid, None)


def has_cutout(fid):
    return _files[fid]["alpha"] is not None


# --- Background removal (same steps as rembg's U2netSession) -------------------------------

MEAN, STD, SIZE = (0.485, 0.456, 0.406), (0.229, 0.224, 0.225), (320, 320)


def cutout_input(fid):
    """The model input for this photo: 1x3x320x320 float32, normalized like rembg."""
    f = _files[fid]
    f["rgb"] = ImageOps.exif_transpose(Image.open(f["path"])).convert("RGB")
    im = np.array(f["rgb"].resize(SIZE, Image.Resampling.LANCZOS))
    im = im / max(np.max(im), 1e-6)
    x = np.zeros((SIZE[1], SIZE[0], 3))
    for c in range(3):
        x[:, :, c] = (im[:, :, c] - MEAN[c]) / STD[c]
    x = np.expand_dims(x.transpose((2, 0, 1)), 0).astype(np.float32)
    return to_js(memoryview(x.ravel()))


def cutout_output(fid, pred):
    """Turns the model output (first 320x320 channel) into the alpha mask, like rembg."""
    f = _files[fid]
    p = np.frombuffer(pred.to_py(), dtype=np.float32).reshape(SIZE[1], SIZE[0])
    p = (p - p.min()) / (p.max() - p.min())
    mask = Image.fromarray((p.clip(0, 1) * 255).astype("uint8"), mode="L")
    f["alpha"] = mask.resize(f["rgb"].size, Image.Resampling.LANCZOS)


# --- Rendering -------------------------------------------------------------------------------

def _process(fid, settings_json):
    f = _files[fid]
    s = core.Settings.from_dict(json.loads(settings_json))
    if s.remove_bg and f["alpha"] is not None:
        src, alpha = core.compose_on_white(f["rgb"], f["alpha"]), f["alpha"]
    else:
        src, alpha = f["gray"], None
    src, alpha = core.prepare(src, alpha, s)
    gray, result, mask = core.render(src, alpha, s)
    return s, src, gray, result, mask


def render(fid, settings_json):
    """All preview views plus what the page shows about the job."""
    s, src, gray, result, mask = _process(fid, settings_json)
    burn = np.array(result.convert("L")) < 128
    stats = core.raster_stats(burn)
    orig = src.resize(result.size, Image.Resampling.LANCZOS)   # same framing as the other views
    return _js({
        "w": result.width, "h": result.height,
        "original": _pixels(orig), "adjusted": _pixels(gray), "engraving": _pixels(result),
        "stats": list(stats), "fill": float(burn.mean()), "mirror": bool(s.mirror),
        "filename": core.output_filename(_files[fid]["name"], s),
    })


def export(fid, settings_json):
    """The engraving PNG (with its DPI) and, if asked for, the cut outline SVG."""
    s, src, gray, result, mask = _process(fid, settings_json)
    buf = io.BytesIO()
    result.save(buf, "PNG", dpi=(s.dpi, s.dpi))
    out = {"png": to_js(memoryview(buf.getvalue())), "filename": core.output_filename(_files[fid]["name"], s)}
    if s.outline:
        try:
            out["svg"] = core.outline_svg(mask, s)
        except ValueError as e:
            out["svg_error"] = str(e)
    return _js(out)


def materials():
    return json.dumps(core.load_materials())


def defaults():
    d = core.Settings().to_dict()
    return json.dumps({"settings": d, "presets": {k: list(v) for k, v in core.PRESET_FIELDS.items()},
                       "dither_modes": list(core.DITHER_MODES), "version": core.VERSION})
