"""dsdiff ui: the JSON API against the CLI, on a server in a background thread, through urllib."""
import os

if os.name == "nt":
    import msvcrt
else:
    import fcntl
    
import http.client
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

from dsdiff.__main__ import main as cli
from dsdiff.gen import main as gen
from dsdiff.store import Store
from dsdiff.ui import server as ui_server
from dsdiff.ui.server import make_server

PRESETS = ["tiny", "story", "clean", "test_relabel", "test_to_train"]


class Client:
    def __init__(self, app):
        self.app, self.base = app, f"http://127.0.0.1:{app.port}"

    def call(self, method, path, body=None, token=True, raw=False):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if token:
            req.add_header("X-Dsdiff-Token", self.app.token)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                status, payload = r.status, r.read()
        except urllib.error.HTTPError as e:
            status, payload = e.code, e.read()
        return status, (payload if raw else json.loads(payload))

    def get(self, path, **kw):
        return self.call("GET", path, **kw)

    def post(self, path, body, **kw):
        return self.call("POST", path, body, **kw)

    def wait(self, job_id, timeout=300):
        t = time.time()
        while time.time() - t < timeout:
            status, job = self.get(f"/api/jobs/{job_id}")
            assert status == 200
            if job["state"] not in ("queued", "running"):
                return job
            time.sleep(0.05)
        raise TimeoutError(job_id)

    def compare(self, body):
        status, out = self.post("/api/compare", body)
        assert status in (200, 202), out
        if out["job"]:
            job = self.wait(out["job"])
            assert job["state"] == "done", job
        return out["report"]

    def commit(self, file, message):
        status, out = self.post("/api/commit", {"file": file, "message": message})
        assert status == 202, out
        return out["job"]


@pytest.fixture(scope="module")
def data(tmp_path_factory):
    root = tmp_path_factory.mktemp("root")
    for p in PRESETS:
        gen(["--preset", p, "--out", str(root / "data" / p)])
    return root


@pytest.fixture
def ui(data):
    """A fresh server on the shared data (no repository open)."""
    server, app = make_server(str(data))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield Client(app)
    server.shutdown()
    server.server_close()


def cli_report(tmp_path, *args):
    out = tmp_path / "cli_report.json"
    assert cli(["diff", *args, "--report", str(out)]) == 0
    return json.loads(out.read_text(encoding="utf-8"))


def all_changes(ui, rid, query=""):
    rows, offset = [], 0
    while True:
        status, page = ui.get(f"/api/report/{rid}/changes?offset={offset}&limit=200{query}")
        assert status == 200 and len(page["items"]) <= 200
        rows += page["items"]
        offset += 200
        if offset >= page["total"]:
            return rows, page["total"]


@pytest.mark.parametrize("preset", PRESETS)
def test_compare_files_matches_cli(ui, data, tmp_path, preset):
    a, b = f"data/{preset}/v1.jsonl", f"data/{preset}/v2.jsonl"
    rid = ui.compare({"file_a": a, "file_b": b})
    want = cli_report(tmp_path, str(data / a), str(data / b))
    status, got = ui.get(f"/api/report/{rid}/export/report.json", raw=True)
    assert status == 200
    got = json.loads(got)
    assert got == want
    status, s = ui.get(f"/api/report/{rid}/summary")
    assert s["verdict"]["code"] == want["verdict"]["code"]
    assert s["leaks"] == {k: len(v) for k, v in want["leaks"].items()}
    rows, total = all_changes(ui, rid)
    keep = set(want["changes"][0]) if want["changes"] else set()
    assert total == len(want["changes"]) and [{k: r[k] for k in keep} for r in rows] == want["changes"]


def test_pages_and_filters(ui, data):
    rid = ui.compare({"file_a": "data/story/v1.jsonl", "file_b": "data/story/v2.jsonl"})
    _, s = ui.get(f"/api/report/{rid}/summary")
    for t, n in s["changes_by_type"].items():
        rows, total = all_changes(ui, rid, f"&types={t}")
        assert total == n == len(rows) and all(t in r["types"] for r in rows)
    rows, total = all_changes(ui, rid, "&group=test")
    assert total > 0 and all("test" in (r["group_old"], r["group_new"]) for r in rows)
    everything, n_all = all_changes(ui, rid)
    assert total == sum("test" in (r["group_old"], r["group_new"]) for r in everything)
    q = everything[0]["new_id"][-7:].upper()  # case-insensitive substring
    rows, _ = all_changes(ui, rid, f"&q={q}")
    assert rows and all(q.lower() in " ".join(filter(None, (r["old_id"], r["new_id"]))).lower() for r in rows)
    rows, _ = all_changes(ui, rid, "&sort=-new_id")
    named = [r["new_id"] for r in rows if r["new_id"] is not None]
    assert named == sorted(named, reverse=True) and len(rows) == n_all
    status, page = ui.get(f"/api/report/{rid}/changes?limit=5000")
    assert status == 200 and len(page["items"]) == 200  # never more than one page of 200
    status, err = ui.get(f"/api/report/{rid}/changes?types=nonsense")
    assert status == 400 and "nonsense" in err["message"]
    status, leaks = ui.get(f"/api/report/{rid}/leaks?kind=by_move")
    assert status == 200 and leaks["total"] == s["leaks"]["by_move"]


def make_repo(ui, data, name):
    os.makedirs(data / name, exist_ok=True)
    status, out = ui.post("/api/repo", {"dir": name, "create": True})
    assert status == 202
    assert ui.wait(out["job"])["state"] == "done"
    return data / name


def test_commit_same_root_as_cli(ui, data, tmp_path):
    repo = make_repo(ui, data, "repo_same_root")
    names = []
    for v in (1, 2):
        job = ui.wait(ui.commit(f"data/tiny/v{v}.jsonl", f"v{v}"))
        assert job["state"] == "done", job
        names.append(job["result"]["commit"])
    for v in (1, 2):
        assert cli(["commit", str(data / "data" / "tiny" / f"v{v}.jsonl"), "-m", f"v{v}", "--repo",
                    str(tmp_path)]) == 0
    ui_store, cli_store = Store(str(repo)), Store(str(tmp_path))
    for name, (cli_name, c) in zip(reversed(names), cli_store.log()):
        u = ui_store.commit_obj(name)
        assert (u["id_root"], u["content_root"]) == (c["id_root"], c["content_root"])
        assert name == cli_name  # same content, message and parent: the same commit object
    _, commits = ui.get("/api/commits")
    assert [c["message"] for c in commits["items"]] == ["v2", "v1"]
    assert commits["items"][0]["log"]["file"] == "data/tiny/v2.jsonl"
    assert commits["items"][0]["log"]["lines"] == commits["items"][0]["records"]


def test_two_commits_at_once_queue(ui, data):
    repo = make_repo(ui, data, "repo_queue")
    jobs = [None, None]

    def post(i):
        jobs[i] = ui.commit(f"data/story/v{i + 1}.jsonl", f"v{i + 1}")
    threads = [threading.Thread(target=post, args=(i,)) for i in (0, 1)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    done = [ui.wait(j) for j in jobs]
    assert [j["state"] for j in done] == ["done", "done"]
    log = list(Store(str(repo)).log())
    assert len(log) == 2 and log[0][1]["parent"] == log[1][0] and log[1][1]["parent"] is None
    assert {j["result"]["commit"] for j in done} == {name for name, _ in log}
    with open(repo / ".dsdiff" / "ui-log.jsonl", encoding="utf-8") as f:
        assert len(f.readlines()) == 2


def test_queued_behind_lock_and_cancel(ui, data):
    """A write waits while another process holds .dsdiff/ui.lock; cancelling it leaves HEAD alone."""
    repo = make_repo(ui, data, "repo_lock")
    with open(repo / ".dsdiff" / "ui.lock", "a+b") as f:
        if os.name == "nt":
            f.seek(0, os.SEEK_END)
            if f.tell() == 0:
                f.write(b"\0")
                f.flush()
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(f, fcntl.LOCK_EX)
        job = ui.commit("data/tiny/v1.jsonl", "v1")
        time.sleep(0.3)
        _, state = ui.get(f"/api/jobs/{job}")
        assert state["state"] == "running" and state["stage"] == "wait_lock"
        status, _ = ui.call("DELETE", f"/api/jobs/{job}")
        assert status == 200
        assert ui.wait(job)["state"] == "cancelled"
    assert Store(str(repo)).head() is None


def test_compare_revs_matches_cli(ui, data, tmp_path):
    repo = make_repo(ui, data, "repo_revs")
    for v in (1, 2):
        assert ui.wait(ui.commit(f"data/story/v{v}.jsonl", f"v{v}"))["state"] == "done"
    rid = ui.compare({"a": "HEAD~1", "b": "HEAD"})
    _, got = ui.get(f"/api/report/{rid}/export/report.json", raw=True)
    assert json.loads(got) == cli_report(tmp_path, "--rev", "HEAD~1", "HEAD", "--repo", str(repo))
    _, s = ui.get(f"/api/report/{rid}/summary")
    assert s["stats"]["mode"].startswith("tree") and s["source"]["a_label"] == "v1"
    assert "check --rev" in s["cli"]["check"]
    assert ui.post("/api/compare", {"a": "HEAD~1", "b": "HEAD"})[1] == {"report": rid, "job": None}  # cached


def test_export_patch_verifies(ui, data, tmp_path):
    rid = ui.compare({"file_a": "data/story/v1.jsonl", "file_b": "data/story/v2.jsonl"})
    for fmt in ("patch.jsonl", "changes.csv", "summary.md"):
        status, body = ui.get(f"/api/report/{rid}/export/{fmt}", raw=True)
        assert status == 200 and body
        (tmp_path / fmt).write_bytes(body)
    assert cli(["verify", str(data / "data/story/v1.jsonl"), str(data / "data/story/v2.jsonl"),
                str(tmp_path / "patch.jsonl")]) == 0
    md = (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert "Вердикт: не доверять" in md and "python -m dsdiff check" in md
    assert (tmp_path / "changes.csv").read_text(encoding="utf-8").count("\n") == 315  # header + 314


def test_paths_outside_root(ui, data, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "v.jsonl").write_text(
        (data / "data/tiny/v1.jsonl").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    link = data / "link_out"
    if not link.exists():
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
                pytest.skip(
                    "Creating symlinks requires Windows Developer Mode "
                    "or elevated privileges"
                )
            raise

    for path in (
        "/api/files?dir=..",
        f"/api/files?dir={outside}",
        "/api/files?dir=link_out",
        "/api/files?dir=data/../..",
    ):
        status, err = ui.get(path)
        assert status == 403 and err["error"] == "path_outside_root", path

    for body in (
        {"file_a": "link_out/v.jsonl", "file_b": "data/tiny/v2.jsonl"},
        {"file_a": str(outside / "v.jsonl"), "file_b": "data/tiny/v2.jsonl"},
        {"file_a": "../outside/v.jsonl", "file_b": "data/tiny/v2.jsonl"},
    ):
        assert ui.post("/api/compare", body)[0] == 403

    make_repo(ui, data, "repo_paths")
    assert ui.post(
        "/api/commit",
        {"file": "link_out/v.jsonl", "message": "x"},
    )[0] == 403

    _, listing = ui.get("/api/files?dir=")
    assert "link_out" not in [e["name"] for e in listing["entries"]]
    assert ui.post("/api/repo", {"dir": "..", "create": True})[0] == 403

def test_token_and_host(ui):
    assert ui.get("/api/state", token=False)[0] == 403
    conn = http.client.HTTPConnection("127.0.0.1", ui.app.port, timeout=10)
    conn.request("GET", "/api/state", headers={"Host": "evil.example:80", "X-Dsdiff-Token": ui.app.token})
    assert conn.getresponse().status == 403
    status, page = ui.get("/", token=False, raw=True)
    assert status == 200 and ui.app.token.encode() in page


def test_errors_are_messages(ui, data):
    bad = data / "data" / "broken.jsonl"
    lines = (data / "data/tiny/v1.jsonl").read_text(encoding="utf-8").splitlines()
    lines[4] = lines[4][:-1]  # cut the closing brace on line 5
    bad.write_text("\n".join(lines) + "\n", encoding="utf-8")
    rid = ui.post("/api/compare", {"file_a": "data/broken.jsonl", "file_b": "data/tiny/v2.jsonl"})[1]["report"]
    deadline = time.time() + 30
    while (s := ui.get(f"/api/report/{rid}/summary"))[0] == 202 and time.time() < deadline:
        time.sleep(0.05)
    status, err = s
    assert status == 422 and err["error"] == "bad_jsonl" and "строка 5" in err["message"]
    assert "Traceback" not in json.dumps(err)
    assert ui.post("/api/compare", {"a": "HEAD~1", "b": "HEAD"})[1]["error"] == "no_repo"
    make_repo(ui, data, "repo_errors")
    job = ui.wait(ui.commit("data/broken.jsonl", "broken"))
    assert job["state"] == "failed" and job["error"]["detail"]["line"] == 5
    status, err = ui.post("/api/compare", {"a": "deadbeef", "b": "HEAD"})
    assert status == 404 and err["error"] == "unknown_revision" and "deadbeef" in err["message"]
    assert ui.get("/api/report/nope/summary")[0] == 404
    assert ui.get("/api/nothing")[0] == 404


def test_confirmation_by_estimated_time(data):
    """Asked only when the flat compare is estimated longer than --confirm-seconds (10 s by default)."""
    body = {"file_a": "data/clean/v1.jsonl", "file_b": "data/clean/v2.jsonl"}  # 2 x 10 000 lines: ~0.4 s
    for seconds, warn in ((ui_server.CONFIRM_SECONDS, False), (0.1, True)):
        server, app = make_server(str(data), confirm_seconds=seconds)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            ui = Client(app)
            status, est = ui.post("/api/compare/estimate", body)
            assert status == 200 and est["lines_a"] == 10_000 and est["warn"] is warn
            assert 0.1 < est["seconds"] < ui_server.CONFIRM_SECONDS and est["threshold_seconds"] == seconds
            status, out = ui.post("/api/compare", body)
            if warn:
                assert status == 409 and out["error"] == "needs_confirmation" and "репозиторий" in out["message"]
                assert ui.compare(dict(body, confirm=True))
            else:
                assert status == 202 and out["report"]
        finally:
            server.shutdown()
            server.server_close()


def test_medium_page_is_fast(tmp_path_factory):
    root = tmp_path_factory.mktemp("medium")
    gen(["--preset", "medium", "--out", str(root / "m")])
    server, app = make_server(str(root))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        ui = Client(app)
        rid = ui.compare({"file_a": "m/v1.jsonl", "file_b": "m/v2.jsonl", "confirm": True})
        t = time.perf_counter()
        status, s = ui.get(f"/api/report/{rid}/summary")
        status2, page = ui.get(f"/api/report/{rid}/changes?offset=0&limit=200")
        status3, _ = ui.get(f"/api/report/{rid}/changes?types=relabeled&group=test&q=img&sort=-old_id")
        assert time.perf_counter() - t < 2.0
        assert (status, status2, status3) == (200, 200, 200)
        assert s["changes_total"] > 10_000 and len(page["items"]) == 200
    finally:
        server.shutdown()
        server.server_close()


def test_cache_evicts_by_changes(data):
    """LRU by the changes held: story (314) + tiny (29) fit in 350; a third report pushes out the least
    recently used; a report alone over the limit stays while it is the last one opened."""
    server, app = make_server(str(data), cache_changes=350)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        ui = Client(app)
        files = lambda p, a=1, b=2: {"file_a": f"data/{p}/v{a}.jsonl", "file_b": f"data/{p}/v{b}.jsonl"}
        alive = lambda rid: ui.get(f"/api/report/{rid}/summary")[0] == 200
        story, tiny = ui.compare(files("story")), ui.compare(files("tiny"))
        assert alive(tiny) and alive(story)  # 343 changes: both kept; story is now the most recent
        tiny_back = ui.compare(files("tiny", 2, 1))  # 29 more -> 372 > 350: the oldest, tiny, goes
        assert not alive(tiny) and alive(story) and alive(tiny_back)
        assert ui.get("/api/state")[1]["cache"] == {"changes_held": 343, "limit": 350, "reports": 2}
        clean = ui.compare(files("clean"))  # 1805 alone: kept as the last opened, the rest dropped
        assert alive(clean) and not alive(story) and not alive(tiny_back)
        status, err = ui.get(f"/api/report/{story}/summary")
        assert status == 404 and "сравнение снова" in err["message"]
        tiny = ui.compare(files("tiny"))  # now clean is not the last one: it goes
        assert alive(tiny) and not alive(clean)
        assert ui.get("/api/state")[1]["cache"]["changes_held"] == 29
    finally:
        server.shutdown()
        server.server_close()


def test_download_link_once(ui, monkeypatch):
    rid = ui.compare({"file_a": "data/story/v1.jsonl", "file_b": "data/story/v2.jsonl"})
    _, direct = ui.get(f"/api/report/{rid}/export/patch.jsonl", raw=True)
    status, link = ui.post(f"/api/report/{rid}/export-link", {"format": "patch.jsonl"})
    assert status == 200 and link["url"].startswith("/dl/") and ui.app.token not in link["url"]
    req = urllib.request.Request(ui.base + link["url"])  # no session token: the link is enough
    with urllib.request.urlopen(req, timeout=60) as r:
        assert "attachment" in r.headers["Content-Disposition"] and "patch.jsonl" in r.headers["Content-Disposition"]
        assert r.read() == direct
    status, err = ui.get(link["url"], token=False)
    assert status == 410 and "уже использована" in err["message"]
    status, link = ui.post(f"/api/report/{rid}/export-link", {"format": "report.json"})
    clock = time.monotonic
    monkeypatch.setattr(ui_server.time, "monotonic", lambda: clock() + ui_server.LINK_TTL + 1)
    status, err = ui.get(link["url"], token=False)
    assert status == 410 and "устарела" in err["message"]
    monkeypatch.undo()
    assert ui.get("/dl/not-a-link", token=False)[0] == 403
    assert ui.post(f"/api/report/{rid}/export-link", {"format": "x.exe"})[0] == 400
    assert ui.post(f"/api/report/{rid}/export-link", {"format": "patch.jsonl"}, token=False)[0] == 403


def three_versions(ui, data, name):
    """tiny v1, tiny v2, and v3 = v2 with images/r1_000.jpg renamed again to images/r2_000.jpg and relabeled."""
    repo = make_repo(ui, data, name)
    v2 = [json.loads(x) for x in (data / "data/tiny/v2.jsonl").read_text(encoding="utf-8").splitlines()]
    v3 = [dict(r, sample_id="images/r2_000.jpg", labels=["zebra"]) if r["sample_id"] == "images/r1_000.jpg" else r
          for r in v2]
    (data / "data" / "tiny_v3.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in v3),
                                                 encoding="utf-8")
    for f, m in (("data/tiny/v1.jsonl", "v1"), ("data/tiny/v2.jsonl", "v2"), ("data/tiny_v3.jsonl", "v3")):
        assert ui.wait(ui.commit(f, m))["state"] == "done"
    return repo


def test_record_in_commit_and_file(ui, data):
    make_repo(ui, data, "repo_record")
    assert ui.wait(ui.commit("data/story/v2.jsonl", "v2"))["state"] == "done"
    lines = (data / "data/story/v2.jsonl").read_text(encoding="utf-8").splitlines()
    for line in lines[::997]:
        rec = json.loads(line)
        q = urllib.parse.quote(rec["sample_id"])
        _, a = ui.get(f"/api/record?id={q}&rev=HEAD")
        _, b = ui.get(f"/api/record?id={q}&file=data/story/v2.jsonl")
        assert a["found"] and a["records"][0]["raw"] == rec == b["records"][0]["raw"]
        assert a["objects_read"] <= 4  # commit + depth(B) tree nodes + one bucket, not the version
    _, none = ui.get("/api/record?id=images/nope.jpg&rev=HEAD")
    assert none["found"] is False and none["records"] == []
    _, up = ui.get("/api/record?id=" + urllib.parse.quote(json.loads(lines[5])["sample_id"].upper()) + "&rev=HEAD")
    assert up["found"]  # the id is normalized as in the core
    assert ui.get("/api/record?id=x&rev=deadbeef")[0] == 404
    assert ui.get("/api/record?id=x&file=../etc/passwd")[0] == 403


def test_history_and_trace(ui, data):
    three_versions(ui, data, "repo_trace")
    _, plain = ui.get("/api/record/history?id=images/r2_000.jpg")
    assert [x["present"] for x in plain["items"]] == [True, False, False]
    assert plain["items"][0]["change"] == ["added"]
    status, out = ui.post("/api/record/trace", {"id": "images/r2_000.jpg"})
    job = ui.wait(out["job"])
    assert status == 202 and job["state"] == "done", job
    t = job["result"]
    assert [(x["message"], x["id"], x.get("renamed_from")) for x in t["items"]] == [
        ("v3", "images/r2_000.jpg", "images/r1_000.jpg"),
        ("v2", "images/r1_000.jpg", "images/img_000.jpg"),
        ("v1", "images/img_000.jpg", None)]
    assert t["items"][0]["change"] == ["renamed", "relabeled"] and t["stopped"] == "first_commit"
    assert all(x["present"] for x in t["items"])
    _, kept = ui.get("/api/record/history?id=images/img_001.jpg")  # relabeled in v2, then unchanged
    assert [x["change"] for x in kept["items"]] == [[], ["relabeled"], ["added"]]


def test_patch_verify(ui, data, tmp_path):
    rid = ui.compare({"file_a": "data/tiny/v1.jsonl", "file_b": "data/tiny/v2.jsonl"})
    _, patch = ui.get(f"/api/report/{rid}/export/patch.jsonl", raw=True)
    (data / "data" / "tiny_patch.jsonl").write_bytes(patch)
    (data / "data" / "broken_patch.jsonl").write_bytes(patch[:-5] + b"\n")

    def verify(base, patch_file, target):
        status, out = ui.post("/api/patch/verify", {"base": base, "patch": patch_file, "target": target})
        assert status == 202, out
        return ui.wait(out["job"])

    ok = verify({"file": "data/tiny/v1.jsonl"}, "data/tiny_patch.jsonl", {"file": "data/tiny/v2.jsonl"})
    r = ok["result"]
    assert ok["state"] == "done" and r["applied"] and r["strict"] and r["semantic"] and r["ops"]["rename"] > 0
    wrong = verify({"file": "data/tiny/v2.jsonl"}, "data/tiny_patch.jsonl", {"file": "data/tiny/v2.jsonl"})
    assert wrong["result"]["applied"] is False and wrong["result"]["error"]["error"] == "patch_base_mismatch"
    assert "другой базовой версии" in wrong["result"]["error"]["message"]
    other = verify({"file": "data/tiny/v1.jsonl"}, "data/tiny_patch.jsonl", {"file": "data/tiny/v1.jsonl"})
    o = other["result"]
    assert o["applied"] and not o["strict"] and not o["semantic"] and o["mismatch"]["examples"]
    broken = verify({"file": "data/tiny/v1.jsonl"}, "data/broken_patch.jsonl", {"file": "data/tiny/v2.jsonl"})
    assert broken["state"] == "failed" and broken["error"]["error"] == "bad_jsonl"
    notpatch = verify({"file": "data/tiny/v1.jsonl"}, "data/tiny/v2.jsonl", {"file": "data/tiny/v2.jsonl"})
    assert notpatch["result"]["error"]["error"] == "patch_not_a_patch"
    make_repo(ui, data, "repo_patch")
    for v in (1, 2):
        assert ui.wait(ui.commit(f"data/tiny/v{v}.jsonl", f"v{v}"))["state"] == "done"
    revs = verify({"rev": "HEAD~1"}, "data/tiny_patch.jsonl", {"rev": "HEAD"})
    assert revs["result"]["strict"] and revs["result"]["semantic"]
    assert ui.post("/api/patch/verify", {"base": {"file": "data/tiny/v1.jsonl"}, "patch": "../x",
                                         "target": {"rev": "HEAD"}})[0] == 403


def test_importance_order_and_cosmetic(ui):
    """Default table order: the rows behind the verdict first, then by class; pure cosmetic hidden on request."""
    from dsdiff.ui.reports import TYPE_RANK
    rid = ui.compare({"file_a": "data/story/v1.jsonl", "file_b": "data/story/v2.jsonl"})
    _, s = ui.get(f"/api/report/{rid}/summary")
    rows, total = all_changes(ui, rid, "&sort=importance&hide_cosmetic=1")
    _, first = ui.get(f"/api/report/{rid}/changes?sort=importance&hide_cosmetic=1")
    assert all(r["types"] != ["cosmetic"] for r in rows)
    assert total + first["hidden_cosmetic"] == s["changes_total"] and first["hidden_cosmetic"] == 190
    flags = [r["affects_verdict"] for r in rows]
    assert flags == sorted(flags, reverse=True)  # all verdict rows first
    assert sum(flags) == s["verdict"]["reasons"][0]["count"] == 102  # 51 moves + 51 copies train -> test
    ranks = [min(TYPE_RANK[t] for t in r["types"]) for r in rows if not r["affects_verdict"]]
    assert ranks == sorted(ranks)
    rows, total = all_changes(ui, rid, "&types=cosmetic&hide_cosmetic=1")
    assert total == 190  # asking for cosmetic shows it even with hide_cosmetic
    rid = ui.compare({"file_a": "data/test_relabel/v1.jsonl", "file_b": "data/test_relabel/v2.jsonl"})
    rows, _ = all_changes(ui, rid, "&sort=importance&hide_cosmetic=1")
    top = [r for r in rows if r["affects_verdict"]]
    assert top and rows[:len(top)] == top and all("test" in (r["group_old"], r["group_new"]) for r in top)


def test_buckets_read_per_side(ui, data):
    make_repo(ui, data, "repo_sides")
    for v in (1, 2):
        assert ui.wait(ui.commit(f"data/story/v{v}.jsonl", f"v{v}"))["state"] == "done"
    rid = ui.compare({"a": "HEAD~1", "b": "HEAD"})
    _, s = ui.get(f"/api/report/{rid}/summary")
    sides = s["sides"]
    assert sides["buckets"] == 512 and 0 < sides["a"] <= 512 and 0 < sides["b"] <= 512
    assert sides["a"] + sides["b"] == s["stats"]["buckets_read"]  # the core's figure is A and B together
