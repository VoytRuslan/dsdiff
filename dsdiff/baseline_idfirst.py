"""Baseline "v0": id-first diff, after Olya's source/diff.py (the first version of the case).

    1. records of each version keyed by sample_id exactly as written (no id normalization; a
       repeated id keeps the last record)
    2. merge of the two sorted id lists: same id -> compare the values, otherwise deleted / added
    3. what is left goes by content_hash: a deleted id whose hash is carried by exactly one added
       id is a rename; several candidates -> "renamed_ambiguous", the records stay deleted / added
    4. leaks: a content_hash in both train and test in either version
    5. verdict: leaks -> do_not_trust; the set of test ids changed -> metrics_not_comparable; else trust

Values (not ids) are canonicalized as in her model.py: strings stripped and lowercased, "\\" -> "/",
lists sorted, dict keys sorted. One addition: a record whose raw line changed while its canonical
values did not is reported as "cosmetic" (her type list has it, her diff never emits it).
The output uses the change rows of dsdiff.diff (types, old_id, new_id, raw ids) so both can be
scored against the generator's truth.
"""
import json
from collections import defaultdict

VERDICT_LABELS = {"do_not_trust": "не доверять", "metrics_not_comparable": "метрики несопоставимы",
                  "trust": "доверять"}


def canonicalize(value):
    if isinstance(value, str):
        return value.strip().lower().replace("\\", "/")
    if isinstance(value, list):
        return sorted(canonicalize(v) for v in value)
    if isinstance(value, dict):
        return {k: canonicalize(v) for k, v in sorted(value.items())}
    return value


def canonical_record(rec):
    return {f: canonicalize(rec.get(f, d)) for f, d in
            (("content_hash", ""), ("group", ""), ("labels", []), ("metadata", {}))}


def classify(a, b):
    ca, cb = canonical_record(a), canonical_record(b)
    if ca == cb:
        return [] if json.dumps(a, ensure_ascii=False) == json.dumps(b, ensure_ascii=False) else ["cosmetic"]
    return [t for t, f in (("relabeled", "labels"), ("content_replaced", "content_hash"),
                           ("group_moved", "group"), ("metadata_changed", "metadata")) if ca[f] != cb[f]]


def _row(types, a, b):
    return {"types": types, "old_id": a and a["sample_id"], "new_id": b and b["sample_id"],
            "content_hash": (b or a)["content_hash"]}


def diff_idfirst(old, new):
    """-> {"changes": [...], "unchanged", "ambiguous": [(old id, [new ids])], "leaks": [...], "verdict"}"""
    v1 = {r["sample_id"]: r for r in old}
    v2 = {r["sample_id"]: r for r in new}
    ids1, ids2 = sorted(v1), sorted(v2)
    changes, deleted, added, unchanged = [], [], [], 0
    i = j = 0
    while i < len(ids1) or j < len(ids2):  # merge-join of the sorted id lists
        a = ids1[i] if i < len(ids1) else None
        b = ids2[j] if j < len(ids2) else None
        if a is not None and a == b:
            t = classify(v1[a], v2[b])
            if t:
                changes.append(_row(t, v1[a], v2[b]))
            else:
                unchanged += 1
            i, j = i + 1, j + 1
        elif b is None or (a is not None and a < b):
            deleted.append(a)
            i += 1
        else:
            added.append(b)
            j += 1

    by_hash = defaultdict(list)
    for sid in added:
        by_hash[canonicalize(v2[sid]["content_hash"])].append(sid)
    used, ambiguous = set(), []
    for sid in deleted:
        cands = [x for x in by_hash.get(canonicalize(v1[sid]["content_hash"]), ()) if x not in used]
        if len(cands) == 1:
            used.add(cands[0])
            t = [x for x in classify(v1[sid], v2[cands[0]]) if x != "cosmetic"]
            changes.append(_row(["renamed"] + t, v1[sid], v2[cands[0]]))
        else:
            if cands:
                ambiguous.append((sid, cands))
            changes.append(_row(["deleted"], v1[sid], None))
    changes += [_row(["added"], None, v2[sid]) for sid in added if sid not in used]

    leaks = find_leaks(v1, v2)
    test1 = {sid for sid, r in v1.items() if canonicalize(r["group"]) == "test"}
    test2 = {sid for sid, r in v2.items() if canonicalize(r["group"]) == "test"}
    code = "do_not_trust" if leaks else "metrics_not_comparable" if test1 != test2 else "trust"
    return {"changes": changes, "unchanged": unchanged, "ambiguous": ambiguous, "leaks": leaks,
            "verdict": {"code": code, "label": VERDICT_LABELS[code]}}


def find_leaks(v1, v2):
    """Contents present in both train and test in v1 or in v2 (her rule: either version counts)."""
    out = []
    for version in (v1, v2):
        groups = defaultdict(set)
        for r in version.values():
            groups[canonicalize(r["content_hash"])].add(canonicalize(r["group"]))
        out.append({h for h, g in groups.items() if {"train", "test"} <= g})
    return [{"content_hash": h} for h in sorted(out[0] | out[1])]
