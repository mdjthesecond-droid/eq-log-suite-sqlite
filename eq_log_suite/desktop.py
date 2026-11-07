"""The app in its own window instead of a browser tab:

    .venv/bin/python -m eq_log_suite.desktop

Same pages, same in-app tailer -- the web server runs inside this process
on 127.0.0.1 and pywebview shows it in a native window (Qt WebEngine on
Linux, Edge WebView2 on Windows). Pages that open a new tab (merged
breakdown, compare) open another app window instead. Closing the last
window stops everything, tailer included -- each line is committed as it's
recorded, so nothing is lost.

If the app is already running (e.g. bin/start-web.sh), this just opens a
window onto it rather than starting a second copy (only one may record).
If pywebview isn't installed, it falls back to the browser.
"""

import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser

HOST = "127.0.0.1"
PORT = 8000
START_PAGE = "/encounters"
TITLE = "EQ Log Suite"


def _app_at(port: int) -> bool:
    """Whether this app (not something else) is answering on port."""
    try:
        with urllib.request.urlopen(f"http://{HOST}:{port}/", timeout=1) as r:
            return b"<title>EQ Log Suite" in r.read(4096)
    except OSError:
        return False


def _port_free(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex((HOST, port)) != 0


def _free_port() -> int:
    with socket.socket() as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


def _start_server(port: int):
    import uvicorn
    from eq_log_suite.web.app import app
    server = uvicorn.Server(uvicorn.Config(app, host=HOST, port=port, log_level="warning"))
    server.install_signal_handlers = lambda: None   # it's not the main thread
    thread = threading.Thread(target=server.run, name="eqls-web", daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            return server, thread
        time.sleep(0.05)
    raise RuntimeError("the web server didn't start")


class Api:
    """Called from the pages (window.pywebview.api) -- see base.html."""

    def __init__(self, base: str):
        self.base = base

    def open_window(self, url: str):
        import webview
        if url.startswith("/"):
            url = self.base + url
        if not url.startswith(self.base):
            webbrowser.open(url)   # anything off-site goes to the browser
            return
        webview.create_window(TITLE, url, width=1300, height=850, js_api=self)


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="EQ Log Suite in its own window.")
    ap.add_argument("--port", type=int, default=PORT, help=f"port to use or find the app on (default {PORT})")
    args = ap.parse_args(argv)
    server = thread = None
    if _app_at(args.port):
        base = f"http://{HOST}:{args.port}"
    else:
        port = args.port if _port_free(args.port) else _free_port()
        server, thread = _start_server(port)
        base = f"http://{HOST}:{port}"

    if sys.platform.startswith("linux"):
        # Chromium (inside Qt WebEngine) freezes timers in a window that's
        # hidden or covered -- e.g. by a fullscreen game -- which stops /live
        # polling, so the meter stalls (confirmed real with a browser tab,
        # 2026-11-07). This window is meant to sit behind the game, so don't.
        flags = "--disable-background-timer-throttling --disable-renderer-backgrounding " \
                "--disable-backgrounding-occluded-windows"
        os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = (os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "") + " " + flags).strip()
    try:
        import webview
    except ImportError:
        print("pywebview isn't installed (.venv/bin/pip install -r requirements.txt) -- opening the browser instead.")
        webbrowser.open(base + START_PAGE)
        if thread:
            thread.join()
        return

    api = Api(base)
    window = webview.create_window(TITLE, base + START_PAGE, width=1400, height=900, js_api=api)
    if os.environ.get("EQLS_SMOKE_TEST"):
        # Checks a build end to end: the page really rendered in the window
        # and the page-to-app bridge is there. Prints the result and exits.
        def smoke():
            ok = window.events.loaded.wait(60)
            title = window.evaluate_js("document.title") if ok else None
            bridge = window.evaluate_js("typeof window.pywebview") if ok else None
            print(f"SMOKE TEST: loaded={ok} title={title!r} bridge={bridge!r}", flush=True)
            window.destroy()
        threading.Thread(target=smoke, daemon=True).start()
    # Qt on Linux (installed by requirements.txt); pywebview picks Edge on Windows.
    gui = "qt" if sys.platform.startswith("linux") else None
    # Private: no browser profile on disk. Nothing needs one (settings live in
    # the database), and with a saved profile Qt never finished loading a
    # second window (checked 2026-11-07, pywebview 6.2.1 / Qt 6.12).
    webview.start(gui=gui, private_mode=True)

    if server:   # last window closed: stop the server, and with it the tailer
        server.should_exit = True
        thread.join(timeout=5)
    # Qt WebEngine can hang tearing itself down at interpreter exit; every
    # recorded line is already committed, so end the process here.
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
