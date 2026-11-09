# -*- mode: python ; coding: utf-8 -*-
# بناء نسخة ويندوز:  pyinstaller DahabStore.spec --noconfirm
# الناتج: dist/DahabStore/DahabStore.exe  (مجلد واحد، لا يحتاج تثبيت Python على جهاز المحل)
import os
_extra = [("build_defaults.json", ".")] if os.path.exists("build_defaults.json") else []
a = Analysis(
    ["dahab_app.py"],
    pathex=["."],
    datas=[("templates", "templates"), ("static", "static"), ("assets", "assets"), ("site", "site")] + _extra,
    hiddenimports=["app", "safety", "bootstrap", "cloud", "updater", "version", "qrcode_py", "urllib.request", "zipfile", "waitress", "PIL", "PIL.Image", "PIL.ImageOps", "sqlite3"],
    excludes=["tkinter", "matplotlib", "numpy", "pandas", "scipy", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="DahabStore",
    console=False,                    # بدون نافذة سوداء
    icon="assets/dahab.ico",
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="DahabStore", upx=False)
