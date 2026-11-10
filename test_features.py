"""اختبار الميزات: بيانات العميل من الكاشير، العروض، صور المنتجات، خصم العميل، صلاحيات الكاشير.
python test_features.py   (قاعدة بيانات مؤقتة؛ لا يلمس بيانات المحل)"""
import io
import os
import re
import tempfile
from datetime import date, timedelta

tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(tmp, "store.db")

from PIL import Image  # noqa: E402

import app as A  # noqa: E402


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
        r = self.post("/login", {"username": u, "password": p})
        assert r.status_code == 302 and "/login" not in r.headers["Location"], (u, r.headers)


def S(sql, args=()):
    with A.app.app_context():
        return A.scalar(sql, args)


def rows(sql, args=()):
    with A.app.app_context():
        return [dict(r) for r in A.db().execute(sql, args).fetchall()]


def png(color=(200, 30, 90), size=(300, 200)):
    b = io.BytesIO()
    Image.new("RGB", size, color).save(b, "PNG")
    b.seek(0)
    return b


admin, mgr, cash, anon = Client(), Client(), Client(), Client()
admin.login("admin", "admin123")
admin.post("/profile", {"current": "admin123", "new": "secret99", "new2": "secret99"})
admin.post("/users", {"username": "mgr", "full_name": "المديرة منى", "role": "manager", "password": "mgr12345"})
admin.post("/users", {"username": "kas", "full_name": "كاشير سارة", "role": "cashier", "password": "cash1234"})
mgr.login("mgr", "mgr12345")
cash.login("kas", "cash1234")

# =============================================================== 1) صور المنتجات (المدير فقط)
admin.post("/products/new", {"name": "كريم الوجه", "barcode": "111", "cost": "50", "price": "100", "stock": "30", "min_qty": "3"},
           files={"image": (png(), "face.png")})
assert S("SELECT COUNT(*) FROM products") == 1
img = rows("SELECT image FROM products WHERE id=1")[0]["image"]
assert img and re.fullmatch(r"[0-9a-f]{24}\.jpg", img), img
assert os.path.exists(os.path.join(A.safety.IMAGE_DIR, img))
r = cash.get(f"/media/{img}")
assert r.mimetype == "image/jpeg" and r.data[:3] == b"\xff\xd8\xff"
assert Image.open(io.BytesIO(r.data)).size[0] <= 800
anon.get(f"/media/{img}", 302)                                  # لا صور بدون تسجيل دخول
cash.get("/media/..%2Fapp.py", 404); cash.get("/media/abc.jpg", 404)
before = S("SELECT COUNT(*) FROM products")
admin.post("/products/new", {"name": "ملف مزيف", "barcode": "999", "price": "5"},
           files={"image": (io.BytesIO(b"this is not an image"), "fake.png")})
assert S("SELECT COUNT(*) FROM products") == before, "fake image rejected, product not created"
big = io.BytesIO(b"\x89PNG" + os.urandom(9 * 1024 * 1024))
admin.post("/products/new", {"name": "ضخم", "barcode": "998", "price": "5"}, files={"image": (big, "big.png")})
assert S("SELECT COUNT(*) FROM products") == before, "oversize image rejected"
mgr.post("/products/1/edit", {"name": "كريم الوجه", "barcode": "111", "cost": "50", "price": "100", "stock": "30", "min_qty": "3"},
         files={"image": (png((10, 120, 60)), "face2.png")})
img2 = rows("SELECT image FROM products WHERE id=1")[0]["image"]
assert img2 != img and os.path.exists(os.path.join(A.safety.IMAGE_DIR, img)), "old image file is never deleted"
mgr.post("/products/1/edit", {"name": "كريم الوجه", "barcode": "111", "cost": "50", "price": "100", "stock": "30", "min_qty": "3",
                              "remove_image": "1"})
assert rows("SELECT image FROM products WHERE id=1")[0]["image"] is None
mgr.post("/products/1/edit", {"name": "كريم الوجه", "barcode": "111", "cost": "50", "price": "100", "stock": "30", "min_qty": "3"},
         files={"image": (png(), "face.png")})
assert rows("SELECT image FROM products WHERE id=1")[0]["image"] == img, "same picture -> same file (dedupe)"
cash.get("/products/1/edit", 403); cash.get("/products/new", 403)
j = cash.c.get("/api/products?q=111").get_json()[0]
assert j["image"].endswith(img) and "cost" not in j, "cashier sees picture, never the cost"

# =============================================================== 2) العروض: المدير يضيف، الكاشير يشاهد فقط
today = date.today()
yd, tm = (today - timedelta(days=1)).isoformat(), (today + timedelta(days=1)).isoformat()
page = cash.get("/offers").get_data(as_text=True)
assert "حفظ العرض" not in page and 'name="title"' not in page, "no edit form for cashier"
cash.post("/offers", {"title": "عرض مخترق", "percent": "99"}, code=(403,))
assert S("SELECT COUNT(*) FROM offers") == 0
mgr.post("/offers", {"title": "خصم الصيف", "details": "على كريم الوجه", "product_id": "1", "percent": "20"},
         files={"image": (png((250, 180, 0)), "offer.png")})
assert S("SELECT COUNT(*) FROM offers") == 1 and rows("SELECT image FROM offers")[0]["image"]
page = cash.get("/offers").get_data(as_text=True)
assert "خصم الصيف" in page and "كريم الوجه" in page and "تعديل" not in page and "أرشفة" not in page, "cashier: read-only view"
cash.post("/offers/1/toggle", code=(403,))
assert "خصم الصيف" in cash.get("/pos").get_data(as_text=True), "offers strip inside POS"
# تحقق من مدخلات العرض
for bad in ({"title": "", "percent": "10"}, {"title": "x", "starts_on": "2026-13-45"}, {"title": "x", "starts_on": tm, "ends_on": yd}):
    mgr.post("/offers", bad)
assert S("SELECT COUNT(*) FROM offers") == 1, "invalid offers rejected"
mgr.post("/offers", {"title": "خصم زائد", "percent": "150"})
assert S("SELECT MAX(percent) FROM offers") == 100, "percent clamped to 100"
mgr.post("/offers/2/toggle")                                    # أرشفة، لا حذف
assert S("SELECT COUNT(*) FROM offers") == 2 and S("SELECT active FROM offers WHERE id=2") == 0

# سعر العرض يظهر للكاشير ويُطبَّق في الفاتورة
j = cash.c.get("/api/products?q=111").get_json()[0]
assert (j["price"], j["orig_price"], j["offer_pct"]) == (80, 100, 20), j
# عروض منتهية / لم تبدأ / مؤرشفة لا تُطبَّق
mgr.post("/offers", {"title": "منتهي", "product_id": "1", "percent": "50", "ends_on": yd})
mgr.post("/offers", {"title": "قادم", "product_id": "1", "percent": "60", "starts_on": tm})
assert cash.c.get("/api/products?q=111").get_json()[0]["price"] == 80
page = cash.get("/offers").get_data(as_text=True)
assert "منتهي" not in page and "قادم" not in page and "خصم زائد" not in page, "cashier sees only running offers"
mgr.post("/offers/1/toggle")
assert cash.c.get("/api/products?q=111").get_json()[0]["price"] == 100, "archived offer not applied"
mgr.post("/offers/1/toggle")
assert cash.c.get("/api/products?q=111").get_json()[0]["price"] == 80

# =============================================================== 3) الكاشير يدخل بيانات العميل ← تظهر في قائمة العملاء عند المديرين
SALE = {"items": [{"id": 1, "qty": 1}], "method": "cash"}
assert "اسم العميل" in cash.j("/api/sale", SALE, 400)["error"], "customer required by default"
assert cash.j("/api/sale", {**SALE, "customer_name": "منى أحمد"}, 400)["ok"] is False
assert cash.j("/api/sale", {**SALE, "customer_name": "منى أحمد", "customer_phone": "123"}, 400)["ok"] is False
assert cash.j("/api/sale", {**SALE, "customer_phone": "01001234567"}, 400)["ok"] is False
assert S("SELECT COUNT(*) FROM sales") == 0 and S("SELECT stock FROM products WHERE id=1") == 30, "nothing saved on rejection"
r = cash.j("/api/sale", {**SALE, "customer_name": "منى أحمد", "customer_phone": "0100 123 4567"})
assert r["ok"] and r["receipt"]["customer"] == "منى أحمد" and r["receipt"]["customer_phone"] == "0100 123 4567"
assert r["receipt"]["total"] == 80, "offer price applied"
cu = rows("SELECT * FROM customers")
assert len(cu) == 1 and cu[0]["name"] == "منى أحمد" and cu[0]["phone"] == "0100 123 4567" and cu[0]["discount"] == 0
assert rows("SELECT customer_id FROM sales WHERE id=1")[0]["customer_id"] == cu[0]["id"]
lst = mgr.get("/m/customers").get_data(as_text=True)                       # المدير يرى العميل الذي أدخله الكاشير
assert "منى أحمد" in lst and "0100 123 4567" in lst
assert "customer_add_pos" in admin.get("/audit").get_data(as_text=True)
for u in ("/m/customers", "/m/customers?edit=1", "/sales", "/audit", "/users"):
    cash.get(u, 403)                                                       # الكاشير لا يرى قائمة العملاء
cash.post("/m/customers", {"name": "تلاعب", "phone": "01222222222", "discount": "90"}, code=(403,))
cash.post("/m/customers/1/pay", {"amount": "5"}, code=(403,))
assert S("SELECT COUNT(*) FROM customers") == 1
# نفس الرقم بصيغ مختلفة = نفس العميل (لا تكرار)
for ph in ("+20 100 123 4567", "00201001234567", "01001234567", "1001234567"):
    rr = cash.j("/api/sale", {**SALE, "customer_name": "اسم آخر", "customer_phone": ph})
    assert rr["receipt"]["customer"] == "منى أحمد", (ph, rr["receipt"]["customer"])
assert S("SELECT COUNT(*) FROM customers") == 1

# =============================================================== 4) خصم العميل: المدير يحدده ← يظهر للكاشير بمجرد كتابة الرقم
assert cash.c.get("/api/customer?phone=01001234567").get_json() == {"found": True, "name": "منى أحمد", "discount": 0}
mgr.post("/m/customers", {"id": "1", "name": "منى أحمد", "phone": "0100 123 4567", "address": "القاهرة", "discount": "10"})
assert S("SELECT discount FROM customers WHERE id=1") == 10
d = cash.c.get("/api/customer?phone=%2B20%201001234567").get_json()
assert d == {"found": True, "name": "منى أحمد", "discount": 10}, d
assert cash.c.get("/api/customer?phone=01009999999").get_json() == {"found": False}
assert cash.c.get("/api/customer?phone=0100").get_json() == {"found": False}, "no partial lookup / no customer list leak"
assert anon.c.get("/api/customer?phone=01001234567").status_code in (302, 401)
mgr.post("/m/customers", {"id": "1", "name": "منى أحمد", "phone": "0100 123 4567", "discount": "250"})
assert S("SELECT discount FROM customers WHERE id=1") == 100, "clamped"
mgr.post("/m/customers", {"id": "1", "name": "منى أحمد", "phone": "0100 123 4567", "discount": "10"})
# التطبيق في الفاتورة: عرض 20% ثم خصم العميل 10% على الإجمالي بعد العرض
r = cash.j("/api/sale", {"items": [{"id": 1, "qty": 2}], "method": "cash", "customer_name": "منى", "customer_phone": "01001234567", "discount": 50})
rc = r["receipt"]
assert (rc["subtotal"], rc["discount"], rc["total"]) == (160, 16, 144), rc
assert "خصم العميل 10%" in rc["discount_note"], "manual discount from a cashier is ignored"
# إلغاء العرض ← يبقى خصم العميل فقط
mgr.post("/offers/1/toggle")
rc = cash.j("/api/sale", {"items": [{"id": 1, "qty": 2}], "method": "cash", "customer_phone": "01001234567", "customer_name": "x"})["receipt"]
assert (rc["subtotal"], rc["discount"], rc["total"]) == (200, 20, 180), rc
mgr.post("/offers/1/toggle")
# المدير يستطيع الخصم اليدوي، والخصم لا يتجاوز الإجمالي
rm = mgr.j("/api/sale", {"items": [{"id": 1, "qty": 1}], "method": "cash", "customer_name": "عميل جديد", "customer_phone": "01155555555", "discount": 5})["receipt"]
assert (rm["subtotal"], rm["discount"], rm["total"]) == (80, 5, 75)
rm = mgr.j("/api/sale", {"items": [{"id": 1, "qty": 1}], "method": "cash", "customer_name": "عميل جديد", "customer_phone": "01155555555", "discount": 5000})["receipt"]
assert rm["total"] == 0 and rm["discount"] == 80
assert S("SELECT COUNT(*) FROM customers") == 2
# الفاتورة المحفوظة تعرض سبب الخصم والعميل
sid = S("SELECT MAX(id) FROM sales WHERE customer_id=1")
html = admin.get(f"/sales/{sid}").get_data(as_text=True)
assert "منى أحمد" in html
# عميل أُرشف لا يُطابَق، وبياناته القديمة تبقى
mgr.post("/m/customers/2/delete")
assert cash.c.get("/api/customer?phone=01155555555").get_json() == {"found": False}
assert S("SELECT COUNT(*) FROM sales WHERE customer_id=2") == 2, "old invoices keep their customer"

# =============================================================== 5) إعداد «بيانات العميل إجبارية»
mgr.post("/m/customers", {"action": "require_customer"})                   # بدون علامة = إيقاف
r = cash.j("/api/sale", {"items": [{"id": 1, "qty": 1}], "method": "cash"})
assert r["ok"] and r["receipt"]["customer"] == "", "optional when the manager turns the requirement off"
assert cash.j("/api/sale", {"items": [{"id": 1, "qty": 1}], "method": "credit"}, 400)["ok"] is False, "credit still needs a customer"
cash.post("/m/customers", {"action": "require_customer", "require": "1"}, code=(403,))
mgr.post("/m/customers", {"action": "require_customer", "require": "1"})
assert cash.j("/api/sale", {"items": [{"id": 1, "qty": 1}], "method": "cash"}, 400)["ok"] is False

# =============================================================== 6) لا شيء يضيع: دفتر المخزون مطابق، والصور تُنسخ احتياطيًا
with A.app.app_context():
    assert len(A.reconcile()) == 0
ext = os.path.join(tmp, "usb")
admin.post("/backups", {"action": "extra_dir", "extra_dir": ext})
res = A.safety.make_backup("manual")
assert res and A.safety.is_valid_db(res)
mine = set(os.listdir(A.safety.IMAGE_DIR))
assert mine and mine <= set(os.listdir(os.path.join(ext, "images"))), "product/offer pictures copied to the external backup folder"
for pg in ("/", "/offers", "/m/customers", "/products", "/sales", "/pos"):
    mgr.get(pg)
print("FEATURES OK")
