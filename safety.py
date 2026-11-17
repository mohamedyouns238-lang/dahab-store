"""طبقة حماية البيانات — Dahab Store
- مكان التخزين الدائم (لا يتأثر بتحديث/إعادة تثبيت البرنامج)
- نسخ احتياطي تلقائي متحقَّق منه + نسخة في مجلد خارجي اختياري
- فحص سلامة قاعدة البيانات عند كل تشغيل واسترجاع تلقائي عند التلف
"""
import json
import logging
import os
import shutil
import sqlite3
import sys
import threading
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

BASE = os.path.dirname(os.path.abspath(__file__))


def resource_path(*parts):
    """مسار ملفات البرنامج (القوالب والتنسيق) سواء شغّال من الكود أو من نسخة exe."""
    ov = os.environ.get("DAHAB_OVERRIDE")                    # مجلد إصدار محدَّث (يتقدم على الملفات المدمجة)
    if ov and os.path.exists(os.path.join(ov, *parts)):
        return os.path.join(ov, *parts)
    return os.path.join(getattr(sys, "_MEIPASS", BASE), *parts)


def _data_dir():
    if os.environ.get("DB_PATH"):
        return os.path.dirname(os.path.abspath(os.environ["DB_PATH"]))
    if os.environ.get("DATA_DIR"):
        return os.environ["DATA_DIR"]
    if os.name == "nt":
        return os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "DahabStore")
    return BASE


DATA_DIR = _data_dir()
DB_PATH = os.environ.get("DB_PATH") or os.path.join(DATA_DIR, "store.db")
BACKUP_DIR = os.path.join(DATA_DIR, "backups")
LOG_DIR = os.path.join(DATA_DIR, "logs")
CORRUPT_DIR = os.path.join(DATA_DIR, "corrupt")
IMAGE_DIR = os.path.join(DATA_DIR, "images")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
STATUS_PATH = os.path.join(DATA_DIR, "status.json")
KEY_PATH = os.path.join(DATA_DIR, ".secret_key")
for _d in (DATA_DIR, BACKUP_DIR, LOG_DIR, IMAGE_DIR):
    os.makedirs(_d, exist_ok=True)

DEFAULTS = {"store_name": "Dahab Store", "currency": "ج.م", "extra_backup_dir": "",
            "silent_print": False, "lan_access": False, "port": 58321,
            "store_phone": "01144181826", "site_url": ""}
ROTATING = ("auto", "start", "exit")          # هذه فقط تُحذف تلقائيًا عند التدوير
log = logging.getLogger("dahab")
_lock = threading.RLock()


# ------------------------------------------------------------------ files
def _atomic_write(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        try:
            _atomic_write(CONFIG_PATH, json.dumps(cfg, ensure_ascii=False, indent=2))
        except OSError:
            pass
    except (ValueError, OSError):
        log.warning("config.json غير صالح — استُخدمت الإعدادات الافتراضية")
    return cfg


def save_config(cfg):
    _atomic_write(CONFIG_PATH, json.dumps(cfg, ensure_ascii=False, indent=2))


def get_status():
    try:
        with open(STATUS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def set_status(**kw):
    with _lock:
        st = get_status()
        st.update(kw)
        try:
            _atomic_write(STATUS_PATH, json.dumps(st, ensure_ascii=False, indent=2))
        except OSError:
            pass


_logging_ready = False


def setup_logging():
    global _logging_ready
    if _logging_ready:
        return
    _logging_ready = True
    h = RotatingFileHandler(os.path.join(LOG_DIR, "app.log"), maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(h)


# ------------------------------------------------------------------ validation
def is_valid_db(path, immutable=False):
    """قاعدة بيانات سليمة وتخص النظام (فيها جدول المستخدمين).
    immutable=True للنسخ الاحتياطية فقط: فحص بلا إنشاء أي ملفات جانبية (يعمل حتى على فلاشة محمية ضد الكتابة).
    القاعدة الحية تُفحص بدونها ليُعاد تشغيل سجل WAL بعد انقطاع الكهرباء."""
    try:
        uri = Path(path).resolve().as_uri() + "?mode=ro" + ("&immutable=1" if immutable else "")
        c = sqlite3.connect(uri, uri=True, timeout=10)
        try:
            ok = c.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            has = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'").fetchone() is not None
            return ok and has
        finally:
            c.close()
    except Exception:
        return False


# ------------------------------------------------------------------ backups
def _parse(name):
    try:
        return datetime.strptime(name[6:21], "%Y%m%d_%H%M%S"), name[22:-3]
    except ValueError:
        return None, None


def list_backups(folder=None):
    folder = folder or BACKUP_DIR
    out = []
    try:
        names = os.listdir(folder)
    except OSError:
        return out
    for n in names:
        if n.startswith("store_") and n.endswith(".db"):
            ts, reason = _parse(n)
            if ts:
                try:
                    out.append(dict(name=n, ts=ts, reason=reason, size=os.path.getsize(os.path.join(folder, n))))
                except OSError:
                    pass
    return sorted(out, key=lambda b: b["ts"], reverse=True)


def _clean_sidecars(folder):
    """يحذف مخلفات SQLite الجانبية (-wal/-shm/-journal/.tmp) جنب النسخ الاحتياطية؛ لا تحتوي بيانات مطلوبة."""
    try:
        names = os.listdir(folder)
    except OSError:
        return
    for n in names:
        if n.startswith("store_") and (n.endswith(("-wal", "-shm", "-journal", ".tmp")) or ".db.tmp" in n):
            try:
                os.remove(os.path.join(folder, n))
            except OSError:
                pass


def rotate(folder, now=None, keep_all_hours=24, keep_daily_days=90, min_keep=10):
    """يحتفظ بكل نسخ آخر يوم، ثم آخر نسخة من كل يوم لمدة 90 يومًا. النسخ اليدوية وقبل الاسترجاع لا تُحذف إلا الزائد عن 40."""
    now = now or datetime.now()
    allb = list_backups(folder)
    auto = [b for b in allb if b["reason"] in ROTATING]
    manual = [b for b in allb if b["reason"] not in ROTATING]
    delete, seen_days = [], set()
    for i, b in enumerate(auto):                   # من الأحدث للأقدم
        if i < min_keep or now - b["ts"] <= timedelta(hours=keep_all_hours):
            continue
        day = b["ts"].date()
        if now - b["ts"] > timedelta(days=keep_daily_days) or day in seen_days:
            delete.append(b)
        else:
            seen_days.add(day)
    delete += manual[40:]
    for b in delete:
        try:
            os.remove(os.path.join(folder, b["name"]))
        except OSError:
            pass
    _clean_sidecars(folder)


def make_backup(reason="auto"):
    """نسخة متسقة أثناء التشغيل (SQLite backup API)، تُفحص قبل اعتمادها، وتُنسخ للمجلد الخارجي إن وُجد."""
    with _lock:
        if not os.path.exists(DB_PATH):
            return None
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        final = os.path.join(BACKUP_DIR, f"store_{stamp}_{reason}.db")
        tmp = final + ".tmp"
        try:
            src = sqlite3.connect(DB_PATH, timeout=30)
            try:
                dst = sqlite3.connect(tmp)
                try:
                    src.backup(dst)
                    dst.execute("PRAGMA journal_mode=DELETE")      # ملف واحد مكتفٍ بذاته (بلا -wal/-shm)
                finally:
                    dst.close()
            finally:
                src.close()
            if not is_valid_db(tmp, immutable=True):
                raise RuntimeError("فشل التحقق من النسخة الاحتياطية")
            os.replace(tmp, final)
        except Exception as e:
            try:
                os.remove(tmp)
            except OSError:
                pass
            log.exception("backup failed")
            set_status(backup_error=f"{datetime.now():%Y-%m-%d %H:%M} — {e}")
            raise
        set_status(last_backup=datetime.now().isoformat(timespec="seconds"), backup_error="")
        rotate(BACKUP_DIR)
        extra = (load_config().get("extra_backup_dir") or "").strip()
        if extra:
            try:
                os.makedirs(extra, exist_ok=True)
                shutil.copy2(final, os.path.join(extra, os.path.basename(final)))
                rotate(extra)
                sync_images(extra)
                set_status(extra_ok=True, extra_msg=f"{datetime.now():%Y-%m-%d %H:%M}", extra_dir=extra)
            except Exception as e:
                log.exception("extra backup failed")
                set_status(extra_ok=False, extra_msg=f"{datetime.now():%Y-%m-%d %H:%M} — {e}", extra_dir=extra)
        return final


def sync_images(dest_root):
    """ينسخ صور المنتجات/العروض التي لم تُنسخ بعد إلى المجلد الخارجي (الصور لا تُحذف أبدًا من المصدر)."""
    dest = os.path.join(dest_root, "images")
    os.makedirs(dest, exist_ok=True)
    n = 0
    for name in os.listdir(IMAGE_DIR):
        target = os.path.join(dest, name)
        if not os.path.exists(target) and not name.endswith(".tmp"):
            shutil.copy2(os.path.join(IMAGE_DIR, name), target)
            n += 1
    return n


def _sig():
    out = []
    for p in (DB_PATH, DB_PATH + "-wal"):
        try:
            s = os.stat(p)
            out.append((s.st_mtime_ns, s.st_size))
        except OSError:
            out.append(None)
    return tuple(out)


class BackupThread(threading.Thread):
    """نسخة احتياطية كل 15 دقيقة عند وجود تغييرات."""

    def __init__(self, every=900):
        super().__init__(daemon=True, name="dahab-backup")
        self.every, self.stop = every, threading.Event()

    def run(self):
        last = _sig()
        while not self.stop.wait(self.every):
            cur = _sig()
            if cur != last:
                try:
                    make_backup("auto")
                    last = cur
                except Exception:
                    pass


# ------------------------------------------------------------------ startup check / restore
def _newest_valid_backup():
    cfg = load_config()
    folders = [BACKUP_DIR] + ([cfg["extra_backup_dir"]] if cfg.get("extra_backup_dir") else [])
    cands = []
    for f in folders:
        for b in list_backups(f):
            cands.append((b["ts"], os.path.join(f, b["name"])))
    for _, path in sorted(cands, reverse=True):
        if is_valid_db(path, immutable=True):
            return path
    return None


def _clear_db_files():
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(DB_PATH + suffix)
        except OSError:
            pass


def import_legacy(candidates=None):
    """أول تشغيل في مكان التخزين الدائم: لو وُجدت قاعدة قديمة بجوار البرنامج (الإصدارات السابقة) تُنسخ إليه.
    النسخ يتم بـ SQLite backup API (يشمل ما في ملف WAL) ولا يُغيَّر الملف الأصلي أبدًا."""
    cands = candidates if candidates is not None else [os.path.join(BASE, "store.db"), os.path.join(os.getcwd(), "store.db"),
                                                     os.path.join(os.path.dirname(BASE), "store.db")]
    mine = os.path.abspath(DB_PATH)
    for p in cands:
        p = os.path.abspath(p)
        if p == mine or not os.path.isfile(p) or os.path.getsize(p) == 0:
            continue
        tmp = DB_PATH + ".importing"
        try:
            src = sqlite3.connect(p, timeout=30)
            try:
                dst = sqlite3.connect(tmp)
                try:
                    src.backup(dst)
                    dst.execute("PRAGMA journal_mode=DELETE")      # ملف واحد مكتفٍ بذاته (بلا -wal/-shm)
                finally:
                    dst.close()
            finally:
                src.close()
            if not is_valid_db(tmp, immutable=True):
                raise RuntimeError("القاعدة القديمة غير سليمة")
            os.replace(tmp, DB_PATH)
            set_status(imported=dict(at=datetime.now().isoformat(timespec="seconds"), **{"from": p}))
            log.warning("تم استيراد القاعدة القديمة من %s", p)
            return p
        except Exception:
            log.exception("legacy import failed for %s", p)
            try:
                os.remove(tmp)
            except OSError:
                pass
    return None


def startup_check():
    """يُستدعى قبل فتح القاعدة. يرجع رسالة إن حدث استرجاع."""
    exists = os.path.exists(DB_PATH) and os.path.getsize(DB_PATH) > 0
    if not exists and import_legacy():
        return None
    if exists and is_valid_db(DB_PATH):
        return None
    msg = None
    if exists:                                            # تالفة: احتفظ بها للفحص ثم استرجع
        os.makedirs(CORRUPT_DIR, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(DB_PATH + suffix):
                shutil.copy2(DB_PATH + suffix, os.path.join(CORRUPT_DIR, f"store_{stamp}.db{suffix}"))
        log.error("قاعدة البيانات تالفة — محاولة الاسترجاع")
    best = _newest_valid_backup()
    if best:
        tmp = DB_PATH + ".restoring"
        shutil.copy2(best, tmp)
        _clear_db_files()
        os.replace(tmp, DB_PATH)
        msg = f"تم استرجاع قاعدة البيانات تلقائيًا من النسخة: {os.path.basename(best)}"
        log.warning(msg)
    elif exists:
        _clear_db_files()
        msg = "قاعدة البيانات كانت تالفة ولا توجد نسخة احتياطية سليمة؛ بدأ النظام بقاعدة جديدة. الملف التالف محفوظ في مجلد corrupt."
        log.error(msg)
    if msg:
        set_status(restored=dict(at=datetime.now().isoformat(timespec="seconds"), msg=msg))
    return msg


_thread = None


def start_backup_thread():
    global _thread
    if _thread is None:
        _thread = BackupThread()
        _thread.start()
    return _thread
