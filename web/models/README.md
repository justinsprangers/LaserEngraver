# Background-removal models

`u2net.onnx` split into pieces under 50 MB (GitHub's limit is 100 MB per file), with
`u2net.json` listing the pieces, the total size and the SHA-256. The web app downloads the
pieces once, checks the hash, and keeps the model in the browser's cache.

- **Model:** U²-Net, by Xuebin Qin et al. (https://github.com/xuebinqin/U-2-Net), Apache-2.0.
- **This file:** the ONNX export distributed by rembg
  (https://github.com/danielgatis/rembg, MIT), unchanged. MD5 `60024c5c889badc19c04ad937298a77b`,
  the checksum rembg itself checks.

Regenerate with `python web/tools/split_model.py` (reads `%USERPROFILE%\.u2net\u2net.onnx`,
which the desktop app downloads).

Hosted here instead of downloaded from rembg's GitHub release because browsers block that
download (no cross-origin headers).
