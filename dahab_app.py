"""Dahab Store — مُشغّل نسخة الكمبيوتر (ويندوز)

يشغّل النظام على هذا الجهاز فقط (127.0.0.1) ويفتحه في نافذة مستقلة بدون شريط عنوان
(Edge أو Chrome بوضع التطبيق). لا يحتاج إنترنت. كل البيانات في:  %APPDATA%\\DahabStore
"""
import os
import sys
import tempfile

if sys.stdout is None:                      # نسخة بدون نافذة سوداء
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

# وضع الاختبار الذاتي يعمل على بيانات مؤقتة ولا يلمس بيانات المحل
if "--selftest" in sys.argv:
    os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="dahab_selftest_")
    os.environ.pop("DB_PATH", None)

import argparse
import logging
import shutil
import signal
import socket
import subprocess
import threading
import time
import urllib.request

import bootstrap

if "--selftest" not in sys.argv:
    os.environ["DAHAB_LAUNCHER"] = "1"           # يفعّل نظام التحديث (غير متاح خارج المُشغّل)
safety = None                                     # يُحمَّل بعد اختيار الإصدار الفعّال (الأصلي أو المحدَّث)
log = logging.getLogger("dahab")
SIGNATURE = b"DAHAB-OK"


def early_config():
    """يقرأ الإعدادات قبل تحميل أي كود قابل للتحديث (المنفذ فقط)."""
    import json
    try:
        with open(os.path.join(bootstrap.data_dir(), "config.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def write_selftest(ok, **kw):
    out = os.environ.get("DAHAB_SELFTEST_OUT")
    if out:
        import json
        try:
            with open(out, "w", encoding="utf-8") as f:
                json.dump(dict(ok=ok, **kw), f, ensure_ascii=False)
        except OSError:
            pass


def spawn_self(*extra):
    """يشغّل نسخة جديدة من البرنامج بعد إغلاق هذه (إعادة التشغيل بعد التحديث)."""
    args = ([sys.executable] if getattr(sys, "frozen", False) else [sys.executable, os.path.abspath(__file__)]) + list(extra)
    kw = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    if os.name == "nt":
        kw["creationflags"] = 0x00000008 | 0x00000200      # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    env = {k: v for k, v in os.environ.items() if k != "DAHAB_OVERRIDE"}
    return subprocess.Popen(args, env=env, **kw)


# ------------------------------------------------------------------ helpers
def message_box(text, title="Dahab Store"):
    log.error(text)
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, text, title, 0x10)
        except Exception:
            pass
    else:
        print(text, file=sys.stderr)


def port_state(port):
    """free | ours (نسخة أخرى من البرنامج تعمل) | other (برنامج آخر يستخدم المنفذ)"""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.6):
            pass
    except OSError:
        return "free"
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
            return "ours" if r.read().startswith(SIGNATURE) else "other"
    except Exception:
        return "other"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def find_browser():
    if os.environ.get("DAHAB_BROWSER"):
        return os.environ["DAHAB_BROWSER"]
    cands = []
    if os.name == "nt":
        for key in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
            base = os.environ.get(key)
            if base:
                cands += [os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"),
                          os.path.join(base, "Google", "Chrome", "Application", "chrome.exe")]
    else:
        cands = [shutil.which(n) for n in ("google-chrome", "chromium", "chromium-browser", "microsoft-edge")]
    return next((p for p in cands if p and os.path.exists(p)), None)


def open_window(url, cfg):
    exe = find_browser()
    if not exe:
        import webbrowser
        webbrowser.open(url)
        log.warning("لم يُعثر على Edge/Chrome — فُتح المتصفح الافتراضي")
        return None
    args = [exe, f"--app={url}", f"--user-data-dir={os.path.join(bootstrap.data_dir(), 'browser')}",
            "--no-first-run", "--no-default-browser-check", "--disable-background-mode",
            "--disable-sync", "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
            "--disable-backgrounding-occluded-windows", "--start-maximized", "--lang=ar", "--disable-features=Translate"]
    if cfg.get("silent_print"):
        args.append("--kiosk-printing")             # طباعة مباشرة بلا نافذة تأكيد
    if os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() == 0:
        args.append("--no-sandbox")
    return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class Server:
    """خادم إنتاج (waitress) إن وُجد، وإلا خادم werkzeug."""

    def __init__(self, wsgi, host, port):
        try:
            from waitress import create_server
            self.kind, self.s = "waitress", create_server(wsgi, host=host, port=port, threads=8, ident="DahabStore")
        except ImportError:
            from werkzeug.serving import make_server
            self.kind, self.s = "werkzeug", make_server(host, port, wsgi, threaded=True)

    def run(self):
        (self.s.run if self.kind == "waitress" else self.s.serve_forever)()

    def stop(self):
        try:
            self.s.close() if self.kind == "waitress" else self.s.shutdown()
        except Exception:
            log.exception("server stop")


def wait_health(port, seconds=25):
    end = time.time() + seconds
    while time.time() < end:
        if port_state(port) == "ours":
            return True
        time.sleep(0.2)
    return False


# ------------------------------------------------------------------ main
def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--headless", action="store_true", help="بدون نافذة (للاختبار/السيرفر)")
    ap.add_argument("--selftest", action="store_true", help="فحص ذاتي ثم خروج")
    ap.add_argument("--port", type=int)
    ap.add_argument("--restarted", action="store_true", help="إعادة تشغيل بعد تحديث (بدون نافذة جديدة)")
    opts = ap.parse_args(argv)

    cfg = early_config()
    host = "0.0.0.0" if cfg.get("lan_access") else "127.0.0.1"
    port = opts.port or int(os.environ.get("DAHAB_PORT") or cfg.get("port") or 58321)
    if opts.selftest:
        port = free_port()

    state = port_state(port)
    if state == "ours" and opts.restarted:       # النسخة القديمة لسه بتقفل: انتظر تحرير المنفذ
        end = time.time() + 25
        while state == "ours" and time.time() < end:
            time.sleep(0.5)
            state = port_state(port)
    if state == "ours":                          # نسخة شغّالة بالفعل: افتح نافذة جديدة عليها فقط
        log.info("النظام يعمل بالفعل — فتح نافذة جديدة")
        if not opts.headless and not opts.restarted:
            open_window(f"http://127.0.0.1:{port}", early_config())
        return 0
    if state == "other":
        port = free_port()
        log.warning("المنفذ مشغول ببرنامج آخر — استُخدم المنفذ %s", port)

    global safety
    ov = os.environ.get("DAHAB_OVERRIDE") if opts.selftest else bootstrap.begin_boot()   # الإصدار الفعّال
    try:
        A = bootstrap.load_all(ov)               # فحص السلامة + الترقية + تشغيل النسخ الاحتياطي
    except BaseException as e:
        write_selftest(False, error=f"{type(e).__name__}: {e}")
        raise
    safety = sys.modules["safety"]
    safety.setup_logging()
    if os.environ.get("DAHAB_OVERRIDE"):
        log.info("تشغيل الإصدار المحدَّث: %s", os.environ["DAHAB_OVERRIDE"])
    cfg = safety.load_config()
    url = f"http://127.0.0.1:{port}"
    try:
        safety.make_backup("start")
    except Exception:
        log.exception("start backup")

    stopping = threading.Event()
    srv = Server(A.app, host, port)

    def serve():
        nonlocal srv
        while not stopping.is_set():
            try:
                srv.run()
                return
            except Exception:
                log.exception("الخادم توقف — إعادة التشغيل")
                time.sleep(1)
                try:
                    srv = Server(A.app, host, port)
                except Exception:
                    log.exception("تعذر إعادة التشغيل")
                    time.sleep(3)

    t = threading.Thread(target=serve, name="dahab-server", daemon=True)
    t.start()
    if not wait_health(port):
        message_box("تعذر تشغيل النظام. التفاصيل في:\n" + os.path.join(safety.LOG_DIR, "app.log"))
        return 1
    log.info("Dahab Store يعمل على %s (%s)", url, srv.kind)

    for sig in (signal.SIGINT, getattr(signal, "SIGTERM", None), getattr(signal, "SIGBREAK", None)):
        if sig:
            try:
                signal.signal(sig, lambda *_: stopping.set())
            except (ValueError, OSError):
                pass

    code, restart, confirmed = 0, False, False
    if opts.selftest:
        code = selftest(url)
    else:
        if getattr(A, "start_cloud", None):
            A.start_cloud()                      # نشر العروض + فحص التحديثات في الخلفية
        proc = None if (opts.headless or opts.restarted) else open_window(url, cfg)
        started = last_loop = time.time()
        stale_after = float(os.environ.get("DAHAB_STALE", 150))     # النافذة أُغلقت + لا نشاط
        first_ping_wait = float(os.environ.get("DAHAB_FIRST", 900))  # لم تُفتح الصفحة قط
        idle_exit = float(os.environ.get("DAHAB_IDLE", 900))         # لا نبضات إطلاقًا (حتى لو بدا أن النافذة موجودة)
        pinged = False
        while not stopping.wait(2):
            now = time.time()
            if now - last_loop > 30:             # الجهاز كان نائمًا: لا تعتبر الصفحة ميتة
                A.LAST_PING[0] = now
            last_loop = now
            act = max(A.LAST_PING[0], A.LAST_ACTIVITY[0])           # أي طلب من الصفحة = دليل أنها حيّة
            age = now - act
            pinged = pinged or A.LAST_PING[0] > started
            window_gone = proc is None or proc.poll() is not None
            if not confirmed and (pinged or now - started > float(os.environ.get("DAHAB_CONFIRM", 40))):
                confirmed = True
                try:
                    bootstrap.confirm_boot()     # الإصدار الجديد ثبت — لا تراجع بعد الآن
                except Exception:
                    log.exception("confirm_boot")
            mode = bootstrap.RESTART["mode"]
            if mode == "now" or (mode == "idle" and now - A.LAST_ACTIVITY[0] >= 120):
                restart = True
                break
            if opts.headless:
                continue
            # لا نقفل السيرفر بسرعة: المتصفح يبطّئ مؤقتات الصفحة المصغّرة/المغطاة فتقلّ النبضات (قفل خاطئ = مشكلة للمحل)
            if window_gone and age > (stale_after if pinged else first_ping_wait):
                log.info("إغلاق: النافذة أُغلقت (pinged=%s, آخر نشاط قبل %.0fث)", pinged, age)
                break
            if age > idle_exit:
                log.info("إغلاق: لا أي نشاط منذ %.0fث", age)
                break

    log.info("إيقاف التشغيل…")
    stopping.set()
    srv.stop()
    t.join(timeout=8)
    try:
        safety.make_backup("exit")
    except Exception:
        log.exception("exit backup")
    if restart:
        log.info("إعادة تشغيل لتفعيل التحديث")
        spawn_self("--restarted", *(["--headless"] if opts.headless else []), *(["--port", str(port)] if opts.port else []))
    return code


def selftest(url):
    """يتأكد أن النسخة المجمَّعة تعمل: قاعدة بيانات، قوالب، ملفات static، تسجيل دخول، نسخة احتياطية."""
    import http.cookiejar
    import re
    try:
        jar = http.cookiejar.CookieJar()
        op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))     # جلسة واحدة لكل الطلبات (مطلوب لـ CSRF)

        def get(path):
            return op.open(url + path, timeout=10)
        page = get("/login").read().decode("utf-8")
        assert "Dahab Store" in page and "دخول" in page, "login template"
        assert len(get("/static/style.css").read()) > 1000, "static css"
        assert len(get("/static/icon.png").read()) > 500, "icon"
        tok = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
        body = urllib.parse.urlencode({"csrf": tok, "username": "admin", "password": "admin123"}).encode()
        r = op.open(urllib.request.Request(url + "/login", data=body), timeout=10)
        assert "/profile" in r.geturl(), "login redirect to forced password change: " + r.geturl()
        assert os.access(safety.IMAGE_DIR, os.W_OK), "images folder writable"
        assert safety.make_backup("selftest") and safety.is_valid_db(safety.DB_PATH), "backup"
        A = sys.modules["app"]
        for n in A.app.jinja_env.list_templates():                 # كل القوالب سليمة
            A.app.jinja_env.get_template(n)
        qr = sys.modules["qrcode_py"].matrix("https://example.com/")
        assert len(qr) >= 21, "qr"
        build = sys.modules["version"].BUILD
        write_selftest(True, build=build)
        print("SELFTEST OK build=%s" % build)
        return 0
    except Exception as e:
        log.exception("selftest failed")
        write_selftest(False, error=f"{type(e).__name__}: {e}")
        print("SELFTEST FAILED:", e)
        return 2


if __name__ == "__main__":
    import urllib.parse  # noqa: F401  (يستخدمه selftest)
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:
        log.exception("fatal")
        message_box(f"حدث خطأ أثناء التشغيل:\n{exc}\n\nالتفاصيل في: {os.path.join(bootstrap.data_dir(), 'logs', 'app.log')}")
        sys.exit(1)
