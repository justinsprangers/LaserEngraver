# PyInstaller recipe for the Laser Engraver app. Run via build.bat (not directly).
import os
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

datas = [
    (os.path.join(ROOT, "materials.json"), "."),
    (os.path.join(ROOT, "README.md"), "."),
    (os.path.join(SPECPATH, "icon.ico"), "."),
]
datas += collect_data_files("rembg")
datas += collect_data_files("sv_ttk")        # Sun Valley theme (.tcl + images)
hiddenimports = collect_submodules("rembg.sessions")

a = Analysis(
    [os.path.join(ROOT, "engrave_app.py")],
    pathex=[ROOT],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["matplotlib", "IPython", "pytest", "gradio", "fastapi", "uvicorn"],
    # numba's on-disk cache needs the .py source next to the compiled code.
    module_collection_mode={"pymatting": "pyz+py", "engrave_core": "pyz+py"},
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="LaserEngraver",
    icon=os.path.join(SPECPATH, "icon.ico"),
    console=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="LaserEngraver")
