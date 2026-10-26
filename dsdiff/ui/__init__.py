"""python -m dsdiff ui — a local GUI over dsdiff: versions, compare, record, patch check (see API.md)."""
import argparse
import threading
import webbrowser

from .reports import CACHE_CHANGES
from .server import CONFIRM_SECONDS, make_server


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m dsdiff ui", description=__doc__.split(" — ")[1])
    ap.add_argument("--repo", help="directory holding .dsdiff (default: --root, if it has one)")
    ap.add_argument("--root", default=".", help="only files inside this directory can be chosen (default: current)")
    ap.add_argument("--port", type=int, default=0, help="port on 127.0.0.1 (default: any free one)")
    ap.add_argument("--cache-changes", type=int, default=CACHE_CHANGES,
                    help="changes kept in memory over all cached compare reports (default: %(default)s)")
    ap.add_argument("--confirm-seconds", type=float, default=CONFIRM_SECONDS,
                    help="ask before a flat compare of two files estimated longer than this (default: %(default)s)")
    ap.add_argument("--no-browser", action="store_true", help="do not open the browser")
    a = ap.parse_args(argv)
    server, app = make_server(a.root, a.repo, a.port, a.cache_changes, a.confirm_seconds)
    url = f"http://127.0.0.1:{app.port}/"
    print(f"dsdiff ui: {url}\n  root: {app.root.real}\n  repo: {app.repo or '— (не открыт)'}\n"
          f"  Ctrl+C — остановить", flush=True)
    if not a.no_browser:
        threading.Timer(0.3, webbrowser.open, (url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
