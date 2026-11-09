"""الاتصال بـ GitHub: نشر صفحة العروض (GitHub Pages) وتوزيع التحديثات.

كل شيء بمكتبة بايثون القياسية فقط. كل الدوال لا ترفع أخطاء للخارج: ترجع (ok, رسالة) ليظل النظام يعمل بدون إنترنت.
"""
import base64
import hashlib
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import safety

log = logging.getLogger("dahab")
UA = "DahabStore-Client"
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
PUBLISH_PATH = "docs"


def build_defaults():
    """قيم افتراضية تُدمج وقت البناء (اسم مستودع GitHub الذي بُني منه البرنامج)."""
    try:
        with open(safety.resource_path("build_defaults.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def settings():
    cfg = safety.load_config()
    d = build_defaults()
    return dict(repo=(cfg.get("github_repo") or d.get("repo") or "").strip(), token=(cfg.get("github_token") or "").strip(),
                api=(cfg.get("github_api") or "https://api.github.com").rstrip("/"), phone=(cfg.get("store_phone") or "").strip(),
                site_url=(cfg.get("site_url") or "").strip(), auto_update=bool(cfg.get("auto_update", True)),
                store=cfg.get("store_name") or "Dahab Store")


def site_url_effective(st=None):
    st = st or settings()
    if st["site_url"]:
        return st["site_url"]
    if REPO_RE.match(st["repo"]):
        owner, name = st["repo"].split("/")
        return f"https://{owner.lower()}.github.io/{name}/" if not name.lower().startswith(owner.lower() + ".github.io") else f"https://{owner.lower()}.github.io/"
    return ""


class GhError(Exception):
    pass


def _req(st, method, path, body=None, raw=False, timeout=25):
    url = path if path.startswith("http") else st["api"] + path
    headers = {"User-Agent": UA, "Accept": "application/vnd.github.raw+json" if raw else "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    if st["token"]:
        headers["Authorization"] = "Bearer " + st["token"]
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    rq = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(rq, timeout=timeout) as r:
            payload = r.read()
            return r.status, payload if raw else (json.loads(payload) if payload else {})
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read() or b"{}")
        except ValueError:
            payload = {}
        return e.code, payload
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise GhError("لا يوجد اتصال بالإنترنت" if isinstance(e, (urllib.error.URLError, OSError)) else str(e))


def blob_sha(data):
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _explain(code, payload):
    msg = (payload or {}).get("message", "")
    if code in (401, 403):
        return "التوكن غير صالح أو لا يملك صلاحية الكتابة (Contents: Read and write)" + (f" — {msg}" if msg else "")
    if code == 404:
        return "المستودع غير موجود أو التوكن لا يصل إليه"
    return f"GitHub رد بالخطأ {code}" + (f": {msg}" if msg else "")


# ------------------------------------------------------------------ site
def publish_site(offers_payload, images, template_html, st=None):
    """ينشر docs/index.html و docs/offers.json و docs/img/*. images: {اسم_الملف: bytes}.  -> (ok, msg, site_url)"""
    st = st or settings()
    if not REPO_RE.match(st["repo"]):
        return False, "لم يتم إدخال مستودع GitHub في الإعدادات", ""
    if not st["token"]:
        return False, "لم يتم إدخال توكن GitHub في الإعدادات", ""
    repo = st["repo"]
    try:
        code, info = _req(st, "GET", f"/repos/{repo}")
        if code != 200:
            return False, _explain(code, info), ""
        if not (info.get("permissions") or {}).get("push", True):
            return False, "التوكن للقراءة فقط — يلزم صلاحية Contents: Read and write", ""
        branch = info.get("default_branch") or "main"
        code, tree = _req(st, "GET", f"/repos/{repo}/git/trees/{urllib.parse.quote(branch)}?recursive=1")
        remote = {t["path"]: t["sha"] for t in tree.get("tree", [])} if code == 200 else {}
        files = {f"{PUBLISH_PATH}/index.html": template_html.encode("utf-8"),
                 f"{PUBLISH_PATH}/offers.json": json.dumps(offers_payload, ensure_ascii=False, indent=1, sort_keys=True).encode("utf-8"),
                 f"{PUBLISH_PATH}/.nojekyll": b""}
        for name, data in images.items():
            files[f"{PUBLISH_PATH}/img/{name}"] = data
        changed = 0
        for path, data in files.items():
            if remote.get(path) == blob_sha(data):
                continue
            body = {"message": "Dahab Store: update offers site", "content": base64.b64encode(data).decode(), "branch": branch}
            if path in remote:
                body["sha"] = remote[path]
            code, res = _req(st, "PUT", f"/repos/{repo}/contents/{urllib.parse.quote(path)}", body)
            if code not in (200, 201):
                return False, "فشل رفع " + path + " — " + _explain(code, res), ""
            changed += 1
        url = ""
        code, pg = _req(st, "GET", f"/repos/{repo}/pages")
        if code == 404:
            code, pg = _req(st, "POST", f"/repos/{repo}/pages", {"source": {"branch": branch, "path": "/" + PUBLISH_PATH}})
            if code not in (200, 201, 409):
                return True, "تم رفع الصفحة، لكن تعذّر تفعيل GitHub Pages تلقائيًا — فعّله من Settings ▸ Pages (Branch: %s / docs)" % branch, ""
        if isinstance(pg, dict):
            url = pg.get("html_url") or ""
        return True, ("تم نشر الصفحة" if changed else "الصفحة محدّثة بالفعل"), url
    except GhError as e:
        return False, str(e), ""
    except Exception as e:                                  # لا شيء يوقف النظام
        log.exception("publish_site")
        return False, f"خطأ غير متوقع: {e}", ""


# ------------------------------------------------------------------ update feed
FEED_DIR = "updates"


def feed_latest(st=None):
    """-> (ok, dict|msg)"""
    st = st or settings()
    if not REPO_RE.match(st["repo"]):
        return False, "لم يتم إدخال مستودع GitHub"
    try:
        code, data = _req(st, "GET", f"/repos/{st['repo']}/contents/{FEED_DIR}/latest.json", raw=True)
        if code == 404:
            return True, None                               # لا توجد تحديثات منشورة بعد
        if code != 200:
            return False, _explain(code, {})
        info = json.loads(data)
        if not (isinstance(info.get("build"), int) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}\.zip", str(info.get("file", "")))
                and re.fullmatch(r"[0-9a-f]{64}", str(info.get("sha256", "")))):
            return False, "ملف latest.json غير صالح"
        return True, info
    except GhError as e:
        return False, str(e)
    except ValueError:
        return False, "ملف latest.json غير صالح"


def feed_download(info, st=None, max_bytes=80 * 1024 * 1024):
    st = st or settings()
    try:
        code, data = _req(st, "GET", f"/repos/{st['repo']}/contents/{FEED_DIR}/{info['file']}", raw=True, timeout=120)
        if code != 200:
            return False, _explain(code, {})
        if len(data) > max_bytes or hashlib.sha256(data).hexdigest() != info["sha256"]:
            return False, "الملف المحمّل تالف أو لا يطابق البصمة"
        return True, data
    except GhError as e:
        return False, str(e)


def feed_publish(zip_bytes, manifest, st=None):
    """يرفع التحديث إلى المستودع ليصل للأجهزة الأخرى. -> (ok, msg)"""
    st = st or settings()
    if not REPO_RE.match(st["repo"]) or not st["token"]:
        return False, "لتوزيع التحديث على باقي الأجهزة أدخل مستودع GitHub والتوكن في الإعدادات"
    repo, build = st["repo"], int(manifest["build"])
    name = f"dahab-{build}.zip"
    try:
        code, info = _req(st, "GET", f"/repos/{repo}")
        if code != 200:
            return False, _explain(code, info)
        branch = info.get("default_branch") or "main"
        latest = json.dumps(dict(build=build, version=manifest.get("version", ""), notes=manifest.get("notes", ""), file=name,
                                 sha256=hashlib.sha256(zip_bytes).hexdigest(), size=len(zip_bytes)), ensure_ascii=False, indent=1).encode()
        for path, data in ((f"{FEED_DIR}/{name}", zip_bytes), (f"{FEED_DIR}/latest.json", latest)):      # الملف أولًا ثم المؤشر
            body = {"message": f"Dahab Store: update build {build}", "content": base64.b64encode(data).decode(), "branch": branch}
            code, ex = _req(st, "GET", f"/repos/{repo}/contents/{path}?ref={urllib.parse.quote(branch)}")
            if code == 200 and isinstance(ex, dict) and ex.get("sha"):
                body["sha"] = ex["sha"]
            code, res = _req(st, "PUT", f"/repos/{repo}/contents/{path}", body, timeout=120)
            if code not in (200, 201):
                return False, "فشل رفع التحديث — " + _explain(code, res)
        return True, "تم رفع التحديث للمستودع وسيصل للأجهزة الأخرى تلقائيًا عند اتصالها بالإنترنت"
    except GhError as e:
        return False, str(e)


def test_connection(st=None):
    st = st or settings()
    if not REPO_RE.match(st["repo"]):
        return False, "اكتب اسم المستودع بالشكل  اسم-المستخدم/اسم-المستودع"
    try:
        code, info = _req(st, "GET", f"/repos/{st['repo']}")
        if code != 200:
            return False, _explain(code, info)
        push = (info.get("permissions") or {}).get("push")
        if st["token"] and push is False:
            return False, "تم الاتصال لكن التوكن بلا صلاحية كتابة"
        return True, "الاتصال سليم" + ("" if st["token"] else " (بدون توكن: قراءة التحديثات فقط)")
    except GhError as e:
        return False, str(e)


# ------------------------------------------------------------------ background worker
class Worker(threading.Thread):
    """يعمل في الخلفية: ينشر الموقع لو فيه تغييرات معلّقة ويفحص التحديثات كل فترة. لا يوقف النظام أبدًا."""

    def __init__(self, site_job, update_job, tick=60, update_every=1800):
        super().__init__(name="dahab-cloud", daemon=True)
        self.site_job, self.update_job, self.tick, self.update_every = site_job, update_job, tick, update_every
        self.stop = threading.Event()
        self.last_update, self.last_site_try = 0.0, 0.0

    def run(self):
        self.stop.wait(20)
        while not self.stop.is_set():
            now = time.time()
            try:
                if safety.get_status().get("site_dirty") and now - self.last_site_try >= 240:
                    self.last_site_try = now
                    self.site_job()
                if now - self.last_update >= self.update_every:
                    self.last_update = now
                    self.update_job()
            except Exception:
                log.exception("cloud worker")
            self.stop.wait(self.tick)
