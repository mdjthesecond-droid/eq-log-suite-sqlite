# PyInstaller build of the app window (eq_log_suite/desktop.py):
#
#     .venv/bin/pyinstaller --noconfirm packaging/eq-log-suite.spec
#
# (on Windows: packaging\build-windows.ps1 does the whole thing)
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

# The in-game overlays (GTK window, Vulkan layer) are their
# own Linux-only programs, run from source -- not part of the app.
excludes = ["tkinter", "eq_log_suite.overlay.window", "gi"]
hiddenimports = ["eq_log_suite.web.app"]
if sys.platform == "win32":
    excludes += ["PyQt6", "qtpy"]          # Windows draws the window with Edge WebView2
    # zoneinfo's time zones (tailer.py's rotation times): Windows has no
    # system copy, and zoneinfo finds the tzdata package by name at run time.
    from PyInstaller.utils.hooks import collect_data_files
    hiddenimports += ["tzdata"]
    datas += collect_data_files("tzdata")

a = Analysis(
    [str(ROOT / "packaging" / "launch.py")],
    pathex=[str(ROOT)],
    datas=datas,
    hiddenimports=hiddenimports,
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
