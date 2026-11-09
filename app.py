"""Dahab Store — نظام إدارة محل مستحضرات تجميل (Flask + SQLite)
تشغيل للتطوير:  python app.py   ثم افتح http://localhost:5000
نسخة ويندوز:    dahab_app.py (تتحول إلى DahabStore.exe)
"""
import csv
import hashlib
import io
import logging
import math
import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from functools import wraps

from flask import (Flask, Response, abort, flash, g, jsonify, redirect,
                   render_template, request, send_file, send_from_directory, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

import bootstrap
import cloud
import qrcode_py
import safety
import updater
import version

BASE = safety.BASE
DB_PATH = safety.DB_PATH
KEY_PATH = safety.KEY_PATH
safety.setup_logging()
log = logging.getLogger("dahab")
CFG = safety.load_config()
STORE_NAME = os.environ.get("STORE_NAME") or CFG["store_name"]
CURRENCY = os.environ.get("CURRENCY") or CFG["currency"]
PER_PAGE = 40
LAST_PING = [time.time()]
LAST_ACTIVITY = [time.time()]      # آخر نشاط فعلي (غير /ping) — لتأجيل إعادة التشغيل للتحديث لوقت الهدوء

app = Flask(__name__, template_folder=safety.resource_path("templates"),
            static_folder=safety.resource_path("static"))
app.config["MAX_CONTENT_LENGTH"] = 300 * 1024 * 1024


def _secret():
    if os.path.exists(KEY_PATH):
        with open(KEY_PATH) as f:
            return f.read().strip()
    k = secrets.token_hex(32)
    with open(KEY_PATH, "w") as f:
        f.write(k)
        f.flush()
        os.fsync(f.fileno())
    return k


app.secret_key = os.environ.get("SECRET_KEY") or _secret()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  PERMANENT_SESSION_LIFETIME=timedelta(hours=12))

ROLES = {"admin": "مدير النظام", "manager": "مدير", "cashier": "كاشير"}
ALL = ("admin", "manager", "cashier")
MGR = ("admin", "manager")
METHODS = {"cash": "نقدي", "card": "بطاقة", "transfer": "تحويل / محفظة", "credit": "آجل"}

# ----------------------------------------------------------------- database
SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
  full_name TEXT NOT NULL, role TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
  must_change INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS categories(id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS brands(id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS suppliers(id INTEGER PRIMARY KEY, name TEXT NOT NULL, phone TEXT, address TEXT, active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS customers(id INTEGER PRIMARY KEY, name TEXT NOT NULL, phone TEXT, address TEXT, active INTEGER NOT NULL DEFAULT 1,
  discount REAL NOT NULL DEFAULT 0, phone_key TEXT);
CREATE TABLE IF NOT EXISTS products(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, barcode TEXT UNIQUE,
  category_id INTEGER REFERENCES categories(id), brand_id INTEGER REFERENCES brands(id),
  cost REAL NOT NULL DEFAULT 0, price REAL NOT NULL DEFAULT 0,
  stock INTEGER NOT NULL DEFAULT 0, min_qty INTEGER NOT NULL DEFAULT 0,
  active INTEGER NOT NULL DEFAULT 1, image TEXT);
CREATE INDEX IF NOT EXISTS idx_products_name ON products(name);
CREATE TABLE IF NOT EXISTS sales(
  id INTEGER PRIMARY KEY, invoice_no TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL,
  customer_id INTEGER REFERENCES customers(id), user_id INTEGER REFERENCES users(id),
  subtotal REAL NOT NULL, discount REAL NOT NULL DEFAULT 0, total REAL NOT NULL,
  paid REAL NOT NULL DEFAULT 0, method TEXT NOT NULL DEFAULT 'cash',
  status TEXT NOT NULL DEFAULT 'active', discount_note TEXT);
CREATE INDEX IF NOT EXISTS idx_sales_created ON sales(created_at);
CREATE TABLE IF NOT EXISTS sale_items(
  id INTEGER PRIMARY KEY, sale_id INTEGER NOT NULL REFERENCES sales(id),
  product_id INTEGER NOT NULL REFERENCES products(id),
  qty INTEGER NOT NULL, price REAL NOT NULL, cost REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_sale_items_sale ON sale_items(sale_id);
CREATE TABLE IF NOT EXISTS purchases(
  id INTEGER PRIMARY KEY, ref TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL,
  supplier_id INTEGER REFERENCES suppliers(id), user_id INTEGER REFERENCES users(id),
  total REAL NOT NULL, paid REAL NOT NULL DEFAULT 0, note TEXT,
  status TEXT NOT NULL DEFAULT 'active');
CREATE TABLE IF NOT EXISTS purchase_items(
  id INTEGER PRIMARY KEY, purchase_id INTEGER NOT NULL REFERENCES purchases(id),
  product_id INTEGER NOT NULL REFERENCES products(id), qty INTEGER NOT NULL, cost REAL NOT NULL);
CREATE TABLE IF NOT EXISTS expenses(
  id INTEGER PRIMARY KEY, spent_on TEXT NOT NULL, title TEXT NOT NULL,
  amount REAL NOT NULL, user_id INTEGER REFERENCES users(id),
  voided INTEGER NOT NULL DEFAULT 0, void_reason TEXT);
CREATE TABLE IF NOT EXISTS audit_log(
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, user_id INTEGER, username TEXT,
  action TEXT NOT NULL, details TEXT);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
CREATE TABLE IF NOT EXISTS stock_moves(
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, product_id INTEGER NOT NULL REFERENCES products(id),
  delta INTEGER NOT NULL, reason TEXT NOT NULL, ref TEXT, note TEXT, user_id INTEGER);
CREATE INDEX IF NOT EXISTS idx_moves_prod ON stock_moves(product_id);
CREATE TABLE IF NOT EXISTS offers(
  id INTEGER PRIMARY KEY, title TEXT NOT NULL, details TEXT, product_id INTEGER REFERENCES products(id),
  percent REAL NOT NULL DEFAULT 0, starts_on TEXT NOT NULL DEFAULT '', ends_on TEXT NOT NULL DEFAULT '',
  image TEXT, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, created_by INTEGER);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""
SCHEMA_VERSION = 3
LOOKUPS = ("categories", "brands", "suppliers", "customers")
# لا يُحذف أي صف من أي جدول نهائيًا (يُفرض على مستوى قاعدة البيانات نفسها)
NO_DELETE = ("users", "categories", "brands", "suppliers", "customers", "products", "sales", "sale_items",
             "purchases", "purchase_items", "expenses", "audit_log", "stock_moves", "offers")
IMMUTABLE = ("audit_log", "stock_moves", "sale_items", "purchase_items")
REASONS = {"opening": "رصيد افتتاحي", "sale": "بيع", "sale_cancel": "إلغاء بيع", "purchase": "شراء",
           "purchase_cancel": "إلغاء شراء", "manual_edit": "تعديل يدوي", "stocktake": "جرد"}


def triggers_sql():
    s = [f"CREATE TRIGGER IF NOT EXISTS nodel_{t} BEFORE DELETE ON {t} "
         f"BEGIN SELECT RAISE(ABORT,'ممنوع حذف البيانات نهائيا'); END;" for t in NO_DELETE]
    s += [f"CREATE TRIGGER IF NOT EXISTS noupd_{t} BEFORE UPDATE ON {t} "
          f"BEGIN SELECT RAISE(ABORT,'السجل غير قابل للتعديل'); END;" for t in IMMUTABLE]
    s.append("CREATE TRIGGER IF NOT EXISTS stock_nonneg_u BEFORE UPDATE OF stock ON products WHEN NEW.stock<0 "
             "BEGIN SELECT RAISE(ABORT,'المخزون لا يمكن أن يكون سالبا'); END;")
    s.append("CREATE TRIGGER IF NOT EXISTS stock_nonneg_i BEFORE INSERT ON products WHEN NEW.stock<0 "
             "BEGIN SELECT RAISE(ABORT,'المخزون لا يمكن أن يكون سالبا'); END;")
    return "\n".join(s)


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, isolation_level=None, timeout=15)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys=ON")
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA synchronous=FULL")      # لا ضياع بيانات عند انقطاع الكهرباء
    return g.db


@app.teardown_appcontext
def close_db(_e=None):
    d = g.pop("db", None)
    if d:
        d.close()


def q(sql, args=(), one=False):
    rows = db().execute(sql, args).fetchall()
    if one:
        return rows[0] if rows else None
    return rows


def ex(sql, args=()):
    return db().execute(sql, args).lastrowid


def scalar(sql, args=()):
    return (q(sql, args, one=True)[0]) or 0


@contextmanager
def tx():
    c = db()
    c.execute("BEGIN IMMEDIATE")
    try:
        yield c
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise


def _cols(c, t):
    return {r[1] for r in c.execute(f"PRAGMA table_info({t})")}


def migrate(c):
    """ترقية آمنة للبيانات الموجودة؛ تُنفَّذ مرة واحدة لكل إصدار."""
    ver = c.execute("PRAGMA user_version").fetchone()[0]
    if ver >= SCHEMA_VERSION:
        return
    for t in LOOKUPS:
        if "active" not in _cols(c, t):
            c.execute(f"ALTER TABLE {t} ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
    if "voided" not in _cols(c, "expenses"):
        c.execute("ALTER TABLE expenses ADD COLUMN voided INTEGER NOT NULL DEFAULT 0")
        c.execute("ALTER TABLE expenses ADD COLUMN void_reason TEXT")
    c.execute("""INSERT INTO stock_moves(ts,product_id,delta,reason,ref,note)
                 SELECT ?, p.id, p.stock, 'opening', '', 'رصيد افتتاحي' FROM products p
                 WHERE p.stock<>0 AND NOT EXISTS(SELECT 1 FROM stock_moves m WHERE m.product_id=p.id)""", (now(),))
    # --- الإصدار 3: صور المنتجات، العروض، بيانات وخصومات العملاء
    for t, col, ddl in (("products", "image", "TEXT"), ("customers", "discount", "REAL NOT NULL DEFAULT 0"),
                        ("customers", "phone_key", "TEXT"), ("sales", "discount_note", "TEXT")):
        if col not in _cols(c, t):
            c.execute(f"ALTER TABLE {t} ADD COLUMN {col} {ddl}")
    c.execute("CREATE INDEX IF NOT EXISTS idx_customers_phone ON customers(phone_key)")
    for r in c.execute("SELECT id, phone FROM customers WHERE phone_key IS NULL AND phone IS NOT NULL AND phone<>''").fetchall():
        c.execute("UPDATE customers SET phone_key=? WHERE id=?", (phone_key(r[1]) or None, r[0]))
    c.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


def init_db():
    if os.path.exists(DB_PATH) and os.path.getsize(DB_PATH) > 0:       # نسخة أمان قبل أي ترقية
        try:
            c0 = sqlite3.connect(DB_PATH)
            old = c0.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION
            c0.close()
            if old:
                safety.make_backup("pre_upgrade")
        except Exception:
            log.exception("pre-upgrade backup")
    c = sqlite3.connect(DB_PATH, isolation_level=None)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=FULL")
    c.executescript(SCHEMA)
    migrate(c)
    c.executescript(triggers_sql())
    if not c.execute("SELECT 1 FROM users LIMIT 1").fetchone():
        pw = os.environ.get("ADMIN_PASSWORD", "admin123")
        c.execute("INSERT INTO users(username,password_hash,full_name,role,must_change) VALUES(?,?,?,?,1)",
                  ("admin", generate_password_hash(pw), "مدير النظام", "admin"))
    c.close()


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def phone_key(s):
    """رقم الهاتف كأرقام فقط وبصيغة موحّدة (للبحث والمطابقة): +20 100 123 4567 → 01001234567"""
    d = re.sub(r"\D", "", s or "")
    if d.startswith("0020"):
        d = d[4:]
    if d.startswith("20") and len(d) == 12:
        d = "0" + d[2:]
    elif len(d) == 10 and d.startswith("1"):
        d = "0" + d
    return d


def get_setting(key, default=""):
    r = q("SELECT value FROM settings WHERE key=?", (key,), one=True)
    return r["value"] if r else default


def set_setting(key, value):
    ex("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


MAX_IMG = 8 * 1024 * 1024
IMG_MAGIC = ((b"\xff\xd8\xff", "jpg"), (b"\x89PNG", "png"), (b"RIFF", "webp"), (b"GIF8", "gif"))


def save_image(f):
    """يحفظ صورة مرفوعة (تُصغَّر وتُضغَط) في مجلد الصور الدائم ويرجع اسم الملف. الصور لا تُحذف أبدًا."""
    if not f or not f.filename:
        return None
    raw = f.read(MAX_IMG + 1)
    if not raw:
        return None
    if len(raw) > MAX_IMG:
        raise ValueError("حجم الصورة أكبر من 8 ميجا")
    try:
        from PIL import Image, ImageOps
    except ImportError:
        ext = next((e for m, e in IMG_MAGIC if raw.startswith(m)), None)
        if not ext or len(raw) > 1_500_000:
            raise ValueError("الصورة غير صالحة أو أكبر من 1.5 ميجا")
        data = raw
    else:
        try:
            Image.MAX_IMAGE_PIXELS = 40_000_000
            im = Image.open(io.BytesIO(raw))
            im.load()
            im = ImageOps.exif_transpose(im)
            if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
                rgba = im.convert("RGBA")
                bg = Image.new("RGB", rgba.size, (255, 255, 255))
                bg.paste(rgba, mask=rgba.split()[3])
                im = bg
            else:
                im = im.convert("RGB")
            im.thumbnail((800, 800))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=84, optimize=True)
            data, ext = buf.getvalue(), "jpg"
        except Exception:
            raise ValueError("الملف ليس صورة صالحة (استخدم JPG أو PNG)")
    name = hashlib.sha256(data).hexdigest()[:24] + "." + ext
    path = os.path.join(safety.IMAGE_DIR, name)
    if not os.path.exists(path):
        tmp = path + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    return name


app.jinja_env.globals["img_url"] = lambda n: url_for("media", name=n) if n else ""


def offer_pct(pid, cx=None, today=None):
    """أعلى نسبة خصم عرض سارٍ اليوم على المنتج (0 لو لا يوجد)."""
    t = today or date.today().isoformat()
    r = (cx or db()).execute("""SELECT MAX(percent) m FROM offers WHERE active=1 AND product_id=? AND percent>0
                                AND (starts_on='' OR starts_on<=?) AND (ends_on='' OR ends_on>=?)""", (pid, t, t)).fetchone()
    return float(r["m"] or 0)


def active_offers(limit=None):
    t = date.today().isoformat()
    sql = """SELECT o.*, p.name pname, p.price pprice FROM offers o LEFT JOIN products p ON p.id=o.product_id
             WHERE o.active=1 AND (o.starts_on='' OR o.starts_on<=?) AND (o.ends_on='' OR o.ends_on>=?) ORDER BY o.id DESC"""
    return q(sql + (f" LIMIT {int(limit)}" if limit else ""), (t, t))


def offer_status(o):
    t = date.today().isoformat()
    if not o["active"]:
        return "مؤرشف"
    if o["starts_on"] and o["starts_on"] > t:
        return "لم يبدأ"
    if o["ends_on"] and o["ends_on"] < t:
        return "منتهي"
    return "نشط"


app.jinja_env.globals["offer_status"] = offer_status


def move(c, pid, delta, reason, ref="", note=""):
    """الطريقة الوحيدة لتغيير المخزون: تحدّث الرصيد وتسجّل الحركة في دفتر المخزون."""
    c.execute("UPDATE products SET stock=stock+? WHERE id=?", (delta, pid))
    u = g.get("user")
    c.execute("INSERT INTO stock_moves(ts,product_id,delta,reason,ref,note,user_id) VALUES(?,?,?,?,?,?,?)",
              (now(), pid, delta, reason, ref, note, u["id"] if u else None))


def reconcile():
    """منتجات رصيدها لا يطابق مجموع حركاتها (يجب أن تكون صفرًا دائمًا)."""
    return q("""SELECT p.id, p.name, p.stock, COALESCE(SUM(m.delta),0) AS ledger FROM products p
                LEFT JOIN stock_moves m ON m.product_id=p.id GROUP BY p.id HAVING p.stock<>ledger""")


def audit(action, details="", uid=None, uname=None):
    u = g.get("user")
    uid = uid if uid is not None else (u["id"] if u else None)
    uname = uname if uname is not None else (u["username"] if u else None)
    ex("INSERT INTO audit_log(ts,user_id,username,action,details) VALUES(?,?,?,?,?)",
       (now(), uid, uname, action, details))


def num(v, default=0.0):
    try:
        f = float(str(v).replace(",", "").strip())
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


def paged(base_sql, args, page, per=PER_PAGE):
    page = max(1, page)
    total = scalar(f"SELECT COUNT(*) FROM ({base_sql})", args)
    rows = q(base_sql + " LIMIT ? OFFSET ?", (*args, per, (page - 1) * per))
    return rows, page, max(1, math.ceil(total / per)), total


# --------------------------------------------------------- auth / security
@app.before_request
def before():
    if request.path not in ("/ping", "/health"):
        LAST_ACTIVITY[0] = time.time()
    g.user = None
    if session.get("uid"):
        u = q("SELECT * FROM users WHERE id=? AND active=1", (session["uid"],), one=True)
        if u:
            g.user = u
        else:
            session.clear()
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    if request.method == "POST":
        tok = request.form.get("csrf") or request.headers.get("X-CSRF") or ""
        if not secrets.compare_digest(tok, session["csrf"]):
            abort(400)
    if (g.user and g.user["must_change"] and request.endpoint
            and request.endpoint not in ("profile", "logout", "static")):
        return redirect(url_for("profile"))


@app.after_request
def headers(resp):
    resp.headers["X-Frame-Options"] = "SAMEORIGIN"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


def login_required(roles=None):
    def deco(f):
        @wraps(f)
        def w(*a, **k):
            if not g.user:
                if request.path.startswith("/api/"):
                    return jsonify(ok=False, error="انتهت الجلسة، سجّل الدخول مرة أخرى"), 401
                return redirect(url_for("login", next=request.path))
            if roles and g.user["role"] not in roles:
                abort(403)
            return f(*a, **k)
        return w
    return deco


NAV = [
    ("dashboard", "لوحة التحكم", MGR),
    ("pos", "الكاشير", ALL),
    ("sales", "فواتير المبيعات", MGR),
    ("products", "المنتجات", MGR),
    ("offers", "العروض", ALL),
    ("settings_page", "إعدادات المتجر", MGR),
    ("simple:categories", "الفئات", MGR),
    ("simple:brands", "الماركات", MGR),
    ("purchases", "المشتريات", MGR),
    ("simple:suppliers", "الموردون", MGR),
    ("simple:customers", "العملاء", MGR),
    ("expenses", "المصروفات", MGR),
    ("reports", "التقارير", MGR),
    ("stocktake", "الجرد", MGR),
    ("stock_moves", "حركة المخزون", MGR),
    ("users", "المستخدمون", ("admin",)),
    ("backups", "النسخ الاحتياطي والتحديثات", MGR),
    ("audit_view", "سجل المراجعة", ("admin",)),
]


@app.context_processor
def inject():
    items = []
    if g.get("user"):
        for ep, label, roles in NAV:
            if g.user["role"] in roles:
                url = url_for("simple", kind=ep.split(":")[1]) if ep.startswith("simple:") else url_for(ep)
                items.append((url, label))
    return dict(nav_items=items, STORE_NAME=STORE_NAME, CURRENCY=CURRENCY, ROLES=ROLES,
                METHODS=METHODS, REASONS=REASONS, csrf_token=lambda: session.get("csrf", ""),
                today=date.today().isoformat())


@app.template_filter("money")
def money(v):
    return f"{(v or 0):,.2f}"


@app.template_filter("dt")
def dt(v):
    return (v or "")[:16]


@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
def err(e):
    msgs = {400: "طلب غير صالح (قد تكون الجلسة انتهت، أعد تحميل الصفحة)",
            403: "ليس لديك صلاحية للوصول إلى هذه الصفحة", 404: "الصفحة غير موجودة"}
    return render_template("error.html", code=e.code, msg=msgs.get(e.code, "خطأ")), e.code


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        uname = request.form.get("username", "").strip()
        since = (datetime.now() - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
        fails = scalar("SELECT COUNT(*) FROM audit_log WHERE action='login_failed' AND details=? AND ts>?",
                       (uname, since))
        u = q("SELECT * FROM users WHERE username=? AND active=1", (uname,), one=True)
        if fails >= 5:
            flash("محاولات كثيرة خاطئة. حاول بعد 10 دقائق.", "err")
        elif u and check_password_hash(u["password_hash"], request.form.get("password", "")):
            session.clear()
            session["uid"] = u["id"]
            session["csrf"] = secrets.token_hex(16)
            audit("login", "", uid=u["id"], uname=u["username"])
            nxt = request.args.get("next", "")
            if not nxt.startswith("/") or nxt.startswith("//"):
                nxt = url_for("dashboard")
            return redirect(nxt)
        else:
            audit("login_failed", uname, uid=0, uname="-")
            flash("اسم المستخدم أو كلمة المرور غير صحيحة", "err")
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    if g.user:
        audit("logout")
    session.clear()
    return redirect(url_for("login"))


@app.route("/profile", methods=["GET", "POST"])
@login_required()
def profile():
    if request.method == "POST":
        cur, new, new2 = (request.form.get(k, "") for k in ("current", "new", "new2"))
        if not check_password_hash(g.user["password_hash"], cur):
            flash("كلمة المرور الحالية غير صحيحة", "err")
        elif len(new) < 6:
            flash("كلمة المرور الجديدة يجب ألا تقل عن 6 أحرف", "err")
        elif new != new2:
            flash("تأكيد كلمة المرور غير مطابق", "err")
        else:
            ex("UPDATE users SET password_hash=?, must_change=0 WHERE id=?",
               (generate_password_hash(new), g.user["id"]))
            audit("change_password")
            flash("تم تغيير كلمة المرور", "ok")
            return redirect(url_for("dashboard"))
    return render_template("profile.html")


# ---------------------------------------------------------------- dashboard
def period_stats(d1, d2):
    r = {}
    r["sales"] = scalar("SELECT SUM(total) FROM sales WHERE status='active' AND date(created_at) BETWEEN ? AND ?", (d1, d2))
    r["count"] = scalar("SELECT COUNT(*) FROM sales WHERE status='active' AND date(created_at) BETWEEN ? AND ?", (d1, d2))
    r["discount"] = scalar("SELECT SUM(discount) FROM sales WHERE status='active' AND date(created_at) BETWEEN ? AND ?", (d1, d2))
    margin = scalar("""SELECT SUM((si.price-si.cost)*si.qty) FROM sale_items si JOIN sales s ON s.id=si.sale_id
                       WHERE s.status='active' AND date(s.created_at) BETWEEN ? AND ?""", (d1, d2))
    r["gross"] = margin - r["discount"]
    r["purchases"] = scalar("SELECT SUM(total) FROM purchases WHERE status='active' AND date(created_at) BETWEEN ? AND ?", (d1, d2))
    r["expenses"] = scalar("SELECT SUM(amount) FROM expenses WHERE voided=0 AND spent_on BETWEEN ? AND ?", (d1, d2))
    r["net"] = r["gross"] - r["expenses"]
    return r


@app.route("/")
@login_required()
def dashboard():
    if g.user["role"] == "cashier":
        return redirect(url_for("pos"))
    t = date.today()
    today, month_start = t.isoformat(), t.replace(day=1).isoformat()
    days = []
    for i in range(6, -1, -1):
        d = (t - timedelta(days=i)).isoformat()
        days.append((d[5:], scalar("SELECT SUM(total) FROM sales WHERE status='active' AND date(created_at)=?", (d,))))
    mx = max([v for _, v in days] + [1])
    return render_template(
        "dashboard.html", td=period_stats(today, today), mo=period_stats(month_start, today),
        inv_cost=scalar("SELECT SUM(cost*stock) FROM products WHERE active=1"),
        inv_price=scalar("SELECT SUM(price*stock) FROM products WHERE active=1"),
        n_products=scalar("SELECT COUNT(*) FROM products WHERE active=1"),
        n_customers=scalar("SELECT COUNT(*) FROM customers WHERE active=1"),
        alerts=health_alerts(),
        owed_by=scalar("SELECT SUM(total-paid) FROM sales WHERE status='active' AND paid<total-0.005"),
        owed_to=scalar("SELECT SUM(total-paid) FROM purchases WHERE status='active' AND paid<total-0.005"),
        days=days, mx=mx,
        low=q("""SELECT p.*, c.name cat FROM products p LEFT JOIN categories c ON c.id=p.category_id
                 WHERE p.active=1 AND p.stock<=p.min_qty ORDER BY p.stock LIMIT 10"""),
        n_low=scalar("SELECT COUNT(*) FROM products WHERE active=1 AND stock<=min_qty"),
        top=q("""SELECT p.name, SUM(si.qty) qty, SUM(si.qty*si.price) rev FROM sale_items si
                 JOIN sales s ON s.id=si.sale_id JOIN products p ON p.id=si.product_id
                 WHERE s.status='active' AND date(s.created_at)>=? GROUP BY p.id ORDER BY qty DESC LIMIT 5""",
              (month_start,)))


# ----------------------------------------------------------------- products
def lookups(cat_id=None, brand_id=None):
    return (q("SELECT * FROM categories WHERE active=1 OR id IS ? ORDER BY name", (cat_id,)),
            q("SELECT * FROM brands WHERE active=1 OR id IS ? ORDER BY name", (brand_id,)))


@app.route("/products")
@login_required(MGR)
def products():
    s = request.args.get("q", "").strip()
    cat = request.args.get("cat", "")
    brand = request.args.get("brand", "")
    low = request.args.get("low")
    sql = """SELECT p.*, c.name cat, b.name brand FROM products p
             LEFT JOIN categories c ON c.id=p.category_id LEFT JOIN brands b ON b.id=p.brand_id WHERE p.active=1"""
    args = []
    if s:
        sql += " AND (p.name LIKE ? OR p.barcode LIKE ?)"
        args += [f"%{s}%", f"%{s}%"]
    if cat.isdigit():
        sql += " AND p.category_id=?"
        args.append(int(cat))
    if brand.isdigit():
        sql += " AND p.brand_id=?"
        args.append(int(brand))
    if low:
        sql += " AND p.stock<=p.min_qty"
    sql += " ORDER BY p.name"
    rows, page, pages, total = paged(sql, tuple(args), request.args.get("page", 1, int))
    cats, brands = lookups()
    return render_template("products.html", rows=rows, page=page, pages=pages, total=total,
                           cats=cats, brands=brands, s=s, cat=cat, brand=brand, low=low)


def parse_product(f, pid=None):
    name = f.get("name", "").strip()
    if not name:
        return None, "اسم المنتج مطلوب"
    barcode = f.get("barcode", "").strip() or None
    cost, price = num(f.get("cost")), num(f.get("price"))
    stock, minq = int(num(f.get("stock"))), int(num(f.get("min_qty")))
    if cost < 0 or price < 0 or stock < 0 or minq < 0:
        return None, "القيم لا يمكن أن تكون سالبة"
    if barcode and q("SELECT 1 FROM products WHERE barcode=? AND id IS NOT ?", (barcode, pid), one=True):
        return None, "الباركود مستخدم لمنتج آخر"
    cat = f.get("category_id", "")
    brand = f.get("brand_id", "")
    return dict(name=name, barcode=barcode, cost=cost, price=price, stock=stock, min_qty=minq,
                category_id=int(cat) if cat.isdigit() else None,
                brand_id=int(brand) if brand.isdigit() else None), None


@app.route("/products/new", methods=["GET", "POST"])
@login_required(MGR)
def product_new():
    p = {}
    if request.method == "POST":
        p, e = parse_product(request.form)
        if not e:
            try:
                p["image"] = save_image(request.files.get("image"))
            except ValueError as ve:
                e = str(ve)
        if e:
            flash(e, "err")
            p = request.form
        else:
            with tx() as c:
                pid = c.execute("""INSERT INTO products(name,barcode,category_id,brand_id,cost,price,stock,min_qty,image)
                                   VALUES(:name,:barcode,:category_id,:brand_id,:cost,:price,0,:min_qty,:image)""", p).lastrowid
                if p["stock"]:
                    move(c, pid, p["stock"], "opening", note="رصيد عند إضافة المنتج")
            audit("product_add", f"#{pid} {p['name']} مخزون={p['stock']}")
            flash("تمت إضافة المنتج", "ok")
            return redirect(url_for("product_new") if request.form.get("again") else url_for("products"))
    cats, brands = lookups()
    return render_template("product_form.html", p=p, cats=cats, brands=brands, new=True)


@app.route("/products/<int:pid>/edit", methods=["GET", "POST"])
@login_required(MGR)
def product_edit(pid):
    old = q("SELECT * FROM products WHERE id=? AND active=1", (pid,), one=True) or abort(404)
    p = dict(old)
    if request.method == "POST":
        p, e = parse_product(request.form, pid)
        img_name = None
        if not e:
            try:
                img_name = save_image(request.files.get("image"))
            except ValueError as ve:
                e = str(ve)
        if e:
            flash(e, "err")
            p = request.form
        else:
            with tx() as c:
                c.execute("""UPDATE products SET name=:name,barcode=:barcode,category_id=:category_id,brand_id=:brand_id,
                             cost=:cost,price=:price,min_qty=:min_qty WHERE id=:id""", {**p, "id": pid})
                cur = c.execute("SELECT stock FROM products WHERE id=?", (pid,)).fetchone()["stock"]
                if p["stock"] != cur:
                    move(c, pid, p["stock"] - cur, "manual_edit", note=f"تعديل يدوي {cur}→{p['stock']}")
                if img_name:
                    c.execute("UPDATE products SET image=? WHERE id=?", (img_name, pid))
                elif request.form.get("remove_image"):
                    c.execute("UPDATE products SET image=NULL WHERE id=?", (pid,))
            ch = f"#{pid} {p['name']}"
            if old["stock"] != p["stock"]:
                ch += f" تعديل المخزون {old['stock']}→{p['stock']}"
            if old["price"] != p["price"]:
                ch += f" السعر {old['price']}→{p['price']}"
            audit("product_edit", ch)
            if scalar("SELECT COUNT(*) FROM offers WHERE active=1 AND product_id=?", (pid,)):
                mark_site_dirty()                     # سعر/صورة المنتج ظاهرة في صفحة العروض
            flash("تم حفظ التعديلات", "ok")
            return redirect(url_for("products"))
    cats, brands = lookups(old["category_id"], old["brand_id"])
    return render_template("product_form.html", p=p, cats=cats, brands=brands, new=False, pid=pid)


@app.route("/products/<int:pid>/delete", methods=["POST"])
@login_required(MGR)
def product_delete(pid):
    p = q("SELECT * FROM products WHERE id=? AND active=1", (pid,), one=True) or abort(404)
    ex("UPDATE products SET active=0, barcode=NULL WHERE id=?", (pid,))
    audit("product_delete", f"#{pid} {p['name']}")
    if scalar("SELECT COUNT(*) FROM offers WHERE active=1 AND product_id=?", (pid,)):
        mark_site_dirty()
    flash("تم حذف المنتج (محفوظ في سجل الفواتير القديمة)", "ok")
    return redirect(url_for("products"))


@app.route("/api/products")
@login_required()
def api_products():
    s = request.args.get("q", "").strip()
    if not s:
        return jsonify([])
    base = "SELECT p.id,p.name,p.barcode,p.price,p.cost,p.stock,p.image,b.name brand FROM products p LEFT JOIN brands b ON b.id=p.brand_id WHERE p.active=1 AND "
    rows = q(base + "p.barcode=?", (s,))
    if not rows:
        rows = q(base + "(p.name LIKE ? OR p.barcode LIKE ?) ORDER BY p.name LIMIT 20", (f"%{s}%", f"%{s}%"))
    out = []
    for r in rows:
        d = dict(r)
        pct = offer_pct(d["id"])
        d["orig_price"] = d["price"]
        d["offer_pct"] = pct
        d["price"] = round(d["price"] * (1 - pct / 100), 2)
        d["image"] = url_for("media", name=d["image"]) if d["image"] else ""
        if g.user["role"] == "cashier":
            d.pop("cost")
        out.append(d)
    return jsonify(out)


@app.route("/api/customer")
@login_required()
def api_customer():
    """يظهر للكاشير اسم العميل وخصمه بمجرد كتابة رقم هاتفه (مطابقة تامة فقط، بدون قائمة عملاء)."""
    key = phone_key(request.args.get("phone", ""))
    if len(key) < 8:
        return jsonify(found=False)
    r = q("SELECT name,discount FROM customers WHERE active=1 AND phone_key=? ORDER BY id LIMIT 1", (key,), one=True)
    if not r:
        return jsonify(found=False)
    return jsonify(found=True, name=r["name"], discount=r["discount"] or 0)


@app.route("/media/<name>")
@login_required()
def media(name):
    if not re.fullmatch(r"[0-9a-f]{24}\.(jpg|png|webp|gif)", name):
        abort(404)
    return send_from_directory(safety.IMAGE_DIR, name, max_age=2592000)


# ------------------------------------------- موقع العروض (GitHub Pages) + QR
def shop_info():
    """رقم المحل + رابط صفحة العروض + مصفوفة QR للفاتورة."""
    st = cloud.settings()
    url = cloud.site_url_effective(st)
    try:
        rows = qrcode_py.rows(url) if url else []
    except ValueError:
        rows = []
    return dict(phone=st["phone"], url=url, qr=rows)


BG_PUBLISH = True          # (الاختبارات تعطّله لتجنب التسابق)


def mark_site_dirty():
    """يسجل أن الموقع يحتاج نشرًا؛ يُنشر فورًا في الخلفية وإلا يُعاد المحاولة تلقائيًا عند توفر الإنترنت."""
    safety.set_status(site_dirty=True)
    if BG_PUBLISH and os.environ.get("DAHAB_LAUNCHER") == "1":
        threading.Thread(target=publish_site_job, daemon=True, name="dahab-publish").start()


_pub_lock = threading.Lock()


def offers_payload():
    st = cloud.settings()
    t = date.today().isoformat()
    rows = q("""SELECT o.*, p.name pname, p.price pprice, p.image pimage FROM offers o LEFT JOIN products p ON p.id=o.product_id
                WHERE o.active=1 AND (o.ends_on='' OR o.ends_on>=?) ORDER BY o.id DESC""", (t,))
    items, images = [], {}
    for o in rows:
        img = o["image"] or o["pimage"]
        path = ""
        if img and re.fullmatch(r"[0-9a-f]{24}\.(jpg|png|webp|gif)", img):
            try:
                with open(os.path.join(safety.IMAGE_DIR, img), "rb") as f:
                    images[img] = f.read()
                path = "img/" + img
            except OSError:
                pass
        pct = float(o["percent"] or 0)
        items.append(dict(id=o["id"], title=o["title"], details=o["details"] or "", percent=pct, starts_on=o["starts_on"],
                          ends_on=o["ends_on"], image=path, product=o["pname"] or "",
                          price_old=o["pprice"] if o["pprice"] and pct else None,
                          price_now=round(o["pprice"] * (1 - pct / 100), 2) if o["pprice"] and pct else None))
    return dict(store=st["store"], phone=st["phone"], currency=CURRENCY, offers=items), images


def publish_site_job():
    """ينشر موقع العروض. آمن: لا يرفع أخطاء ولا يؤثر على الكاشير. -> (ok, msg)"""
    if not _pub_lock.acquire(blocking=False):
        return False, "عملية نشر جارية"
    try:
        st = cloud.settings()
        if not cloud.REPO_RE.match(st["repo"]) or not st["token"]:
            return False, "لم يتم إعداد GitHub بعد"
        with app.app_context():
            payload, images = offers_payload()
        with open(safety.resource_path("site", "index.html"), encoding="utf-8") as f:
            html = f.read()
        ok, msg, url = cloud.publish_site(payload, images, html, st)
        safety.set_status(site_dirty=not ok, site_ok=ok, site_msg=msg, site_time=now())
        if ok and url and not st["site_url"]:
            cfg = safety.load_config()
            cfg["site_url"] = url
            safety.save_config(cfg)
        return ok, msg
    except Exception as e:
        log.exception("publish_site_job")
        safety.set_status(site_dirty=True, site_ok=False, site_msg=f"خطأ: {e}")
        return False, str(e)
    finally:
        _pub_lock.release()


def start_cloud():
    """يُستدعى من المُشغّل: عامل خلفية للنشر وفحص التحديثات."""
    st = cloud.settings()
    if cloud.REPO_RE.match(st["repo"]) and st["token"] and not safety.get_status().get("site_ok"):
        safety.set_status(site_dirty=True)
    w = cloud.Worker(publish_site_job, updater.check_remote)
    w.start()
    return w


@app.route("/settings", methods=["GET", "POST"])
@login_required(MGR)
def settings_page():
    is_admin = g.user["role"] == "admin"
    if request.method == "POST":
        f, act = request.form, request.form.get("action", "save")
        cfg = safety.load_config()
        try:
            if act == "save":
                phone = f.get("phone", "").strip()
                if phone and not (re.fullmatch(r"\+?[0-9 \-()]{5,25}", phone) and 5 <= len(re.sub(r"\D", "", phone)) <= 16):
                    raise ValueError("رقم التليفون غير صحيح")
                cfg["store_phone"] = phone
                if is_admin:
                    repo = f.get("repo", "").strip().strip("/")
                    if repo and not cloud.REPO_RE.match(repo):
                        raise ValueError("اسم المستودع يجب أن يكون بالشكل: اسم-المستخدم/اسم-المستودع")
                    url = f.get("site_url", "").strip()
                    if url and not re.match(r"^https://[^\s]+$", url):
                        raise ValueError("رابط الموقع يجب أن يبدأ بـ https://")
                    cfg.update(github_repo=repo, site_url=url, auto_update=bool(f.get("auto_update")))
                    if f.get("token", "").strip():
                        cfg["github_token"] = f["token"].strip()
                    elif f.get("clear_token"):
                        cfg["github_token"] = ""
                safety.save_config(cfg)
                audit("settings_save", "إعدادات المتجر")
                mark_site_dirty()
                flash("تم حفظ الإعدادات", "ok")
            elif act == "publish":
                ok, msg = publish_site_job()
                flash(msg, "ok" if ok else "err")
            elif act in ("test", "check_updates") and is_admin:
                if act == "test":
                    ok, msg = cloud.test_connection()
                else:
                    msg = updater.check_remote(force=True)
                    ok = True
                flash(msg, "ok" if ok else "err")
        except ValueError as e:
            flash(str(e), "err")
        return redirect(url_for("settings_page"))
    st = cloud.settings()
    return render_template("settings.html", st=st, status=safety.get_status(), is_admin=is_admin,
                           site=cloud.site_url_effective(st), has_token=bool(st["token"]), upd=updater.info())


# ----------------------------------------------------------------- offers
@app.route("/offers", methods=["GET", "POST"])
@login_required()
def offers():
    can_edit = g.user["role"] in MGR
    if request.method == "POST":
        if not can_edit:
            abort(403)
        f = request.form
        oid = f.get("id", "")
        title, details = f.get("title", "").strip()[:120], f.get("details", "").strip()[:500]
        pct = min(max(round(num(f.get("percent")), 2), 0), 100)
        pid = f.get("product_id", "")
        pid = int(pid) if pid.isdigit() and q("SELECT 1 FROM products WHERE id=? AND active=1", (int(pid),), one=True) else None
        s_on, e_on = f.get("starts_on", "").strip(), f.get("ends_on", "").strip()
        err = None
        for v in (s_on, e_on):
            if v:
                try:
                    date.fromisoformat(v)
                except ValueError:
                    err = "تاريخ غير صحيح"
        if not err and s_on and e_on and e_on < s_on:
            err = "تاريخ الانتهاء قبل تاريخ البداية"
        if not err and not title:
            err = "عنوان العرض مطلوب"
        img = None
        if not err:
            try:
                img = save_image(request.files.get("image"))
            except ValueError as ve:
                err = str(ve)
        if err:
            flash(err, "err")
            return redirect(url_for("offers", edit=oid) if oid.isdigit() else url_for("offers"))
        if oid.isdigit():
            ex("UPDATE offers SET title=?,details=?,product_id=?,percent=?,starts_on=?,ends_on=? WHERE id=?",
               (title, details, pid, pct, s_on, e_on, int(oid)))
            if img:
                ex("UPDATE offers SET image=? WHERE id=?", (img, int(oid)))
            elif f.get("remove_image"):
                ex("UPDATE offers SET image=NULL WHERE id=?", (int(oid),))
            audit("offer_edit", f"#{oid} {title} {pct:g}%")
        else:
            ex("""INSERT INTO offers(title,details,product_id,percent,starts_on,ends_on,image,created_at,created_by)
                  VALUES(?,?,?,?,?,?,?,?,?)""", (title, details, pid, pct, s_on, e_on, img, now(), g.user["id"]))
            audit("offer_add", f"{title} {pct:g}%")
        mark_site_dirty()
        flash("تم حفظ العرض" if (pid or not pct) else
              "تم حفظ العرض — ملحوظة: الخصم لا يُطبَّق تلقائيًا في الكاشير إلا إذا اخترت منتجًا للعرض", "ok")
        return redirect(url_for("offers"))
    edit, prods = None, []
    if can_edit:
        rows = q("""SELECT o.*, p.name pname FROM offers o LEFT JOIN products p ON p.id=o.product_id
                    ORDER BY o.active DESC, o.id DESC""")
        if request.args.get("edit", "").isdigit():
            edit = q("SELECT * FROM offers WHERE id=?", (int(request.args["edit"]),), one=True)
        prods = q("SELECT id,name FROM products WHERE active=1 ORDER BY name")
    else:
        rows = active_offers()
    return render_template("offers.html", rows=rows, edit=edit, prods=prods, can_edit=can_edit)


@app.route("/offers/<int:oid>/toggle", methods=["POST"])
@login_required(MGR)
def offer_toggle(oid):
    o = q("SELECT * FROM offers WHERE id=?", (oid,), one=True) or abort(404)
    ex("UPDATE offers SET active=? WHERE id=?", (0 if o["active"] else 1, oid))
    audit("offer_archive" if o["active"] else "offer_restore", f"#{oid} {o['title']}")
    mark_site_dirty()
    flash("تمت أرشفة العرض (يمكن إعادته لاحقًا)" if o["active"] else "تمت إعادة تفعيل العرض", "ok")
    return redirect(url_for("offers"))


# ------------------------------------------------- simple lookup tables
SIMPLE = {
    "categories": dict(title="الفئات", table="categories", fields=[("name", "الاسم")], roles=MGR, delete=MGR),
    "brands": dict(title="الماركات", table="brands", fields=[("name", "الاسم")], roles=MGR, delete=MGR),
    "suppliers": dict(title="الموردون", table="suppliers", fields=[("name", "الاسم"), ("phone", "الهاتف"), ("address", "العنوان")],
                      roles=MGR, delete=MGR, balance=("purchases", "supplier_id")),
    "customers": dict(title="العملاء", table="customers", fields=[("name", "الاسم"), ("phone", "الهاتف"), ("address", "العنوان"), ("discount", "خصم العميل %")],
                      roles=MGR, delete=MGR, balance=("sales", "customer_id")),
}


@app.route("/m/<kind>", methods=["GET", "POST"])
@login_required()
def simple(kind):
    cfg = SIMPLE.get(kind) or abort(404)
    if g.user["role"] not in cfg["roles"]:
        abort(403)
    cols = [f[0] for f in cfg["fields"]]
    if request.method == "POST":
        if kind == "customers" and request.form.get("action") == "require_customer":
            on = "1" if request.form.get("require") else "0"
            set_setting("require_customer", on)
            audit("setting_require_customer", on)
            flash("تم حفظ الإعداد", "ok")
            return redirect(url_for("simple", kind=kind))
        vals = [request.form.get(c, "").strip() for c in cols]
        rid = request.form.get("id", "")
        key = None
        if not vals[0]:
            flash("الاسم مطلوب", "err")
        else:
            if kind == "customers":
                vals[cols.index("discount")] = min(max(round(num(vals[cols.index("discount")]), 2), 0), 100)
                raw_phone = vals[cols.index("phone")]
                key = phone_key(raw_phone)
                if raw_phone and not 7 <= len(key) <= 15:
                    flash("رقم الهاتف غير صحيح", "err")
                    return redirect(url_for("simple", kind=kind))
                if key and q("SELECT 1 FROM customers WHERE active=1 AND phone_key=? AND id IS NOT ?",
                             (key, int(rid) if rid.isdigit() else None), one=True):
                    flash("رقم الهاتف مسجل لعميل آخر", "err")
                    return redirect(url_for("simple", kind=kind))
            try:
                if rid.isdigit():
                    cid = int(rid)
                    ex(f"UPDATE {cfg['table']} SET " + ",".join(f"{c}=?" for c in cols) + " WHERE id=?", (*vals, cid))
                    audit(f"{kind}_edit", f"#{rid} {vals[0]}" + (f" خصم {vals[cols.index('discount')]:g}%" if kind == "customers" else ""))
                else:
                    cid = ex(f"INSERT INTO {cfg['table']}({','.join(cols)}) VALUES({','.join('?' * len(cols))})", vals)
                    audit(f"{kind}_add", vals[0])
                if kind == "customers":
                    ex("UPDATE customers SET phone_key=? WHERE id=?", (key or None, cid))
                flash("تم الحفظ", "ok")
            except sqlite3.IntegrityError:
                old = q(f"SELECT id,active FROM {cfg['table']} WHERE name=?", (vals[0],), one=True)
                if old and not old["active"]:
                    ex(f"UPDATE {cfg['table']} SET active=1 WHERE id=?", (old["id"],))
                    audit(f"{kind}_restore", vals[0])
                    flash("كان محفوظًا في الأرشيف وتمت إعادته للقائمة", "ok")
                else:
                    flash("هذا الاسم موجود مسبقًا", "err")
        return redirect(url_for("simple", kind=kind))
    bal = cfg.get("balance")
    sel = "SELECT t.*" + (f", (SELECT COALESCE(SUM(total-paid),0) FROM {bal[0]} x WHERE x.{bal[1]}=t.id AND x.status='active' AND x.paid<x.total-0.005) AS balance" if bal else "")
    if kind == "customers":
        sel += (", (SELECT COUNT(*) FROM sales s WHERE s.customer_id=t.id AND s.status='active') AS n_sales"
                ", (SELECT COALESCE(SUM(s.total),0) FROM sales s WHERE s.customer_id=t.id AND s.status='active') AS spent")
    where, args, s = "t.active=1", [], request.args.get("q", "").strip()
    if s and "phone" in cols:
        where += " AND (t.name LIKE ? OR t.phone LIKE ? OR t.phone_key LIKE ?)" if kind == "customers" else " AND (t.name LIKE ? OR t.phone LIKE ?)"
        args = [f"%{s}%", f"%{s}%"] + ([f"%{phone_key(s) or s}%"] if kind == "customers" else [])
    rows = q(f"{sel} FROM {cfg['table']} t WHERE {where} ORDER BY t.name", tuple(args))
    edit = None
    if request.args.get("edit", "").isdigit():
        edit = q(f"SELECT * FROM {cfg['table']} WHERE id=?", (int(request.args["edit"]),), one=True)
    return render_template("simple.html", kind=kind, cfg=cfg, rows=rows, edit=edit, s=s,
                           require_customer=get_setting("require_customer", "1") == "1")


@app.route("/m/<kind>/<int:rid>/delete", methods=["POST"])
@login_required()
def simple_delete(kind, rid):
    cfg = SIMPLE.get(kind) or abort(404)
    if g.user["role"] not in cfg["delete"]:
        abort(403)
    bal = cfg.get("balance")
    if bal and scalar(f"SELECT SUM(total-paid) FROM {bal[0]} WHERE {bal[1]}=? AND status='active' AND paid<total-0.005", (rid,)) > 0:
        flash("لا يمكن الأرشفة: عليه مديونية غير مسددة", "err")
    else:
        ex(f"UPDATE {cfg['table']} SET active=0 WHERE id=?", (rid,))
        audit(f"{kind}_archive", f"#{rid}")
        flash("تمت الأرشفة — يختفي من القوائم وتبقى فواتيره القديمة كما هي", "ok")
    return redirect(url_for("simple", kind=kind))


@app.route("/m/<kind>/<int:rid>/pay", methods=["POST"])
@login_required()
def simple_pay(kind, rid):
    cfg = SIMPLE.get(kind) or abort(404)
    if "balance" not in cfg or g.user["role"] not in cfg["roles"]:
        abort(403)
    table, col = cfg["balance"]
    amount = round(num(request.form.get("amount")), 2)
    if amount <= 0:
        flash("أدخل مبلغًا صحيحًا", "err")
        return redirect(url_for("simple", kind=kind))
    left = amount
    with tx() as c:
        rows = c.execute(f"SELECT id,total,paid FROM {table} WHERE {col}=? AND status='active' AND paid<total-0.005 ORDER BY id", (rid,)).fetchall()
        for r in rows:
            if left <= 0:
                break
            pay = min(left, round(r["total"] - r["paid"], 2))
            c.execute(f"UPDATE {table} SET paid=ROUND(paid+?,2) WHERE id=?", (pay, r["id"]))
            left = round(left - pay, 2)
    done = round(amount - left, 2)
    audit(f"{kind}_payment", f"#{rid} مبلغ {done}")
    flash(f"تم تسجيل دفعة {done:,.2f}" + (f" (الزيادة {left:,.2f} لم تُسجَّل لعدم وجود مديونية)" if left > 0 else ""), "ok")
    return redirect(url_for("simple", kind=kind))


# ---------------------------------------------------------------------- POS
@app.route("/pos")
@login_required()
def pos():
    return render_template("pos.html", shop=shop_info(), offers=active_offers(6), can_discount=g.user["role"] in MGR,
                           require_customer=get_setting("require_customer", "1") == "1")


def resolve_customer(c, name, phone, cid, required):
    """يرجع (id, نسبة الخصم, أُنشئ_الآن). العميل الجديد يُضاف تلقائيًا لقائمة العملاء عند المديرين."""
    name, phone = (name or "").strip()[:80], (phone or "").strip()[:30]
    if cid:
        r = c.execute("SELECT id,discount FROM customers WHERE id=? AND active=1", (cid,)).fetchone()
        if r:
            return r["id"], float(r["discount"] or 0), False
    if not name and not phone:
        if required:
            raise ValueError("أدخل اسم العميل ورقم هاتفه لإتمام الفاتورة")
        return None, 0.0, False
    key = phone_key(phone)
    if required and (not name or not key):
        raise ValueError("اسم العميل ورقم الهاتف مطلوبان")
    if phone and not 7 <= len(key) <= 15:
        raise ValueError("رقم الهاتف غير صحيح")
    if key:
        r = c.execute("SELECT id,discount FROM customers WHERE active=1 AND phone_key=? ORDER BY id LIMIT 1", (key,)).fetchone()
        if r:
            return r["id"], float(r["discount"] or 0), False
    if not name:
        raise ValueError("أدخل اسم العميل")
    new = c.execute("INSERT INTO customers(name,phone,phone_key) VALUES(?,?,?)", (name, phone, key or None)).lastrowid
    return new, 0.0, True


@app.route("/api/sale", methods=["POST"])
@login_required()
def api_sale():
    d = request.get_json(silent=True) or {}
    want = {}
    for it in d.get("items") or []:
        try:
            pid, qty = int(it["id"]), int(it["qty"])
        except (KeyError, ValueError, TypeError):
            return jsonify(ok=False, error="بيانات الأصناف غير صحيحة"), 400
        if qty <= 0:
            return jsonify(ok=False, error="الكمية يجب أن تكون أكبر من صفر"), 400
        want[pid] = want.get(pid, 0) + qty
    if not want:
        return jsonify(ok=False, error="السلة فارغة"), 400
    method = d.get("method") if d.get("method") in METHODS else "cash"
    cid = d.get("customer_id")
    cid = int(cid) if str(cid or "").isdigit() else None
    required = get_setting("require_customer", "1") == "1"
    is_mgr = g.user["role"] in MGR
    created = None
    try:
        with tx() as c:
            cust, cpct, created = resolve_customer(c, d.get("customer_name"), d.get("customer_phone"), cid, required)
            today = date.today().isoformat()
            lines, sub = [], 0.0
            for pid, qty in want.items():
                p = c.execute("SELECT * FROM products WHERE id=? AND active=1", (pid,)).fetchone()
                if not p:
                    raise ValueError("منتج غير موجود")
                if p["stock"] < qty:
                    raise ValueError(f"المخزون غير كافٍ للمنتج «{p['name']}» (المتاح {p['stock']})")
                unit = round(p["price"] * (1 - offer_pct(pid, c, today) / 100), 2)      # سعر العرض إن وُجد
                lines.append((p, qty, unit))
                sub += unit * qty
            sub = round(sub, 2)
            cdisc = round(sub * cpct / 100, 2)                                           # خصم العميل (يحدده المدير)
            manual = max(round(num(d.get("discount")), 2), 0) if is_mgr else 0.0         # الخصم اليدوي للمديرين فقط
            disc = min(round(cdisc + manual, 2), sub)
            note = " + ".join(x for x in (f"خصم العميل {cpct:g}%" if cdisc else "", f"خصم يدوي {manual:,.2f}" if manual else "") if x)
            total = round(sub - disc, 2)
            paid = total if method != "credit" and d.get("paid") in (None, "") else round(max(num(d.get("paid")), 0), 2)
            if method == "credit" and d.get("paid") in (None, ""):
                paid = 0
            paid = min(paid, total)
            if paid < total - 0.005 and not cust:
                raise ValueError("البيع الآجل أو الدفع الجزئي يتطلب بيانات عميل")
            sid = c.execute("""INSERT INTO sales(invoice_no,created_at,customer_id,user_id,subtotal,discount,total,paid,method,discount_note)
                               VALUES(?,?,?,?,?,?,?,?,?,?)""",
                            ("tmp" + secrets.token_hex(4), now(), cust, g.user["id"], sub, disc, total, paid, method, note)).lastrowid
            inv = f"INV-{sid:06d}"
            c.execute("UPDATE sales SET invoice_no=? WHERE id=?", (inv, sid))
            for p, qty, unit in lines:
                c.execute("INSERT INTO sale_items(sale_id,product_id,qty,price,cost) VALUES(?,?,?,?,?)",
                          (sid, p["id"], qty, unit, p["cost"]))
                move(c, p["id"], -qty, "sale", inv)
            cinfo = c.execute("SELECT name,phone FROM customers WHERE id=?", (cust,)).fetchone() if cust else None
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    except sqlite3.IntegrityError:
        return jsonify(ok=False, error="تعذر إتمام البيع: المخزون غير كافٍ. حدّث الصفحة وحاول مرة أخرى."), 400
    if created:
        audit("customer_add_pos", f"{cinfo['name']} {cinfo['phone']} (من الكاشير)")
    audit("sale", f"{inv} إجمالي {total:,.2f}" + (f" ({note})" if note else ""))
    receipt = dict(store=STORE_NAME, currency=CURRENCY, invoice=inv, date=now()[:16], cashier=g.user["full_name"],
                   customer=cinfo["name"] if cinfo else "", customer_phone=(cinfo["phone"] or "") if cinfo else "",
                   method=METHODS[method], items=[dict(name=p["name"], qty=qty, price=unit) for p, qty, unit in lines],
                   subtotal=sub, discount=disc, discount_note=note, total=total, paid=paid,
                   change=round(max(paid - total, 0), 2), due=round(max(total - paid, 0), 2), shop=shop_info())
    return jsonify(ok=True, id=sid, invoice=inv, receipt=receipt)


@app.route("/sales")
@login_required(MGR)
def sales():
    s = request.args.get("q", "").strip()
    d1, d2 = request.args.get("from", ""), request.args.get("to", "")
    sql = """SELECT s.*, c.name customer, u.full_name cashier FROM sales s
             LEFT JOIN customers c ON c.id=s.customer_id LEFT JOIN users u ON u.id=s.user_id WHERE 1=1"""
    args = []
    if g.user["role"] == "cashier":
        sql += " AND s.user_id=? AND date(s.created_at)=?"
        args += [g.user["id"], date.today().isoformat()]
    if s:
        sql += " AND (s.invoice_no LIKE ? OR c.name LIKE ?)"
        args += [f"%{s}%", f"%{s}%"]
    if d1:
        sql += " AND date(s.created_at)>=?"
        args.append(d1)
    if d2:
        sql += " AND date(s.created_at)<=?"
        args.append(d2)
    rows, page, pages, total = paged(sql + " ORDER BY s.id DESC", tuple(args), request.args.get("page", 1, int))
    return render_template("sales.html", rows=rows, page=page, pages=pages, s=s, d1=d1, d2=d2)


@app.route("/sales/<int:sid>")
@login_required()
def sale_view(sid):
    s = q("""SELECT s.*, c.name customer, c.phone cphone, u.full_name cashier FROM sales s
             LEFT JOIN customers c ON c.id=s.customer_id LEFT JOIN users u ON u.id=s.user_id WHERE s.id=?""",
          (sid,), one=True) or abort(404)
    if g.user["role"] == "cashier" and s["user_id"] != g.user["id"]:
        abort(403)
    items = q("SELECT si.*, p.name FROM sale_items si JOIN products p ON p.id=si.product_id WHERE si.sale_id=?", (sid,))
    shop = shop_info()
    return render_template("sale_view.html", s=s, items=items, print_now=request.args.get("print"), shop=shop,
                           qr_svg=qrcode_py.svg(shop["url"], scale=3, border=2) if shop["url"] else "")


@app.route("/sales/<int:sid>/cancel", methods=["POST"])
@login_required(MGR)
def sale_cancel(sid):
    s = q("SELECT * FROM sales WHERE id=?", (sid,), one=True) or abort(404)
    if s["status"] != "active":
        flash("الفاتورة ملغاة بالفعل", "err")
    else:
        with tx() as c:
            for it in c.execute("SELECT * FROM sale_items WHERE sale_id=?", (sid,)).fetchall():
                move(c, it["product_id"], it["qty"], "sale_cancel", s["invoice_no"])
            c.execute("UPDATE sales SET status='cancelled' WHERE id=?", (sid,))
        audit("sale_cancel", f"{s['invoice_no']} إجمالي {s['total']:,.2f}")
        flash("تم إلغاء الفاتورة وإرجاع الأصناف للمخزون", "ok")
    return redirect(url_for("sale_view", sid=sid))


# ---------------------------------------------------------------- purchases
@app.route("/purchases")
@login_required(MGR)
def purchases():
    rows, page, pages, _ = paged("""SELECT p.*, s.name supplier FROM purchases p
                                    LEFT JOIN suppliers s ON s.id=p.supplier_id ORDER BY p.id DESC""", (), request.args.get("page", 1, int))
    return render_template("purchases.html", rows=rows, page=page, pages=pages)


@app.route("/purchases/new")
@login_required(MGR)
def purchase_new():
    return render_template("purchase_form.html", suppliers=q("SELECT id,name FROM suppliers WHERE active=1 ORDER BY name"))


@app.route("/api/purchase", methods=["POST"])
@login_required(MGR)
def api_purchase():
    d = request.get_json(silent=True) or {}
    lines = []
    for it in d.get("items") or []:
        try:
            pid, qty, cost = int(it["id"]), int(it["qty"]), round(num(it["cost"], -1), 2)
        except (KeyError, ValueError, TypeError):
            return jsonify(ok=False, error="بيانات الأصناف غير صحيحة"), 400
        if qty <= 0 or cost < 0:
            return jsonify(ok=False, error="الكمية والتكلفة يجب أن تكونا صحيحتين"), 400
        lines.append((pid, qty, cost))
    if not lines:
        return jsonify(ok=False, error="أضف صنفًا واحدًا على الأقل"), 400
    sup = d.get("supplier_id")
    sup = int(sup) if str(sup or "").isdigit() else None
    total = round(sum(qty * cost for _, qty, cost in lines), 2)
    paid = min(max(round(num(d.get("paid"), total), 2), 0), total)
    if paid < total - 0.005 and not sup:
        return jsonify(ok=False, error="الدفع الجزئي أو الآجل يتطلب اختيار مورد"), 400
    try:
        with tx() as c:
            pid_ = c.execute("INSERT INTO purchases(ref,created_at,supplier_id,user_id,total,paid,note) VALUES(?,?,?,?,?,?,?)",
                             ("tmp" + secrets.token_hex(4), now(), sup, g.user["id"], total, paid, (d.get("note") or "")[:300])).lastrowid
            ref = f"PUR-{pid_:06d}"
            c.execute("UPDATE purchases SET ref=? WHERE id=?", (ref, pid_))
            for pid, qty, cost in lines:
                p = c.execute("SELECT * FROM products WHERE id=? AND active=1", (pid,)).fetchone()
                if not p:
                    raise ValueError("منتج غير موجود")
                avg = round((max(p["stock"], 0) * p["cost"] + qty * cost) / (max(p["stock"], 0) + qty), 2)
                c.execute("UPDATE products SET cost=? WHERE id=?", (avg, pid))
                move(c, pid, qty, "purchase", ref)
                c.execute("INSERT INTO purchase_items(purchase_id,product_id,qty,cost) VALUES(?,?,?,?)", (pid_, pid, qty, cost))
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    audit("purchase", f"{ref} إجمالي {total:,.2f}")
    return jsonify(ok=True, id=pid_)


@app.route("/purchases/<int:pid>")
@login_required(MGR)
def purchase_view(pid):
    p = q("SELECT p.*, s.name supplier, u.full_name user FROM purchases p LEFT JOIN suppliers s ON s.id=p.supplier_id LEFT JOIN users u ON u.id=p.user_id WHERE p.id=?",
          (pid,), one=True) or abort(404)
    items = q("SELECT pi.*, pr.name FROM purchase_items pi JOIN products pr ON pr.id=pi.product_id WHERE pi.purchase_id=?", (pid,))
    return render_template("purchase_view.html", p=p, items=items)


@app.route("/purchases/<int:pid>/cancel", methods=["POST"])
@login_required(("admin",))
def purchase_cancel(pid):
    p = q("SELECT * FROM purchases WHERE id=?", (pid,), one=True) or abort(404)
    if p["status"] != "active":
        flash("الفاتورة ملغاة بالفعل", "err")
        return redirect(url_for("purchase_view", pid=pid))
    try:
        with tx() as c:
            for it in c.execute("SELECT pi.*, pr.stock, pr.name FROM purchase_items pi JOIN products pr ON pr.id=pi.product_id WHERE purchase_id=?", (pid,)).fetchall():
                if it["stock"] < it["qty"]:
                    raise ValueError(f"لا يمكن الإلغاء: تم بيع جزء من «{it['name']}»")
                move(c, it["product_id"], -it["qty"], "purchase_cancel", p["ref"])
            c.execute("UPDATE purchases SET status='cancelled' WHERE id=?", (pid,))
        audit("purchase_cancel", p["ref"])
        flash("تم إلغاء فاتورة الشراء وخصم الكميات من المخزون", "ok")
    except ValueError as e:
        flash(str(e), "err")
    return redirect(url_for("purchase_view", pid=pid))


# ----------------------------------------------------------------- expenses
@app.route("/expenses", methods=["GET", "POST"])
@login_required(MGR)
def expenses():
    if request.method == "POST":
        title, amt = request.form.get("title", "").strip(), round(num(request.form.get("amount")), 2)
        day = request.form.get("spent_on") or date.today().isoformat()
        if not title or amt <= 0:
            flash("أدخل البند ومبلغًا صحيحًا", "err")
        else:
            ex("INSERT INTO expenses(spent_on,title,amount,user_id) VALUES(?,?,?,?)", (day, title, amt, g.user["id"]))
            audit("expense_add", f"{title} {amt:,.2f}")
            flash("تم تسجيل المصروف", "ok")
        return redirect(url_for("expenses"))
    month = request.args.get("month") or date.today().strftime("%Y-%m")
    rows = q("SELECT e.*, u.full_name FROM expenses e LEFT JOIN users u ON u.id=e.user_id WHERE substr(e.spent_on,1,7)=? ORDER BY e.spent_on DESC, e.id DESC", (month,))
    return render_template("expenses.html", rows=rows, month=month, total=sum(r["amount"] for r in rows if not r["voided"]))


@app.route("/expenses/<int:eid>/delete", methods=["POST"])
@login_required(MGR)
def expense_delete(eid):
    e = q("SELECT * FROM expenses WHERE id=?", (eid,), one=True) or abort(404)
    ex("UPDATE expenses SET voided=1, void_reason=? WHERE id=? AND voided=0", (f"أُلغي بواسطة {g.user['username']} {now()}", eid))
    audit("expense_void", f"{e['title']} {e['amount']:,.2f}")
    flash("تم إلغاء المصروف (يبقى ظاهرًا في السجل ولا يدخل في الحسابات)", "ok")
    return redirect(url_for("expenses", month=e["spent_on"][:7]))


# ------------------------------------------------------------------ reports
def report_range():
    t = date.today()
    return request.args.get("from") or t.replace(day=1).isoformat(), request.args.get("to") or t.isoformat()


@app.route("/reports")
@login_required(MGR)
def reports():
    d1, d2 = report_range()
    st = period_stats(d1, d2)
    by_day = q("""SELECT date(s.created_at) d, COUNT(DISTINCT s.id) n, SUM(si.qty*si.price) - 0 gross_sales,
                  (SELECT SUM(discount) FROM sales x WHERE x.status='active' AND date(x.created_at)=date(s.created_at)) disc,
                  SUM((si.price-si.cost)*si.qty) margin
                  FROM sales s JOIN sale_items si ON si.sale_id=s.id
                  WHERE s.status='active' AND date(s.created_at) BETWEEN ? AND ? GROUP BY d ORDER BY d DESC""", (d1, d2))
    top = q("""SELECT p.name, SUM(si.qty) qty, SUM(si.qty*si.price) rev, SUM((si.price-si.cost)*si.qty) profit
               FROM sale_items si JOIN sales s ON s.id=si.sale_id JOIN products p ON p.id=si.product_id
               WHERE s.status='active' AND date(s.created_at) BETWEEN ? AND ? GROUP BY p.id ORDER BY qty DESC LIMIT 15""", (d1, d2))
    by_cat = q("""SELECT COALESCE(c.name,'بدون فئة') name, SUM(si.qty) qty, SUM(si.qty*si.price) rev
                  FROM sale_items si JOIN sales s ON s.id=si.sale_id JOIN products p ON p.id=si.product_id
                  LEFT JOIN categories c ON c.id=p.category_id
                  WHERE s.status='active' AND date(s.created_at) BETWEEN ? AND ? GROUP BY c.id ORDER BY rev DESC""", (d1, d2))
    by_user = q("""SELECT u.full_name name, COUNT(*) n, SUM(s.total) total FROM sales s JOIN users u ON u.id=s.user_id
                   WHERE s.status='active' AND date(s.created_at) BETWEEN ? AND ? GROUP BY u.id ORDER BY total DESC""", (d1, d2))
    return render_template("reports.html", d1=d1, d2=d2, st=st, by_day=by_day, top=top, by_cat=by_cat, by_user=by_user,
                           inv_cost=scalar("SELECT SUM(cost*stock) FROM products WHERE active=1"),
                           inv_price=scalar("SELECT SUM(price*stock) FROM products WHERE active=1"))


def csv_response(name, header, rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    return Response("﻿" + buf.getvalue(), mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename={name}"})


@app.route("/reports/sales.csv")
@login_required(MGR)
def export_sales():
    d1, d2 = report_range()
    rows = q("""SELECT s.invoice_no, s.created_at, COALESCE(c.name,''), u.full_name, s.subtotal, s.discount, s.total, s.paid, s.method, s.status
                FROM sales s LEFT JOIN customers c ON c.id=s.customer_id LEFT JOIN users u ON u.id=s.user_id
                WHERE date(s.created_at) BETWEEN ? AND ? ORDER BY s.id""", (d1, d2))
    audit("export", f"sales {d1}→{d2}")
    return csv_response(f"sales_{d1}_{d2}.csv", ["رقم الفاتورة", "التاريخ", "العميل", "الكاشير", "الإجمالي قبل الخصم", "الخصم", "الصافي", "المدفوع", "طريقة الدفع", "الحالة"],
                        [[*r[:8], METHODS.get(r[8], r[8]), "نشطة" if r[9] == "active" else "ملغاة"] for r in rows])


@app.route("/reports/inventory.csv")
@login_required(MGR)
def export_inventory():
    rows = q("""SELECT p.barcode, p.name, COALESCE(c.name,''), COALESCE(b.name,''), p.cost, p.price, p.stock, p.min_qty, p.cost*p.stock
                FROM products p LEFT JOIN categories c ON c.id=p.category_id LEFT JOIN brands b ON b.id=p.brand_id
                WHERE p.active=1 ORDER BY p.name""")
    audit("export", "inventory")
    return csv_response("inventory.csv", ["الباركود", "المنتج", "الفئة", "الماركة", "التكلفة", "سعر البيع", "المخزون", "الحد الأدنى", "قيمة المخزون"],
                        [[x if x is not None else "" for x in r] for r in rows])


# -------------------------------------------------------------------- users
@app.route("/users", methods=["GET", "POST"])
@login_required(("admin",))
def users():
    if request.method == "POST":
        un, fn = request.form.get("username", "").strip(), request.form.get("full_name", "").strip()
        role, pw = request.form.get("role"), request.form.get("password", "")
        if not un or not fn or role not in ROLES or len(pw) < 6:
            flash("أكمل البيانات (كلمة المرور 6 أحرف على الأقل)", "err")
        else:
            try:
                ex("INSERT INTO users(username,password_hash,full_name,role) VALUES(?,?,?,?)",
                   (un, generate_password_hash(pw), fn, role))
                audit("user_add", f"{un} ({role})")
                flash("تمت إضافة المستخدم", "ok")
            except sqlite3.IntegrityError:
                flash("اسم المستخدم موجود مسبقًا", "err")
        return redirect(url_for("users"))
    return render_template("users.html", rows=q("SELECT * FROM users ORDER BY id"))


@app.route("/users/<int:uid>", methods=["POST"])
@login_required(("admin",))
def user_edit(uid):
    u = q("SELECT * FROM users WHERE id=?", (uid,), one=True) or abort(404)
    role = request.form.get("role")
    active = 1 if request.form.get("active") else 0
    fn = request.form.get("full_name", "").strip() or u["full_name"]
    if role not in ROLES:
        abort(400)
    if uid == g.user["id"] and (role != "admin" or not active):
        flash("لا يمكنك تعطيل نفسك أو إزالة صلاحية المدير عن حسابك", "err")
        return redirect(url_for("users"))
    ex("UPDATE users SET full_name=?, role=?, active=? WHERE id=?", (fn, role, active, uid))
    pw = request.form.get("password", "")
    if pw:
        if len(pw) < 6:
            flash("كلمة المرور يجب ألا تقل عن 6 أحرف", "err")
            return redirect(url_for("users"))
        ex("UPDATE users SET password_hash=?, must_change=0 WHERE id=?", (generate_password_hash(pw), uid))
    audit("user_edit", f"{u['username']} role={role} active={active}" + (" +كلمة مرور" if pw else ""))
    flash("تم حفظ بيانات المستخدم", "ok")
    return redirect(url_for("users"))


# -------------------------------------------------------------------- audit
@app.route("/audit")
@login_required(("admin",))
def audit_view():
    s = request.args.get("q", "").strip()
    sql, args = "SELECT * FROM audit_log WHERE 1=1", []
    if s:
        sql += " AND (action LIKE ? OR details LIKE ? OR username LIKE ?)"
        args = [f"%{s}%"] * 3
    rows, page, pages, _ = paged(sql + " ORDER BY id DESC", tuple(args), request.args.get("page", 1, int), 60)
    return render_template("audit.html", rows=rows, page=page, pages=pages, s=s)


@app.route("/backup")
@login_required(("admin",))
def backup():
    data = db().serialize()
    audit("backup")
    return Response(data, mimetype="application/octet-stream",
                    headers={"Content-Disposition": f"attachment; filename=store_backup_{date.today().isoformat()}.db"})


# ------------------------------------------------- health / alerts / ping
@app.route("/health")
def health():
    return f"DAHAB-OK build={version.BUILD}"


@app.route("/ping")
def ping():
    LAST_PING[0] = time.time()
    return ("", 204)


def health_alerts():
    out, st = [], safety.get_status()
    if st.get("restored"):
        out.append(("err", f"{st['restored']['msg']} ({st['restored']['at'][:16]})"))
    if st.get("imported"):
        out.append(("warn", f"تم استيراد بياناتك القديمة تلقائيًا من: {st['imported']['from']} — الملف الأصلي لم يُمَس."))
    if st.get("backup_error"):
        out.append(("err", "فشل آخر نسخ احتياطي: " + st["backup_error"]))
    try:
        stale = datetime.now() - datetime.fromisoformat(st["last_backup"]) > timedelta(hours=26)
    except (KeyError, ValueError):
        stale = True
    if stale:
        out.append(("warn", "لم تُؤخذ نسخة احتياطية منذ أكثر من يوم — افتح صفحة «النسخ الاحتياطي»."))
    if st.get("extra_ok") is False:
        out.append(("warn", "فشل النسخ إلى المجلد الخارجي: " + st.get("extra_msg", "")))
    n = len(reconcile())
    if n:
        out.append(("err", f"{n} منتج رصيده لا يطابق دفتر حركة المخزون — راجع صفحة «حركة المخزون»."))
    return out


# ----------------------------------------------------- stocktake / ledger
@app.route("/stocktake", methods=["GET", "POST"])
@login_required(MGR)
def stocktake():
    s = request.values.get("q", "").strip()
    page = request.values.get("page", 1, int)
    if request.method == "POST":
        note = request.form.get("note", "").strip()[:200]
        ref = "STK-" + datetime.now().strftime("%Y%m%d-%H%M")
        entries = []
        for k, v in request.form.items():
            if k.startswith("c_") and k[2:].isdigit() and v.strip() != "":
                cnt = int(num(v, -1))
                if cnt >= 0:
                    entries.append((int(k[2:]), cnt))
        changed = 0
        short = surplus = 0.0
        with tx() as c:
            for pid, cnt in entries:
                p = c.execute("SELECT id,stock,cost FROM products WHERE id=? AND active=1", (pid,)).fetchone()
                if not p or p["stock"] == cnt:
                    continue
                d = cnt - p["stock"]
                move(c, pid, d, "stocktake", ref, note or f"جرد: النظام {p['stock']} / الفعلي {cnt}")
                changed += 1
                if d < 0:
                    short += -d * p["cost"]
                else:
                    surplus += d * p["cost"]
        if changed:
            audit("stocktake", f"{ref} أصناف={changed} عجز={short:,.2f} زيادة={surplus:,.2f}")
            flash(f"تم اعتماد الجرد: {changed} صنف تغيّر — قيمة العجز {short:,.2f} / قيمة الزيادة {surplus:,.2f} {CURRENCY}", "ok")
        else:
            flash("لا توجد فروقات لاعتمادها", "ok")
        return redirect(url_for("stocktake", q=s, page=page))
    sql = "SELECT p.id,p.name,p.barcode,p.stock,c.name cat FROM products p LEFT JOIN categories c ON c.id=p.category_id WHERE p.active=1"
    args = []
    if s:
        sql += " AND (p.name LIKE ? OR p.barcode LIKE ?)"
        args = [f"%{s}%", f"%{s}%"]
    rows, page, pages, total = paged(sql + " ORDER BY p.name", tuple(args), page, 50)
    hist = q("""SELECT m.*, p.name pname, p.cost FROM stock_moves m JOIN products p ON p.id=m.product_id
                WHERE m.reason='stocktake' ORDER BY m.id DESC LIMIT 30""")
    return render_template("stocktake.html", rows=rows, page=page, pages=pages, s=s, hist=hist)


@app.route("/stock-moves")
@login_required(MGR)
def stock_moves():
    s, reason = request.args.get("q", "").strip(), request.args.get("reason", "")
    sql = """SELECT m.*, p.name pname, u.full_name uname FROM stock_moves m JOIN products p ON p.id=m.product_id
             LEFT JOIN users u ON u.id=m.user_id WHERE 1=1"""
    args = []
    if s:
        sql += " AND (p.name LIKE ? OR p.barcode LIKE ? OR m.ref LIKE ?)"
        args += [f"%{s}%"] * 3
    if reason in REASONS:
        sql += " AND m.reason=?"
        args.append(reason)
    rows, page, pages, _ = paged(sql + " ORDER BY m.id DESC", tuple(args), request.args.get("page", 1, int), 60)
    return render_template("stock_moves.html", rows=rows, page=page, pages=pages, s=s, reason=reason, bad=reconcile())


# ------------------------------------------------------------ backups page
def restore_into_live(path):
    """يستبدل محتوى القاعدة الحية بمحتوى النسخة عبر SQLite backup API (آمن حتى لو فشل في المنتصف)."""
    if not safety.is_valid_db(path):
        raise ValueError("الملف ليس نسخة سليمة من قاعدة بيانات النظام")
    safety.make_backup("pre_restore")
    src, dst = sqlite3.connect(path), sqlite3.connect(DB_PATH, timeout=30)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    init_db()


@app.route("/backups", methods=["GET", "POST"])
@login_required(MGR)
def backups():
    if request.method == "POST":
        act = request.form.get("action")
        if act in ("extra_dir", "ack") and g.user["role"] != "admin":
            abort(403)
        try:
            if act == "backup_now":
                safety.make_backup("manual")
                audit("backup_manual")
                flash("تم أخذ نسخة احتياطية جديدة", "ok")
            elif act == "extra_dir":
                path = request.form.get("extra_dir", "").strip()
                if path:
                    os.makedirs(path, exist_ok=True)
                    probe = os.path.join(path, ".write_test")
                    open(probe, "w").close()
                    os.remove(probe)
                cfg = safety.load_config()
                cfg["extra_backup_dir"] = path
                safety.save_config(cfg)
                audit("backup_extra_dir", path or "(معطّل)")
                if path:
                    safety.make_backup("manual")
                flash("تم حفظ مكان النسخ الخارجي" if path else "تم إيقاف النسخ الخارجي", "ok")
            elif act == "ack":
                safety.set_status(restored=None, imported=None)
                flash("تم", "ok")
            elif act == "check":
                res = db().execute("PRAGMA integrity_check").fetchall()
                ok = len(res) == 1 and res[0][0] == "ok"
                n = len(reconcile())
                flash(f"سلامة قاعدة البيانات: {'سليمة' if ok else 'يوجد خلل'} — منتجات بفروقات في دفتر المخزون: {n}",
                      "ok" if ok and not n else "err")
        except Exception as e:
            log.exception("backups action")
            flash(f"تعذر تنفيذ العملية: {e}", "err")
        return redirect(url_for("backups"))
    return render_template("backups.html", items=safety.list_backups(), st=safety.get_status(),
                           cfg=safety.load_config(), alerts=health_alerts(), data_dir=safety.DATA_DIR,
                           upd=updater.info(), is_admin=g.user["role"] == "admin",
                           cloud_ready=bool(cloud.REPO_RE.match(cloud.settings()["repo"]) and cloud.settings()["token"]))


@app.route("/updates/upload", methods=["POST"])
@login_required(MGR)
def update_upload():
    """رفع ملف تحديث: يُفحص ويُجهَّز، ثم يُنشر لباقي الأجهزة، ثم يُعاد تشغيل النظام ليعمل بالإصدار الجديد."""
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify(ok=False, error="اختر ملف التحديث أولًا"), 400
    data = f.read(updater.MAX_TOTAL + 1)
    try:
        man = updater.stage(data, source="upload")
    except updater.UpdateError as e:
        audit("update_rejected", str(e)[:200])
        return jsonify(ok=False, error=str(e)), 400
    except Exception as e:
        log.exception("update_upload")
        return jsonify(ok=False, error=f"تعذر تجهيز التحديث: {e}"), 500
    audit("update_staged", f"إصدار {man['build']} ({man.get('version', '')}) {man.get('notes', '')[:100]}")
    pub_msg, pub_ok = "", None
    if request.form.get("publish", "1") == "1":
        updater.queue_publish(data, man)
        pub_msg = updater.flush_outbox() or ""
        pub_ok = not os.listdir(updater._outbox()) if os.path.isdir(updater._outbox()) else True
    bootstrap.request_restart("now")
    return jsonify(ok=True, build=man["build"], version=man.get("version", ""), published=pub_ok, publish_msg=pub_msg)


@app.route("/updates/check", methods=["POST"])
@login_required(MGR)
def update_check():
    return jsonify(ok=True, msg=updater.check_remote(force=True))


@app.route("/updates/restart", methods=["POST"])
@login_required(MGR)
def update_restart():
    if not updater.supported():
        return jsonify(ok=False, error="غير متاح خارج نسخة سطح المكتب"), 400
    audit("restart_for_update")
    bootstrap.request_restart("now")
    return jsonify(ok=True)


@app.route("/backups/download/<name>")
@login_required(("admin",))
def backup_download(name):
    if name not in {b["name"] for b in safety.list_backups()}:
        abort(404)
    audit("backup_download", name)
    return send_file(os.path.join(safety.BACKUP_DIR, name), as_attachment=True, download_name=name)


@app.route("/backups/restore", methods=["POST"])
@login_required(("admin",))
def backup_restore():
    if request.form.get("confirm", "").strip() != "استرجاع":
        flash("اكتب كلمة «استرجاع» في خانة التأكيد", "err")
        return redirect(url_for("backups"))
    name, f, tmp = request.form.get("name", ""), request.files.get("file"), None
    try:
        if name:
            if name not in {b["name"] for b in safety.list_backups()}:
                abort(404)
            path = os.path.join(safety.BACKUP_DIR, name)
        elif f and f.filename:
            tmp = os.path.join(safety.DATA_DIR, "upload_restore.tmp")
            f.save(tmp)
            path = tmp
        else:
            flash("اختر نسخة من القائمة أو ارفع ملف نسخة", "err")
            return redirect(url_for("backups"))
        restore_into_live(path)
        audit("restore", name or "ملف مرفوع")
        flash("تم استرجاع البيانات بنجاح. سجّل الدخول من جديد.", "ok")
        session.clear()
        return redirect(url_for("login"))
    except ValueError as e:
        flash(str(e), "err")
    except Exception as e:
        log.exception("restore")
        flash(f"فشل الاسترجاع (بياناتك الحالية لم تتغير): {e}", "err")
    finally:
        if tmp and os.path.exists(tmp):
            os.remove(tmp)
    return redirect(url_for("backups"))


@app.errorhandler(Exception)
def crash(e):
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return e
    log.exception("unhandled error on %s", request.path)
    if request.path.startswith("/api/"):
        return jsonify(ok=False, error="حدث خطأ غير متوقع ولم تُحفظ العملية. حاول مرة أخرى."), 500
    return render_template("error.html", code=500, msg="حدث خطأ غير متوقع. بياناتك المحفوظة لم تتأثر، حاول مرة أخرى."), 500


_restored_msg = safety.startup_check()
init_db()
safety.start_backup_thread()

if __name__ == "__main__":
    _host, _port = os.environ.get("HOST", "0.0.0.0"), int(os.environ.get("PORT", 5000))
    try:
        from waitress import serve  # خادم إنتاج إن كان مثبتًا
        print(f"النظام يعمل على http://localhost:{_port}")
        serve(app, host=_host, port=_port)
    except ImportError:
        app.run(host=_host, port=_port, debug=False)
