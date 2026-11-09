"""اختبار طبقة الحماية: نسخ، تدوير، اكتشاف تلف، استرجاع تلقائي، استيراد قاعدة قديمة، نسخ الصور."""
import os, sqlite3, tempfile, shutil
from datetime import datetime, timedelta
tmp = tempfile.mkdtemp(); os.environ["DB_PATH"] = os.path.join(tmp, "store.db")
import safety as S

def mk(rows):
    c = sqlite3.connect(S.DB_PATH); c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, n TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS t(x)")
    for r in range(rows): c.execute("INSERT INTO t VALUES(?)", (r,))
    c.commit(); c.close()

# --- استيراد قاعدة قديمة (بجوار البرنامج) عند أول تشغيل، بما فيها ما لم يُكتب بعد في ملف WAL
legacy_dir = os.path.join(tmp, "oldapp"); os.makedirs(legacy_dir)
lp = os.path.join(legacy_dir, "store.db")
lc = sqlite3.connect(lp); lc.execute("PRAGMA journal_mode=WAL")
lc.execute("CREATE TABLE users(id INTEGER PRIMARY KEY, n TEXT)"); lc.execute("CREATE TABLE t(x)")
for i in range(7): lc.execute("INSERT INTO t VALUES(?)", (i,))
lc.commit()                                   # لا نغلق الاتصال: البيانات في WAL
assert not os.path.exists(S.DB_PATH)
assert S.import_legacy([lp]) == os.path.abspath(lp)
c = sqlite3.connect(S.DB_PATH); assert c.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 7; c.close()
assert S.get_status()["imported"]["from"] == os.path.abspath(lp)
lc.close(); assert os.path.getsize(lp) > 0, "original untouched"
assert S.import_legacy([lp]) == os.path.abspath(lp)        # يعيد النسخ فقط عند الاستدعاء؛ startup_check لا يستدعيه والقاعدة موجودة
os.remove(S.DB_PATH)
for suf in ("-wal", "-shm"):
    try: os.remove(S.DB_PATH + suf)
    except OSError: pass

assert S.startup_check() is None or True
if os.path.exists(S.DB_PATH): os.remove(S.DB_PATH)
mk(50)
b1 = S.make_backup("start"); assert b1 and S.is_valid_db(b1)
mk(10); b2 = S.make_backup("auto")
ex = os.path.join(tmp, "usb"); cfg = S.load_config(); cfg["extra_backup_dir"] = ex; S.save_config(cfg)
open(os.path.join(S.IMAGE_DIR, "a" * 24 + ".jpg"), "wb").write(b"img1")
b3 = S.make_backup("manual"); assert os.path.exists(os.path.join(ex, os.path.basename(b3))), "extra copy"
assert S.get_status()["extra_ok"] is True
assert open(os.path.join(ex, "images", "a" * 24 + ".jpg"), "rb").read() == b"img1", "images mirrored"
open(os.path.join(S.IMAGE_DIR, "b" * 24 + ".jpg"), "wb").write(b"img2"); S.make_backup("manual")
assert os.path.exists(os.path.join(ex, "images", "b" * 24 + ".jpg")), "new image mirrored"

# تدوير
now = datetime.now()
for d in range(0, 120):
    for h in (1, 5, 9):
        ts = (now - timedelta(days=d, hours=h)).strftime("%Y%m%d_%H%M%S")
        shutil.copy2(b1, os.path.join(S.BACKUP_DIR, f"store_{ts}_auto.db"))
before = len(S.list_backups()); S.rotate(S.BACKUP_DIR, now=now); after = S.list_backups()
old = [b for b in after if b["ts"] < now - timedelta(days=5) and b["reason"] == "auto"]
days = {b["ts"].date() for b in old}
assert len(old) == len(days) and len(after) < before, (len(old), len(days), before, len(after))
assert max(b["ts"] for b in after) >= now - timedelta(seconds=5)
assert any(b["reason"] == "manual" for b in after), "manual kept"
assert not any(b["ts"] < now - timedelta(days=91) and b["reason"] == "auto" for b in after)
assert os.listdir(os.path.join(S.IMAGE_DIR)), "images never rotated"

# تلف -> استرجاع تلقائي
with open(S.DB_PATH, "r+b") as f: f.seek(0); f.write(os.urandom(4096))
assert not S.is_valid_db(S.DB_PATH)
msg = S.startup_check(); assert msg and "استرجاع" in msg, msg
assert S.is_valid_db(S.DB_PATH)
c = sqlite3.connect(S.DB_PATH); assert c.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 60; c.close()
assert os.listdir(S.CORRUPT_DIR), "corrupt copy kept"
assert S.get_status()["restored"]["msg"] == msg
os.remove(S.DB_PATH)
for suf in ("-wal", "-shm"):
    try: os.remove(S.DB_PATH + suf)
    except OSError: pass
assert S.startup_check() and S.is_valid_db(S.DB_PATH)
open(os.path.join(S.BACKUP_DIR, "store_29990101_000000_auto.db"), "wb").write(b"junk")
os.remove(S.DB_PATH); S.startup_check(); assert S.is_valid_db(S.DB_PATH), "skips junk backup"
print("SAFETY OK")

# ---------- نسخ احتياطية ملف واحد بلا مخلفات، وانقطاع كهرباء (بيانات في WAL فقط) لا يُعتبر تلفًا
import glob
mk(5); S.make_backup("manual"); S.make_backup("auto")
litter = [n for n in os.listdir(S.BACKUP_DIR) if n.endswith(("-wal", "-shm", "-journal", ".tmp"))]
assert not litter, litter
checked = 0
for b in S.list_backups():
    p = os.path.join(S.BACKUP_DIR, b["name"])
    if b["name"] == "store_29990101_000000_auto.db":      # ملف junk وضعه اختبار سابق عمدًا
        continue
    with open(p, "rb") as f:
        hdr = f.read(20)
    if len(hdr) == 20 and b["reason"] in ("manual", "auto", "start", "exit"):
        # الإصدارات السابقة كانت تحفظ WAL؛ المطلوب أن النسخ الجديدة فقط ملف واحد
        if b["ts"] > datetime.now() - timedelta(minutes=5):
            assert hdr[18] == 1 and hdr[19] == 1, ("backup is a rollback-journal (single-file) database", b["name"])
            checked += 1
assert checked >= 2, checked
open(os.path.join(S.BACKUP_DIR, "store_20200101_000000_auto.db-wal"), "wb").write(b"x")     # مخلفات قديمة
open(os.path.join(S.BACKUP_DIR, "store_20200101_000000_auto.db.tmp-shm"), "wb").write(b"x")
S.rotate(S.BACKUP_DIR)
assert not [n for n in os.listdir(S.BACKUP_DIR) if n.endswith(("-wal", "-shm"))], "old sidecar litter cleaned"
# نسخة على وسط للقراءة فقط (فلاشة محمية) تُفحص وتُستخدم في الاسترجاع بلا كتابة أي ملف جانبي
ro = os.path.join(tmp, "ro"); os.makedirs(ro)
real = next(b for b in S.list_backups() if b["name"] != "store_29990101_000000_auto.db")
shutil.copy2(os.path.join(S.BACKUP_DIR, real["name"]), os.path.join(ro, "store_20260101_000000_manual.db"))
os.chmod(os.path.join(ro, "store_20260101_000000_manual.db"), 0o444); os.chmod(ro, 0o555)
try:
    assert S.is_valid_db(os.path.join(ro, "store_20260101_000000_manual.db"), immutable=True)
    assert os.listdir(ro) == ["store_20260101_000000_manual.db"], "validation wrote nothing"
finally:
    os.chmod(ro, 0o755)
# انقطاع الكهرباء: صفوف مؤكَّدة موجودة في ملف -wal فقط
crash = os.path.join(tmp, "crash"); os.makedirs(crash)
c0 = sqlite3.connect(os.path.join(crash, "live.db")); c0.execute("PRAGMA journal_mode=WAL"); c0.execute("PRAGMA wal_autocheckpoint=0")
c0.execute("CREATE TABLE users(id INTEGER PRIMARY KEY)"); c0.execute("CREATE TABLE t(x)"); c0.commit(); c0.execute("PRAGMA wal_checkpoint(TRUNCATE)")
for i in range(300): c0.execute("INSERT INTO t VALUES(?)", (i,))
c0.commit()
shutil.copy2(os.path.join(crash, "live.db"), os.path.join(crash, "after.db")); shutil.copy2(os.path.join(crash, "live.db-wal"), os.path.join(crash, "after.db-wal"))
assert S.is_valid_db(os.path.join(crash, "after.db")), "hot WAL after a power cut is recoverable, not 'corrupt'"
assert sqlite3.connect(os.path.join(crash, "after.db")).execute("SELECT COUNT(*) FROM t").fetchone()[0] == 300
print("SAFETY EXTRA OK")
