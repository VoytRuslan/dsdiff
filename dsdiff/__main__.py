"""python -m dsdiff {diff,apply,verify,check,commit,log,cat-file} — semantic diff and patch of manifest versions."""
import argparse
import json
import os
import sys
import time

from .diff import VERDICTS, DiffConfig, diff
from .jsonl import dumps, read_jsonl, write_jsonl
from .normalize import canonical_lines
from .patch import PatchError, apply, build_patch
from .store import Store
from .treediff import tree_diff


def _write_json(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")


def _ensure_dir(path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)


def _read(path, timings):
    t = time.perf_counter()
    recs = read_jsonl(path)
    timings["read"] = timings.get("read", 0) + time.perf_counter() - t
    return recs


def print_verdict(v, out=sys.stdout):
    print(f"verdict: {v['label']} ({v['code']})", file=out)
    for r in v["reasons"]:
        ex = f" e.g. {', '.join(r['examples'])}" if r["examples"] else ""
        print(f"  - [{r['reason_code']}] {r['reason']}: {r['count']}{ex}", file=out)


def run_diff(a, timings):
    """Flat diff of two files, or with --rev a tree diff of two commits."""
    cfg = DiffConfig(examples=a.examples)
    if a.rev:
        return tree_diff(Store(a.repo), a.old, a.new, cfg)
    old, new = _read(a.old, timings), _read(a.new, timings)
    return diff(old, new, cfg)


def cmd_diff(a):
    timings = {}
    d = run_diff(a, timings)
    timings.update(d.timings)
    rep = d.to_report()
    t = time.perf_counter()
    if a.report:
        _write_json(a.report, rep)
    if a.patch:
        _ensure_dir(a.patch)
        write_jsonl(a.patch, build_patch(d))
    if a.report or a.patch:
        timings["write"] = time.perf_counter() - t
    s = rep["summary"]
    print(f"{a.old} -> {a.new}: {s['n_old']} -> {s['n_new']} records, {s['unchanged']} unchanged, "
          f"{s['ambiguous_renames']} ambiguous renames")
    print("changes:", ", ".join(f"{k} {v}" for k, v in s["changes_by_combination"].items()) or "none")
    for kind in ("old", "new"):
        if d.validation[kind]:
            print(f"validation ({kind}):", ", ".join(f"{e['type']}" for e in d.validation[kind][:a.examples]),
                  f"({len(d.validation[kind])} total)")
    print_verdict(d.verdict)
    print("time:", ", ".join(f"{k} {v:.3f}s" for k, v in timings.items()),
          f"| total {sum(timings.values()):.3f}s")
    if d.stats:
        print("read:", ", ".join(f"{k} {v}" for k, v in d.stats.items()))
    return 0


def cmd_apply(a):
    old = read_jsonl(a.old)
    try:
        out = apply(old, read_jsonl(a.patch))
    except PatchError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    _ensure_dir(a.out)
    write_jsonl(a.out, out)
    print(f"{a.out}: {len(out)} records")
    return 0


def cmd_verify(a):
    old, ops = read_jsonl(a.old), read_jsonl(a.patch)
    try:
        out = apply(old, ops, check_target=False)
    except PatchError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    with open(a.new, encoding="utf-8") as f:
        new_lines = sorted(line.rstrip("\n") for line in f)
    strict = sorted(dumps(r) for r in out) == new_lines
    semantic = canonical_lines(out, True) == canonical_lines([json.loads(x) for x in new_lines], True)
    print(f"strict (every line byte-identical, order ignored): {'ok' if strict else 'FAIL'}")
    print(f"semantic (after normalization): {'ok' if semantic else 'FAIL'}")
    return 0 if strict and semantic else 1


def cmd_check(a):
    d = run_diff(a, {})
    print_verdict(d.verdict)
    return VERDICTS[d.verdict["code"]][1]  # exit code = severity: 0 trust … 3 do not trust


def cmd_commit(a):
    st = Store(a.repo)
    name, c = st.commit(read_jsonl(a.file), a.message, a.buckets)
    print(f"[{name[:12]}] {a.message}: {c['counts']['records']} records in {c['buckets']} buckets, "
          f"{st.stats['objects_written']} new objects, {c['leaks_count']} train/test leaks")
    return 0


def cmd_log(a):
    st = Store(a.repo)
    for name, c in st.log():
        counts = ", ".join(f"{k} {v}" for k, v in c["counts"].items())
        print(f"{name[:12]}  {c['message']}  ({counts}; leaks {c['leaks_count']}; {c['buckets']} buckets; root {c['id_root'][:12]})")
    return 0


def cmd_cat_file(a):
    st = Store(a.repo)
    try:
        name = st.resolve(a.object)
    except KeyError:
        name = a.object
    kind, body = st.get(name)
    print(f"{kind} {name}")
    sys.stdout.write(body.decode())
    if not body.endswith(b"\n"):
        print()
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["ui"]:  # the local GUI lives in dsdiff/ui and has its own options
        from .ui import main as ui_main
        return ui_main(argv[1:])
    ap = argparse.ArgumentParser(prog="python -m dsdiff", description=__doc__.split(" — ")[1])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("diff", help="diff two versions; print summary, verdict and stage timings")
    p.add_argument("old")
    p.add_argument("new")
    p.add_argument("--report", help="write the JSON report here")
    p.add_argument("--patch", help="write the patch (JSON lines) here")
    p.add_argument("--examples", type=int, default=5, help="ids listed per verdict reason")
    p = sub.add_parser("apply", help="apply a patch to its base version")
    p.add_argument("old")
    p.add_argument("patch")
    p.add_argument("--out", required=True)
    p = sub.add_parser("verify", help="check that OLD + PATCH reproduces NEW, strictly and semantically")
    p.add_argument("old")
    p.add_argument("new")
    p.add_argument("patch")
    p = sub.add_parser("check", help="print only the verdict; exit code 0 trust, 1 metrics not comparable, "
                                     "2 do not retrain, 3 do not trust")
    p.add_argument("old")
    p.add_argument("new")
    p.add_argument("--examples", type=int, default=5)
    p = sub.add_parser("commit", help="store a version file as a commit on top of HEAD")
    p.add_argument("file")
    p.add_argument("-m", "--message", required=True)
    p.add_argument("--buckets", type=int, help="number of buckets, a power of two (default: about n/16, "
                                               "at least 64, the parent's while n/16 stays within 2x of it)")
    p = sub.add_parser("log", help="list commits from HEAD back")
    p = sub.add_parser("cat-file", help="print a stored object (commit rev or object hash)")
    p.add_argument("object")
    for cmd in ("diff", "check"):
        sub.choices[cmd].add_argument("--rev", action="store_true", help="OLD and NEW are commits (HEAD, HEAD~1, hash prefix) diffed through the trees")
    for p in sub.choices.values():
        p.add_argument("--repo", default=".", help="directory holding .dsdiff (default: current)")
    a = ap.parse_args(argv)
    return {"diff": cmd_diff, "apply": cmd_apply, "verify": cmd_verify, "check": cmd_check,
            "commit": cmd_commit, "log": cmd_log, "cat-file": cmd_cat_file}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
