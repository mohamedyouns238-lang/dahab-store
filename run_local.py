"""تشغيل Dahab Store محليًا: خادم + فتح الواجهة. لا يقفل نفسه أبدًا — يظل يعمل حتى تطفئ الجهاز أو توقفه بنفسك."""
import os
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import safety  # noqa: E402

safety.setup_logging()
PORT = int(safety.load_config().get("port", 58321))
URL = f"http://127.0.0.1:{PORT}"


def running():
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=0.6):
            pass
        return urllib.request.urlopen(URL + "/health", timeout=2).read().startswith(b"DAHAB-OK")
    except Exception:
        return False


def open_ui():
    for exe in (r"C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe", r"C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
                r"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe", r"C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe"):
        if os.path.exists(exe):
            args = [exe, f"--app={URL}", "--start-maximized", "--lang=ar"]
            if safety.load_config().get("silent_print"):
                args.append("--kiosk-printing")
            try:
                subprocess.Popen(args)
                return
            except OSError:
                pass
    webbrowser.open(URL)


def main():
    if running():                       # شغّال بالفعل: افتح نافذة عليه فقط
        open_ui()
        return 0
    import app as A
    import threading
    try:
        from waitress import create_server
        srv = create_server(A.app, host="127.0.0.1", port=PORT, threads=8, ident="DahabStore")
        run = srv.run
    except ImportError:                  # احتياطي لو waitress غير متاح
        from werkzeug.serving import make_server
        srv = make_server("127.0.0.1", PORT, A.app, threaded=True)
        run = srv.serve_forever
    threading.Thread(target=lambda: (time.sleep(1.5), open_ui()), daemon=True).start()
    run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
