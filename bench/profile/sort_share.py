"""Sorting inside flat diff at n = 1e6, gc off: the sorted() calls of validate and match, timed alone.

    python bench/profile/sort_share.py WORK_DIR    # -> sort_share.json
"""
import gc
import json
import os
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from dsdiff.diff import Rec  # noqa: E402
from dsdiff.jsonl import read_jsonl  # noqa: E402

gc.disable()
work = sys.argv[1]
old = [Rec.of(i, r) for i, r in enumerate(read_jsonl(os.path.join(work, "v1.jsonl")))]
new = [Rec.of(i, r) for i, r in enumerate(read_jsonl(os.path.join(work, "v2.jsonl")))]
out, pc = {}, time.perf_counter
by_id, by_c = defaultdict(list), defaultdict(list)
for recs in (old, new):
    a, b = defaultdict(list), defaultdict(list)
    for r in recs:
        a[r.id_key].append(r)
        b[r.content_key].append(r)
    t = pc(); sorted(a.items()); sorted(b.items()); out["validate: sorted(by_id/by_c.items()), per version"] = out.get("validate: sorted(by_id/by_c.items()), per version", 0) + pc() - t
for r in old:
    by_id[r.content_key].append(r)
for r in new:
    by_c[r.content_key].append(r)
order = lambda r: (r.id_key, r.path_key or "", r.raw_hash)
t = pc(); cks = sorted(set(by_id) | set(by_c)); out["match: sorted(all content keys)"] = pc() - t
t = pc()
for ck in cks:
    sorted(by_id.get(ck, ()), key=order)
    sorted(by_c.get(ck, ()), key=order)
out["match: per-content sorted(key=order), 2 per key"] = pc() - t
out["total"] = sum(out.values())
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "sort_share.json"), "w") as f:
    json.dump(out, f, indent=1, ensure_ascii=False)
print(json.dumps(out, indent=1, ensure_ascii=False))
