"""اختبار شامل سريع:  python test_smoke.py   (يستخدم قاعدة بيانات مؤقتة)"""
import os
import re
import tempfile

tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(tmp, "t.db")

import app as A  # noqa: E402

c = A.app.test_client()


def S(sql, args=()):
    with A.app.app_context():
        return A.scalar(sql, args)


def csrf():
    with c.session_transaction() as s:
        return s.get("csrf", "")


def get(url, code=200):
    r = c.get(url, follow_redirects=False)
    assert r.status_code == code, (url, r.status_code, r.data[:300])
    return r


def post(url, data=None, code=(200, 302)):
    d = dict(data or {})
    d["csrf"] = csrf()
    r = c.post(url, data=d)
    assert r.status_code in code, (url, r.status_code, r.data[:300])
    return r


def jpost(url, payload, code=200):
    r = c.post(url, json=payload, headers={"X-CSRF": csrf()})
    assert r.status_code == code, (url, r.status_code, r.get_json())
    return r.get_json()


# --- auth
get("/login")
get("/", 302)
assert c.post("/login", data={"username": "admin", "password": "x"}).status_code == 400   # no csrf
post("/login", {"username": "admin", "password": "wrong"})
r = post("/login", {"username": "admin", "password": "admin123"})
assert r.status_code == 302
assert get("/", 302).headers["Location"].endswith("/profile")           # forced password change
post("/profile", {"current": "admin123", "new": "secret99", "new2": "secret99"})
get("/")

# --- master data
for k, vals in {"categories": {"name": "مكياج"}, "brands": {"name": "ماك"},
                "suppliers": {"name": "مورد 1", "phone": "010"}, "customers": {"name": "عميل 1", "phone": "01100000000"}}.items():
    post(f"/m/{k}", vals)
    get(f"/m/{k}")
post("/m/brands", {"name": "ماك"})  # duplicate -> flash, no crash
post("/m/customers", {"action": "require_customer"})  # إيقاف إلزام بيانات العميل لهذا الاختبار

r = post("/products/new", {"name": "أحمر شفاه", "barcode": "6221111", "category_id": "1", "brand_id": "1",
                           "cost": "50", "price": "100", "stock": "10", "min_qty": "3"})
post("/products/new", {"name": "مكرر", "barcode": "6221111", "price": "5"})  # duplicate barcode rejected
assert S("SELECT COUNT(*) FROM products") == 1
get("/products")
get("/products?q=622&low=1")
get("/products/1/edit")
post("/products/1/edit", {"name": "أحمر شفاه", "barcode": "6221111", "category_id": "1", "brand_id": "1",
                          "cost": "50", "price": "100", "stock": "10", "min_qty": "3"})
assert c.get("/api/products?q=6221111").get_json()[0]["name"] == "أحمر شفاه"

# --- POS
j = jpost("/api/sale", {"items": [{"id": 1, "qty": 2}], "discount": 10, "method": "cash"})
assert j["ok"] and j["invoice"] == "INV-000001"
assert S("SELECT stock FROM products WHERE id=1") == 8
assert S("SELECT total FROM sales WHERE id=1") == 190
assert jpost("/api/sale", {"items": [{"id": 1, "qty": 99}]}, 400)["ok"] is False          # stock guard
assert jpost("/api/sale", {"items": [{"id": 1, "qty": 1}], "method": "credit"}, 400)["ok"] is False  # credit needs customer
assert jpost("/api/sale", {"items": [{"id": 1, "qty": -1}]}, 400)["ok"] is False
j2 = jpost("/api/sale", {"items": [{"id": 1, "qty": 1}], "method": "credit", "customer_id": 1})
assert S("SELECT stock FROM products WHERE id=1") == 7
get("/sales")
get(f"/sales/{j['id']}?print=1")
get("/m/customers")
assert "100.00" in c.get("/m/customers").get_data(as_text=True)
post("/m/customers/1/pay", {"amount": "40"})
assert S("SELECT paid FROM sales WHERE id=2") == 40

# --- purchase
get("/purchases/new")
jp = jpost("/api/purchase", {"items": [{"id": 1, "qty": 10, "cost": 60}], "supplier_id": 1, "paid": 300})
assert S("SELECT stock FROM products WHERE id=1") == 17
assert S("SELECT cost FROM products WHERE id=1") == round((7 * 50 + 10 * 60) / 17, 2)
get("/purchases")
get(f"/purchases/{jp['id']}")

# --- expenses / reports / admin pages
post("/expenses", {"title": "إيجار", "amount": "500"})
get("/expenses")
post("/expenses/1/delete")
post("/expenses", {"title": "كهرباء", "amount": "120"})
get("/reports")
assert b"," in get("/reports/sales.csv").data
get("/reports/inventory.csv")
get("/")
get("/users")
get("/audit")
get("/audit?q=sale")
assert get("/backup").data[:6] == b"SQLite"

# --- cancel sale restores stock; cancel purchase guarded
post("/sales/2/cancel")
assert S("SELECT stock FROM products WHERE id=1") == 18 - 0  # 17 + 1 returned
post("/purchases/1/cancel")
assert S("SELECT stock FROM products WHERE id=1") == 8

# --- roles: cashier is restricted
post("/users", {"username": "kas", "full_name": "كاشير", "role": "cashier", "password": "cash123"})
post("/logout")
get("/login")
post("/login", {"username": "kas", "password": "cash123"})
assert get("/", 302).headers["Location"].endswith("/profile") is False
for u in ("/users", "/audit", "/purchases", "/reports", "/expenses", "/products/new", "/backup", "/m/suppliers",
          "/products", "/sales", "/m/customers", "/purchases/new"):
    get(u, 403)
get("/pos")
jc = jpost("/api/sale", {"items": [{"id": 1, "qty": 1}], "method": "cash"})
assert jc["ok"] and jc["receipt"]["items"][0]["qty"] == 1 and jc["receipt"]["total"] == 100
get(f"/sales/{jc['id']}")        # يرى فاتورته فقط
get("/sales/1", 403)             # لا يرى فواتير غيره
post("/sales/1/cancel", code=(403,))
assert "cost" not in c.get("/api/products?q=6221111").get_json()[0]
jpost("/api/purchase", {"items": [{"id": 1, "qty": 1, "cost": 1}]}, 403)
post("/logout")
get("/pos", 302)
print("ALL OK")
