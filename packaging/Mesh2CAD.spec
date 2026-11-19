# PyInstaller recipe for the Mesh2CAD desktop app.
#
#   pip install . pyinstaller
#   pyinstaller --noconfirm packaging/Mesh2CAD.spec
#
# macOS -> dist/Mesh2CAD.app      Windows / Linux -> dist/Mesh2CAD/ (folder with the program)
import re
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).parent  # noqa: F821  (SPECPATH is provided by PyInstaller)
VERSION = re.search(r'__version__ = "([^"]+)"', (ROOT / "mesh2cad" / "__init__.py").read_text()).group(1)

# OCP is one large compiled module plus a small Python package per OCCT toolkit
# (OCP.TopoDS, OCP.gp, ...). Ship every one of those small packages so any import works.
hidden = collect_submodules("OCP", filter=lambda name: not name.startswith("OCP.OCP"))
hidden += collect_submodules("mesh2cad")
datas = collect_data_files("trimesh")

if sys.platform == "darwin":
    icon = str(ROOT / "packaging" / "Mesh2CAD.icns")
elif sys.platform.startswith("win"):
    icon = str(ROOT / "packaging" / "Mesh2CAD.ico")
else:
    icon = None

a = Analysis(  # noqa: F821
    [str(ROOT / "packaging" / "mesh2cad_app.py")],
    pathex=[str(ROOT)],
    hiddenimports=hidden,
    datas=datas,
    excludes=["matplotlib", "IPython", "pytest", "pandas", "sympy", "PIL", "PyQt5", "PyQt6", "PySide2", "PySide6",
              "vtkmodules.qt", "vtkmodules.web", "vtkmodules.gtk", "vtkmodules.tk", "vtkmodules.wx"],
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821
exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Mesh2CAD",
    console=False,
    icon=icon,
    argv_emulation=False,  # Tk handles files dropped on the Dock icon itself
    codesign_identity=None,
)
coll = COLLECT(exe, a.binaries, a.datas, name="Mesh2CAD")  # noqa: F821

if sys.platform == "darwin":
    app = BUNDLE(  # noqa: F821
        coll,
        name="Mesh2CAD.app",
        icon=icon,
        bundle_identifier="io.github.akrtek.mesh2cad",
        version=VERSION,
        info_plist={
            "CFBundleName": "Mesh2CAD",
            "CFBundleDisplayName": "Mesh2CAD",
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "NSHighResolutionCapable": True,
            "NSAppSleepDisabled": True,  # keep App Nap from pausing the window during long conversions
            "LSMinimumSystemVersion": "14.0",
            "CFBundleDocumentTypes": [{
                "CFBundleTypeName": "3D mesh",
                "CFBundleTypeRole": "Viewer",
                "LSHandlerRank": "Alternate",
                "CFBundleTypeExtensions": ["stl", "3mf", "obj", "ply", "off", "glb"],
            }],
        },
    )
