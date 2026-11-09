# رفع نظام «دهب» على الإنترنت — خطوة بخطوة

## الطريقة الأسهل: PythonAnywhere (مجانية، وبياناتك تتحفظ)

1. افتح https://www.pythonanywhere.com واضغط **Pricing & signup** ثم **Create a Beginner account** (مجاني). اختر اسم مستخدم، وسيكون رابط موقعك: `https://اسمك.pythonanywhere.com`
2. من القائمة العلوية اضغط **Files**، ثم اضغط **Upload a file** وارفع ملف `cosmetics_store.zip`.
3. من القائمة العلوية اضغط **Consoles** ثم **Bash**. اكتب هذين السطرين (سطر سطر، Enter بعد كل واحد):
   ```
   unzip cosmetics_store.zip
   pip install --user flask
   ```
4. اضغط **Web** ثم **Add a new web app** ثم **Next**، اختر **Flask**، ثم اختر أحدث إصدار Python (يجب أن يكون 3.11 أو أحدث). في خانة المسار اكتب:
   `/home/اسمك/cosmetics_store/app.py`
   (استبدل «اسمك» باسم المستخدم الذي اخترته). اضغط **Next**.
5. في صفحة Web ستجد رابط **WSGI configuration file**، اضغط عليه، **امسح كل ما بداخله** والصق هذا (غيّر «اسمك»):
   ```python
   import sys
   path = '/home/اسمك/cosmetics_store'
   if path not in sys.path:
       sys.path.insert(0, path)
   from app import app as application
   ```
   ثم اضغط **Save**.
6. ارجع لصفحة **Web**، انزل لقسم **Security** وفعّل **Force HTTPS**، ثم اضغط الزر الأخضر الكبير **Reload**.
7. افتح `https://اسمك.pythonanywhere.com` — هتظهر شاشة دخول «دهب».
   - المستخدم: `admin` وكلمة المرور: `admin123` (النظام سيطلب منك تغييرها فورًا).

**ملاحظات مهمة**
- في الخطة المجانية لازم تدخل صفحة **Web** كل 3 أشهر وتضغط **Run until 3 months from today** وإلا يتوقف الموقع.
- البيانات في ملف `store.db` داخل مجلد `cosmetics_store`، وبتفضل محفوظة. خذ نسخة منها دوريًا من **المستخدمون ← تنزيل نسخة احتياطية**.
- لتجربة بيانات تجريبية (اختياري، قبل أي بيانات حقيقية): في Bash اكتب `cd cosmetics_store && python3 seed.py`.
- لو ظهر خطأ، افتح من صفحة Web الرابط **Error log** وابعتلي آخر سطرين منه.

---

## بديل: Render (أسهل في الرفع لكن البيانات تحتاج خطة مدفوعة)

1. ارفع مجلد المشروع على حساب GitHub.
2. في https://render.com اضغط **New ← Blueprint** واختر المستودع؛ الملف `render.yaml` جاهز.
3. تنبيه: الخطة المجانية تمسح البيانات مع كل تحديث، لذلك الملف مضبوط على خطة `starter` (مدفوعة) مع قرص دائم.

## للتشغيل على جهاز المحل فقط (بدون إنترنت)

- **ويندوز:** فك الضغط، واضغط مرتين على `run.bat`، ثم افتح http://localhost:5000
- أجهزة الكاشير الأخرى على نفس الشبكة تفتح `http://IP-الجهاز:5000` (اعرف الـ IP من `ipconfig`).
- يلزم تثبيت Python من https://python.org (اختر **Add Python to PATH** أثناء التثبيت).
