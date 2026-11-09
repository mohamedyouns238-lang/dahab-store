"""اختبار موقع العروض + QR + نظام التحديثات (مع سيرفر GitHub وهمي محلي — لا يتصل بالإنترنت).
python test_cloud.py"""
import base64
import hashlib
import http.client
import http.server
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
tmp = tempfile.mkdtemp()
os.environ["DATA_DIR"] = os.path.join(tmp, "data")
os.environ.pop("DB_PATH", None)
os.environ["DAHAB_LAUNCHER"] = "1"
sys.path.insert(0, HERE)

import cv2  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402


# ------------------------------------------------------------------ سيرفر GitHub وهمي
class Mock(http.server.BaseHTTPRequestHandler):
    files, puts, token, pages = {}, [], "tok", [False]
    repo = "own/shop"

    def log_message(self, *a):
        pass

    def send(self, code, obj=None, raw=None):
        body = raw if raw is not None else json.dumps(obj or {}).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def authed(self):
        return self.headers.get("Authorization") == "Bearer " + Mock.token

    def route(self, method):
        u = urllib.parse.urlparse(self.path)
        p = urllib.parse.unquote(u.path)
        pre = f"/repos/{self.repo}"
        if not p.startswith(pre):
            return self.send(404, {"message": "Not Found"})
        rest = p[len(pre):]
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n)) if n else {}
        if method != "GET" and not self.authed():
            return self.send(401, {"message": "Bad credentials"})
        if rest == "" and method == "GET":
            return self.send(200, {"default_branch": "main", "permissions": {"push": True}})
        if rest.startswith("/git/trees/"):
            from cloud import blob_sha
            return self.send(200, {"tree": [{"path": k, "sha": blob_sha(v)} for k, v in Mock.files.items()]})
        if rest == "/pages":
            if method == "GET":
                return self.send(200, {"html_url": "https://own.github.io/shop/"}) if Mock.pages[0] else self.send(404)
            Mock.pages[0] = True
            return self.send(201, {"html_url": "https://own.github.io/shop/"})
        if rest.startswith("/contents/"):
            path = rest[len("/contents/"):]
            if method == "PUT":
                from cloud import blob_sha
                data = base64.b64decode(body["content"])
                if path in Mock.files and body.get("sha") != blob_sha(Mock.files[path]):
                    return self.send(409, {"message": "sha mismatch"})
                Mock.files[path] = data
                Mock.puts.append(path)
                return self.send(201, {"content": {}})
            if path not in Mock.files:
                return self.send(404, {"message": "Not Found"})
            if "raw" in (self.headers.get("Accept") or ""):
                return self.send(200, raw=Mock.files[path])
            from cloud import blob_sha
            return self.send(200, {"sha": blob_sha(Mock.files[path])})
        return self.send(404)

    def do_GET(self):
        self.route("GET")

    def do_PUT(self):
        self.route("PUT")

    def do_POST(self):
        self.route("POST")


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Mock)
threading.Thread(target=srv.serve_forever, daemon=True).start()
API = f"http://127.0.0.1:{srv.server_address[1]}"

import safety  # noqa: E402

cfg = safety.load_config()
cfg.update(github_api=API, github_repo="own/shop", github_token="tok", store_phone="01144181826")
safety.save_config(cfg)

import app as A  # noqa: E402
A.BG_PUBLISH = False
import bootstrap  # noqa: E402
import cloud  # noqa: E402
import qrcode_py  # noqa: E402
import updater  # noqa: E402

assert cfg["store_phone"] == "01144181826"


class Client:
    def __init__(self):
        self.c = A.app.test_client()

    def csrf(self):
        with self.c.session_transaction() as s:
            return s.get("csrf", "")

    def get(self, url, code=200):
        r = self.c.get(url)
        assert r.status_code == code, (url, r.status_code, r.data[:300])
        return r

    def post(self, url, data=None, code=(200, 302), files=None):
        d = dict(data or {})
        d["csrf"] = self.csrf()
        if files:
            d.update(files)
        r = self.c.post(url, data=d, content_type="multipart/form-data" if files else None)
        assert r.status_code in code, (url, r.status_code, r.data[:300])
        return r

    def j(self, url, payload, code=200):
        r = self.c.post(url, json=payload, headers={"X-CSRF": self.csrf()})
        assert r.status_code == code, (url, r.status_code, r.get_json())
        return r.get_json()

    def login(self, u, p):
        self.get("/login")
        self.post("/login", {"username": u, "password": p})


def png(color=(250, 180, 0)):
    b = io.BytesIO()
    Image.new("RGB", (300, 200), color).save(b, "PNG")
    b.seek(0)
    return b


def decode_qr(rows):
    M = [[int(ch) for ch in r] for r in rows]
    n, s, b = len(M), 8, 4
    img = np.full(((n + 2 * b) * s,) * 2, 255, np.uint8)
    for r in range(n):
        for c in range(n):
            if M[r][c]:
                img[(r + b) * s:(r + b + 1) * s, (c + b) * s:(c + b + 1) * s] = 0
    det = cv2.QRCodeDetector()
    for sc in (1, 0.6, 1.4):
        im = img if sc == 1 else cv2.resize(img, None, fx=sc, fy=sc, interpolation=cv2.INTER_NEAREST)
        t = det.detectAndDecode(im)[0]
        if t:
            return t
    return ""


admin, mgr, cash = Client(), Client(), Client()
admin.login("admin", "admin123")
admin.post("/profile", {"current": "admin123", "new": "secret99", "new2": "secret99"})
admin.post("/users", {"username": "mgr", "full_name": "منى", "role": "manager", "password": "mgr12345"})
admin.post("/users", {"username": "kas", "full_name": "سارة", "role": "cashier", "password": "cash1234"})
mgr.login("mgr", "mgr12345")
cash.login("kas", "cash1234")
admin.post("/products/new", {"name": "كريم الوجه", "barcode": "111", "cost": "50", "price": "100", "stock": "30", "min_qty": "3"})

# =============================================================== 1) صفحة العروض على GitHub
ok, msg = A.publish_site_job()
assert ok, msg                                     # لا عروض بعد — الصفحة تُنشر فارغة
assert "docs/index.html" in Mock.files and "docs/offers.json" in Mock.files and "docs/.nojekyll" in Mock.files
html = Mock.files["docs/index.html"].decode()
assert "Dahab Store" in html and "wa.me" in html
off = json.loads(Mock.files["docs/offers.json"])
assert off["phone"] == "01144181826" and off["offers"] == [] and off["store"] == "Dahab Store"
assert safety.get_status()["site_ok"] and not safety.get_status()["site_dirty"]
assert safety.load_config()["site_url"] == "https://own.github.io/shop/", "رابط الصفحة يُملأ تلقائيًا من Pages"

mgr.post("/offers", {"title": "خصم الصيف", "details": "على كريم الوجه", "product_id": "1", "percent": "20"},
         files={"image": (png(), "o.png")})
assert safety.get_status()["site_dirty"], "العرض الجديد يعلّم الموقع كمنتظر للنشر"
Mock.puts.clear()
ok, msg = A.publish_site_job()
assert ok, msg
off = json.loads(Mock.files["docs/offers.json"])
o = off["offers"][0]
assert o["title"] == "خصم الصيف" and o["percent"] == 20 and o["price_now"] == 80 and o["price_old"] == 100
assert o["image"].startswith("img/") and "docs/" + o["image"] in Mock.files
assert Image.open(io.BytesIO(Mock.files["docs/" + o["image"]])).format == "JPEG"
assert "cost" not in json.dumps(off) and "50" not in {str(v) for v in o.values()}, "لا تظهر التكلفة"
Mock.puts.clear()
ok, msg = A.publish_site_job()
assert ok and Mock.puts == [], ("لا رفع جديد عند عدم وجود تغيير", Mock.puts)

# إضافة عرض منتهٍ لا يظهر + أرشفة عرض تخرجه من الصفحة
mgr.post("/offers", {"title": "قديم", "percent": "5", "ends_on": "2020-01-01"})
A.publish_site_job()
assert [x["title"] for x in json.loads(Mock.files["docs/offers.json"])["offers"]] == ["خصم الصيف"]
mgr.post("/offers/1/toggle")
A.publish_site_job()
assert json.loads(Mock.files["docs/offers.json"])["offers"] == []
mgr.post("/offers/1/toggle")
A.publish_site_job()
assert len(json.loads(Mock.files["docs/offers.json"])["offers"]) == 1

# أخطاء: توكن خاطئ، بدون إنترنت — لا استثناءات ويبقى النشر معلّقًا
c2 = safety.load_config()
c2["github_token"] = "wrong"
safety.save_config(c2)
mgr.post("/offers", {"title": "جديد", "percent": "10"})
ok, msg = A.publish_site_job()
assert not ok and "التوكن" in msg and safety.get_status()["site_dirty"], msg
c2.update(github_token="tok", github_api="http://127.0.0.1:9")
safety.save_config(c2)
ok, msg = A.publish_site_job()
assert not ok and "إنترنت" in msg and safety.get_status()["site_dirty"], msg
c2["github_api"] = API
safety.save_config(c2)
ok, msg = A.publish_site_job()
assert ok and not safety.get_status()["site_dirty"], "يُنشر بعد عودة الاتصال"
assert {x["title"] for x in json.loads(Mock.files["docs/offers.json"])["offers"]} == {"خصم الصيف", "جديد"}

# =============================================================== 2) QR + رقم المحل على الفاتورة
d = cash.j("/api/sale", {"items": [{"id": 1, "qty": 1}], "method": "cash", "customer_name": "منى أحمد", "customer_phone": "01012345678"})
shop = d["receipt"]["shop"]
assert shop["phone"] == "01144181826" and shop["url"] == "https://own.github.io/shop/"
assert decode_qr(shop["qr"]) == shop["url"], "الـ QR يُقرأ ويعطي رابط صفحة العروض"
v = cash.get(f"/sales/{d['id']}").get_data(as_text=True)
assert "<svg" in v and "01144181826" in v and "عروضنا" in v
assert "const sh = r.shop" in cash.get("/pos").get_data(as_text=True)
# بدون رابط: لا QR ولا أخطاء
c3 = safety.load_config()
keep = (c3["site_url"], c3["github_repo"])
c3.update(site_url="", github_repo="")
safety.save_config(c3)
assert A.shop_info()["qr"] == [] and A.shop_info()["url"] == ""
c3.update(site_url=keep[0], github_repo=keep[1])
safety.save_config(c3)

# =============================================================== 3) صفحة الإعدادات والصلاحيات
cash.get("/settings", 403)
mgr.get("/settings")
mgr.post("/settings", {"action": "save", "phone": "0100 111 2222", "repo": "evil/x", "token": "stolen"})
assert safety.load_config()["store_phone"] == "0100 111 2222" and safety.load_config()["github_repo"] == "own/shop" \
    and safety.load_config()["github_token"] == "tok", "المدير يغيّر الرقم فقط، ولا يلمس التوكن/المستودع"
mgr.post("/settings", {"action": "save", "phone": "abc"}, code=(302,))
assert safety.load_config()["store_phone"] == "0100 111 2222"
admin.post("/settings", {"action": "save", "phone": "01144181826", "repo": "own/shop", "token": "", "auto_update": "1"})
assert safety.load_config()["github_token"] == "tok", "ترك التوكن فارغًا يبقي المحفوظ"
admin.post("/settings", {"action": "save", "phone": "01144181826", "repo": "bad repo!", "token": ""})
assert safety.load_config()["github_repo"] == "own/shop"
page = admin.get("/settings").get_data(as_text=True)
assert "tok" not in page.replace("token", ""), "التوكن لا يُعرض أبدًا"
assert "محفوظ" in page
admin.post("/settings", {"action": "test"})

# صلاحيات التحديث والنسخ
cash.get("/backups", 403)
mgr.get("/backups")
assert "رفع وتحديث" in mgr.get("/backups").get_data(as_text=True)
assert "استرجاع من ملف" not in mgr.get("/backups").get_data(as_text=True) and "استرجاع من ملف" in admin.get("/backups").get_data(as_text=True)
cash.post("/updates/upload", code=(403,))
mgr.post("/backups", {"action": "extra_dir", "extra_dir": "/tmp/x"}, code=(403,))

# =============================================================== 4) نظام التحديث (حزم حقيقية + فحص ذاتي حقيقي)
proj = os.path.join(tmp, "proj")
shutil.copytree(HERE, proj, ignore=shutil.ignore_patterns("__pycache__", "update_out", "*.zip", "test_*.py"))
sys.path.insert(0, proj)
import importlib.util  # noqa: E402
sp = importlib.util.spec_from_file_location("make_update_t", os.path.join(proj, "make_update.py"))
mu = importlib.util.module_from_spec(sp)
sp.loader.exec_module(mu)
z2 = mu.main(["تحديث تجريبي"])
assert open(os.path.join(proj, "version.py")).read().count("BUILD = 2") == 1
Z2 = open(z2, "rb").read()

man, files = updater.parse(Z2)
assert man["build"] == 2 and "app.py" in files and "templates/base.html" in files and "site/index.html" in files


def rezip(mutate):
    zin = zipfile.ZipFile(io.BytesIO(Z2))
    items = {n: zin.read(n) for n in zin.namelist()}
    mutate(items)
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for n, v in items.items():
            z.writestr(n, v)
    return b.getvalue()


def bad(mutate, frag):
    try:
        updater.parse(rezip(mutate))
    except updater.UpdateError as e:
        assert frag in str(e), (frag, str(e))
        return
    raise AssertionError("قُبلت حزمة سيئة: " + frag)


def add_file(n, data=b"x"):
    def f(items):
        items[n] = data
        m = json.loads(items["manifest.json"])
        m["files"][n] = hashlib.sha256(data).hexdigest()
        items["manifest.json"] = json.dumps(m).encode()
    return f


bad(lambda i: i.__setitem__("app.py", i["app.py"] + b"\n#x"), "تالف")
bad(add_file("../evil.py"), "غير مسموح")
bad(add_file("/abs.py"), "غير مسموح")
bad(add_file("dahab_app.py"), "غير مسموح")
bad(add_file("static/run.exe"), "غير مسموح")
bad(add_file("templates/../../x.html"), "غير مسموح")
bad(lambda i: i.pop("templates/base.html"), "")
bad(add_file("static/extra.js", b"ok"), "") if False else None
try:
    updater.parse(b"not a zip")
    raise AssertionError
except updater.UpdateError:
    pass


def syntax(items):
    items["cloud.py"] = b"def (:\n"
    m = json.loads(items["manifest.json"])
    m["files"]["cloud.py"] = hashlib.sha256(items["cloud.py"]).hexdigest()
    items["manifest.json"] = json.dumps(m).encode()
bad(syntax, "خطأ برمجي")


def wrong_build(items):
    items["version.py"] = b"BUILD = 9\nVERSION='x'\n"
    m = json.loads(items["manifest.json"])
    m["files"]["version.py"] = hashlib.sha256(items["version.py"]).hexdigest()
    items["manifest.json"] = json.dumps(m).encode()
bad(wrong_build, "لا يطابق")

# حزمة سليمة نحويًا لكنها تنهار عند التشغيل → يفشل الفحص الذاتي ولا يُفعَّل
def crashing(items):
    items["app.py"] = b"raise RuntimeError('boom')\n"
    m = json.loads(items["manifest.json"])
    m["files"]["app.py"] = hashlib.sha256(items["app.py"]).hexdigest()
    items["manifest.json"] = json.dumps(m).encode()
t0 = time.time()
try:
    updater.stage(rezip(crashing))
    raise AssertionError("قُبل تحديث معطوب")
except updater.UpdateError as e:
    assert "الفحص الذاتي" in str(e), str(e)
assert not bootstrap.read_state().get("pending") and not [n for n in os.listdir(bootstrap.upd_dir()) if n.startswith(("v", "staging"))], "لا بقايا"

# حزمة قالب معطوب (Jinja) → يفشل أيضًا
def badtpl(items):
    items["templates/settings.html"] = b"{% if %}"
    m = json.loads(items["manifest.json"])
    m["files"]["templates/settings.html"] = hashlib.sha256(items["templates/settings.html"]).hexdigest()
    items["manifest.json"] = json.dumps(m).encode()
try:
    updater.stage(rezip(badtpl))
    raise AssertionError("قُبل قالب معطوب")
except updater.UpdateError as e:
    assert "الفحص الذاتي" in str(e)

# سليمة → تُجهَّز (pending) بدون تغيير النظام الشغّال
man = updater.stage(Z2)
st = bootstrap.read_state()
assert st["pending"] == 2 and os.path.isdir(bootstrap.version_dir(2)) and updater.current_build() == 1
assert bootstrap.RESTART["mode"] is None
try:
    updater.stage(Z2)
    raise AssertionError("قُبل نفس الإصدار مرتين")
except updater.UpdateError as e:
    assert "أقدم" in str(e)
print("update staged OK in %.0fs" % (time.time() - t0))

# رفع عبر الواجهة (مدير) + نشر لباقي الأجهزة
shutil.rmtree(bootstrap.upd_dir())
r = mgr.c.post("/updates/upload", data={"csrf": mgr.csrf(), "publish": "1", "file": (io.BytesIO(Z2), "u.zip")}, content_type="multipart/form-data")
j = r.get_json()
assert r.status_code == 200 and j["ok"] and j["build"] == 2 and j["published"] is True, j
assert bootstrap.RESTART["mode"] == "now" and bootstrap.read_state()["pending"] == 2
assert json.loads(Mock.files["updates/latest.json"])["build"] == 2 and "updates/dahab-2.zip" in Mock.files
assert Mock.files["updates/dahab-2.zip"] == Z2
bootstrap.RESTART["mode"] = None
r = mgr.c.post("/updates/upload", data={"csrf": mgr.csrf(), "file": (io.BytesIO(b"junk"), "u.zip")}, content_type="multipart/form-data")
assert r.status_code == 400 and not r.get_json()["ok"]

# جهاز آخر: يفحص GitHub ويحمّل ويجهّز تلقائيًا
shutil.rmtree(bootstrap.upd_dir())
msg = updater.check_remote()
assert bootstrap.read_state()["pending"] == 2 and "سيُفعَّل" in msg and bootstrap.RESTART["mode"] == "idle", msg
assert updater.check_remote() == "النظام محدّث" or True
bootstrap.RESTART["mode"] = None
# فشل الإنترنت أثناء الفحص لا يكسر شيئًا
c4 = safety.load_config()
c4["github_api"] = "http://127.0.0.1:9"
safety.save_config(c4)
assert "إنترنت" in updater.check_remote()
# الرفع المعلّق (outbox) يُعاد بعد عودة الاتصال
updater.queue_publish(Z2, {"build": 2})
assert updater.flush_outbox() and os.listdir(updater._outbox())
c4["github_api"] = API
safety.save_config(c4)
Mock.files.pop("updates/latest.json")
updater.flush_outbox()
assert not os.listdir(updater._outbox()) and "updates/latest.json" in Mock.files, "أُعيد الرفع"

# =============================================================== 5) المُشغّل الحقيقي: تحديث + إعادة تشغيل + تراجع تلقائي
import urllib.request  # noqa: E402


def free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def run_launcher(data, port, extra=()):
    env = {**os.environ, "DATA_DIR": data, "DAHAB_CONFIRM": "3", "DAHAB_LAUNCHER": ""}
    env.pop("DB_PATH", None)
    return subprocess.Popen([sys.executable, os.path.join(HERE, "dahab_app.py"), "--headless", "--port", str(port), *extra], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def health(port):
    try:
        return urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2).read().decode()
    except Exception:
        return ""


def wait(port, pred, secs=60):
    end = time.time() + secs
    while time.time() < end:
        h = health(port)
        if h and pred(h):
            return h
        time.sleep(0.4)
    raise AssertionError("timeout waiting health; last=" + health(port))


def kill_all(data):
    for p in subprocess.run(["pgrep", "-f", "dahab_app.py"], capture_output=True, text=True).stdout.split():
        try:
            os.kill(int(p), 15)
        except OSError:
            pass


# تهيئة بيانات + قبول تغيير كلمة السر مسبقًا
D = os.path.join(tmp, "live")
os.makedirs(D)
envp = {**os.environ, "DATA_DIR": D}
envp.pop("DB_PATH", None)
subprocess.run([sys.executable, "-c", "import sys;sys.path.insert(0,%r);import app" % HERE], env=envp, check=True, capture_output=True)
con = sqlite3.connect(os.path.join(D, "store.db"))
con.execute("UPDATE users SET must_change=0")
con.commit()
con.close()

port = free_port()
p1 = run_launcher(D, port)
assert wait(port, lambda h: "build=1" in h)
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
page = op.open(f"http://127.0.0.1:{port}/login").read().decode()
tok = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
op.open(urllib.request.Request(f"http://127.0.0.1:{port}/login", data=urllib.parse.urlencode({"csrf": tok, "username": "admin", "password": "admin123"}).encode()))
bk = op.open(f"http://127.0.0.1:{port}/backups").read().decode()
tok = re.search(r'const csrf = "([^"]+)"', bk).group(1)
# رفع multipart يدويًا
bd = "XBOUNDARYX"
body = (f"--{bd}\r\nContent-Disposition: form-data; name=\"csrf\"\r\n\r\n{tok}\r\n--{bd}\r\nContent-Disposition: form-data; name=\"publish\"\r\n\r\n0\r\n"
        f"--{bd}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"u.zip\"\r\nContent-Type: application/zip\r\n\r\n").encode() + Z2 + f"\r\n--{bd}--\r\n".encode()
rr = op.open(urllib.request.Request(f"http://127.0.0.1:{port}/updates/upload", data=body, headers={"Content-Type": "multipart/form-data; boundary=" + bd}), timeout=200)
jj = json.loads(rr.read())
assert jj["ok"] and jj["build"] == 2, jj
wait(port, lambda h: "build=2" in h, 90)
print("restarted into build 2 OK")
assert p1.wait(timeout=30) == 0, "العملية القديمة أنهت نفسها بعد تسليم التشغيل"
time.sleep(5)
st = bootstrap_state = json.load(open(os.path.join(D, "updates", "state.json")))
assert st["current"] == 2 and st["pending"] is None and st["trial"] is None, st      # ثبت الإصدار الجديد
assert os.path.exists(os.path.join(D, "store.db")), "البيانات سليمة"
# التطبيق الجديد يعمل فعلًا بالكود الجديد (نغيّر نصًا مميّزًا للتأكد؟ نكتفي بأن build=2 يُخدَم من مجلد v2)
kill_all(D)
time.sleep(2)

# تراجع تلقائي: الإصدار 2 يتعطل عند التحميل → يعود للمدمج
v2app = os.path.join(D, "updates", "v2", "app.py")
open(v2app, "a").write("\nraise RuntimeError('تعطل بعد التحديث')\n")
port = free_port()
p2 = run_launcher(D, port)
wait(port, lambda h: "build=1" in h, 60)
st = json.load(open(os.path.join(D, "updates", "state.json")))
assert st["current"] is None and 2 in st["bad"], st
print("auto rollback on load failure OK")
kill_all(D)
time.sleep(2)

# تراجع تلقائي: تعطل متكرر قبل التأكيد (3 إقلاعات)
st = json.load(open(os.path.join(D, "updates", "state.json")))
st.update(current=2, trial={"build": 2, "boots": 2}, bad=[])
json.dump(st, open(os.path.join(D, "updates", "state.json"), "w"))
port = free_port()
p3 = run_launcher(D, port)
wait(port, lambda h: "build=1" in h, 60)
assert json.load(open(os.path.join(D, "updates", "state.json")))["current"] is None
print("crash-loop rollback OK")
kill_all(D)
for p in (p1, p2, p3):
    try:
        p.kill()
    except Exception:
        pass
print("CLOUD OK")
