"""
Builds the Windows installer. Run build.bat (one folder up) rather than this directly.

  1. PyInstaller bundles Python + all libraries into dist/LaserEngraver/
  2. Inno Setup wraps that folder into installer/LaserEngraver-Setup-<version>.exe

Needs: python -m pip install pyinstaller   and   Inno Setup 6 (winget install JRSoftware.InnoSetup)
"""

import glob
import os
import re
import subprocess
import sys

PKG = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(PKG)


def version():
    with open(os.path.join(ROOT, "engrave_core.py"), encoding="utf-8") as f:
        return re.search(r'VERSION = "([^"]+)"', f.read()).group(1)


def make_icon(path):
    """Draws the app icon: a laser beam striking a plate, with a spark."""
    from PIL import Image, ImageDraw, ImageFilter
    S = 256
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((8, 8, S - 8, S - 8), radius=48, fill=(32, 36, 44, 255))
    d.rectangle((40, 176, 216, 204), fill=(205, 160, 110, 255))           # wood plate
    d.rectangle((40, 176, 216, 182), fill=(225, 185, 135, 255))
    d.polygon([(108, 36), (148, 36), (140, 70), (116, 70)], fill=(150, 155, 165, 255))  # laser head
    glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    g = ImageDraw.Draw(glow)
    g.line((128, 70, 128, 178), fill=(255, 60, 40, 255), width=14)
    g.ellipse((104, 160, 152, 200), fill=(255, 120, 60, 255))
    img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(7)))
    d.line((128, 70, 128, 178), fill=(255, 230, 220, 255), width=4)
    d.ellipse((118, 170, 138, 188), fill=(255, 250, 230, 255))
    img.save(path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])


def find_iscc():
    candidates = [
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"),
        os.path.expandvars(r"%ProgramFiles%\Inno Setup 6\ISCC.exe"),
    ]
    return next((c for c in candidates if os.path.exists(c)), None)


def main():
    v = version()
    print(f"=== Building Laser Engraver {v} ===")
    icon = os.path.join(PKG, "icon.ico")
    if not os.path.exists(icon):
        make_icon(icon)

    print("\n[1/2] Bundling with PyInstaller (a few minutes)...")
    subprocess.check_call([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
                           "--distpath", os.path.join(ROOT, "dist"),
                           "--workpath", os.path.join(ROOT, "build"),
                           os.path.join(PKG, "LaserEngraver.spec")])

    print("\nSelf-test of the bundled app...")
    report = os.path.join(ROOT, "build", "selftest.txt")
    exe = os.path.join(ROOT, "dist", "LaserEngraver", "LaserEngraver.exe")
    code = subprocess.call([exe, "--selftest", report], timeout=300)
    with open(report, encoding="utf-8") as f:
        print(f.read())
    if code != 0:
        sys.exit("Self-test failed, so no installer was made.")

    iscc = find_iscc()
    if not iscc:
        print("\nInno Setup 6 not found, so no installer was made.")
        print(r"The app itself is ready to run: dist\LaserEngraver\LaserEngraver.exe")
        return
    print("\n[2/2] Making the installer with Inno Setup...")
    for old in glob.glob(os.path.join(ROOT, "installer", "LaserEngraver-Setup-*.exe")):
        os.remove(old)
    subprocess.check_call([iscc, "/Q", f"/DAppVersion={v}", os.path.join(PKG, "installer.iss")])
    print(f"\nDone: installer\\LaserEngraver-Setup-{v}.exe")


if __name__ == "__main__":
    main()
