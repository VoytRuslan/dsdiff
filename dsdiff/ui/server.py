"""HTTP server: the page from dsdiff/ui/static/ and the JSON API (dsdiff/ui/API.md), on 127.0.0.1 only."""
import json
import os
import re
import secrets
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlsplit

from ..normalize import normalize_name
from ..store import Store
from .jobs import Job, Jobs, RepoWriter, commit_job, create_repo_job, estimate, read_log
from .patchcheck import verify_job
from .records import FileIndex, history, in_commit, shown
from .reports import CACHE_CHANGES, Reports, compare_files_job, compare_revs_job
from .util import ApiError, Root

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
VERSION = "1.1-ui"
CONFIRM_SECONDS = 10.0  # a flat compare estimated longer than this asks first (--confirm-seconds)
MAX_BODY = 1 << 20
LINK_TTL = 60.0  # seconds a download link lives; it works once
LINK_MEMORY = 600.0  # used or expired links are remembered this long to answer 410, not 403
EXPORTS = ("report.json", "patch.jsonl", "changes.csv", "summary.md")
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}


class App:
    """Server state: --root, the open repository, jobs, write queues and cached reports."""

    def __init__(self, root, repo=None, cache_changes=CACHE_CHANGES, confirm_seconds=CONFIRM_SECONDS):
        self.root = Root(root)
        self.repo = os.path.realpath(repo) if repo else None
        if self.repo is None and os.path.isdir(os.path.join(self.root.real, ".dsdiff")):
            self.repo = self.root.real
        self.token = secrets.token_urlsafe(24)
        self.jobs, self.reports = Jobs(), Reports(cache_changes)
        self.writers, self.writers_lock = {}, threading.Lock()
        self.links, self.links_lock = {}, threading.Lock()  # download token -> {report, format, expires, used}
        self.file_index = FileIndex()
        self.confirm_seconds = confirm_seconds
        self.port = None

    def writer(self, repo):
        with self.writers_lock:
            if repo not in self.writers:
                self.writers[repo] = RepoWriter(repo)
            return self.writers[repo]

    def store(self):
        if self.repo is None:
            raise ApiError(409, "no_repo", "Репозиторий не открыт: создайте его или откройте папку с .dsdiff.")
        return Store(self.repo)

    def resolve(self, rev):
        try:
            return self.store().resolve(str(rev))
        except KeyError:
            raise ApiError(404, "unknown_revision", f"Версия «{rev}» не найдена в репозитории.", {"rev": rev})

    # ---- handlers --------------------------------------------------------

    def state(self):
        head = Store(self.repo).head() if self.repo else None
        return {"root": self.root.real, "repo": self.repo, "has_commits": head is not None, "head": head,
                "version": VERSION, "confirm_seconds": self.confirm_seconds,
                "cache": {"changes_held": self.reports.held(), "limit": self.reports.limit,
                          "reports": len(self.reports.by_key)},
                "jobs_running": sum(j.state in ("queued", "running") for j in self.jobs.jobs.values())}

    def open_repo(self, body):
        path = self.root.resolve(body.get("dir", ""), kind="dir")
        if body.get("create"):
            job = Job("create_repo")
            self.jobs.add(job)
            self.writer(path).submit(job, create_repo_job(path, self.set_repo))
            return 202, {"job": job.id}
        if not os.path.isdir(os.path.join(path, ".dsdiff")):
            raise ApiError(404, "no_repo", f"В папке «{self.root.rel(path) or '.'}» нет репозитория (.dsdiff).")
        self.set_repo(path)
        return 200, self.state()

    def set_repo(self, path):
        self.repo = path

    def files(self, q):
        path = self.root.resolve(q.get("dir", ""), kind="dir")
        entries = []
        for name in sorted(os.listdir(path), key=str.lower):
            full = os.path.join(path, name)
            if name.startswith(".") or not self.root.inside(full):
                continue  # hidden, or a symlink that leads outside --root
            if os.path.isdir(full):
                entries.append({"name": name, "type": "dir"})
            elif name.endswith(".jsonl") and os.path.isfile(full):
                st = os.stat(full)
                entries.append({"name": name, "type": "file", "size": st.st_size, "mtime": st.st_mtime})
        rel = self.root.rel(path)
        return {"dir": rel, "parent": None if not rel else os.path.dirname(rel), "entries": entries}

    def commits(self, q):
        st = self.store()
        log = read_log(self.repo)
        items = []
        if st.head():
            for name, c in st.log():
                kinds = {}
                for e in c["validation"]:
                    kinds[e["type"]] = kinds.get(e["type"], 0) + 1
                entry = log.get(name)
                items.append({"hash": name, "short": name[:12], "message": c["message"], "parent": c["parent"],
                              "records": c["counts"]["records"],
                              "groups": {g: c["counts"].get(g, 0) for g in ("train", "val", "test")},
                              "leaks": c["leaks_count"], "validation": kinds, "buckets": c["buckets"],
                              "log": {k: entry[k] for k in ("time", "file", "size", "lines")} if entry else None})
        offset, limit = int(q.get("offset", 0)), int(q.get("limit", 50))
        return {"total": len(items), "items": items[offset:offset + limit]}

    def commit(self, body):
        self.store()
        path = self.root.resolve(body.get("file"), kind="file")
        message = (body.get("message") or "").strip() or os.path.basename(path)
        job = Job("commit")
        self.jobs.add(job)
        self.writer(self.repo).submit(job, commit_job(self.repo, path, self.root.rel(path), message))
        return 202, {"job": job.id}

    def estimate(self, body):
        paths = [self.root.resolve(body.get(k), kind="file") for k in ("file_a", "file_b")]
        lines, seconds = estimate(paths)
        return {"lines_a": lines[0], "lines_b": lines[1], "seconds": round(seconds, 1),
                "warn": seconds > self.confirm_seconds, "threshold_seconds": self.confirm_seconds}

    def compare(self, body):
        if "file_a" in body or "file_b" in body:
            paths = [self.root.resolve(body.get(k), kind="file") for k in ("file_a", "file_b")]
            shown = [self.root.rel(p) for p in paths]
            key = ("file",) + tuple((p, os.path.getsize(p), os.path.getmtime(p)) for p in paths)
            source = {"kind": "file", "a": shown[0], "b": shown[1], "a_path": paths[0], "b_path": paths[1]}
            cached = self.reports.by_key.get(key)
            if cached is None and not body.get("confirm"):
                est = self.estimate(body)
                if est["warn"]:
                    raise ApiError(409, "needs_confirmation",
                                   f"Плоское сравнение займёт ~{est['seconds']:.0f} с; быстрее — добавить обе версии "
                                   f"в репозиторий.", est)
            make = lambda r: compare_files_job(r, paths, shown)
        else:
            a, b = self.resolve(body.get("a", "HEAD~1")), self.resolve(body.get("b", "HEAD"))
            st = self.store()
            key = ("rev", self.repo, a, b)
            source = {"kind": "rev", "a": a, "b": b, "repo": self.repo,
                      "a_label": st.commit_obj(a)["message"], "b_label": st.commit_obj(b)["message"]}
            make = lambda r: compare_revs_job(r, self.repo, a, b)
        report, created = self.reports.lookup_or_add(key, source)
        if created:
            report.job = self.jobs.start(Job("compare"), make(report))
        if report.report is not None:
            return 200, {"report": report.id, "job": None}
        return 202, {"report": report.id, "job": report.job.id}

    def export_link(self, rid, body):
        """A one-time download link: the page navigates to it, so the session token never goes in a URL."""
        fmt = body.get("format")
        if fmt not in EXPORTS:
            raise ApiError(400, "bad_request", f"Нет выгрузки «{fmt}»; есть {', '.join(EXPORTS)}.")
        r = self.reports.get(rid)
        r.ready()
        token = secrets.token_urlsafe(24)
        with self.links_lock:
            self.links[token] = {"report": rid, "format": fmt, "expires": time.monotonic() + LINK_TTL, "used": False}
        return 200, {"url": f"/dl/{token}", "expires_in": LINK_TTL, "filename": fmt}

    def take_link(self, token):
        """(report, format) for a link, once; 403 for a link that never existed, 410 for a used or expired one."""
        now = time.monotonic()
        with self.links_lock:
            for t in [t for t, x in self.links.items() if now - x["expires"] > LINK_MEMORY]:
                del self.links[t]
            link = self.links.get(token)
            if link is None:
                raise ApiError(403, "forbidden", "Ссылка на выгрузку недействительна.")
            if link["used"]:
                raise ApiError(410, "gone", "Ссылка на выгрузку уже использована: нажмите «Выгрузить» ещё раз.")
            if now > link["expires"]:
                raise ApiError(410, "gone", "Ссылка на выгрузку устарела (живёт 60 с): нажмите «Выгрузить» ещё раз.")
            link["used"] = True
        return self.reports.get(link["report"]), link["format"]

    # ---- record and patch -------------------------------------------------

    def record(self, q):
        rid = (q.get("id") or "").strip()
        if not rid:
            raise ApiError(400, "bad_request", "Укажите sample_id.")
        if "file" in q:
            path = self.root.resolve(q["file"], kind="file")
            recs = self.file_index.lookup(path, rid)
            source, extra = {"file": self.root.rel(path)}, {}
        else:
            name = self.resolve(q.get("rev", "HEAD"))
            st = self.store()
            recs, bucket, objects = in_commit(st, name, rid)
            source = {"rev": name, "message": st.commit_obj(name)["message"]}
            extra = {"bucket": bucket, "objects_read": objects}
        return {"id": normalize_name(rid), "found": bool(recs), "records": [shown(r) for r in recs],
                "source": source, **extra}

    def history(self, q):
        rid = (q.get("id") or "").strip()
        if not rid:
            raise ApiError(400, "bad_request", "Укажите sample_id.")
        self.store()
        return history(self.repo, rid, int(q.get("limit", 100)))

    def trace(self, body):
        rid = (body.get("id") or "").strip()
        if not rid:
            raise ApiError(400, "bad_request", "Укажите sample_id.")
        self.store()
        repo, limit = self.repo, int(body.get("limit", 100))
        job = self.jobs.start(Job("trace"), lambda j: history(repo, rid, limit, follow=True, job=j))
        return 202, {"job": job.id}

    def verify_patch(self, body):
        for k in ("base", "target"):
            if not isinstance(body.get(k), dict) or not ({"rev", "file"} & set(body[k])):
                raise ApiError(400, "bad_request", f"Укажите {'базу' if k == 'base' else 'цель'}: версию или файл.")
        self.root.resolve(body.get("patch"), kind="file")  # refuse a bad path now, not in the job
        job = self.jobs.start(Job("patch"), verify_job(self.repo, self.root, body["base"], body["patch"],
                                                       body["target"]))
        return 202, {"job": job.id}

    def report(self, rid, part, q):
        r = self.reports.get(rid)
        if r.report is None:
            if r.job and r.job.state in ("failed", "cancelled"):
                err = r.job.error or {"error": "cancelled", "message": "Сравнение отменено.", "detail": {}}
                raise ApiError(422, err["error"], err["message"], err.get("detail"))
            return 202, {"pending": True, "job": r.job.id if r.job else None}
        if part == "summary":
            return 200, r.summary()
        if part == "changes":
            types = [t for t in q.get("types", "").split(",") if t]
            return 200, r.changes(q.get("offset", 0), q.get("limit", 200), types, q.get("group") or None,
                                  q.get("q") or None, q.get("sort", "report"), q.get("hide_cosmetic") == "1")
        if part == "leaks":
            return 200, r.leaks(q.get("kind", "within"), q.get("offset", 0), q.get("limit", 200))
        if part == "validation":
            return 200, r.validation(q.get("side", "new"), q.get("offset", 0), q.get("limit", 200))
        raise ApiError(404, "not_found", "Нет такого раздела отчёта.")


ROUTES = [  # (method, pattern, handler(app, match, query, body) -> (status, json))
    ("GET", r"/api/state", lambda app, m, q, b: (200, app.state())),
    ("GET", r"/api/record", lambda app, m, q, b: (200, app.record(q))),
    ("GET", r"/api/record/history", lambda app, m, q, b: (200, app.history(q))),
    ("POST", r"/api/record/trace", lambda app, m, q, b: app.trace(b)),
    ("POST", r"/api/patch/verify", lambda app, m, q, b: app.verify_patch(b)),
    ("POST", r"/api/repo", lambda app, m, q, b: app.open_repo(b)),
    ("GET", r"/api/files", lambda app, m, q, b: (200, app.files(q))),
    ("GET", r"/api/commits", lambda app, m, q, b: (200, app.commits(q))),
    ("POST", r"/api/commit", lambda app, m, q, b: app.commit(b)),
    ("GET", r"/api/jobs/(\w+)", lambda app, m, q, b: (200, app.jobs.get(m[1]).to_json())),
    ("DELETE", r"/api/jobs/(\w+)", lambda app, m, q, b: cancel(app, m[1])),
    ("POST", r"/api/compare/estimate", lambda app, m, q, b: (200, app.estimate(b))),
    ("POST", r"/api/compare", lambda app, m, q, b: app.compare(b)),
    ("POST", r"/api/report/(\w+)/export-link", lambda app, m, q, b: app.export_link(m[1], b)),
    ("GET", r"/api/report/(\w+)/(summary|changes|leaks|validation)", lambda app, m, q, b: app.report(m[1], m[2], q)),
]


def cancel(app, job_id):
    job = app.jobs.get(job_id)
    job.cancel.set()
    return 200, job.to_json()


class Handler(BaseHTTPRequestHandler):
    server_version = "dsdiff-ui"
    app: App = None

    def log_message(self, fmt, *args):  # quiet: errors go through log_error
        pass

    def do_GET(self):
        self.handle_request("GET")

    def do_POST(self):
        self.handle_request("POST")

    def do_DELETE(self):
        self.handle_request("DELETE")

    def handle_request(self, method):
        try:
            url = urlsplit(self.path)
            if self.headers.get("Host") not in (f"127.0.0.1:{self.app.port}", f"localhost:{self.app.port}"):
                raise ApiError(403, "forbidden", "Неверный заголовок Host.")
            m = re.fullmatch(r"/dl/([\w-]+)", url.path)
            if m and method == "GET":  # the link is the permission: no session token here
                report, fmt = self.app.take_link(m[1])
                return self.send_stream(*report.export(fmt), attachment=fmt)
            if not url.path.startswith("/api/"):
                return self.static(url.path) if method == "GET" else self.send_json(405, {"error": "method"})
            if not secrets.compare_digest(self.headers.get("X-Dsdiff-Token", ""), self.app.token):
                raise ApiError(403, "forbidden", "Нет токена сессии: откройте страницу заново по адресу из консоли.")
            q = {k: v[-1] for k, v in parse_qs(url.query).items()}
            m = re.fullmatch(r"/api/report/(\w+)/export/([\w.]+)", url.path)
            if m and method == "GET":
                return self.send_stream(*self.app.reports.get(m[1]).export(m[2]), attachment=m[2])
            body = self.read_body() if method == "POST" else {}
            for meth, pattern, fn in ROUTES:
                m = re.fullmatch(pattern, url.path)
                if m and meth == method:
                    status, out = fn(self.app, m, q, body)
                    return self.send_json(status, out)
            raise ApiError(404, "not_found", f"Нет такого запроса: {method} {url.path}.")
        except ApiError as e:
            self.send_json(e.status, e.to_json())
        except Exception as e:
            traceback.print_exc()
            self.send_json(500, {"error": "internal", "message": f"Внутренняя ошибка: {type(e).__name__}: {e}",
                                 "detail": {}})

    def read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ApiError(400, "bad_request", "Слишком большой запрос.")
        raw = self.rfile.read(n) if n else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            raise ApiError(400, "bad_request", "Тело запроса — не JSON.")
        if not isinstance(body, dict):
            raise ApiError(400, "bad_request", "Тело запроса — не JSON-объект.")
        return body

    def static(self, path):
        name = "index.html" if path in ("/", "/index.html") else path.removeprefix("/static/")
        full = os.path.join(STATIC, name)
        if "/" in name or name.startswith(".") or not os.path.isfile(full):
            return self.send_json(404, {"error": "not_found", "message": "Нет такого файла."})
        with open(full, "rb") as f:
            data = f.read()
        if name == "index.html":
            data = data.replace(b"__DSDIFF_TOKEN__", self.app.token.encode())
        self.send_body(200, data, CONTENT_TYPES.get(os.path.splitext(name)[1], "application/octet-stream"))

    def send_json(self, status, obj):
        self.send_body(status, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def send_stream(self, pieces, ctype, attachment):
        """Send text pieces as they are produced; HTTP/1.0 without Content-Length: the end is the close."""
        self.send_response(200)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(attachment)}")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.end_headers()
        buf, size = [], 0
        for piece in pieces:
            buf.append(piece)
            size += len(piece)
            if size >= 1 << 16:
                self.wfile.write("".join(buf).encode())
                buf, size = [], 0
        self.wfile.write("".join(buf).encode())
        self.close_connection = True

    def send_body(self, status, data, ctype):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)


def make_server(root=".", repo=None, port=0, cache_changes=CACHE_CHANGES, confirm_seconds=CONFIRM_SECONDS):
    """(server, app); the caller runs server.serve_forever()."""
    app = App(root, repo, cache_changes, confirm_seconds)
    handler = type("BoundHandler", (Handler,), {"app": app})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    app.port = server.server_address[1]
    return server, app
