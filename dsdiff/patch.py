"""Patch between two manifest versions: build it from a DiffResult, apply it to the base version.

A patch is a list of ops (one JSON object per line in a file); the first is the header:

    {"op": "header", "format": "dsdiff-patch/1", "base": <digest>, "target": <digest>, "n_base", "n_target"}

then, in this order,

    {"op": "delete", "id": <raw sample_id>, "h": <raw content_hash>}
    {"op": "rename", "id": ..., "h": ..., "to": <raw sample_id>[, "force": true]}
    {"op": "update", "id": ..., "h": ..., "kind": <kind or [kinds]>, "set": {<field>: <raw value>, ...}}
                                                                              or "record": <raw record>
    {"op": "add", "record": <raw record>}

A record is addressed by its raw sample_id and raw content_hash as they are when the op runs
(an update after a rename uses the new id). A rename may not land on a canonical id that is in
use, so renames whose target is still held by another renamed record are ordered after it, and
cycles go through "__tmp__/<n>". "force" allows a target id that the new version itself
duplicates. "kind" names the change classes of an update (relabel, group_move, content_replace,
metadata_change, cosmetic) for a reader; apply ignores it. Values are raw, so the result equals the new version line for line (as a multiset:
apply keeps the base order and appends additions). apply refuses a base whose digest differs.
"""
from .diff import DiffResult
from .jsonl import dumps
from .normalize import line_hash, multiset_digest, normalize_name

FORMAT = "dsdiff-patch/1"
TMP = "__tmp__/"


KINDS = {"relabeled": "relabel", "group_moved": "group_move", "content_replaced": "content_replace",
         "metadata_changed": "metadata_change", "cosmetic": "cosmetic"}


class PatchError(Exception):
    pass


def _ref(rec):
    return {"id": rec["sample_id"], "h": rec["content_hash"]}


def order_renames(ops):
    """Rename ops -> the same renames ordered so that no target is held by a pending rename source."""
    pending = sorted(ops, key=lambda op: (normalize_name(op["id"]), op["id"], op["h"]))
    out, tmp = [], 0
    while pending:
        sources = {normalize_name(op["id"]) for op in pending}
        ready = [op for op in pending if normalize_name(op["to"]) not in sources]
        if not ready:  # only cycles left: park one member under a temporary id
            op = pending.pop(0)
            parked = f"{TMP}{tmp}"
            tmp += 1
            out.append(dict(op, to=parked))
            pending.append(dict(op, id=parked))
            continue
        out += ready
        pending = [op for op in pending if all(op is not r for r in ready)]
    return out


def build_patch(d: DiffResult):
    dup_new = {e["sample_id"] for e in d.validation["new"] if e["type"] == "duplicate_sample_id"}
    deletes, renames, updates, adds = [], [], [], []
    for c in d.changes:
        if c.new is None:
            deletes.append({"op": "delete", **_ref(c.old.raw)})
            continue
        if c.old is None:
            adds.append({"op": "add", "record": c.new.raw})
            continue
        old, new = c.old.raw, c.new.raw
        if c.old.id_key != c.new.id_key:
            op = {"op": "rename", **_ref(old), "to": new["sample_id"]}
            if c.new.id_key in dup_new:
                op["force"] = True
            renames.append(op)
            old = dict(old, sample_id=new["sample_id"])
        if dumps(old) == dumps(new):  # dumps, not ==: key order is part of the raw record
            continue
        kinds = [KINDS[t] for t in c.types if t in KINDS] or ["cosmetic"]  # renamed + raw-only difference
        op = {"op": "update", **_ref(old), "kind": kinds[0] if len(kinds) == 1 else kinds}
        if list(old) != list(new):
            op["record"] = new
        else:
            op["set"] = {k: v for k, v in new.items() if dumps(v) != dumps(old[k])}
        updates.append(op)
    header = {"op": "header", "format": FORMAT, "base": d.old_digest, "target": d.new_digest,
              "n_base": d.n_old, "n_target": d.n_new}
    return [header] + deletes + order_renames(renames) + updates + adds


def apply(old_records, ops, check_target=True):
    if not ops or ops[0].get("op") != "header" or ops[0].get("format") != FORMAT:
        raise PatchError("not a dsdiff patch (missing header)")
    head = ops[0]
    base = multiset_digest(line_hash(r) for r in old_records)
    if base != head["base"] or len(old_records) != head["n_base"]:
        raise PatchError(f"patch is for another base version (digest {head['base']}, got {base})")
    recs = [dict(r) for r in old_records]
    index = {}  # canonical id -> positions of live records
    for pos, r in enumerate(recs):
        index.setdefault(normalize_name(r["sample_id"]), []).append(pos)

    def find(op):
        for pos in index.get(normalize_name(op["id"]), ()):
            r = recs[pos]
            if r["sample_id"] == op["id"] and r["content_hash"] == op["h"]:
                return pos
        raise PatchError(f"{op['op']}: no record {op['id']!r} with hash {op['h']!r}")

    def move(pos, old_id, new_id):
        index[normalize_name(old_id)].remove(pos)
        index.setdefault(normalize_name(new_id), []).append(pos)

    for op in ops[1:]:
        kind = op["op"]
        if kind == "delete":
            pos = find(op)
            index[normalize_name(op["id"])].remove(pos)
            recs[pos] = None
        elif kind == "rename":
            pos = find(op)
            if index.get(normalize_name(op["to"])) and not op.get("force"):
                raise PatchError(f"rename: target id {op['to']!r} is in use")
            move(pos, op["id"], op["to"])
            recs[pos]["sample_id"] = op["to"]
        elif kind == "update":
            pos = find(op)
            new = dict(op["record"]) if "record" in op else dict(recs[pos], **op["set"])
            if new["sample_id"] != op["id"]:
                move(pos, op["id"], new["sample_id"])
            recs[pos] = new
        elif kind == "add":
            recs.append(dict(op["record"]))
            index.setdefault(normalize_name(op["record"]["sample_id"]), []).append(len(recs) - 1)
        else:
            raise PatchError(f"unknown op {kind!r}")
    out = [r for r in recs if r is not None]
    if any(r["sample_id"].startswith(TMP) for r in out):
        raise PatchError("temporary ids left after apply")
    if check_target and multiset_digest(line_hash(r) for r in out) != head["target"]:
        raise PatchError("result does not match the patch's target digest")
    return out
