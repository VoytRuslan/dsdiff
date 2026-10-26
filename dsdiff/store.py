"""Git-like store of manifest versions: content-addressed objects in <repo>/.dsdiff/objects.

Objects are "<type> <size>\\0<body>", named by the sha256 of those bytes and stored zlib-compressed
at objects/<2 hex>/<62 hex>, so an object shared by two versions is stored once.

    bucket   records whose H(id_key) falls in one of B buckets, sorted by id_key; body = raw
             JSON lines exactly as in the version file
    cbucket  content index leaf: "<content_key>\\t<group>:<id>,<group>:<id>…" lines, sorted
             (ids escaped: % , tab and newline as %25 %2C %09 %0A)
    tree     "<slot> <type> <hash> <leaks>" lines for the non-empty children, slot = 2 hex (0..63);
             <leaks> = contents present in both train and test in that subtree ("-" in the id tree)
    commit   JSON: buckets, id_root, content_root, parent, message, counts, leaks_count, digest, validation

B, the number of buckets, is a power of two near n / 16 (at least 64) and is kept in the commit.
Bucket b = top log2(B) bits of H(key); both trees have fan-out 64 and depth ceil(log64 B), the
slots on the path to b being its base-64 digits. A node's name is the hash of its children's
names (and leak counts), so the root name is the fingerprint of the version. The leak count of
the whole version is the sum over the root's lines; the leaks themselves are found by descending
only into subtrees with a non-zero count.
"""
import hashlib
import json
import math
import os
import zlib
from collections import defaultdict

from .diff import STORED_EXAMPLES, DiffConfig, Rec, validate
from .jsonl import dumps
from .normalize import multiset_digest

FANOUT_BITS = 6  # 64 children per tree node
RECORDS_PER_BUCKET = 16
MIN_BUCKETS = 64
LEGACY_BUCKETS = 4096  # commits written before "buckets" was stored
LEAK_GROUPS = {"train", "test"}


def choose_buckets(n, parent=None):
    """Power of two near n / 16, at least 64; keeps the parent's B while n / 16 is within 2x of it,
    so that consecutive versions stay comparable through the tree."""
    target = n / RECORDS_PER_BUCKET
    if parent and parent / 2 <= max(target, MIN_BUCKETS) <= parent * 2:
        return parent
    return max(MIN_BUCKETS, 2 ** round(math.log2(target))) if target > 1 else MIN_BUCKETS


def check_buckets(b):
    if b < 1 or b & (b - 1):
        raise ValueError(f"number of buckets must be a power of two, got {b}")
    return b


def depth(buckets):
    return max(1, math.ceil(math.log2(buckets) / FANOUT_BITS))


def bucket_of(key, buckets):
    h = int.from_bytes(hashlib.blake2b(key.encode(), digest_size=8).digest(), "big")
    return h >> (64 - int(math.log2(buckets))) if buckets > 1 else 0


def _esc(s):
    return s.replace("%", "%25").replace(",", "%2C").replace("\t", "%09").replace("\n", "%0A")


def _unesc(s):
    return s.replace("%0A", "\n").replace("%09", "\t").replace("%2C", ",").replace("%25", "%")


def cbucket_body(rows):
    """rows: {content_key: [(group, id)]} -> (body bytes, leaks in it)."""
    lines, leaks = [], 0
    for ck in sorted(rows):
        members = sorted(rows[ck])
        leaks += LEAK_GROUPS <= {g for g, _ in members}
        lines.append(f"{ck}\t{','.join(f'{g}:{_esc(i)}' for g, i in members)}\n")
    return "".join(lines).encode(), leaks


def parse_cbucket(body):
    """-> [(content_key, [(group, id)])]"""
    out = []
    for line in body.decode().splitlines():
        ck, members = line.split("\t", 1)
        out.append((ck, [(g, _unesc(i)) for g, i in (m.split(":", 1) for m in members.split(","))]))
    return out


def tree_body(kids):
    """kids: {slot: (type, name, leaks or None)}"""
    return "".join(f"{slot:02x} {t} {name} {'-' if n is None else n}\n"
                   for slot, (t, name, n) in sorted(kids.items())).encode()


class Store:
    def __init__(self, repo="."):
        self.root = os.path.join(repo, ".dsdiff")
        self.objects = os.path.join(self.root, "objects")
        self.stats = {"objects_read": 0, "objects_written": 0, "buckets_read": 0, "records_read": 0}

    # ---- objects -------------------------------------------------------

    def put(self, kind, body):
        data = f"{kind} {len(body)}\0".encode() + body
        name = hashlib.sha256(data).hexdigest()
        path = os.path.join(self.objects, name[:2], name[2:])
        if not os.path.exists(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = f"{path}.tmp{os.getpid()}"
            with open(tmp, "wb") as f:
                f.write(zlib.compress(data))
            os.replace(tmp, path)
            self.stats["objects_written"] += 1
        return name

    def get(self, name):
        """(type, body bytes) of an object."""
        with open(os.path.join(self.objects, name[:2], name[2:]), "rb") as f:
            data = zlib.decompress(f.read())
        self.stats["objects_read"] += 1
        head, body = data.split(b"\0", 1)
        kind, size = head.decode().split(" ")
        if int(size) != len(body):
            raise ValueError(f"object {name} is corrupt")
        return kind, body

    def resolve(self, rev):
        """Commit name from HEAD, HEAD~k, a full name or a unique prefix (4+ hex)."""
        base, tilde, back = rev.partition("~")
        steps = (int(back) if back else 1) if tilde else 0
        if base == "HEAD":
            name = self.head()
            if name is None:
                raise KeyError("no commits yet")
        else:
            d = os.path.join(self.objects, base[:2])
            hits = [base[:2] + f for f in (os.listdir(d) if len(base) >= 4 and os.path.isdir(d) else ())
                    if f.startswith(base[2:])]
            if len(hits) != 1:
                raise KeyError(f"unknown or ambiguous revision {rev!r}")
            name = hits[0]
        for _ in range(steps):
            name = self.commit_obj(name)["parent"]
            if name is None:
                raise KeyError(f"{rev!r} goes past the first commit")
        return name

    def head(self):
        try:
            with open(os.path.join(self.root, "HEAD"), encoding="utf-8") as f:
                return f.read().strip() or None
        except FileNotFoundError:
            return None

    def set_head(self, name):
        os.makedirs(self.root, exist_ok=True)
        with open(os.path.join(self.root, "HEAD"), "w", encoding="utf-8", newline="\n") as f:
            f.write(name + "\n")

    # ---- trees ---------------------------------------------------------

    def put_tree(self, leaves, kind, buckets):
        """leaves: {bucket: (body bytes, leaks or None)} -> root tree name."""
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

    def tree(self, name):
        """{slot: (type, child name, leaks or None)} of a tree object."""
        kind, body = self.get(name)
        assert kind == "tree", kind
        out = {}
        for line in body.decode().splitlines():
            slot, t, child, n = line.split(" ")
            out[int(slot, 16)] = (t, child, None if n == "-" else int(n))
        return out

    def leaves(self, root, prefix=0):
        """{bucket: leaf name} under a tree."""
        out = {}
        for slot, (t, child, _) in self.tree(root).items():
            key = prefix << FANOUT_BITS | slot
            if t == "tree":
                out.update(self.leaves(child, key))
            else:
                out[key] = child
        return out

    def bucket_records(self, name):
        kind, body = self.get(name)
        assert kind == "bucket", kind
        recs = [json.loads(line) for line in body.decode().splitlines()]
        self.stats["buckets_read"] += 1
        self.stats["records_read"] += len(recs)
        return recs

    def checkout(self, rev="HEAD"):
        """All records of a commit, in bucket order."""
        c = self.commit_obj(self.resolve(rev))
        return [r for _, leaf in sorted(self.leaves(c["id_root"]).items()) for r in self.bucket_records(leaf)]

    def leaks(self, content_root):
        """Within-version leaks, descending only into subtrees whose leak count is non-zero."""
        out = []

        def walk(name):
            for _, (t, child, n) in sorted(self.tree(name).items()):
                if not n:
                    continue
                if t == "tree":
                    walk(child)
                    continue
                for ck, members in parse_cbucket(self.get(child)[1]):
                    m = [(g, i) for g, i in members if g in LEAK_GROUPS]
                    if {g for g, _ in m} == LEAK_GROUPS:
                        out.append({"content_hash": ck,
                                    "records": [{"id": i, "group": g} for i, g in sorted((i, g) for g, i in m)]})

        walk(content_root)
        out.sort(key=lambda x: x["content_hash"])
        return out

    # ---- commits -------------------------------------------------------

    def commit_obj(self, name):
        kind, body = self.get(name)
        assert kind == "commit", kind
        c = json.loads(body)
        c.setdefault("buckets", LEGACY_BUCKETS)
        return c

    def log(self, rev="HEAD"):
        name = self.resolve(rev) if self.head() else None
        while name:
            c = self.commit_obj(name)
            yield name, c
            name = c["parent"]

    def commit(self, records, message, buckets=None):
        """Store a version as a commit on top of HEAD; returns (name, commit object).

        buckets: power of two; default choose_buckets(len(records), parent's buckets).
        """
        parent = self.head()
        if buckets is None:
            buckets = choose_buckets(len(records), self.commit_obj(parent)["buckets"] if parent else None)
        check_buckets(buckets)
        recs = [Rec.of(i, r) for i, r in enumerate(records)]
        by_id, by_c = defaultdict(list), defaultdict(lambda: defaultdict(list))
        counts = {"records": len(recs)}
        for r in recs:
            g = r.norm["group"]
            by_id[bucket_of(r.id_key, buckets)].append((r.id_key, dumps(r.raw)))
            by_c[bucket_of(r.content_key, buckets)][r.content_key].append((g, r.id_key))
            counts[g] = counts.get(g, 0) + 1
        id_leaves = {b: ("".join(f"{line}\n" for _, line in sorted(rows)).encode(), None) for b, rows in by_id.items()}
        c_leaves = {b: cbucket_body(rows) for b, rows in by_c.items()}
        obj = {"buckets": buckets,
               "id_root": self.put_tree(id_leaves, "bucket", buckets),
               "content_root": self.put_tree(c_leaves, "cbucket", buckets),
               "parent": parent, "message": message, "counts": counts,
               "leaks_count": sum(n for _, n in c_leaves.values()),
               "digest": multiset_digest(r.raw_hash for r in recs),
               "validation": validate(recs, DiffConfig(examples=STORED_EXAMPLES))}
        name = self.put("commit", json.dumps(obj, ensure_ascii=False, indent=1).encode())
        self.set_head(name)
        return name, obj
