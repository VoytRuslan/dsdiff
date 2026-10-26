"""Synthetic dataset-manifest versions with a known ground truth.

Record n is derived from (seed, n); between versions every record is visited once
and its change (if any) is derived from (seed, step, n). Nothing is held in memory
except small per-step event lists, so the same code serves tiny and xl.
"""
import argparse
import bisect
import dataclasses
import datetime
import hashlib
import json
import os
import random
import sys
import unicodedata
from collections import Counter
from dataclasses import dataclass, field

from .jsonl import read_jsonl  # noqa: F401  (re-exported for tests)
from .normalize import canonical_lines, normalize_name, normalize_record  # noqa: F401

TAXONOMY = [
    "cat", "dog", "café", "bird", "car", "señal", "tree", "bus", "bike", "boat",
    "plane", "train", "truck", "horse", "sheep", "cow", "bear", "zebra", "giraffe",
    "apple", "banana", "pizza", "chair", "table", "couch", "bed", "laptop", "phone",
    "book", "clock", "vase",
]
GROUPS = ("train", "val", "test")
RESOLUTIONS = [(320, 240), (640, 480), (800, 600), (1024, 768), (1280, 720), (1920, 1080)]
SOURCES = [f"cam{k}" for k in range(1, 9)]
EPOCH = datetime.date(2026, 1, 1).toordinal()
NOISE_RULES = ("id_case", "hash_case", "backslash", "dot_slash",
               "label_order", "meta_order", "export_time", "nfd", "id_whitespace")

# partition of the unit interval used to pick a change for a record at a step
KINDS = [  # (kind, Config field, truth types)
    ("rename", "p_rename", ["renamed"]),
    ("relabel", "p_relabel", ["relabeled"]),
    ("rename_relabel", "p_rename_relabel", ["renamed", "relabeled"]),
    ("replace", "p_replace", ["content_replaced"]),
    ("move", "p_move", ["group_moved"]),
    ("meta", "p_meta", ["metadata_changed"]),
    ("delete", "p_delete", ["deleted"]),
    ("noise", "p_noise", ["cosmetic"]),
]
KIND_TYPES = {k: t for k, _, t in KINDS}


@dataclass
class Config:
    n: int = 10_000
    seed: int = 1
    versions: int = 2
    hash_bits: int = 64
    with_path: bool = False
    sort_output: bool = False
    dirty: bool = False
    split: tuple = (0.8, 0.1, 0.1)
    pad: int = 6
    shard: bool = True                # images/<2 hex>/img_N.jpg vs images/img_N.jpg
    p_rename: float = 0.03
    p_relabel: float = 0.03
    p_rename_relabel: float = 0.005
    p_replace: float = 0.01
    p_move: float = 0.01
    p_meta: float = 0.01
    p_delete: float = 0.01
    p_add: float = 0.02
    p_noise: float = 0.10
    noise_rules: tuple = NOISE_RULES  # the rules a noise change picks from
    p_dup_content: float = 0.005
    n_leaks: int = 5
    leak_at_version: int | None = None
    leak_direction: str = "both"      # copy leaks: "test->train", "train->test" or both (alternating)
    n_rename_cycles: int = 2
    n_dup_ids: int = 2
    n_conflicts: int = 3
    # preset-only knobs (not on the CLI)
    n_add: int | None = None          # overrides round(p_add * n)
    taxonomy: list = field(default_factory=lambda: list(TAXONOMY))
    fixed_groups: list | None = None  # group by record index
    change_groups: tuple | None = None  # only records in these groups change; added ones land in the first
    forced: dict = field(default_factory=dict)       # (step, n) -> (kind, arg)
    forced_dups: dict = field(default_factory=dict)  # n -> source n
    preset: str | None = None
    expected_verdict: str | None = None  # what the preset is meant to show: the diff's verdict code


def digest(seed, tag, *keys, size=16):
    s = "|".join(map(str, (seed, tag) + keys))
    return hashlib.blake2b(s.encode(), digest_size=size).digest()


def frac(d, k=0):
    return int.from_bytes(d[4 * k:4 * k + 4], "big") / 4294967296


def content_hash(seed, cs, bits):
    nbytes = (bits + 7) // 8
    d = hashlib.blake2b(f"{seed}|content|{cs}".encode(), digest_size=nbytes).digest()
    v = int.from_bytes(d, "big") >> (nbytes * 8 - bits)
    return f"{v:0{(bits + 3) // 4}x}"


class VersionWriter:
    """Writes one version file; unless sorted, rows are shuffled via random buckets."""

    def __init__(self, path, seed, v, total, sort_output):
        self.path = path
        self.sort_output = sort_output
        self.count = 0
        if sort_output:
            self.nb = 1
            self.files = [open(path, "w", encoding="utf-8", newline="\n")]
        else:
            self.rng = random.Random(f"{seed}|shuffle|{v}")
            self.nb = total // 400_000 + 1
            self.files = [open(f"{path}.part{b}", "w", encoding="utf-8", newline="\n")
                          for b in range(self.nb)]

    def write(self, rec):
        line = json.dumps(rec, ensure_ascii=False) + "\n"
        self.count += 1
        if self.nb == 1:
            self.files[0].write(line)
        else:
            self.files[self.rng.randrange(self.nb)].write(line)

    def close(self):
        for f in self.files:
            f.close()
        if self.sort_output:
            return
        with open(self.path, "w", encoding="utf-8", newline="\n") as out:
            for b in range(self.nb):
                part = f"{self.path}.part{b}"
                with open(part, encoding="utf-8", newline="\n") as f:
                    lines = f.readlines()
                self.rng.shuffle(lines)
                out.writelines(lines)
                os.remove(part)


class Gen:
    def __init__(self, cfg):
        self.c = cfg
        self.seed = cfg.seed
        self.N = cfg.n
        self.K = cfg.versions
        self.A = int(cfg.p_add * cfg.n + 0.5) if cfg.n_add is None else cfg.n_add
        self.P = self.N + (self.K - 1) * self.A          # primary slots
        self.leak_base = self.P                           # leak slot numbers start here
        self.extra_base = self.P + (self.K - 1) * cfg.n_leaks  # conflict copies start here
        w = [1 / (r + 1) for r in range(len(cfg.taxonomy))]
        tot, acc, self.cum = sum(w), 0.0, []
        for x in w:
            acc += x / tot
            self.cum.append(acc)
        self.cum[-1] = 1.0
        self.split_th = [cfg.split[0], cfg.split[0] + cfg.split[1]]
        self.partition, acc = [], 0.0
        for kind, fld, _ in KINDS:
            acc += getattr(cfg, fld)
            self.partition.append((acc, kind))
        if acc > 1:
            raise ValueError("change probabilities sum to more than 1")
        self.raw_dup = set(cfg.forced_dups)
        if cfg.p_dup_content > 0:
            self.raw_dup |= {i for i in range(1, self.N) if frac(digest(self.seed, "dup", i)) < cfg.p_dup_content}
        self.dup_map = {}  # n -> j: record n shares content (and group/labels/meta) with record j in v1
        for n in sorted(self.raw_dup):
            j = cfg.forced_dups.get(n)
            if j is None:
                j = int.from_bytes(digest(self.seed, "dupsrc", n)[:8], "big") % n
            if j not in self.raw_dup:
                self.dup_map[n] = j
        self.reserved = {}
        for s, n in cfg.forced:
            self.reserved.setdefault(s, set()).add(n)
        self.pin_dup_ids, self.pin_conflicts = self.choose_pinned()
        self.pinned = set(self.pin_dup_ids) | set(self.pin_conflicts)
        self._cyc, self._leak = {}, {}
        self.skipped = []

    # ---- records -------------------------------------------------------

    def draw_labels(self, d, k):
        drawn = []
        for t in range(2, 10):
            cl = self.c.taxonomy[bisect.bisect_right(self.cum, frac(d, t))]
            if cl not in drawn:
                drawn.append(cl)
            if len(drawn) == k:
                break
        return drawn

    def attrs(self, n):
        c = self.c
        d = digest(self.seed, "attr", n, size=64)
        if c.fixed_groups and n < len(c.fixed_groups):
            group = c.fixed_groups[n]
        else:
            group = GROUPS[bisect.bisect_right(self.split_th, frac(d, 0))]
        if c.change_groups and n >= self.N and group not in c.change_groups:
            group = c.change_groups[0]
        u = frac(d, 1)
        drawn = self.draw_labels(d, 1 if u < 0.7 else 2 if u < 0.92 else 3)
        w, h = RESOLUTIONS[int(frac(d, 10) * len(RESOLUTIONS))]
        meta = {"width": w, "height": h,
                "source": SOURCES[int(frac(d, 11) * len(SOURCES))],
                "captured_at": datetime.date.fromordinal(EPOCH + int(frac(d, 12) * 300)).isoformat()}
        return {"group": group, "labels": sorted(drawn), "meta": meta}

    def make_name(self, n):
        stem = "fotó" if n % 20 == 14 else "img"  # a few non-ASCII names so the nfd rule has something to act on
        shard = f"{digest(self.seed, 'shard', n, size=1)[0]:02x}/" if self.c.shard else ""
        return f"images/{shard}{stem}_{n:0{self.c.pad}d}.jpg"

    def make(self, n, a, cs):
        return {"n": n, "name": self.make_name(n), "group": a["group"], "labels": a["labels"], "meta": a["meta"],
                "cs": cs, "h": content_hash(self.seed, cs, self.c.hash_bits), "rules": ()}

    def dup_source(self, n):
        return self.dup_map.get(n)

    def base(self, n):
        j = self.dup_source(n)
        if j is None:
            return self.make(n, self.attrs(n), f"c{n}")
        return self.make(n, self.attrs(j), f"c{j}")  # same group/labels/meta, own id

    def render(self, st):
        c, name, h, labels, meta, rules = self.c, st["name"], st["h"], st["labels"], st["meta"], st["rules"]
        if rules:
            if "id_case" in rules:
                name = name.upper()
            if "dot_slash" in rules:
                name = "./" + name
            if "backslash" in rules:
                name = name.replace("/", "\\")
            if "nfd" in rules:
                name = unicodedata.normalize("NFD", name)
            if "id_whitespace" in rules:  # after the others: the diff strips first, then normalizes
                name = f" {name}" if st["n"] % 2 else f"{name} "
            if "hash_case" in rules:
                h = h.upper()
            if "label_order" in rules:
                labels = labels[::-1]
            if "export_time" in rules:
                meta = dict(meta)
                meta["export_time"] = f"2026-10-21T{st['n'] % 24:02d}:{st['n'] % 60:02d}:{st['n'] * 7 % 60:02d}Z"
            if "meta_order" in rules:
                meta = dict(reversed(list(meta.items())))
        rec = {"sample_id": f"s_{st['n']:0{c.pad}d}" if c.with_path else name}
        if c.with_path:
            rec["path"] = name
        rec.update(content_hash=h, group=st["group"], labels=list(labels), metadata=meta)
        return rec

    # ---- per-step changes ---------------------------------------------

    def pick(self, s, n):
        u = frac(digest(self.seed, "sel", s, n))
        for end, kind in self.partition:
            if u < end:
                return kind
        return None

    def new_name(self, st, s):
        return f"{st['name'].rsplit('/', 1)[0]}/r{s}_{st['n']:0{self.c.pad}d}.jpg"

    def new_labels(self, st, s):
        n = st["n"]
        for a in range(10):
            d = digest(self.seed, "relabel", s, n, a, size=64)
            u = frac(d, 1)
            labels = sorted(self.draw_labels(d, 1 if u < 0.7 else 2 if u < 0.92 else 3))
            if labels != st["labels"]:
                return labels
        t = self.c.taxonomy
        return [t[(t.index(st["labels"][0]) + 1) % len(t)]]

    def new_group(self, st, s):
        u = frac(digest(self.seed, "move", s, st["n"]))
        g = st["group"]
        if g == "train":
            return "test" if u < 0.5 else "val"
        if g == "test":
            return "train" if u < 0.5 else "val"
        return "train" if u < 0.5 else "test"

    def new_meta(self, st, s):
        d = digest(self.seed, "meta", s, st["n"])
        m = dict(st["meta"])
        k = int(frac(d, 0) * 3)
        if k == 0:
            cur = RESOLUTIONS.index((m["width"], m["height"]))
            m["width"], m["height"] = RESOLUTIONS[(cur + 1 + int(frac(d, 1) * (len(RESOLUTIONS) - 1))) % len(RESOLUTIONS)]
        elif k == 1:
            cur = SOURCES.index(m["source"])
            m["source"] = SOURCES[(cur + 1 + int(frac(d, 1) * (len(SOURCES) - 1))) % len(SOURCES)]
        else:
            day = datetime.date.fromisoformat(m["captured_at"]).toordinal() + 1 + int(frac(d, 1) * 30)
            m["captured_at"] = datetime.date.fromordinal(day).isoformat()
        return m

    def rule_ok(self, st, rule):
        if rule in st["rules"]:
            return False
        if rule == "label_order":
            return len(st["labels"]) >= 2
        if rule == "nfd":
            return unicodedata.normalize("NFD", st["name"]) != st["name"]
        if rule == "hash_case":
            return any(ch in "abcdef" for ch in st["h"])  # an all-digit hash has no case to change
        return True

    def pick_rule(self, st, s):
        ok = [r for r in self.c.noise_rules if self.rule_ok(st, r)]
        return ok[int(frac(digest(self.seed, "rule", s, st["n"])) * len(ok))] if ok else None

    def step(self, st, s, n):
        """State of slot n at v_s -> (state at v_s+1 or None, truth types, extra). No types = unchanged."""
        if n in self.pinned:
            return st, [], {}
        ev = self.cycle_events(s).get(n)
        if ev:
            new = dict(st, name=ev["name"])
            return new, ["renamed", "rename_cycle"], ev["extra"]
        kind, arg = self.c.forced.get((s, n), (None, None))
        if kind is None:
            if self.c.change_groups and st["group"] not in self.c.change_groups:
                return st, [], {}
            kind = self.pick(s, n)
        if kind is None or kind == "keep":  # "keep" only reserves a slot in a preset
            return st, [], {}
        new, extra = dict(st), {}
        if kind == "delete":
            return None, ["deleted"], {}
        if kind in ("rename", "rename_relabel"):
            new["name"] = self.new_name(st, s)
        if kind in ("relabel", "rename_relabel"):
            new["labels"] = self.new_labels(st, s)
        if kind == "replace":
            new["cs"] = f"r{s}.{n}"
            new["h"] = content_hash(self.seed, new["cs"], self.c.hash_bits)
        if kind == "move":
            new["group"] = arg or self.new_group(st, s)
        if kind == "meta":
            new["meta"] = self.new_meta(st, s)
        if kind == "noise":
            if arg is not None and not self.rule_ok(st, arg):
                raise ValueError(f"noise rule {arg!r} is not applicable to slot {n}")
            rule = arg or self.pick_rule(st, s)
            if rule is None:
                return st, [], {}
            new["rules"] = st["rules"] + (rule,)
            extra = {"rule": rule}
        types = KIND_TYPES[kind]
        if kind == "move" and st["group"] == "train" and new["group"] == "test":
            types = ["group_moved", "leak_by_move"]  # the model has seen this example in training
        return new, types, extra

    def state_at(self, n, v):
        """State of primary slot n (n < N) in version v, or None if absent."""
        st = self.base(n)
        for s in range(1, v):
            st, _, _ = self.step(st, s, n)
            if st is None:
                return None
        return st

    def probe(self, keys, used, accept):
        """Scan from a hashed start index for a slot accepted by accept(j) -> state | None."""
        start = int.from_bytes(digest(self.seed, "probe", *keys)[:8], "big") % self.N
        for off in range(min(self.N, 20_000)):
            j = (start + off) % self.N
            if j in used or j in self.pinned:
                continue
            st = accept(j)
            if st is not None:
                used.add(j)
                return j, st
        return None

    def choose_pinned(self):
        """Slots reserved for dirty-mode validation errors; they never change."""
        c, taken, dup_ids, conflicts = self.c, set(), [], []
        if not c.dirty:
            return dup_ids, conflicts
        for target, count, tag in ((dup_ids, c.n_dup_ids, "pin-id"), (conflicts, c.n_conflicts, "pin-cf")):
            for k in range(count):
                start = int.from_bytes(digest(self.seed, tag, k)[:8], "big") % self.N
                for off in range(self.N):
                    j = (start + off) % self.N
                    if j not in taken and j not in self.raw_dup:
                        taken.add(j)
                        target.append(j)
                        break
        return dup_ids, conflicts

    def cycle_events(self, s):
        """{slot: {"name", "extra"}} for the swaps / chains at step s."""
        if s in self._cyc:
            return self._cyc[s]
        ev = {}
        used = set(self.reserved.get(s, ()))
        for ci in range(self.c.n_rename_cycles):
            members = []
            for slot in range(2 if ci % 2 == 0 else 3):
                def accept(j):
                    st = self.state_at(j, s)
                    if st is None or st["rules"]:
                        return None
                    if self.c.change_groups and st["group"] not in self.c.change_groups:
                        return None
                    return st
                found = self.probe(("cyc", s, ci, slot), used, accept)
                if found is None:
                    break
                members.append(found[1])
            want = 2 if ci % 2 == 0 else 3
            if len(members) < want:
                self.skipped.append({"what": "rename_cycle", "step": s, "index": ci})
                continue
            names = [m["name"] for m in members]
            targets = names[1:] + ([names[0]] if want == 2 else [self.new_name(members[-1], s)])
            kind = "swap" if want == 2 else "chain"
            for pos, (m, name) in enumerate(zip(members, targets)):
                ev[m["n"]] = {"name": name, "extra": {"cycle_id": ci, "kind": kind, "position": pos}}
        self._cyc[s] = ev
        return ev

    def leak_events(self, s):
        """[(slot, initial state, extra)] for the leaks that appear in v_s+1."""
        if s in self._leak:
            return self._leak[s]
        c, evs = self.c, []
        self._leak[s] = evs
        if c.leak_at_version is not None and s + 1 != c.leak_at_version:
            return evs
        self.cycle_events(s)
        used = set()
        reserved = self.reserved.get(s, set())
        dirs = ["test->train", "train->test"] if c.leak_direction == "both" else [c.leak_direction]
        for k in range(c.n_leaks):
            direction = dirs[k % len(dirs)]
            src_group, dst_group = direction.split("->")

            def accept(j):  # was in src_group in v_s and is untouched by step s
                if j in reserved:
                    return None
                st = self.state_at(j, s)
                if st is None or st["group"] != src_group or self.step(st, s, j)[1]:
                    return None
                return st
            found = self.probe(("leak", s, k), used, accept)
            if found is None:
                self.skipped.append({"what": "leak", "step": s, "index": k})
                continue
            src = found[1]
            n = self.leak_base + (s - 1) * c.n_leaks + k
            st = {"n": n, "name": self.make_name(n), "group": dst_group,
                  "labels": src["labels"], "meta": src["meta"], "cs": src["cs"], "h": src["h"], "rules": ()}
            raw = self.render(src)["sample_id"]
            extra = {"source_id": normalize_name(raw), "source_id_raw": raw, "source_group": src_group,
                     "direction": direction, "leak_version": s + 1}
            evs.append((n, st, extra))
        return evs

    # ---- output --------------------------------------------------------

    def emit(self, vi, st):
        rec = self.render(st)
        self.writers[vi].write(rec)
        if st["cs"] in self.cand:
            g = self.content_groups[vi + 1].setdefault(st["cs"], [st["h"], []])
            g[1].append((normalize_name(rec["sample_id"]), st["group"]))

    def truth(self, s, types, old, new, extra):
        ro = self.render(old) if old else None
        rn = self.render(new) if new else None
        oid_raw = ro["sample_id"] if ro else None
        nid_raw = rn["sample_id"] if rn else None
        oid = normalize_name(oid_raw) if ro else None
        nid = normalize_name(nid_raw) if rn else None
        d = {}
        if self.c.with_path and ro and rn and ro["path"] != rn["path"]:
            d["path_old"], d["path_new"] = normalize_name(ro["path"]), normalize_name(rn["path"])
            d["path_old_raw"], d["path_new_raw"] = ro["path"], rn["path"]
        if "relabeled" in types:
            d["labels_old"], d["labels_new"] = ro["labels"], rn["labels"]
        if "content_replaced" in types:
            d["hash_old"] = old["h"]
        if "group_moved" in types:
            d["group_old"], d["group_new"] = old["group"], new["group"]
            d["direction"] = f"{old['group']}->{new['group']}"
        if "leak_by_move" in types:
            self.leaks.append({"kind": "move", "version": s + 1, "content_hash": old["h"], "id": oid})
        if "metadata_changed" in types:
            d["metadata_old"], d["metadata_new"] = ro["metadata"], rn["metadata"]
        if "added" in types:
            d["record"] = rn
        d.update(extra)
        self.change_id[s] += 1
        cid = self.change_id[s]
        if "renamed" in types:
            d["content_shared"] = d["ambiguous"] = False  # filled in by patch_truth after the pass
            self.pending_renames[s][cid] = (old["cs"], old["h"])
        if "leak_by_move" in types and old["cs"] in self.cand:
            self.pending_moves[s][cid] = (old["cs"], nid)
        row = {"change_id": cid, "types": types, "old_id": oid, "new_id": nid,
               "old_id_raw": oid_raw, "new_id_raw": nid_raw,
               "content_hash": (new or old)["h"], "details": d}
        self.truth_files[s].write(json.dumps(row, ensure_ascii=False) + "\n")
        self.counts[s]["+".join(types)] += 1
        for t in types:
            self.type_counts[s][t] += 1

    def patch_truth(self, out):
        """Fill in what is known only after the pass.

        Renames: content_shared if the content has several ids in the old or new version;
        ambiguous by the diff's own rule: among the changed/deleted/added (non-cosmetic) rows of
        the hash, pair off records with the same canonical id and hash on both sides; of the old
        candidates left keep those whose id is gone from the new version (the others will be
        content_replaced) unless none is; ambiguous if more than one candidate is left on the old
        or the new side. Train->test moves: add "leak" if the
        moved record now shares its content with a train record.
        """
        for s in range(1, self.K):
            renames, moves = self.pending_renames[s], self.pending_moves[s]
            if not renames and not moves:
                continue
            watch = {h for _, h in renames.values()}
            old_keys, new_keys = Counter(), Counter()  # (hash, canonical id) -> count
            path = os.path.join(out, f"truth_v{s}_v{s + 1}.jsonl")
            with open(path, encoding="utf-8", newline="\n") as f:
                for line in f:
                    row = json.loads(line)
                    if row["types"] == ["cosmetic"]:
                        continue
                    if row["old_id"] is not None:
                        h = row["details"].get("hash_old", row["content_hash"]).lower()
                        if h in watch:
                            old_keys[h, row["old_id"]] += 1
                    if row["new_id"] is not None:
                        h = row["content_hash"].lower()
                        if h in watch:
                            new_keys[h, row["new_id"]] += 1
            old_left, new_side = old_keys - new_keys, Counter()  # new_side: hash -> candidates left
            for (h, _), k in (new_keys - old_keys).items():
                new_side[h] += k
            cand_ids = {i for _, i in old_left}
            kept = set()  # old candidate ids still present in the new version
            if cand_ids:
                with open(os.path.join(out, f"v{s + 1}.jsonl"), encoding="utf-8") as f:
                    for line in f:
                        i = normalize_name(json.loads(line)["sample_id"])
                        if i in cand_ids:
                            kept.add(i)
            all_old, gone_old = Counter(), Counter()
            for (h, i), k in old_left.items():
                all_old[h] += k
                if i not in kept:
                    gone_old[h] += k
            old_side = {h: gone_old[h] or all_old[h] for h in all_old}
            groups_old, groups_new = self.content_groups[s], self.content_groups[s + 1]
            with open(path, encoding="utf-8", newline="\n") as f, \
                    open(path + ".tmp", "w", encoding="utf-8", newline="\n") as o:
                for line in f:
                    cid = int(line[14:line.index(",")])
                    if cid in renames:
                        row = json.loads(line)
                        cs, h = renames[cid]
                        row["details"]["content_shared"] = any(
                            len(g.get(cs, (0, ()))[1]) >= 2 for g in (groups_old, groups_new))
                        row["details"]["ambiguous"] = old_side.get(h, 0) > 1 or new_side[h] > 1
                        line = json.dumps(row, ensure_ascii=False) + "\n"
                    elif cid in moves:
                        cs, nid = moves[cid]
                        if any(i != nid and g == "train" for i, g in groups_new.get(cs, (0, ()))[1]):
                            row = json.loads(line)
                            self.counts[s]["+".join(row["types"])] -= 1
                            row["types"].append("leak")
                            self.counts[s]["+".join(row["types"])] += 1
                            self.type_counts[s]["leak"] += 1
                            line = json.dumps(row, ensure_ascii=False) + "\n"
                    o.write(line)
            os.replace(path + ".tmp", path)
            self.counts[s] += Counter()  # drop zero entries

    def run_slot(self, n, st, birth, types=None, extra=None):
        self.emit(birth, st)
        if birth:
            self.truth(birth, types or ["added"], None, st, extra or {})
        for s in range(birth + 1, self.K):
            new, types, extra = self.step(st, s, n)
            if types:
                self.truth(s, types, st, new, extra)
            if new is None:
                return
            st = new
            self.emit(s, st)

    def run(self, out):
        c, K = self.c, self.K
        os.makedirs(out, exist_ok=True)
        self.writers = [VersionWriter(os.path.join(out, f"v{v + 1}.jsonl"), c.seed, v + 1, self.P, c.sort_output)
                        for v in range(K)]
        self.truth_files = {s: open(os.path.join(out, f"truth_v{s}_v{s + 1}.jsonl"), "w", encoding="utf-8", newline="\n")
                            for s in range(1, K)}
        self.change_id = Counter()
        self.counts = {s: Counter() for s in range(1, K)}
        self.type_counts = {s: Counter() for s in range(1, K)}
        self.leaks = []
        self.pending_renames = {s: {} for s in range(1, K)}
        self.pending_moves = {s: {} for s in range(1, K)}
        self.content_groups = {v: {} for v in range(1, K + 1)}
        # contents that may appear under several ids: v1 duplicates, leak sources, conflict pairs
        self.cand = {f"c{j}" for j in self.dup_map.values()}
        for s in range(1, K):
            self.cand |= {st["cs"] for _, st, _ in self.leak_events(s)}
        self.cand |= {self.base(j)["cs"] for j in self.pin_conflicts}
        for n in range(self.P):
            birth = 0 if n < self.N else (n - self.N) // self.A + 1
            self.run_slot(n, self.base(n), birth)
        for s in range(1, K):
            for n, st, extra in self.leak_events(s):
                self.run_slot(n, st, s, ["added", "leak"], extra)
                copy_id = normalize_name(self.render(st)["sample_id"])
                train_id, test_id = ((copy_id, extra["source_id"]) if st["group"] == "train"
                                     else (extra["source_id"], copy_id))
                self.leaks.append({"kind": "copy", "direction": extra["direction"], "version": s + 1,
                                   "content_hash": st["h"], "train_id": train_id, "test_id": test_id})
        errors = self.write_dirty()
        for w in self.writers:
            w.close()
        for f in self.truth_files.values():
            f.close()
        self.patch_truth(out)
        summary = {
            "preset": c.preset, "seed": c.seed,
            "params": {k: v for k, v in dataclasses.asdict(c).items()
                       if k not in ("forced", "forced_dups", "taxonomy", "fixed_groups")},
            "n_records": {f"v{v + 1}": w.count for v, w in enumerate(self.writers)},
            "changes_by_combination": {f"v{s}_v{s + 1}": dict(sorted(self.counts[s].items())) for s in range(1, K)},
            "changes_by_type": {f"v{s}_v{s + 1}": dict(sorted(self.type_counts[s].items())) for s in range(1, K)},
            "duplicate_content_records_v1": len(self.dup_map),
            "leaks": sorted(self.leaks, key=lambda x: json.dumps(x, sort_keys=True)),
            "expected_validation_errors": errors,
            "skipped": self.skipped,
        }
        for v in range(1, K + 1):  # true content groups, not hash collisions
            summary[f"duplicates_v{v}"] = sorted(
                ({"content_hash": h, "ids": sorted(i for i, _ in m)} for h, m in self.content_groups[v].values()
                 if len(m) >= 2),
                key=lambda x: (x["content_hash"], x["ids"]))
        for v in range(1, K + 1):  # contents present in both train and test of the same version
            summary[f"leaks_v{v}"] = sorted(
                ({"content_hash": h, "records": [{"id": i, "group": g} for i, g in sorted(m)]}
                 for h, m in self.content_groups[v].values() if {"train", "test"} <= {g for _, g in m}),
                key=lambda x: (x["content_hash"], [r["id"] for r in x["records"]]))
        with open(os.path.join(out, "truth_summary.json"), "w", encoding="utf-8", newline="\n") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
            f.write("\n")
        return summary

    def write_dirty(self):
        """Constant extra rows emitted into every version: duplicate sample_ids, conflicting labels."""
        errors = []
        for k, j in enumerate(self.pin_dup_ids):
            st = self.base(j)
            clone = dict(st, cs=f"dupid{k}", h=content_hash(self.seed, f"dupid{k}", self.c.hash_bits))
            if self.c.with_path:
                clone["name"] = self.new_name(st, 0)
            for vi in range(self.K):
                self.emit(vi, clone)
            errors.append({"type": "duplicate_sample_id", "sample_id": self.render(st)["sample_id"], "versions": "all"})
        for k, j in enumerate(self.pin_conflicts):
            st = self.base(j)
            n2 = self.extra_base + k
            labels = self.new_labels(st, 0)
            clone = dict(st, n=n2, labels=labels, name=self.make_name(n2))
            for vi in range(self.K):
                self.emit(vi, clone)
            errors.append({"type": "conflicting_labels", "content_hash": st["h"],
                           "sample_ids": [self.render(st)["sample_id"], self.render(clone)["sample_id"]],
                           "labels": [st["labels"], labels], "versions": "all"})
        return errors


# ---- reference implementation of the diff ---------------------------------

def reference_apply(v1, truth):
    """Apply the truth rows to the v1 records; cosmetic rows are no-ops (compare semantically)."""
    by_id, touched = {}, set()
    for r in v1:
        by_id.setdefault(r["sample_id"], r)
    new_records = []
    for row in truth:
        t, d = row["types"], row["details"]
        if "added" in t:
            new_records.append(d["record"])
            continue
        old = row["old_id_raw"]
        touched.add(old)
        if "deleted" in t or "cosmetic" in t:
            if "cosmetic" in t:
                new_records.append(by_id[old])
            continue
        rec = dict(by_id[old])
        if "renamed" in t:
            if "path_new_raw" in d:
                rec["path"] = d["path_new_raw"]
            else:
                rec["sample_id"] = row["new_id_raw"]
        if "relabeled" in t:
            rec["labels"] = d["labels_new"]
        if "content_replaced" in t:
            rec["content_hash"] = row["content_hash"]
        if "group_moved" in t:
            rec["group"] = d["group_new"]
        if "metadata_changed" in t:
            rec["metadata"] = d["metadata_new"]
        new_records.append(rec)
    return [r for r in v1 if r["sample_id"] not in touched] + new_records


# ---- presets and CLI -------------------------------------------------------

def tiny_config():
    forced = {
        (1, 0): ("rename", None), (1, 1): ("relabel", None), (1, 2): ("rename_relabel", None),
        (1, 3): ("replace", None), (1, 4): ("move", "test"), (1, 23): ("move", "train"),
        (1, 5): ("meta", None), (1, 6): ("delete", None), (1, 16): ("rename", None),
        (1, 15): ("keep", None),
        (1, 7): ("noise", "id_case"), (1, 8): ("noise", "hash_case"), (1, 9): ("noise", "backslash"),
        (1, 10): ("noise", "dot_slash"), (1, 12): ("noise", "label_order"), (1, 11): ("noise", "meta_order"),
        (1, 13): ("noise", "export_time"), (1, 14): ("noise", "nfd"), (1, 32): ("noise", "id_whitespace"),
        (1, 26): ("rename", None), (1, 27): ("rename", None),  # both copies of a duplicate renamed: ambiguous
        (1, 28): ("keep", None), (1, 29): ("move", "test"),     # one copy moves to test: leak_by_move + leak
    }
    return Config(
        n=33, seed=1, hash_bits=32, pad=3, shard=False, sort_output=True, preset="tiny",
        n_add=1, n_leaks=2, n_rename_cycles=2,
        p_rename=0, p_relabel=0, p_rename_relabel=0, p_replace=0, p_move=0, p_meta=0, p_delete=0,
        p_add=0, p_noise=0, p_dup_content=0,
        taxonomy=["cat", "dog", "café", "bird", "car", "señal"],
        fixed_groups=["train"] * 20 + ["val"] * 3 + ["test"] * 3 + ["train"] * 6,
        forced=forced, forced_dups={16: 15, 27: 26, 29: 28})


def story_config():
    """The customer's story: the model was not retrained, yet its metric on v2 went up. ~10% of the
    test set are now training examples: half moved from train (leak_by_move), half copied (train->test)."""
    cfg = Config(n=10_000, seed=7, preset="story", expected_verdict="do_not_trust", leak_at_version=2, leak_direction="train->test",
                 n_rename_cycles=0, p_rename=0, p_relabel=0, p_rename_relabel=0, p_replace=0, p_move=0,
                 p_meta=0.002, p_delete=0, p_add=0, p_noise=0.02, p_dup_content=0)
    g = Gen(cfg)
    groups = [g.attrs(i)["group"] for i in range(cfg.n)]
    n_leak = int(0.1 * groups.count("test") + 0.5)
    n_move = n_leak // 2
    cfg.n_leaks = n_leak - n_move
    train = sorted((i for i, gr in enumerate(groups) if gr == "train"),
                   key=lambda i: digest(cfg.seed, "story-move", i))
    cfg.forced = {(1, i): ("move", "test") for i in train[:n_move]}
    return cfg


def clean_config():
    """Changes only in train, no leaks, test (and val) untouched: verdict "trust"."""
    return Config(n=10_000, seed=11, preset="clean", expected_verdict="trust", change_groups=("train",), p_move=0,
                  n_leaks=0)


def test_relabel_config():
    """As clean, plus ~5% of test relabeled and nothing else in test: verdict "metrics not comparable"."""
    cfg = Config(n=10_000, seed=12, preset="test_relabel", expected_verdict="metrics_not_comparable",
                 change_groups=("train",), p_move=0, n_leaks=0)
    g = Gen(cfg)
    test = sorted((i for i in range(cfg.n) if g.attrs(i)["group"] == "test"),
                  key=lambda i: digest(cfg.seed, "relabel-test", i))
    cfg.forced = {(1, i): ("relabel", None) for i in test[:len(test) // 20]}
    return cfg


def test_to_train_config():
    """Copies of test examples land in train, nothing moves: verdict "do not retrain"."""
    return Config(n=10_000, seed=21, preset="test_to_train", expected_verdict="do_not_retrain",
                  leak_direction="test->train", p_move=0)


QUIET = dict(p_rename=0, p_relabel=0, p_rename_relabel=0, p_replace=0, p_move=0, p_meta=0, p_delete=0, p_add=0,
             p_noise=0, p_dup_content=0, n_leaks=0, n_rename_cycles=0)
ID_RULES = ("id_case", "backslash", "dot_slash", "nfd", "id_whitespace")
BREAKS = {  # "what breaks": one phenomenon on a quiet base each, preset -> (label, verdict, Config overrides)
    "break_swaps": ("обмены имён", "trust", dict(n_rename_cycles=100)),
    "break_id_noise": ("шум в id", "trust", dict(p_noise=0.05, noise_rules=ID_RULES)),
    "break_train_to_test": ("перенос train→test", "do_not_trust", dict(p_move=0.02)),
    "break_test_relabel": ("смена меток в test", "metrics_not_comparable", None),  # test_relabel, quieted
}


def break_config(name):
    """A BREAKS scenario; its expected verdict is set by what the scenario is, not computed from the truth.
    Swaps and noisy ids change nothing that matters (trust); records moved from train to test were
    trained on (do not trust); relabeled test examples change the metric (not comparable)."""
    _, verdict, extra = BREAKS[name]
    if extra is None:
        cfg = test_relabel_config()
        for k, v in QUIET.items():
            setattr(cfg, k, v)
    else:
        cfg = Config(n=10_000, seed=5, **QUIET)
        for k, v in extra.items():
            setattr(cfg, k, v)
    cfg.preset, cfg.expected_verdict = name, verdict
    return cfg


def preset_configs(name):
    """[(subdir or '', Config)] for a preset."""
    sizes = {"small": 10_000, "medium": 100_000, "large": 1_000_000, "xl": 10_000_000}
    if name == "tiny":
        return [("", tiny_config())]
    if name == "story":
        return [("", story_config())]
    if name == "clean":
        return [("", clean_config())]
    if name == "test_relabel":
        return [("", test_relabel_config())]
    if name == "test_to_train":
        return [("", test_to_train_config())]
    if name in BREAKS:
        return [("", break_config(name))]
    if name in sizes:
        return [("", Config(n=sizes[name], preset=name))]
    if name == "collisions":
        return [(f"h{b}", Config(n=100_000, hash_bits=b, preset=name)) for b in (16, 24, 32, 64)]
    raise ValueError(f"unknown preset {name!r}")


def default_out(preset):
    return os.path.join("examples" if preset in ("tiny", "story") else "data", preset)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m dsdiff.gen", description=__doc__.splitlines()[0])
    ap.add_argument("--preset", choices=["tiny", "story", "clean", "test_relabel", "test_to_train",
                                         "small", "medium", "large", "xl", "collisions", *BREAKS])
    ap.add_argument("--out")
    for opt, typ in [("n", int), ("seed", int), ("versions", int), ("hash-bits", int), ("leak-at-version", int),
                     ("p-rename", float), ("p-relabel", float), ("p-rename-relabel", float),
                     ("p-replace", float), ("p-move", float), ("p-meta", float), ("p-delete", float),
                     ("p-add", float), ("p-noise", float), ("p-dup-content", float),
                     ("n-leaks", int), ("n-rename-cycles", int), ("n-dup-ids", int), ("n-conflicts", int)]:
        ap.add_argument("--" + opt, type=typ, default=None)
    ap.add_argument("--noise-rules", nargs="+", choices=NOISE_RULES, default=None, type=str)
    ap.add_argument("--leak-direction", choices=["both", "train->test", "test->train"], default=None)
    ap.add_argument("--with-path", action="store_true", default=None)
    ap.add_argument("--sorted", dest="sort_output", action="store_true", default=None,
                    help="keep generation (slot) order instead of shuffling rows")
    ap.add_argument("--dirty", action="store_true", default=None)
    args = vars(ap.parse_args(argv))
    preset, out = args.pop("preset"), args.pop("out")
    if args["noise_rules"]:
        args["noise_rules"] = tuple(args["noise_rules"])
    overrides = {k: v for k, v in args.items() if v is not None}
    runs = preset_configs(preset) if preset else [("", Config())]
    out = out or (default_out(preset) if preset else "out")
    for sub, cfg in runs:
        for k, v in overrides.items():
            if not (preset == "collisions" and k == "hash_bits"):
                setattr(cfg, k, v)
        if cfg.leak_at_version is not None and not 2 <= cfg.leak_at_version <= cfg.versions:
            ap.error("--leak-at-version must be between 2 and --versions")
        target = os.path.join(out, sub)
        summary = Gen(cfg).run(target)
        print(f"{target}: records {summary['n_records']}, changes {summary['changes_by_combination']}")
        if summary["skipped"]:
            print("  skipped:", summary["skipped"], file=sys.stderr)


if __name__ == "__main__":
    main()
