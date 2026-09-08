# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

from utils.packaging import filter_portable_binaries

datas = [('ui\\assets', 'ui\\assets')]
binaries = []
hiddenimports = ['pytesseract', 'fitz', 'PIL.Image', 'pypdf']
tmp_ret = collect_all('selenium')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('pymupdf')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('pytesseract')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('pypdf')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('openpyxl')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
# Do not collect the whole PyQt6 installation. PyInstaller's standard PyQt6
# hooks follow the QtCore/QtGui/QtWidgets imports in ui.main_window and collect
# their required plugins. collect_all('PyQt6') also pulled in unused QtQuick,
# QML, 3D, multimedia, web, and SQL assets, producing a huge one-file archive
# that was slow and unreliable to extract at startup.


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # pytesseract and Pillow expose optional pandas/numpy and Tk integrations.
    # MIAP00 never uses them, but allowing PyInstaller to follow those imports
    # adds pandas, numpy, Tcl/Tk, and the complete tzdata database to the one-file
    # archive. Besides adding substantial size, that unused payload has caused
    # Windows one-file extraction failures on launch.
    excludes=[
        'PyQt5',
        'PySide2',
        'PySide6',
        'pandas',
        'numpy',
        'tzdata',
        'tkinter',
        '_tkinter',
        'matplotlib',
    ],
    noarchive=False,
    optimize=0,
)
# A developer shell can add private native runtimes to PATH. PyInstaller may
# mistake their Windows API forwarders and C runtime for application DLLs,
# causing QtCore to load incompatible procedures on another computer.
a.binaries = filter_portable_binaries(a.binaries)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='MIAP00 Orders Collector',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    # None makes the bootloader resolve the current Windows user's temporary
    # directory at launch. Do not evaluate LOCALAPPDATA here: spec files run on
    # the build machine, which would embed the builder's user profile path.
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    icon=['ui\\assets\\miap00_app_icon.ico'],
    codesign_identity=None,
    entitlements_file=None,
)
