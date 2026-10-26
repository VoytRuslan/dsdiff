"""Benchmarks -> bench/results.csv (long format: one metric per row).

    flat_vs_tree  n in {1e5, 1e6}, d in {10, 100, 1000, 10000} changed records: flat diff of the
                  two files against tree_diff of the two commits, with 4096 buckets and with about
                  n / 16; seconds, buckets / records / objects read, and the commit of each version
                  (d = 0: the base), which the tree diff relies on
    storage       10 consecutive versions (n = 1e5) committed one by one, at two change rates and
                  both bucket counts: object bytes, object count, du of .dsdiff, and the bytes of
                  10 full JSONL copies, plain and gzipped
    baseline      v0 (id-first, dsdiff.baseline_idfirst) / flat diff / tree diff on the presets and on
                  one-phenomenon scenarios: change rows wrong against the truth (missed + false,
                  ambiguous rename groups left out), verdict and the truth's, leaks found, seconds
    collisions    hash_bits in {16, 20, 24, 32, 64}, n = 1e5: renames the diff reports that the
                  truth does not (unrelated records whose hashes collide)

python -m dsdiff.bench [--quick] [--only EXP ...] [--out bench/results.csv] [--work DIR]
"""
import argparse
import csv
import gzip
import hashlib
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time

from .baseline_idfirst import diff_idfirst
from .diff import diff
from .gen import main as gen
from .jsonl import read_jsonl, write_jsonl
from .normalize import normalize_name
from .store import Store
from .treediff import tree_diff

FIELDS = ["experiment", "scenario", "label", "n", "d", "buckets", "hash_bits", "version", "method", "metric", "value"]
BUCKET_CONFIGS = {"fixed4096": 4096, "adaptive": None}  # None: about n / 16 (store.choose_buckets)
QUIET = ["--p-rename", "0", "--p-relabel", "0", "--p-rename-relabel", "0", "--p-replace", "0", "--p-move", "0",
         "--p-meta", "0", "--p-delete", "0", "--p-add", "0", "--p-noise", "0", "--p-dup-content", "0",
         "--n-leaks", "0", "--n-rename-cycles", "0"]
MUTATIONS = ("relabel", "rename", "move", "replace", "meta", "delete", "add")


class Results:
    def __init__(self, path):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.f = open(path, "w", encoding="utf-8", newline="")
        self.w = csv.DictWriter(self.f, FIELDS)
        self.w.writeheader()

    def add(self, metric, value, **keys):
        row = dict(keys, metric=metric, value=round(value, 6) if isinstance(value, float) else value)
        self.w.writerow(row)
        self.f.flush()
        print("  " + ", ".join(f"{k}={v}" for k, v in row.items()), flush=True)


def generate(out, *args):
    with open(os.devnull, "w") as null:
        stdout, sys.stdout = sys.stdout, null
        try:
            gen(list(args) + ["--out", out])
        finally:
            sys.stdout = stdout


def mutate(v1, d, seed=0):
    """v1 with exactly d changed records, cycling through MUTATIONS."""
    rng = random.Random(seed)
    v2, deleted, added = list(v1), set(), []
    for k, i in enumerate(rng.sample(range(len(v1)), d)):
        kind, r = MUTATIONS[k % len(MUTATIONS)], dict(v1[i])
        h = hashlib.blake2b(f"bench|{k}".encode(), digest_size=len(r["content_hash"]) // 2).hexdigest()
        if kind == "relabel":
            r["labels"] = sorted(set(r["labels"]) ^ {"zebra"}) or ["cat"]
        elif kind == "rename":
            r["sample_id"] = r["sample_id"].replace("img_", "mv_").replace("fotó_", "mv_")
        elif kind == "move":
            r["group"] = {"train": "val", "val": "test", "test": "train"}[r["group"]]
        elif kind == "replace":
            r["content_hash"] = h
        elif kind == "meta":
            r["metadata"] = dict(r["metadata"], source="bench")
        elif kind == "delete":
            deleted.add(i)
        else:
            added.append(dict(r, sample_id=f"images/new_{k:07d}.jpg", content_hash=h))
        v2[i] = r
    return [r for i, r in enumerate(v2) if i not in deleted] + added


def du_bytes(path):
    return int(subprocess.run(["du", "-sk", path], capture_output=True, text=True, check=True).stdout.split()[0]) * 1024


def object_bytes(store):
    return sum(os.path.getsize(os.path.join(dp, f)) for dp, _, fs in os.walk(store.objects) for f in fs)


def flat_vs_tree(res, work, ns, ds, bucket_configs):
    for n in ns:
        print(f"flat_vs_tree n={n}", flush=True)
        base = os.path.join(work, f"fvt_{n}")
        generate(base, "--n", str(n), "--versions", "1", "--seed", "1")
        v1_path = os.path.join(base, "v1.jsonl")
        v1 = read_jsonl(v1_path)
        for d in ds:
            write_jsonl(os.path.join(base, f"v2_d{d}.jsonl"), mutate(v1, d))
        flat_reports = {}
        for d in ds:
            keys = dict(experiment="flat_vs_tree", n=n, d=d)
            v2_path = os.path.join(base, f"v2_d{d}.jsonl")
            t = time.perf_counter()
            flat = diff(read_jsonl(v1_path), read_jsonl(v2_path))
            res.add("seconds", time.perf_counter() - t, method="flat", **keys)
            res.add("records_read", flat.n_old + flat.n_new, method="flat", **keys)
            flat_reports[d] = flat.to_report()
            del flat
        for label, buckets in bucket_configs.items():
            repo = os.path.join(base, f"store_{label}")
            t = time.perf_counter()
            a, ca = Store(repo).commit(v1, "v1", buckets)
            res.add("seconds", time.perf_counter() - t, method="commit", buckets=ca["buckets"],
                    experiment="flat_vs_tree", n=n, d=0)
            for d in ds:
                keys = dict(experiment="flat_vs_tree", n=n, d=d, buckets=ca["buckets"])
                v2 = read_jsonl(os.path.join(base, f"v2_d{d}.jsonl"))
                t = time.perf_counter()
                b, _ = Store(repo).commit(v2, f"d={d}", buckets)
                res.add("seconds", time.perf_counter() - t, method="commit", **keys)
                del v2
                st = Store(repo)
                t = time.perf_counter()
                tree = tree_diff(st, a, b)
                res.add("seconds", time.perf_counter() - t, method="tree", **keys)
                for m in ("buckets_read", "records_read", "objects_read"):
                    res.add(m, tree.stats[m], method="tree", **keys)
                if tree.to_report() != flat_reports[d]:
                    raise AssertionError(f"tree diff differs from flat diff at n={n}, d={d}, {label}")
                res.add("changes", len(tree.changes), method="tree", **keys)


def storage(res, work, n, versions, bucket_configs):
    rates = {"default": [], "low": ["--p-rename", "0.0015", "--p-relabel", "0.0015", "--p-replace", "0.0005",
                                    "--p-move", "0.0005", "--p-meta", "0.0005", "--p-delete", "0.0005",
                                    "--p-add", "0.001", "--p-noise", "0.005", "--n-leaks", "1"]}
    for scenario, extra in rates.items():
        print(f"storage {scenario}", flush=True)
        d = os.path.join(work, f"storage_{scenario}")
        generate(d, "--n", str(n), "--versions", str(versions), "--seed", "2", *extra)
        copies = gz = 0
        for v in range(1, versions + 1):
            with open(os.path.join(d, f"v{v}.jsonl"), "rb") as f:
                data = f.read()
            copies += len(data)
            gz += len(gzip.compress(data, compresslevel=6, mtime=0))
            keys = dict(experiment="storage", scenario=scenario, n=n, version=v)
            res.add("jsonl_bytes", copies, method="copies", **keys)
            res.add("jsonl_gz_bytes", gz, method="copies", **keys)
        for label, buckets in bucket_configs.items():
            st = Store(os.path.join(d, f"store_{label}"))
            for v in range(1, versions + 1):
                _, c = st.commit(read_jsonl(os.path.join(d, f"v{v}.jsonl")), f"v{v}", buckets)
                keys = dict(experiment="storage", scenario=scenario, n=n, version=v, buckets=c["buckets"])
                res.add("object_bytes", object_bytes(st), method="store", **keys)
                res.add("objects", sum(len(fs) for _, _, fs in os.walk(st.objects)), method="store", **keys)
                res.add("du_bytes", du_bytes(st.root), method="store", **keys)


TRUTH_ONLY = {"leak", "leak_by_move", "rename_cycle"}  # truth markers that are not change classes
BASELINE_SETS = {  # name -> generator args
    "tiny": ["--preset", "tiny"], "story": ["--preset", "story"], "clean": ["--preset", "clean"],
    "test_relabel": ["--preset", "test_relabel"], "test_to_train": ["--preset", "test_to_train"],
    "n30000_seed3": ["--n", "30000", "--seed", "3"],
}
BREAKS = {  # "what breaks": one phenomenon each on a quiet base (n = 10000), name -> (label, args)
    "swaps": ("обмены имён", ["--n-rename-cycles", "100"]),
    "id_noise": ("шум в id", ["--p-noise", "0.05", "--noise-rules", "id_case", "backslash", "dot_slash",
                              "nfd", "id_whitespace"]),
    "train_to_test": ("перенос train→test", ["--p-move", "0.02"]),
    "test_relabel": ("смена меток в test", ["--preset", "test_relabel"] + QUIET),  # its forced test relabels only
}


def truth_verdict(old, new, truth, summary, v):
    """The verdict the truth calls for, by the rules of dsdiff.diff.make_verdict."""
    leaks_now = {x["content_hash"] for x in summary[f"leaks_v{v}"]}
    leaks_before = {x["content_hash"] for x in summary[f"leaks_v{v - 1}"]}
    events = [x for x in summary["leaks"] if x["version"] == v]
    to_train = {x["content_hash"] for x in events if x["kind"] == "copy" and x["direction"] == "test->train"}
    if leaks_now or any(x["kind"] == "move" for x in events):
        if leaks_now - to_train or leaks_now & leaks_before or len(to_train) < len(events):
            return "do_not_trust"
        return "do_not_retrain"
    g_old = {normalize_name(r["sample_id"]): r["group"] for r in old}
    g_new = {normalize_name(r["sample_id"]): r["group"] for r in new}
    for t in truth:
        if set(t["types"]) & {"added", "deleted", "content_replaced", "relabeled", "group_moved"} and \
                "test" in (g_old.get(t["old_id"]), g_new.get(t["new_id"])):
            return "metrics_not_comparable"
    return "trust"


def score(rows, truth):
    """(missed, false): truth rows not reported exactly, reported rows not in the truth; rows of
    ambiguous rename groups (by the truth) are left out for every method."""
    amb = {r["content_hash"] for r in truth if r["details"].get("ambiguous")}

    def keep(r):
        return r["content_hash"] not in amb and r.get("details", {}).get("hash_old") not in amb

    def key(r):
        ids = tuple(x and normalize_name(x) for x in (r["old_id"], r["new_id"]))
        return ids + (tuple(sorted(set(r["types"]) - TRUTH_ONLY)),)

    want = {key(r) for r in truth if keep(r)}
    got = {key(r) for r in rows if keep(r)}
    return len(want - got), len(got - want)


def baseline(res, work, sets, breaks):
    jobs = [(name, None, args) for name, args in sets.items()]
    jobs += [(name, label, args) for name, (label, args) in breaks.items()]
    for name, label, args in jobs:
        scenario = name if label is None else f"break_{name}"
        print(f"baseline {scenario}", flush=True)
        d = os.path.join(work, f"base_{scenario}")
        if label is not None and args[0] != "--preset":
            args = ["--n", "10000", "--seed", "5"] + QUIET + args
        generate(d, *args)
        old, new = read_jsonl(os.path.join(d, "v1.jsonl")), read_jsonl(os.path.join(d, "v2.jsonl"))
        truth = read_jsonl(os.path.join(d, "truth_v1_v2.jsonl"))
        with open(os.path.join(d, "truth_summary.json"), encoding="utf-8") as f:
            summary = __import__("json").load(f)
        keys = dict(experiment="baseline", scenario=scenario, label=label or "", n=len(old))
        res.add("verdict", truth_verdict(old, new, truth, summary, 2), method="truth", **keys)
        res.add("leaks", len(summary["leaks_v2"]) + sum(x["kind"] == "move" and x["version"] == 2
                                                       for x in summary["leaks"]), method="truth", **keys)
        res.add("changes", len(truth), method="truth", **keys)

        t = time.perf_counter()
        v0 = diff_idfirst(old, new)
        sec = time.perf_counter() - t
        t = time.perf_counter()
        flat = diff(old, new).to_report()
        flat_sec = time.perf_counter() - t
        st = Store(os.path.join(d, "repo"))
        a, _ = st.commit(old, "v1")
        b, _ = st.commit(new, "v2")
        t = time.perf_counter()
        tree = tree_diff(Store(os.path.join(d, "repo")), a, b).to_report()
        tree_sec = time.perf_counter() - t
        for method, rows, verdict, leaks, secs in [
                ("v0", v0["changes"], v0["verdict"]["code"], len(v0["leaks"]), sec),
                ("flat", flat["changes"], flat["verdict"]["code"],
                 len(flat["leaks"]["within"]) + len(flat["leaks"]["by_move"]), flat_sec),
                ("tree", tree["changes"], tree["verdict"]["code"],
                 len(tree["leaks"]["within"]) + len(tree["leaks"]["by_move"]), tree_sec)]:
            missed, false = score(rows, truth)
            res.add("missed", missed, method=method, **keys)
            res.add("false", false, method=method, **keys)
            res.add("errors", missed + false, method=method, **keys)
            res.add("changes", len(rows), method=method, **keys)
            res.add("verdict", verdict, method=method, **keys)
            res.add("leaks", leaks, method=method, **keys)
            res.add("seconds", secs, method=method, **keys)


def collisions(res, work, n, bits):
    for b in bits:
        print(f"collisions hash_bits={b}", flush=True)
        d = os.path.join(work, f"coll_{b}")
        generate(d, "--n", str(n), "--hash-bits", str(b), "--seed", "1")
        r = diff(read_jsonl(os.path.join(d, "v1.jsonl")), read_jsonl(os.path.join(d, "v2.jsonl"))).to_report()
        truth = read_jsonl(os.path.join(d, "truth_v1_v2.jsonl"))
        true_renames = {(t["old_id"], t["new_id"]) for t in truth if "renamed" in t["types"]}
        renames = [c for c in r["changes"] if "renamed" in c["types"]]
        keys = dict(experiment="collisions", n=n, hash_bits=b, method="flat")
        res.add("renames_reported", len(renames), **keys)
        res.add("renames_true", len(true_renames), **keys)
        res.add("false_renames", sum((c["old_id"], c["new_id"]) not in true_renames for c in renames), **keys)
        res.add("missed_renames", len(true_renames - {(c["old_id"], c["new_id"]) for c in renames}), **keys)
        res.add("ambiguous_renames", r["summary"]["ambiguous_renames"], **keys)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m dsdiff.bench", description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=os.path.join("bench", "results.csv"))
    ap.add_argument("--work", help="where to put generated data and stores (default: a temp dir, removed after)")
    ap.add_argument("--quick", action="store_true", help="tiny sizes, for a smoke test")
    ap.add_argument("--only", nargs="+", choices=["flat_vs_tree", "storage", "baseline", "collisions"])
    a = ap.parse_args(argv)
    work = a.work or tempfile.mkdtemp(prefix="dsdiff-bench-")
    os.makedirs(work, exist_ok=True)
    res = Results(a.out)
    only = set(a.only or ["flat_vs_tree", "storage", "baseline", "collisions"])
    try:
        if "flat_vs_tree" in only:
            flat_vs_tree(res, work, [2_000] if a.quick else [100_000, 1_000_000],
                         [10, 100] if a.quick else [10, 100, 1000, 10_000], BUCKET_CONFIGS)
        if "storage" in only:
            storage(res, work, 2_000 if a.quick else 100_000, 3 if a.quick else 10, BUCKET_CONFIGS)
        if "baseline" in only:
            baseline(res, work, {"tiny": BASELINE_SETS["tiny"]} if a.quick else BASELINE_SETS,
                     {} if a.quick else BREAKS)
        if "collisions" in only:
            collisions(res, work, 2_000 if a.quick else 100_000, [16, 32] if a.quick else [16, 20, 24, 32, 64])
    finally:
        res.f.close()
        if not a.work:
            shutil.rmtree(work, ignore_errors=True)
    print(f"-> {a.out}")


if __name__ == "__main__":
    main()
