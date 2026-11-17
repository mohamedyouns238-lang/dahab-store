# -*- mode: python ; coding: utf-8 -*-
# بناء نسخة ويندوز:  pyinstaller DahabStore.spec --noconfirm
# الناتج: dist/DahabStore/DahabStore.exe  (مجلد واحد، لا يحتاج تثبيت Python على جهاز المحل)
a = Analysis(
    ["dahab_app.py"],
    pathex=["."],
    datas=[("templates", "templates"), ("static", "static"), ("assets", "assets")],
    hiddenimports=["app", "safety", "qrcode_py", "urllib.request", "waitress", "PIL", "PIL.Image", "PIL.ImageOps", "sqlite3"],
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
