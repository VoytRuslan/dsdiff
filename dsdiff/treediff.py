"""Diff of two commits through their trees: only buckets whose hashes differ are read.

Records of every differing id bucket on both sides are pooled and go through the same match as
the flat diff (a rename usually moves a record to another bucket, so matching by content has to
see all differing buckets at once). Records in equal buckets are unchanged by construction.
Within-version leaks come from the new commit's content tree, by_move leaks from the changes.
Commits with different bucket counts share no subtree, so they are compared by the flat diff of
their records.
"""
from .diff import (STORED_EXAMPLES, DiffConfig, DiffResult, Laps, Rec, compare, diff, find_leaks,
                   make_verdict, trim_validation)

FANOUT_BITS = 6


def differing_buckets(st, root_a, root_b):
    """[(bucket, leaf in A or None, leaf in B or None)], descending only where hashes differ."""
    out = []

    def walk(a, b, prefix):
        ta = st.tree(a) if a else {}
        tb = st.tree(b) if b else {}
        for slot in sorted(set(ta) | set(tb)):
            x, y = ta.get(slot), tb.get(slot)
            if x and y and x[1] == y[1]:
                continue
            key = prefix << FANOUT_BITS | slot
            if (x or y)[0] == "tree":
                walk(x[1] if x else None, y[1] if y else None, key)
            else:
                out.append((key, x[1] if x else None, y[1] if y else None))

    if root_a != root_b:
        walk(root_a, root_b, 0)
    return out


def tree_diff(st, rev_a, rev_b, cfg=None):
    cfg = cfg or DiffConfig()
    if cfg.examples > STORED_EXAMPLES:
        raise ValueError(f"commits keep {STORED_EXAMPLES} ids per validation error; --examples is {cfg.examples}")
    lap = Laps()
    ca, cb = st.commit_obj(st.resolve(rev_a)), st.commit_obj(st.resolve(rev_b))
    if ca["buckets"] != cb["buckets"]:
        old, new = st.checkout(rev_a), st.checkout(rev_b)
        lap("read")
        result = diff(old, new, cfg)
        result.timings = {**lap.timings, **result.timings}
        result.stats = {"mode": f"flat: {ca['buckets']} vs {cb['buckets']} buckets",
                        "buckets_read": st.stats["buckets_read"], "records_read": st.stats["records_read"],
                        "objects_read": st.stats["objects_read"]}
        return result
    diff_buckets = differing_buckets(st, ca["id_root"], cb["id_root"])
    lap("descend")
    raw_a = [r for _, a, _ in diff_buckets if a for r in st.bucket_records(a)]
    raw_b = [r for _, _, b in diff_buckets if b for r in st.bucket_records(b)]
    lap("read")
    old = [Rec.of(i, r) for i, r in enumerate(raw_a)]
    new = [Rec.of(i, r) for i, r in enumerate(raw_b)]
    lap("normalize")
    changes, unchanged = compare(old, new)
    unchanged += ca["counts"]["records"] - len(old)
    lap("match")
    within = st.leaks(cb["content_root"]) if cb["leaks_count"] else []
    leaks = find_leaks(changes, within)
    verdict = make_verdict(changes, leaks, cfg)
    lap("leaks_verdict")
    validation = {"old": trim_validation(ca["validation"], cfg.examples),
                  "new": trim_validation(cb["validation"], cfg.examples)}
    stats = {"mode": f"tree: {cb['buckets']} buckets", "buckets_differing": len(diff_buckets),
             "buckets_read": st.stats["buckets_read"], "records_read": st.stats["records_read"],
             "objects_read": st.stats["objects_read"]}
    return DiffResult(ca["counts"]["records"], cb["counts"]["records"], unchanged, changes,
                      validation, leaks, verdict, lap.timings, ca["digest"], cb["digest"], stats)
