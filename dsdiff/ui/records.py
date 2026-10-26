"""One record: found in a commit by descending the id tree to its bucket (no version is read whole),
or in a file through an id -> line offsets index; its history over the commits, optionally following
renames through tree diffs of neighbouring commits."""
import json
import os
import threading
from collections import OrderedDict

from ..diff import Rec, classify
from ..normalize import normalize_name, normalize_record
from ..store import FANOUT_BITS, Store, bucket_of, depth
from ..treediff import tree_diff
from .util import ApiError

FILE_INDEXES = 4  # files whose id index is kept in memory


def shown(rec):
    return {"raw": rec, "normalized": normalize_record(rec)}


def in_commit(st, name, rid):
    """(records with this canonical id, bucket, objects read) in a commit: depth(B) tree nodes + 1 bucket."""
    c = st.commit_obj(name)
    key = normalize_name(rid)
    b, d = bucket_of(key, c["buckets"]), depth(c["buckets"])
    before = st.stats["objects_read"]
    node = c["id_root"]
    for level in range(d):
        kids = st.tree(node)
        slot = (b >> (FANOUT_BITS * (d - 1 - level))) & 63
        if slot not in kids:
            return [], b, st.stats["objects_read"] - before + 1
        node = kids[slot][1]
    recs = [r for r in st.bucket_records(node) if normalize_name(r["sample_id"]) == key]
    return recs, b, st.stats["objects_read"] - before + 1  # + the commit object


class FileIndex:
    """id -> byte offsets of its lines in a version file, built once per (path, size, mtime)."""

    def __init__(self):
        self.cache, self.lock = OrderedDict(), threading.Lock()

    def lookup(self, path, rid):
        st = os.stat(path)
        key = (path, st.st_size, st.st_mtime)
        with self.lock:
            index = self.cache.get(key)
            if index is not None:
                self.cache.move_to_end(key)
        if index is None:
            index = self.build(path)
            with self.lock:
                self.cache[key] = index
                while len(self.cache) > FILE_INDEXES:
                    self.cache.popitem(last=False)
        out = []
        with open(path, "rb") as f:
            for off in index.get(normalize_name(rid), ()):
                f.seek(off)
                out.append(json.loads(f.readline()))
        return out

    @staticmethod
    def build(path):
        index, off = {}, 0
        with open(path, "rb") as f:
            for n, line in enumerate(f, 1):
                try:
                    sid = json.loads(line)["sample_id"]
                except (json.JSONDecodeError, KeyError, TypeError):
                    raise ApiError(400, "bad_jsonl", f"Файл {os.path.basename(path)}, строка {n}: не запись "
                                                     f"манифеста.", {"line": n}) from None
                index.setdefault(normalize_name(sid), []).append(off)
                off += len(line)
        return index


def step_change(newer, older):
    """What happened to a record between an older and a newer commit, by the core's classify."""
    if newer and older:
        return classify(Rec.of(0, older[0]), Rec.of(0, newer[0]))
    if newer:
        return ["added"]
    if older:
        return ["deleted"]
    return None


def history(repo, rid, limit=100, follow=False, job=None):
    """Newest first. With follow, a record that appears in a commit but not in its parent is looked up
    in the tree diff of the two: a rename continues the history under the old name."""
    st = Store(repo)
    if st.head() is None:
        raise ApiError(409, "no_repo", "В репозитории ещё нет версий.")
    commits = []
    for name, c in st.log():
        commits.append((name, c))
        if len(commits) >= limit + 1:
            break
    items, current, stopped = [], normalize_name(rid), None
    found = {}

    def look(name, rid_):
        if (name, rid_) not in found:
            found[name, rid_] = in_commit(st, name, rid_)
        return found[name, rid_]

    for i, (name, c) in enumerate(commits[:limit]):
        if job:
            job.check()
            job.progress(commits_done=i, commits_total=min(limit, len(commits)))
        recs, bucket, objects = look(name, current)
        item = {"commit": name, "short": name[:12], "message": c["message"], "id": current,
                "present": bool(recs), "records": [shown(r) for r in recs], "bucket": bucket,
                "objects_read": objects}
        parent = commits[i + 1] if i + 1 < len(commits) else None
        if parent is None:
            item["change"] = ["added"] if recs else None
            stopped = stopped or ("first_commit" if c["parent"] is None else "limit")
            items.append(item)
            break
        older, _, _ = look(parent[0], current)
        item["change"] = step_change(recs, older)
        if follow and recs and not older:
            if job:
                job.stage = "diff"
            d = tree_diff(Store(repo), parent[0], name)
            ch = next((x for x in d.changes if "renamed" in x.types and x.new.id_key == current), None)
            if ch is not None and ch.ambiguous:
                item["change"] = ch.types
                item["ambiguous"] = {"candidates": ch.candidates}
                items.append(item)
                stopped = "ambiguous"
                break
            if job:
                job.stage = "history"
            if ch is None:  # really added here: nothing older to follow
                items.append(item)
                stopped = "added"
                break
            item["change"] = ch.types
            item["renamed_from"] = ch.old.id_key
            current = ch.old.id_key
        items.append(item)
    return {"id": normalize_name(rid), "items": items, "followed": follow,
            "stopped": stopped or ("limit" if len(items) >= limit else None),
            "objects_read": sum(x["objects_read"] for x in items)}
