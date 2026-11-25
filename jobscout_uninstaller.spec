# -*- mode: python ; coding: utf-8 -*-
# Build: pyinstaller jobscout_uninstaller.spec  ->  dist/JobScoutUninstaller.exe
from PyInstaller.utils.hooks import collect_all

datas = []
binaries = []
hiddenimports = []
for pkg in ("keyring",):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    ["run_uninstaller.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "webview", "googleapiclient", "uvicorn", "fastapi", "anthropic"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="JobScoutUninstaller",
    debug=False,
    strip=False,
    upx=False,
    console=False,
    icon="static/icon.ico",
)
