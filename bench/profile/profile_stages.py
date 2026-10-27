"""Stage profile at n = 1e6: flat diff of two files and commit of a new version (d = 1%).

    python bench/profile/profile_stages.py prepare  --work DIR          # v1 (gen, seed 1), v2 = bench.mutate(v1, d), store with v1
    python bench/profile/profile_stages.py flat     --work DIR [--no-gc] # -> stages_flat[_nogc].json
    python bench/profile/profile_stages.py commit   --work DIR [--no-gc] # -> stages_commit[_nogc].json
    python bench/profile/profile_stages.py cprofile --work DIR          # -> flat.prof, commit.prof, *_top.txt, *_categories.json

Nothing in dsdiff/ is changed. Wall-clock stages call the core functions themselves; where a core
function mixes several kinds of work (Rec.of, Store.commit, Store.put) the same operations are
repeated here with timers in between, and the result is checked against the core (same report,
same commit name). Times: minimum over --runs runs after one warm-up run, as in bench/.
Garbage-collector time is measured with gc.callbacks and shown inside the stage it fell into.
"""
import argparse
import cProfile
import gc
import hashlib
import json
import math
import os
import pstats
import sys
import time
import zlib
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from dsdiff.bench import generate, mutate  # noqa: E402
from dsdiff.diff import (STORED_EXAMPLES, DiffConfig, DiffResult, Rec, compare, diff, find_leaks,  # noqa: E402
                         make_verdict, validate, within_leaks)
from dsdiff.jsonl import dumps, read_jsonl, write_jsonl  # noqa: E402
from dsdiff.normalize import _h, line_hash, multiset_digest, normalize_record  # noqa: E402
from dsdiff.store import Store, bucket_of, cbucket_body, check_buckets, tree_body, depth, FANOUT_BITS  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
BUCKETS = int(os.environ.get("PROF_BUCKETS", 65_536))
pc = time.perf_counter


class GcClock:
    """Seconds spent in the cyclic garbage collector."""

    def __init__(self):
        self.total, self.t0 = 0.0, None
        gc.callbacks.append(self)

    def __call__(self, phase, info):
        if phase == "start":
            self.t0 = pc()
        elif self.t0 is not None:
            self.total += pc() - self.t0
            self.t0 = None


GC = GcClock()


class Laps:
    def __init__(self):
        self.t, self.g, self.wall, self.gc = pc(), GC.total, {}, {}

    def __call__(self, stage):
        now, g = pc(), GC.total
        self.wall[stage] = self.wall.get(stage, 0) + now - self.t
        self.gc[stage] = self.gc.get(stage, 0) + g - self.g
        self.t, self.g = now, g


def paths(work):
    return os.path.join(work, "v1.jsonl"), os.path.join(work, "v2.jsonl"), os.path.join(work, "repo")


def read_lines(path):
    with open(path, encoding="utf-8") as f:
        return f.readlines()


# ---- flat ---------------------------------------------------------------

def flat_run(v1p, v2p, check=False):
    lap = Laps()
    l1, l2 = read_lines(v1p), read_lines(v2p)
    lap("read file (I/O + utf-8 + split)")
    r1, r2 = [json.loads(x) for x in l1], [json.loads(x) for x in l2]
    del l1, l2
    lap("json.loads")
    old = [Rec.of(i, r) for i, r in enumerate(r1)]
    new = [Rec.of(i, r) for i, r in enumerate(r2)]
    lap("Rec.of (normalize + keys + hashes)")
    cfg = DiffConfig()
    validation = {"old": validate(old, cfg), "new": validate(new, cfg)}
    lap("validate")
    changes, unchanged = compare(old, new)
    lap("match + classify (compare)")
    leaks = find_leaks(changes, within_leaks(new))
    verdict = make_verdict(changes, leaks, cfg)
    lap("leaks + verdict")
    res = DiffResult(len(old), len(new), unchanged, changes, validation, leaks, verdict, {},
                     multiset_digest(r.raw_hash for r in old), multiset_digest(r.raw_hash for r in new))
    lap("multiset digest")
    if check:
        ref = diff(r1, r2)
        assert ref.to_report() == res.to_report() and ref.old_digest == res.old_digest, "staged flat != diff()"
    return lap, (r1, r2, old, new)


def recof_parts(raws):
    """Rec.of over the same records, split into its operations (each a separate pass)."""
    out = {}
    t = pc(); norms = [normalize_record(r) for r in raws]; out["normalize_record"] = pc() - t
    t = pc()
    pl = [json.dumps({"group": n["group"], "labels": n["labels"], "metadata": n["metadata"]},
                     sort_keys=True, ensure_ascii=False) for n in norms]
    out["json.dumps payload (sort_keys)"] = pc() - t
    t = pc(); ln = [json.dumps(r, ensure_ascii=False) for r in raws]; out["json.dumps line"] = pc() - t
    t = pc(); a = [s.encode() for s in pl]; b = [s.encode() for s in ln]; out["str.encode"] = pc() - t
    t = pc()
    ph = [hashlib.blake2b(x, digest_size=16).hexdigest() for x in a]
    rh = [hashlib.blake2b(x, digest_size=16).hexdigest() for x in b]
    out["blake2b (2 per record)"] = pc() - t
    t = pc()
    recs = [Rec(i, r, n, n["sample_id"], n["content_hash"], p, h)
            for i, (r, n, p, h) in enumerate(zip(raws, norms, ph, rh))]
    out["Rec dataclass"] = pc() - t
    del recs, a, b, pl, ln
    t = pc(); [Rec.of(i, r) for i, r in enumerate(raws)]; out["_total Rec.of"] = pc() - t
    return out


def blake_bytes(raws):
    n = 0
    for r in raws:
        n += len(json.dumps(r, ensure_ascii=False).encode())
    return n


# ---- commit -------------------------------------------------------------

class TimedStore(Store):
    """Store.put with a timer around every step; same bytes, same names, same files."""

    def __init__(self, repo):
        super().__init__(repo)
        self.t = defaultdict(float)
        self.written = []

    def put(self, kind, body):
        t0 = pc()
        data = f"{kind} {len(body)}\0".encode() + body
        t1 = pc()
        name = hashlib.sha256(data).hexdigest()
        t2 = pc()
        path = os.path.join(self.objects, name[:2], name[2:])
        t3 = pc()
        ex = os.path.exists(path)
        t4 = pc()
        self.t["put: header + concat"] += t1 - t0
        self.t["sha256"] += t2 - t1
        self.t["put: os.path.join"] += t3 - t2
        self.t["fs: os.path.exists"] += t4 - t3
        if not ex:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            t5 = pc()
            tmp = f"{path}.tmp{os.getpid()}"
            z = zlib.compress(data)
            t6 = pc()
            with open(tmp, "wb") as f:
                f.write(z)
            t7 = pc()
            os.replace(tmp, path)
            t8 = pc()
            self.t["fs: os.makedirs"] += t5 - t4
            self.t["zlib.compress"] += t6 - t5
            self.t["fs: open + write + close"] += t7 - t6
            self.t["fs: os.replace"] += t8 - t7
            self.stats["objects_written"] += 1
            self.written.append(path)
            self.t["_bytes zlib in"] += len(data)
            self.t["_bytes zlib out"] += len(z)
        self.t["_objects"] += 1
        self.t["_bytes sha256"] += len(data)
        return name

    def put_tree(self, leaves, kind, buckets):
        level = {b: (kind, self.put(kind, body), n) for b, (body, n) in leaves.items()}
        for _ in range(depth(buckets)):
            parents = defaultdict(dict)
            for key, node in level.items():
                parents[key >> FANOUT_BITS][key & 63] = node
            level = {}
            for key, kids in parents.items():
                n = None if kind == "bucket" else sum(k[2] for k in kids.values())
                level[key] = ("tree", self.put("tree", tree_body(kids)), n)
        return level[0][1] if level else self.put("tree", b"")


def commit_run(v2p, repo):
    """Store.commit of v2 with laps; returns (laps, put timers, commit name)."""
    lap = Laps()
    lines = read_lines(v2p)
    lap("read file (I/O + utf-8 + split)")
    records = [json.loads(x) for x in lines]
    del lines
    lap("json.loads")
    st = TimedStore(repo)
    parent = st.head()
    buckets = check_buckets(BUCKETS)
    recs = [Rec.of(i, r) for i, r in enumerate(records)]
    lap("Rec.of (normalize + keys + hashes)")
    bid = [bucket_of(r.id_key, buckets) for r in recs]
    bc = [bucket_of(r.content_key, buckets) for r in recs]
    lap("bucket_of (blake2b 8 B, 2 per record)")
    raw_lines = [dumps(r.raw) for r in recs]
    lap("json.dumps raw line for bucket body")
    by_id, by_c = defaultdict(list), defaultdict(lambda: defaultdict(list))
    counts = {"records": len(recs)}
    for r, a, c, line in zip(recs, bid, bc, raw_lines):
        g = r.norm["group"]
        by_id[a].append((r.id_key, line))
        by_c[c][r.content_key].append((g, r.id_key))
        counts[g] = counts.get(g, 0) + 1
    del raw_lines
    lap("group into buckets (dicts, appends)")
    id_leaves = {b: ("".join(f"{line}\n" for _, line in sorted(rows)).encode(), None) for b, rows in by_id.items()}
    lap("id buckets: sort + join + encode")
    c_leaves = {b: cbucket_body(rows) for b, rows in by_c.items()}
    lap("content buckets: sort + format (cbucket_body)")
    st.t.clear()
    id_root = st.put_tree(id_leaves, "bucket", buckets)
    content_root = st.put_tree(c_leaves, "cbucket", buckets)
    put_t = dict(st.t)
    lap("put_tree (objects: sha256, zlib, fs)")
    obj = {"buckets": buckets, "id_root": id_root, "content_root": content_root,
           "parent": parent, "message": "v2", "counts": counts,
           "leaks_count": sum(n for _, n in c_leaves.values())}
    obj["digest"] = multiset_digest(r.raw_hash for r in recs)
    lap("multiset digest")
    obj["validation"] = validate(recs, DiffConfig(examples=STORED_EXAMPLES))
    lap("validate")
    name = st.put("commit", json.dumps(obj, ensure_ascii=False, indent=1).encode())
    st.set_head(name)
    lap("commit object + HEAD")
    return lap, put_t, name, st.written


def snapshot(repo):
    objs = os.path.join(repo, ".dsdiff", "objects")
    return {os.path.join(dp, f) for dp, _, fs in os.walk(objs) for f in fs}


def reset(repo, before, head):
    for p in snapshot(repo) - before:
        os.remove(p)
    Store(repo).set_head(head)


# ---- commands -----------------------------------------------------------

def cmd_prepare(a):
    v1p, v2p, repo = paths(a.work)
    os.makedirs(a.work, exist_ok=True)
    t = pc(); generate(a.work, "--n", str(a.n), "--versions", "1", "--seed", "1"); print(f"gen {pc() - t:.1f} s")
    v1 = read_jsonl(v1p)
    write_jsonl(v2p, mutate(v1, a.d))
    t = pc(); name, c = Store(repo).commit(v1, "v1", BUCKETS); print(f"commit v1 {pc() - t:.1f} s, {c['buckets']} buckets")
    for p in (v1p, v2p):
        print(p, os.path.getsize(p) / 1e6, "MB")


def best(runs):
    keys = runs[0][0].wall.keys()
    return {k: min(r[0].wall[k] for r in runs) for k in keys}, {k: min(r[0].gc[k] for r in runs) for k in keys}


def suffix(a):
    return "" if a.gc else "_nogc"


def cmd_flat(a):
    v1p, v2p, _ = paths(a.work)
    if not a.gc:
        gc.disable()
    out = {"n_files": [sum(1 for _ in open(p, encoding="utf-8")) for p in (v1p, v2p)],
           "bytes": [os.path.getsize(p) for p in (v1p, v2p)], "runs": []}
    out["diff_reference"] = []
    for _ in range(a.runs):
        t = pc(); ref = diff(read_jsonl(v1p), read_jsonl(v2p))
        out["diff_reference"].append({"total": pc() - t, "internal": ref.timings})
        del ref
        gc.collect()
    out["diff_total_reference"] = min(r["total"] for r in out["diff_reference"])
    for k in range(a.runs + 1):  # first run = warm-up and correctness check
        t = pc()
        lap, data = flat_run(v1p, v2p, check=(k == 0))
        total = pc() - t
        r1, r2, *_ = data
        del data
        if k:
            out["runs"].append({"total": total, "wall": lap.wall, "gc": lap.gc})
            print(f"run {k}: {total:.1f} s", flush=True)
        else:
            parts = [recof_parts(r1), recof_parts(r2)]
            out["recof_parts"] = {x: parts[0][x] + parts[1][x] for x in parts[0]}
            out["line_bytes"] = blake_bytes(r1) + blake_bytes(r2)
            keys1 = {normalize_record(r)["content_hash"] for r in r1}
            keys2 = {normalize_record(r)["content_hash"] for r in r2}
            t = pc(); sorted(keys1 | keys2); out["big_sort_content_keys"] = pc() - t
            out["n_content_keys"] = len(keys1 | keys2)
        del r1, r2
        gc.collect()
    out["min_total"] = min(r["total"] for r in out["runs"])
    out["min_wall"] = {s: min(r["wall"][s] for r in out["runs"]) for s in out["runs"][0]["wall"]}
    out["min_gc"] = {s: min(r["gc"][s] for r in out["runs"]) for s in out["runs"][0]["gc"]}
    with open(os.path.join(HERE, f"stages_flat{suffix(a)}.json"), "w") as f:
        json.dump(out, f, indent=1, ensure_ascii=False)
    print(json.dumps(out["min_wall"], indent=1, ensure_ascii=False))


def cmd_commit(a):
    _, v2p, repo = paths(a.work)
    if not a.gc:
        gc.disable()
    st = Store(repo)
    head = st.head()
    before = snapshot(repo)
    out = {"runs": []}
    t = pc(); ref_name, _ = Store(repo).commit(read_jsonl(v2p), "v2", BUCKETS); out["store_commit_reference"] = pc() - t
    out["new_objects"] = len(snapshot(repo) - before)
    reset(repo, before, head)
    gc.collect()
    for k in range(a.runs + 1):
        t = pc()
        lap, put_t, name, written = commit_run(v2p, repo)
        total = pc() - t
        assert name == ref_name, "staged commit != Store.commit"
        reset(repo, before, head)
        gc.collect()
        if k:
            out["runs"].append({"total": total, "wall": lap.wall, "gc": lap.gc, "put": put_t})
            print(f"run {k}: {total:.1f} s, {len(written)} objects written", flush=True)
    out["min_total"] = min(r["total"] for r in out["runs"])
    out["min_wall"] = {s: min(r["wall"][s] for r in out["runs"]) for s in out["runs"][0]["wall"]}
    out["min_gc"] = {s: min(r["gc"][s] for r in out["runs"]) for s in out["runs"][0]["gc"]}
    out["min_put"] = {s: min(r["put"][s] for r in out["runs"]) for s in out["runs"][0]["put"]}
    with open(os.path.join(HERE, f"stages_commit{suffix(a)}.json"), "w") as f:
        json.dump(out, f, indent=1, ensure_ascii=False)
    print(json.dumps(out["min_wall"], indent=1, ensure_ascii=False))
    print(json.dumps(out["min_put"], indent=1, ensure_ascii=False))


CATEGORIES = [  # (category, predicate on (file, line, func)); first match wins
    ("json.loads (C decoder)", lambda f, l, n: "scan_once" in n or n == "raw_decode" or (f.endswith("json/decoder.py")) or f.endswith("json/__init__.py") and n == "loads"),
    ("json.dumps (C encoder)", lambda f, l, n: "encode" in n and ("json" in f or "_json" in n) or f.endswith("json/__init__.py") and n == "dumps" or f.endswith("json/encoder.py")),
    ("blake2b", lambda f, l, n: "blake2b" in n),
    ("sha256", lambda f, l, n: "sha256" in n or "openssl_sha256" in n),
    ("hexdigest/digest", lambda f, l, n: "hexdigest" in n or n.endswith("digest' of '_blake2.blake2b' objects>") or "'digest'" in n),
    ("zlib", lambda f, l, n: "zlib" in n),
    ("fs (os/io)", lambda f, l, n: any(x in n for x in ("posix.", "io.open", "_io.", "built-in method io", "method write", "method read", "method readlines", "__exit__")) or f.endswith(("genericpath.py", "os.py", "posixpath.py"))),
    ("unicodedata.normalize", lambda f, l, n: "unicodedata" in n),
    ("sorting (sorted/list.sort)", lambda f, l, n: "builtins.sorted" in n or "'sort' of 'list'" in n),
    ("sort key lambdas", lambda f, l, n: n == "<lambda>" and f.endswith("diff.py")),
    ("str methods", lambda f, l, n: "of 'str' objects" in n or "of 'bytes' objects" in n),
    ("gc (in profile)", lambda f, l, n: "gc." in n),
    ("dsdiff Python code", lambda f, l, n: "dsdiff" in f),
]


def categorize(stats):
    cats = defaultdict(float)
    rows = []
    for (f, l, n), (cc, nc, tt, ct, callers) in stats.stats.items():
        cat = next((c for c, p in CATEGORIES if p(f, l, n)), "other builtins")
        cats[cat] += tt
        rows.append((tt, cat, f"{os.path.basename(f)}:{l}({n})"))
    rows.sort(reverse=True)
    return dict(sorted(cats.items(), key=lambda x: -x[1])), rows[:40]


def cmd_cprofile(a):
    v1p, v2p, repo = paths(a.work)
    st = Store(repo)
    head, before = st.head(), snapshot(repo)
    jobs = {"flat": lambda: diff(read_jsonl(v1p), read_jsonl(v2p)),
            "commit": lambda: Store(repo).commit(read_jsonl(v2p), "v2", BUCKETS)}
    for name, job in jobs.items():
        if a.only and name not in a.only:
            continue
        gc.collect()
        pr = cProfile.Profile()
        t = pc()
        pr.enable()
        job()
        pr.disable()
        wall = pc() - t
        if name == "commit":
            reset(repo, before, head)
        prof = os.path.join(HERE, f"{name}.prof")
        pr.dump_stats(prof)
        s = pstats.Stats(prof)
        with open(os.path.join(HERE, f"{name}_top.txt"), "w") as f:
            f.write(f"wall under cProfile: {wall:.1f} s\n\n")
            pstats.Stats(prof, stream=f).sort_stats("tottime").print_stats(35)
            pstats.Stats(prof, stream=f).sort_stats("cumulative").print_stats(35)
        cats, top = categorize(s)
        with open(os.path.join(HERE, f"{name}_categories.json"), "w") as f:
            json.dump({"wall_under_profile": wall, "total_tottime": s.total_tt, "categories": cats,
                       "top": top}, f, indent=1, ensure_ascii=False)
        print(name, f"{wall:.1f} s under cProfile")
        for c, v in cats.items():
            print(f"  {c:32s} {v:7.2f} s  {v / s.total_tt:6.1%}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["prepare", "flat", "commit", "cprofile"])
    ap.add_argument("--work", required=True)
    ap.add_argument("--n", type=int, default=1_000_000)
    ap.add_argument("--d", type=int, default=10_000)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--no-gc", dest="gc", action="store_false", help="gc.disable() for the whole run (stage costs without the collector)")
    a = ap.parse_args()
    {"prepare": cmd_prepare, "flat": cmd_flat, "commit": cmd_commit, "cprofile": cmd_cprofile}[a.cmd](a)


if __name__ == "__main__":
    main()
