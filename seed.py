"""بيانات تجريبية اختيارية:  python seed.py
(لا تشغّله على قاعدة بيانات حقيقية بها بيانات)"""
from app import app, db, ex, q, phone_key

CATS = ["عناية بالبشرة", "مكياج", "عناية بالشعر", "عطور"]
BRANDS = ["لوريال", "نيفيا", "ميبيلين", "ماك"]
PRODUCTS = [  # الاسم، الباركود، فئة، ماركة، تكلفة، سعر، مخزون، حد أدنى
    ("كريم مرطب للوجه 50 مل", "6221000000011", 0, 1, 60, 95, 30, 5),
    ("أحمر شفاه مات", "6221000000028", 1, 3, 80, 140, 20, 5),
    ("ماسكارا مكثفة", "6221000000035", 1, 2, 70, 120, 4, 6),
    ("شامبو للشعر الجاف 400 مل", "6221000000042", 2, 0, 55, 85, 25, 8),
    ("عطر نسائي 100 مل", "6221000000059", 3, 3, 300, 520, 8, 3),
]

with app.app_context():
    if q("SELECT 1 FROM products LIMIT 1", one=True):
        raise SystemExit("توجد منتجات بالفعل — تم الإلغاء")
    cat_ids = [ex("INSERT INTO categories(name) VALUES(?)", (c,)) for c in CATS]
    brand_ids = [ex("INSERT INTO brands(name) VALUES(?)", (b,)) for b in BRANDS]
    for n, bc, c, b, cost, price, stock, mn in PRODUCTS:
        ex("INSERT INTO products(name,barcode,category_id,brand_id,cost,price,stock,min_qty) VALUES(?,?,?,?,?,?,?,?)",
           (n, bc, cat_ids[c], brand_ids[b], cost, price, stock, mn))
    ex("INSERT INTO suppliers(name,phone) VALUES('شركة النور للتوريدات','01000000000')")
    ex("INSERT INTO customers(name,phone,phone_key,discount) VALUES('عميلة تجريبية','01111111111',?,10)", (phone_key('01111111111'),))
    print("تمت إضافة البيانات التجريبية")
