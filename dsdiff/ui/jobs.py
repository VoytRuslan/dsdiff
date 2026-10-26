"""Background jobs: compares run in parallel; writes to a repository (commit, create) go one at a time
through a per-repository queue and a lock file in .dsdiff/, and are logged to .dsdiff/ui-log.jsonl."""
import datetime
import fcntl
import json
import os
import secrets
import threading
import time
import traceback

from ..store import Store
from .util import ApiError, Cancelled, count_lines, read_records

LOCK_RETRY = 1.0  # seconds between attempts to take the lock file


class Job:
    def __init__(self, kind):
        self.id = secrets.token_hex(6)
        self.kind = kind
        self.state, self.stage = "queued", "queued"
        self.counters = {}
        self.result = self.error = None
        self.queue_position = None
        self.cancel = threading.Event()
        self.created = time.time()
        self.started = self.finished = None

    def check(self):
        if self.cancel.is_set():
            raise Cancelled()

    def progress(self, **counters):
        self.counters.update(counters)

    def to_json(self):
        end = self.finished or time.time()
        return {"id": self.id, "kind": self.kind, "state": self.state, "stage": self.stage,
                "queue_position": self.queue_position, **self.counters,
                "elapsed": round(end - self.started, 2) if self.started else 0.0,
                "result": self.result, "error": self.error}


def run(job, fn):
    """Run fn(job) and record the outcome on the job; never raises."""
    job.state, job.started = "running", job.started or time.time()
    try:
        job.check()
        job.result = fn(job)
        job.state, job.stage = "done", "done"
    except Cancelled:
        job.state = "cancelled"
    except ApiError as e:
        job.state, job.error = "failed", e.to_json()
    except Exception as e:  # the page gets a message, the console the traceback
        traceback.print_exc()
        job.state = "failed"
        job.error = {"error": "internal", "message": f"Внутренняя ошибка: {type(e).__name__}: {e}", "detail": {}}
    job.finished = time.time()


class Jobs:
    def __init__(self):
        self.jobs, self.lock = {}, threading.Lock()

    def add(self, job):
        with self.lock:
            self.jobs[job.id] = job
        return job

    def get(self, job_id):
        job = self.jobs.get(job_id)
        if job is None:
            raise ApiError(404, "not_found", "Задача не найдена (сервер перезапускали?).", {"job": job_id})
        return job

    def start(self, job, fn):
        self.add(job)
        threading.Thread(target=run, args=(job, fn), daemon=True, name=f"{job.kind}-{job.id}").start()
        return job


class UiStore(Store):
    """Store whose writes can be cancelled between objects and report how many were written."""

    def __init__(self, repo, job):
        super().__init__(repo)
        self.job = job

    def put(self, kind, body):
        self.job.check()
        name = super().put(kind, body)
        self.job.progress(objects_written=self.stats["objects_written"])
        return name


class RepoWriter:
    """One worker thread per repository: queued writes run in order, each under the lock file."""

    def __init__(self, repo):
        self.repo = repo
        self.pending, self.cv = [], threading.Condition()
        threading.Thread(target=self.loop, daemon=True, name=f"writer-{os.path.basename(repo)}").start()

    def submit(self, job, fn):
        with self.cv:
            self.pending.append((job, fn))
            self.renumber()
            self.cv.notify()
        return job

    def renumber(self):
        for i, (job, _) in enumerate(self.pending):
            job.queue_position = i

    def loop(self):
        while True:
            with self.cv:
                while not self.pending:
                    self.cv.wait()
                job, fn = self.pending[0]
            if not job.cancel.is_set():
                run(job, lambda j: self.locked(j, fn))
            else:
                job.state, job.finished = "cancelled", time.time()
            with self.cv:
                self.pending.pop(0)
                job.queue_position = None
                self.renumber()

    def locked(self, job, fn):
        root = os.path.join(self.repo, ".dsdiff")
        os.makedirs(root, exist_ok=True)
        with open(os.path.join(root, "ui.lock"), "a") as f:
            job.stage = "wait_lock"
            while True:
                try:
                    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:  # another dsdiff ui writes to this repository
                    job.check()
                    time.sleep(LOCK_RETRY)
            try:
                return fn(job)
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)


def log_path(repo):
    return os.path.join(repo, ".dsdiff", "ui-log.jsonl")


def read_log(repo):
    """{commit: entry} from the ui journal; a missing or damaged journal is just empty."""
    out = {}
    try:
        with open(log_path(repo), encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                    out[e["commit"]] = e
                except (json.JSONDecodeError, KeyError, TypeError):
                    continue
    except FileNotFoundError:
        pass
    return out


def commit_job(repo, path, shown, message):
    """fn for RepoWriter: read the file, commit it like `dsdiff commit`, log it."""
    def fn(job):
        job.stage = "read"
        size = os.path.getsize(path)

        def progress(n, total):
            job.check()
            job.progress(lines_read=n, lines_total=total)
        records = read_records(path, shown, progress)
        job.stage = "commit"
        job.progress(objects_written=0)
        st = UiStore(repo, job)
        name, c = st.commit(records, message)
        entry = {"commit": name, "time": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                 "file": shown, "size": size, "lines": len(records), "message": message}
        with open(log_path(repo), "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return {"commit": name, "short": name[:12], "records": c["counts"]["records"],
                "objects_written": st.stats["objects_written"], "buckets": c["buckets"]}
    return fn


def create_repo_job(repo, on_done):
    def fn(job):
        job.stage = "create"
        os.makedirs(os.path.join(repo, ".dsdiff", "objects"), exist_ok=True)
        on_done(repo)
        return {"repo": repo}
    return fn


def estimate(paths):
    """Line counts of two files and the flat-compare time estimate."""
    lines = [count_lines(p) for p in paths]
    return lines, FLAT_SECONDS_PER_RECORD * sum(lines)


# flat diff at n = 1e6 on both sides: 39 s (bench/results.csv, minimum of 3 runs) -> per record of A and B
FLAT_SECONDS_PER_RECORD = 39.0 / 2e6
