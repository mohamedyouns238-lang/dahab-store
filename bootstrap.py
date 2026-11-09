"""إقلاع آمن مع التحديثات — هذا الملف ثابت داخل التثبيت ولا يتغير بالتحديثات.

مجلد التحديثات:  <بيانات المحل>\\updates\\
  state.json  : current / previous / pending / trial / history
  v<build>\\   : نسخة كاملة من كود وقوالب ذلك الإصدار (تُحمَّل بدل المدمجة)
لو فشل إصدار جديد في الإقلاع 3 مرات متتالية، أو تعذّر تحميله، يرجع النظام تلقائيًا للإصدار السابق.
لا يلمس هذا الملف أي بيانات للمحل (قاعدة البيانات/الصور/النسخ الاحتياطية).
"""
import importlib
import importlib.util
import json
import logging
import os
import sys
import threading
import time

log = logging.getLogger("dahab")
LOAD_ORDER = ("version", "qrcode_py", "safety", "cloud", "updater")      # ثم app (تُحمَّل عند الحاجة)
RESTART = {"mode": None}          # None | "now" | "idle"  — يقرؤها المُشغّل
_lock = threading.RLock()


def data_dir():
    if os.environ.get("DB_PATH"):
        return os.path.dirname(os.path.abspath(os.environ["DB_PATH"]))
    if os.environ.get("DATA_DIR"):
        return os.environ["DATA_DIR"]
    if os.name == "nt":
        return os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "DahabStore")
    return os.path.dirname(os.path.abspath(__file__))


def upd_dir():
    d = os.path.join(data_dir(), "updates")
    os.makedirs(d, exist_ok=True)
    return d


def _state_path():
    return os.path.join(upd_dir(), "state.json")


def read_state():
    try:
        with open(_state_path(), encoding="utf-8") as f:
            st = json.load(f)
            return st if isinstance(st, dict) else {}
    except (OSError, ValueError):
        return {}


def write_state(st):
    with _lock:
        p = _state_path()
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)


def version_dir(build):
    return os.path.join(upd_dir(), f"v{int(build)}")


def _valid_dir(build):
    d = version_dir(build)
    return all(os.path.exists(os.path.join(d, x)) for x in ("app.py", "safety.py", "version.py", "manifest.json"))


def _history(st, msg):
    st.setdefault("history", []).append({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "msg": msg})
    st["history"] = st["history"][-40:]


def promote_pending():
    """عند الإقلاع: لو يوجد إصدار مُجهَّز ومفحوص يصبح هو الحالي."""
    st = read_state()
    b = st.get("pending")
    if not b:
        return st
    if not _valid_dir(b):
        _history(st, f"إصدار {b} المجهّز غير مكتمل — تم تجاهله")
        st["pending"] = None
    else:
        st["previous"], st["current"], st["pending"] = st.get("current"), b, None
        st["trial"] = {"build": b, "boots": 0}
        _history(st, f"تم تفعيل الإصدار {b}")
    write_state(st)
    return st


def rollback(reason):
    st = read_state()
    bad = st.get("current")
    if bad:
        st.setdefault("bad", []).append(bad)
    prev = st.get("previous")
    st["current"] = prev if prev and _valid_dir(prev) else None
    st["previous"], st["trial"] = None, None
    _history(st, f"تراجع تلقائي عن الإصدار {bad}: {reason}")
    write_state(st)
    log.error("تراجع عن الإصدار %s: %s", bad, reason)


def begin_boot():
    """يرجع مجلد الإصدار الفعّال (أو None = الكود المدمج)."""
    st = promote_pending()
    cur = st.get("current")
    if cur and not _valid_dir(cur):
        rollback("ملفات الإصدار ناقصة")
        st = read_state()
        cur = st.get("current")
    trial = st.get("trial")
    if cur and trial and trial.get("build") == cur:
        if trial.get("boots", 0) >= 2:                      # فشل التثبيت مرتين دون تأكيد
            rollback("فشل الإقلاع المتكرر")
            return None if not read_state().get("current") else version_dir(read_state()["current"])
        trial["boots"] = trial.get("boots", 0) + 1
        write_state(st)
    return version_dir(cur) if cur else None


def confirm_boot():
    """النظام اشتغل بثبات: ثبّت الإصدار واحذف نسخ الكود القديمة (الكود فقط، لا بيانات)."""
    st = read_state()
    if st.get("trial"):
        _history(st, f"تأكد نجاح الإصدار {st['trial'].get('build')}")
        st["trial"] = None
        write_state(st)
    keep = {f"v{b}" for b in (st.get("current"), st.get("previous"), st.get("pending")) if b}
    import shutil
    try:
        for n in os.listdir(upd_dir()):
            p = os.path.join(upd_dir(), n)
            if os.path.isdir(p) and (n.startswith("v") or n.startswith("staging")) and n not in keep:
                shutil.rmtree(p, ignore_errors=True)
    except OSError:
        pass


def _exec(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return mod


def purge():
    for n in LOAD_ORDER + ("app",):
        sys.modules.pop(n, None)


def load(name, override_dir):
    """يحمّل وحدة من مجلد الإصدار صراحةً (لا يعتمد على ترتيب البحث في PyInstaller)."""
    if override_dir:
        p = os.path.join(override_dir, name + ".py")
        if os.path.exists(p):
            return _exec(name, p)
    return importlib.import_module(name)


def load_all(override_dir):
    """يحمّل الوحدات المساندة ثم app. عند أي خطأ في كود الإصدار الجديد: تراجع تلقائي ثم الكود المدمج."""
    if override_dir:
        os.environ["DAHAB_OVERRIDE"] = override_dir
    else:
        os.environ.pop("DAHAB_OVERRIDE", None)
    try:
        purge()
        for n in LOAD_ORDER:
            load(n, override_dir)
        return load("app", override_dir)
    except BaseException as e:
        if not override_dir or os.environ.get("DAHAB_OVERRIDE_FIXED"):    # الفحص الذاتي: أبلغ بالفشل ولا تتراجع
            raise
        log.exception("فشل تحميل الإصدار الجديد")
        purge()
        rollback(f"خطأ أثناء التحميل: {type(e).__name__}: {e}")
        os.environ.pop("DAHAB_OVERRIDE", None)
        for n in LOAD_ORDER:
            load(n, None)
        return load("app", None)


def request_restart(mode="now"):
    if RESTART["mode"] != "now":
        RESTART["mode"] = mode
