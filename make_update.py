"""تجهيز ملف تحديث لـ Dahab Store.

    python make_update.py "وصف التعديلات"

ينتج  update_out/DahabStore-update-<رقم>.zip  (ارفعه من: النسخ الاحتياطي ▸ التحديثات).
يزيد رقم الإصدار في version.py تلقائيًا. الحزمة تحمل كل ملفات النظام (كود + قوالب + تنسيق).
"""
import hashlib
import json
import os
import re
import sys
import zipfile

BASE = os.path.dirname(os.path.abspath(__file__))
CODE = ("app.py", "safety.py", "cloud.py", "qrcode_py.py", "updater.py", "version.py")
DIRS = ("templates", "static", "site")
EXTS = {".html", ".css", ".js", ".png", ".jpg", ".jpeg", ".svg", ".ico", ".json", ".txt", ".webp", ".woff2", ".webmanifest"}


def main(argv):
    notes = " ".join(a for a in argv if not a.startswith("--")) or "تحديث"
    vp = os.path.join(BASE, "version.py")
    src = open(vp, encoding="utf-8").read()
    build = int(re.search(r"^BUILD\s*=\s*(\d+)", src, re.M).group(1))
    if "--no-bump" not in argv:
        build += 1
        src = re.sub(r"^BUILD\s*=\s*\d+", f"BUILD = {build}", src, flags=re.M)
        open(vp, "w", encoding="utf-8").write(src)
    ver = re.search(r'^VERSION\s*=\s*"([^"]*)"', src, re.M).group(1)
    files = {}
    for n in CODE:
        files[n] = open(os.path.join(BASE, n), "rb").read()
    for d in DIRS:
        for root, _, names in os.walk(os.path.join(BASE, d)):
            for n in names:
                if os.path.splitext(n)[1].lower() in EXTS:
                    p = os.path.join(root, n)
                    files[os.path.relpath(p, BASE).replace(os.sep, "/")] = open(p, "rb").read()
    man = dict(build=build, version=ver, notes=notes, files={k: hashlib.sha256(v).hexdigest() for k, v in sorted(files.items())})
    out = os.path.join(BASE, "update_out")
    os.makedirs(out, exist_ok=True)
    path = os.path.join(out, f"DahabStore-update-{build}.zip")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(man, ensure_ascii=False, indent=1))
        for k, v in sorted(files.items()):
            z.writestr(k, v)
    print(f"تم: {path}  (إصدار {build}، {len(files)} ملف)")
    return path


if __name__ == "__main__":
    main(sys.argv[1:])
