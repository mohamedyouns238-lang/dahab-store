"""نظام التحديث: رفع حزمة تحديث (zip) → فحص → فحص ذاتي معزول → تفعيل عند إعادة التشغيل → تراجع تلقائي عند الفشل.

لا يمس بيانات المحل أبدًا (قاعدة البيانات/الصور/النسخ). يستبدل الكود والقوالب والتنسيق فقط.
المكتبات الجديدة (pip) تحتاج مُثبِّتًا جديدًا؛ التحديث يغطي ملفات النظام نفسها.
"""
import hashlib
import io
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import zipfile

import bootstrap
import cloud
import safety
import version

log = logging.getLogger("dahab")
CODE_FILES = ("app.py", "safety.py", "cloud.py", "qrcode_py.py", "updater.py", "version.py")
REQUIRED = ("app.py", "safety.py", "version.py", "templates/base.html")
DIR_PREFIXES = ("templates/", "static/", "site/")
EXTS = {".html", ".css", ".js", ".png", ".jpg", ".jpeg", ".svg", ".ico", ".json", ".txt", ".webp", ".woff2", ".webmanifest"}
MAX_FILES, MAX_TOTAL = 600, 60 * 1024 * 1024
_lock = threading.Lock()


class UpdateError(Exception):
    pass


def supported():
    return os.environ.get("DAHAB_LAUNCHER") == "1"


def current_build():
    return int(version.BUILD)


def pending_build():
    return bootstrap.read_state().get("pending")


def info():
    st = bootstrap.read_state()
    cur = st.get("current")
    return dict(build=current_build(), version=version.VERSION, supported=supported(), pending=st.get("pending"),
                running_override=bool(os.environ.get("DAHAB_OVERRIDE")), previous=st.get("previous"),
                history=list(reversed(st.get("history", [])))[:12], bad=st.get("bad", []), current_state=cur)


# ------------------------------------------------------------------ package
def _safe_name(n):
    if not n or n.startswith(("/", "\\")) or "\\" in n or ".." in n.split("/") or ":" in n or "\0" in n or n.endswith("/"):
        return False
    if n in CODE_FILES:
        return True
    return n.startswith(DIR_PREFIXES) and os.path.splitext(n)[1].lower() in EXTS


def parse(zip_bytes):
    """يتحقق من الحزمة كاملة ويرجع (manifest, {path: bytes}) أو يرفع UpdateError برسالة عربية واضحة."""
    try:
        z = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        raise UpdateError("الملف ليس حزمة تحديث صالحة (zip تالف)")
    infos = [i for i in z.infolist() if not i.is_dir()]
    if len(infos) > MAX_FILES or sum(i.file_size for i in infos) > MAX_TOTAL:
        raise UpdateError("حجم الحزمة أكبر من المسموح")
    try:
        man = json.loads(z.read("manifest.json"))
        build, files = int(man["build"]), man["files"]
        assert isinstance(files, dict) and files
    except Exception:
        raise UpdateError("ملف manifest.json مفقود أو غير صالح")
    out = {}
    for i in infos:
        if i.filename == "manifest.json":
            continue
        if not _safe_name(i.filename):
            raise UpdateError(f"الحزمة تحتوي ملفًا غير مسموح به: {i.filename}")
        if i.filename not in files:
            raise UpdateError(f"ملف غير مدرج في manifest: {i.filename}")
        data = z.read(i.filename)
        if hashlib.sha256(data).hexdigest() != files[i.filename]:
            raise UpdateError(f"الملف تالف (البصمة لا تطابق): {i.filename}")
        out[i.filename] = data
    if set(files) != set(out):
        raise UpdateError("الحزمة ناقصة: ملفات مدرجة في manifest غير موجودة")
    for r in REQUIRED:
        if r not in out:
            raise UpdateError(f"الحزمة ناقصة: {r}")
    for n, data in out.items():
        if n.endswith(".py"):
            try:
                compile(data, n, "exec")
            except SyntaxError as e:
                raise UpdateError(f"خطأ برمجي في {n} سطر {e.lineno}")
    m = re.search(rb"^BUILD\s*=\s*(\d+)", out["version.py"], re.M)
    if not m or int(m.group(1)) != build:
        raise UpdateError("رقم الإصدار في version.py لا يطابق manifest")
    man["build"] = build
    return man, out


def _selftest(staged_dir):
    """يشغّل البرنامج نفسه (عملية منفصلة ببيانات مؤقتة) بالكود الجديد ليتأكد أنه يقلع ويعمل قبل تفعيله."""
    res = os.path.join(staged_dir, "selftest.json")
    env = {k: v for k, v in os.environ.items() if k not in ("DATA_DIR", "DB_PATH", "DAHAB_LAUNCHER", "PORT", "DAHAB_PORT")}
    env.update(DAHAB_OVERRIDE=staged_dir, DAHAB_OVERRIDE_FIXED="1", DAHAB_SELFTEST_OUT=res)
    launcher = os.path.join(os.path.dirname(os.path.abspath(bootstrap.__file__)), "dahab_app.py")
    cmd = [sys.executable, "--selftest"] if getattr(sys, "frozen", False) else [sys.executable, launcher, "--selftest"]
    try:
        p = subprocess.run(cmd, env=env, timeout=150, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise UpdateError("الفحص الذاتي للتحديث استغرق وقتًا أطول من المتوقع")
    try:
        with open(res, encoding="utf-8") as f:
            r = json.load(f)
    except (OSError, ValueError):
        r = {}
    if p.returncode != 0 or not r.get("ok"):
        raise UpdateError("فشل الفحص الذاتي للتحديث — لم يتم تفعيله (نظامك الحالي سليم). " + str(r.get("error", ""))[:200])
    return r


def stage(zip_bytes, source="upload"):
    """يجهّز التحديث: لا يغيّر شيئًا في النظام الشغّال. -> manifest. يُفعَّل عند إعادة التشغيل."""
    if not supported():
        raise UpdateError("التحديث التلقائي متاح في نسخة سطح المكتب (Dahab Store) فقط")
    with _lock:
        man, files = parse(zip_bytes)
        build = man["build"]
        st = bootstrap.read_state()
        newest = max(current_build(), int(st.get("pending") or 0))
        if build <= newest:
            raise UpdateError(f"هذا التحديث (رقم {build}) أقدم من أو مساوٍ للإصدار الحالي ({newest})")
        stg = os.path.join(bootstrap.upd_dir(), f"staging-{build}")
        shutil.rmtree(stg, ignore_errors=True)
        try:
            for n, data in files.items():
                p = os.path.join(stg, *n.split("/"))
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "wb") as f:
                    f.write(data)
            with open(os.path.join(stg, "manifest.json"), "w", encoding="utf-8") as f:
                json.dump(man, f, ensure_ascii=False)
            r = _selftest(stg)
            if r.get("build") != build:
                raise UpdateError("الفحص الذاتي شغّل إصدارًا مختلفًا عن المتوقع")
            final = bootstrap.version_dir(build)
            shutil.rmtree(final, ignore_errors=True)
            try:
                os.remove(os.path.join(stg, "selftest.json"))
            except OSError:
                pass
            os.replace(stg, final)
        except BaseException:
            shutil.rmtree(stg, ignore_errors=True)
            raise
        st = bootstrap.read_state()
        st["pending"] = build
        bootstrap._history(st, f"تجهيز الإصدار {build} ({man.get('version', '')}) من {'GitHub' if source == 'auto' else 'رفع يدوي'}: {man.get('notes', '')[:120]}")
        bootstrap.write_state(st)
        safety.set_status(update_error=None)
        return man


# ------------------------------------------------------------------ GitHub propagation
def _outbox():
    d = os.path.join(bootstrap.upd_dir(), "outbox")
    os.makedirs(d, exist_ok=True)
    return d


def queue_publish(zip_bytes, man):
    """يحفظ الحزمة لتُرفع إلى GitHub (ويُعاد المحاولة لو لا يوجد إنترنت)."""
    p = os.path.join(_outbox(), f"{man['build']}.zip")
    with open(p, "wb") as f:
        f.write(zip_bytes)


def flush_outbox():
    """يرفع الحزم المعلّقة. -> رسالة أو None."""
    msg = None
    try:
        names = sorted(os.listdir(_outbox()), key=lambda x: int(x.split(".")[0]) if x.split(".")[0].isdigit() else 0)
    except OSError:
        return None
    for n in names:
        p = os.path.join(_outbox(), n)
        try:
            data = open(p, "rb").read()
            man, _ = parse(data)
        except Exception:
            os.replace(p, p + ".bad")
            continue
        ok, msg = cloud.feed_publish(data, man)
        safety.set_status(feed_msg=msg, feed_ok=ok, feed_build=man["build"] if ok else None)
        if not ok:
            return msg
        os.remove(p)
    return msg


def check_remote(force=False):
    """يفحص مستودع GitHub؛ لو فيه إصدار أحدث يحمّله ويجهّزه ويطلب إعادة تشغيل عند الهدوء. -> رسالة."""
    st = cloud.settings()
    if not supported():
        return "غير مدعوم"
    if not force and not st["auto_update"]:
        return "التحديث التلقائي متوقف"
    flush_outbox()
    ok, latest = cloud.feed_latest(st)
    now = time.strftime("%Y-%m-%d %H:%M")
    if not ok:
        safety.set_status(update_check=now, update_check_msg=latest)
        return latest
    newest = max(current_build(), int(pending_build() or 0))
    if not latest or latest["build"] <= newest:
        safety.set_status(update_check=now, update_check_msg="النظام محدّث")
        return "النظام محدّث"
    if latest["build"] in bootstrap.read_state().get("bad", []) or (safety.get_status().get("update_failed_build") == latest["build"] and not force):
        return "إصدار سبق أن فشل على هذا الجهاز"
    ok, data = cloud.feed_download(latest, st)
    if not ok:
        safety.set_status(update_check=now, update_check_msg=data)
        return data
    try:
        man = stage(data, source="auto")
    except UpdateError as e:
        safety.set_status(update_check=now, update_check_msg=str(e), update_failed_build=latest["build"])
        log.warning("تحديث تلقائي مرفوض: %s", e)
        return str(e)
    bootstrap.request_restart("idle")
    msg = f"تم تحميل الإصدار {man['build']} وسيُفعَّل عند أول فترة هدوء أو عند إعادة التشغيل"
    safety.set_status(update_check=now, update_check_msg=msg)
    return msg
