# PyInstaller build of the app window (eq_log_suite/desktop.py):
#
#     .venv/bin/pyinstaller --noconfirm packaging/eq-log-suite.spec
#
# -> dist/EQ Log Suite/ (one folder: run "EQ Log Suite" / "EQ Log Suite.exe"
# inside it). Build on the OS you're building for -- a Windows build has to be
# made on Windows. Config and the database go to a per-user folder, not next
# to the app (db._data_root).
import sys
from pathlib import Path

ROOT = Path(SPECPATH).parent
PKG = ROOT / "eq_log_suite"

datas = [
    (str(PKG / "web" / "templates"), "eq_log_suite/web/templates"),
    (str(PKG / "web" / "static"), "eq_log_suite/web/static"),
    (str(PKG / "schema.sql"), "eq_log_suite"),
    (str(ROOT / "config" / "local.example.yaml"), "config"),
]

# The in-game overlays (GTK window, MangoHud writer, Vulkan layer) are their
# own Linux-only programs, run from source -- not part of the app.
excludes = ["tkinter", "eq_log_suite.overlay.window", "eq_log_suite.overlay.mangohud_writer", "gi"]
if sys.platform == "win32":
    excludes += ["PyQt6", "qtpy"]          # Windows draws the window with Edge WebView2

a = Analysis(
    [str(ROOT / "packaging" / "launch.py")],
    pathex=[str(ROOT)],
    datas=datas,
    hiddenimports=["eq_log_suite.web.app"],
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="EQ Log Suite",
    console=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="EQ Log Suite")
