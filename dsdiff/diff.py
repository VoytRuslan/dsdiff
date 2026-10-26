"""Semantic diff of two manifest versions: match by content hash first, then by id."""
import os
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from .normalize import keys, multiset_digest

TYPE_ORDER = ("renamed", "relabeled", "content_replaced", "group_moved", "metadata_changed",
              "cosmetic", "added", "deleted")
VERDICTS = {  # code -> (label, severity)
    "do_not_trust": ("не доверять", 3),
    "do_not_retrain": ("не переобучать на этой версии", 2),
    "metrics_not_comparable": ("метрики несопоставимы", 1),
    "trust": ("доверять", 0),
}


@dataclass
class DiffConfig:
    examples: int = 5          # ids listed per verdict reason / validation error


@dataclass
class Rec:
    pos: int                   # line index in its version (informational; nothing depends on it)
    raw: dict                  # record as read
    norm: dict
    id_key: str
    content_key: str
    payload_hash: str
    raw_hash: str

    @classmethod
    def of(cls, pos, raw):
        return cls(pos, raw, *keys(raw))

    @property
    def path_key(self):
        return self.norm.get("path")


@dataclass
class Change:
    types: list
    old: Rec | None
    new: Rec | None
    ambiguous: bool = False
    candidates: list = field(default_factory=list)  # old ids the rename could have come from (if ambiguous)

    def details(self):
        o, n, d = self.old, self.new, {}
        if "renamed" in self.types:
            if o.path_key != n.path_key:
                d["path_old"], d["path_new"] = o.path_key, n.path_key
            d["ambiguous"] = self.ambiguous
            if self.candidates:
                d["candidates"] = self.candidates
        if "relabeled" in self.types:
            d["labels_old"], d["labels_new"] = o.norm["labels"], n.norm["labels"]
        if "content_replaced" in self.types:
            d["hash_old"] = o.content_key
        if "group_moved" in self.types:
            d["group_old"], d["group_new"] = o.norm["group"], n.norm["group"]
            d["direction"] = f"{o.norm['group']}->{n.norm['group']}"
        if "metadata_changed" in self.types:
            d["metadata_old"], d["metadata_new"] = o.norm["metadata"], n.norm["metadata"]
        if "added" in self.types:
            d["record"] = n.raw
        return d

    def to_json(self):
        o, n = self.old, self.new
        return {"types": self.types,
                "old_id": o.id_key if o else None, "new_id": n.id_key if n else None,
                "old_id_raw": o.raw["sample_id"] if o else None, "new_id_raw": n.raw["sample_id"] if n else None,
                "content_hash": (n or o).content_key, "details": self.details()}


@dataclass
class DiffResult:
    n_old: int
    n_new: int
    unchanged: int
    changes: list              # [Change], sorted by (old_id, new_id)
    validation: dict           # {"old": [...], "new": [...]}
    leaks: dict                # {"within": [...], "by_move": [...]}
    verdict: dict              # {"code", "label", "reasons": [...]}
    timings: dict = field(default_factory=dict)  # stage -> seconds; not part of the report
    old_digest: str = ""       # multiset_digest of each version, for the patch header
    new_digest: str = ""
    stats: dict = field(default_factory=dict)  # tree diff: buckets / records / objects read

    def counts(self):
        by_type, by_combo = Counter(), Counter()
        for c in self.changes:
            by_combo["+".join(c.types)] += 1
            by_type.update(c.types)
        return dict(sorted(by_type.items())), dict(sorted(by_combo.items()))

    def to_report(self):
        by_type, by_combo = self.counts()
        return {
            "summary": {"n_old": self.n_old, "n_new": self.n_new, "unchanged": self.unchanged,
                        "changes_by_type": by_type, "changes_by_combination": by_combo,
                        "ambiguous_renames": sum(c.ambiguous for c in self.changes)},
            "verdict": self.verdict,
            "leaks": self.leaks,
            "validation": self.validation,
            "changes": [c.to_json() for c in self.changes],
        }


def classify(o, n):
    t = []
    if o.id_key != n.id_key or o.path_key != n.path_key:
        t.append("renamed")
    if o.norm["labels"] != n.norm["labels"]:
        t.append("relabeled")
    if o.content_key != n.content_key:
        t.append("content_replaced")
    if o.norm["group"] != n.norm["group"]:
        t.append("group_moved")
    if o.norm["metadata"] != n.norm["metadata"]:
        t.append("metadata_changed")
    if not t and o.raw_hash != n.raw_hash:
        t.append("cosmetic")
    return t


def validate(recs, cfg):
    """Duplicate ids and conflicting labels inside one version."""
    errors = []
    by_id = defaultdict(list)
    for r in recs:
        by_id[r.id_key].append(r)
    for i, rs in sorted(by_id.items()):
        if len(rs) > 1:
            errors.append({"type": "duplicate_sample_id", "sample_id": i, "count": len(rs),
                           "content_hashes": sorted({r.content_key for r in rs})})
    by_c = defaultdict(list)
    for r in recs:
        by_c[r.content_key].append(r)
    for c, rs in sorted(by_c.items()):
        labels = {tuple(r.norm["labels"]) for r in rs}
        if len(labels) > 1:
            errors.append({"type": "conflicting_labels", "content_hash": c,
                           "sample_ids": sorted(r.id_key for r in rs)[:cfg.examples],
                           "labels": sorted(map(list, labels))})
    return errors


STORED_EXAMPLES = 100  # ids kept per validation error in a commit; --examples up to this matches the flat diff


def trim_validation(errors, k):
    """Validation errors as validate(..., examples=k) would give them, from a list made with more examples."""
    return [dict(e, sample_ids=e["sample_ids"][:k]) if e["type"] == "conflicting_labels" else e for e in errors]


def name_parts(r):
    """(directory, token) of the record's name: token is the last digit run of the stem, else the stem."""
    name = r.path_key or r.id_key
    d, base = os.path.split(name)
    stem = os.path.splitext(base)[0]
    digits = re.findall(r"\d+", stem)
    return d, digits[-1] if digits else stem


def similarity(o, n):
    """Higher is more alike: shared number/stem first, then shared directory."""
    (od, ot), (nd, nt) = name_parts(o), name_parts(n)
    return (ot == nt, od == nd)


def match(old, new):
    """[(old Rec | None, new Rec | None, ambiguous, candidates)] covering every record once."""
    new_ids = {r.id_key for r in new}
    by_c_old, by_c_new = defaultdict(list), defaultdict(list)
    for r in old:
        by_c_old[r.content_key].append(r)
    for r in new:
        by_c_new[r.content_key].append(r)
    order = lambda r: (r.id_key, r.path_key or "", r.raw_hash)
    pairs, left_old, left_new = [], [], []
    for ck in sorted(set(by_c_old) | set(by_c_new)):
        # 1. same content, same id
        pool = defaultdict(list)
        for o in sorted(by_c_old.get(ck, ()), key=order):
            pool[o.id_key].append(o)
        rest_new = []
        for n in sorted(by_c_new.get(ck, ()), key=order):
            if pool[n.id_key]:
                pairs.append((pool[n.id_key].pop(0), n, False, []))
            else:
                rest_new.append(n)
        rest_old = sorted((o for os_ in pool.values() for o in os_), key=order)
        if not rest_old or not rest_new:
            left_old += rest_old
            left_new += rest_new
            continue
        # 2. same content, another id: renames; prefer old records whose id is gone from the new version
        gone = [o for o in rest_old if o.id_key not in new_ids]
        cands = list(gone or rest_old)
        ambiguous = len(cands) > 1 or len(rest_new) > 1
        cand_ids = sorted({c.id_key for c in cands}) if ambiguous else []
        # most similar names first; ties by position in id_key order (cands and rest_new are sorted)
        scored = sorted((tuple(not x for x in similarity(o, n)), i, j)
                        for i, o in enumerate(cands) for j, n in enumerate(rest_new))
        used_o, used_n = set(), set()
        for _, i, j in scored:
            if i not in used_o and j not in used_n:
                used_o.add(i)
                used_n.add(j)
                pairs.append((cands[i], rest_new[j], ambiguous, cand_ids))
        taken = {id(cands[i]) for i in used_o}
        rest_old = [o for o in rest_old if id(o) not in taken]
        left_new += [n for j, n in enumerate(rest_new) if j not in used_n]
        left_old += rest_old
    # 3. what is left: same id, other content -> content_replaced; the rest is deleted / added
    pool = defaultdict(list)
    for o in sorted(left_old, key=order):
        pool[o.id_key].append(o)
    for n in sorted(left_new, key=order):
        if pool[n.id_key]:
            pairs.append((pool[n.id_key].pop(0), n, False, []))
        else:
            pairs.append((None, n, False, []))
    pairs += [(o, None, False, []) for os_ in pool.values() for o in os_]
    return pairs


def within_leaks(new):
    """[{"content_hash", "records": [{"id", "group"}]}]: contents in both train and test, sorted."""
    by_c = defaultdict(list)
    for r in new:
        if r.norm["group"] in ("train", "test"):
            by_c[r.content_key].append({"id": r.id_key, "group": r.norm["group"]})
    return [{"content_hash": ck, "records": sorted(m, key=lambda x: (x["id"], x["group"]))}
            for ck, m in sorted(by_c.items()) if {x["group"] for x in m} == {"train", "test"}]


def find_leaks(changes, within):
    """Add causes to the version's within-leaks (from the changes) and list train->test moves."""
    arrived = {}  # (id, content, group) of a new record -> how it got into its group
    for c in changes:
        if "added" in c.types or "group_moved" in c.types:
            n = c.new
            arrived[n.id_key, n.content_key, n.norm["group"]] = "copy" if "added" in c.types else "move"
    out = []
    for leak in within:
        causes = []
        for r in leak["records"]:
            how = arrived.get((r["id"], leak["content_hash"], r["group"]))
            if how:
                g = r["group"]
                causes.append({"id": r["id"], "cause": how, "direction": f"{'test' if g == 'train' else 'train'}->{g}"})
        out.append(dict(leak, causes=sorted(causes, key=lambda x: x["id"]) or None))
    by_move = [{"old_id": c.old.id_key, "new_id": c.new.id_key, "content_hash": c.new.content_key}
               for c in changes if "group_moved" in c.types
               and c.old.norm["group"] == "train" and c.new.norm["group"] == "test"]
    return {"within": out, "by_move": by_move}


def make_verdict(changes, leaks, cfg):
    k = cfg.examples
    reasons = []
    t2t = [x["new_id"] for x in leaks["by_move"]]
    t2t += [c["id"] for l in leaks["within"] for c in l["causes"] or () if c["direction"] == "train->test"]
    t2t = sorted(set(t2t))
    pre = [l["records"][0]["id"] for l in leaks["within"] if not l["causes"]]
    if t2t:
        reasons.append({"code": "do_not_trust", "reason_code": "leak_train_to_test", "count": len(t2t),
                        "examples": t2t[:k],
                        "reason": "в test попали примеры из train: модель на них училась, метрика завышена"})
    if pre:
        reasons.append({"code": "do_not_trust", "reason_code": "leak_preexisting", "count": len(pre),
                        "examples": sorted(pre)[:k],
                        "reason": "содержимое одновременно в train и test, и эта пара была уже в старой версии: "
                                  "метрика была завышена и раньше, скачок метрики этим не объясняется"})
    t2r = [c["id"] for l in leaks["within"] for c in l["causes"] or () if c["direction"] == "test->train"]
    if t2r:
        reasons.append({"code": "do_not_retrain", "reason_code": "leak_test_to_train", "count": len(t2r),
                        "examples": sorted(t2r)[:k],
                        "reason": "в train попали примеры из test: модель, обученная на этой версии, "
                                  "увидит тестовые примеры"})
    leaked = set(t2t)
    test_changed = []
    for c in changes:
        if c.types == ["cosmetic"] or c.types == ["metadata_changed"] or c.types == ["renamed"]:
            continue
        in_test = (c.old and c.old.norm["group"] == "test") or (c.new and c.new.norm["group"] == "test")
        if in_test and (c.new is None or c.new.id_key not in leaked):
            test_changed.append(f"{(c.new or c.old).id_key} ({'+'.join(c.types)})")
    if test_changed:
        reasons.append({"code": "metrics_not_comparable", "reason_code": "test_changed", "count": len(test_changed),
                        "examples": sorted(test_changed)[:k],
                        "reason": "test изменился (состав, метки или содержимое): метрики версий несопоставимы"})
    code = max((r["code"] for r in reasons), key=lambda x: VERDICTS[x][1], default="trust")
    if not reasons:
        reasons.append({"code": "trust", "reason_code": "no_test_change", "count": 0, "examples": [],
                        "reason": "test не менялся (кроме косметики и метаданных), утечек нет"})
    return {"code": code, "label": VERDICTS[code][0], "reasons": reasons}


class Laps:
    """Stage timer: laps("stage") records the seconds since the previous call."""

    def __init__(self):
        self.t, self.timings = time.perf_counter(), {}

    def __call__(self, stage):
        now = time.perf_counter()
        self.timings[stage] = self.timings.get(stage, 0) + now - self.t
        self.t = now


def compare(old, new):
    """Changes between two lists of Recs (whole versions or the differing buckets) -> (changes, unchanged)."""
    changes, unchanged = [], 0
    for o, n, amb, cands in match(old, new):
        if o is None:
            types = ["added"]
        elif n is None:
            types = ["deleted"]
        else:
            types = classify(o, n)
        if not types:
            unchanged += 1
            continue
        changes.append(Change(types, o, n, amb and "renamed" in types, cands if "renamed" in types else []))
    changes.sort(key=lambda c: (c.old.id_key if c.old else "\uffff", c.new.id_key if c.new else "",
                                c.old.raw_hash if c.old else "", c.new.raw_hash if c.new else ""))
    return changes, unchanged


def diff(old_records, new_records, cfg=None):
    cfg = cfg or DiffConfig()
    lap = Laps()
    old = [Rec.of(i, r) for i, r in enumerate(old_records)]
    new = [Rec.of(i, r) for i, r in enumerate(new_records)]
    lap("normalize")
    validation = {"old": validate(old, cfg), "new": validate(new, cfg)}
    lap("validate")
    changes, unchanged = compare(old, new)
    lap("match")
    leaks = find_leaks(changes, within_leaks(new))
    verdict = make_verdict(changes, leaks, cfg)
    lap("leaks_verdict")
    return DiffResult(len(old), len(new), unchanged, changes, validation, leaks, verdict, lap.timings,
                      multiset_digest(r.raw_hash for r in old), multiset_digest(r.raw_hash for r in new))
