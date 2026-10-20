"""
Splits a background-removal model into pieces small enough for GitHub (< 50 MB each) and
writes a manifest the web app reads. Run from anywhere:

    python web/tools/split_model.py            # u2net from %USERPROFILE%\\.u2net

The web app downloads the pieces once, checks the SHA-256, and caches the model in the browser.
"""
import hashlib
import json
import os
import sys

PART = 45 * 1024 * 1024
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "models")


def split(model):
    src = os.path.join(os.path.expanduser("~"), ".u2net", f"{model}.onnx")
    data = open(src, "rb").read()
    os.makedirs(OUT, exist_ok=True)
    parts = []
    for i in range(0, len(data), PART):
        name = f"{model}.onnx.part{i // PART}"
        with open(os.path.join(OUT, name), "wb") as f:
            f.write(data[i:i + PART])
        parts.append(name)
    manifest = {"model": model, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                "parts": parts}
    with open(os.path.join(OUT, f"{model}.json"), "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"{model}: {len(data) / 1e6:.1f} MB in {len(parts)} parts, sha256 {manifest['sha256'][:16]}…")


if __name__ == "__main__":
    for m in sys.argv[1:] or ["u2net"]:
        split(m)
