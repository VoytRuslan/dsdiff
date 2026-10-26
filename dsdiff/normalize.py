"""Canonical form of manifest records and the keys the diff matches on."""
import hashlib
import json
import unicodedata


def normalize_name(s):
    s = unicodedata.normalize("NFC", s.strip()).replace("\\", "/").lower()
    while s.startswith("./"):
        s = s[2:]
    return s


def normalize_record(rec):
    """Undo cosmetic noise: surrounding whitespace, case, separators, ./ prefix, NFD, label/key order, export_time."""
    r = dict(rec, sample_id=normalize_name(rec["sample_id"]), content_hash=rec["content_hash"].strip().lower(),
             labels=sorted(unicodedata.normalize("NFC", x) for x in rec["labels"]),
             metadata={k: v for k, v in rec["metadata"].items() if k != "export_time"})
    if "path" in rec:
        r["path"] = normalize_name(rec["path"])
    return r


def _h(obj):
    s = obj if isinstance(obj, str) else json.dumps(obj, sort_keys=True, ensure_ascii=False)
    return hashlib.blake2b(s.encode(), digest_size=16).hexdigest()


def keys(rec):
    """(normalized record, id_key, content_key, payload_hash, raw_hash).

    payload_hash covers group + labels + metadata after normalization; raw_hash covers the record
    exactly as written (key order included), so equal payload with unequal raw means cosmetic.
    """
    norm = normalize_record(rec)
    payload = {"group": norm["group"], "labels": norm["labels"], "metadata": norm["metadata"]}
    return norm, norm["sample_id"], norm["content_hash"], _h(payload), line_hash(rec)


def line_hash(rec):
    """Hash of the record exactly as serialized in a version file."""
    return _h(json.dumps(rec, ensure_ascii=False))


def multiset_digest(line_hashes):
    """Order-independent digest of a version: sum of its line hashes mod 2**128."""
    return f"{sum(int(h, 16) for h in line_hashes) % (1 << 128):032x}"


def canonical_lines(records, semantic):
    if semantic:
        return sorted(json.dumps(normalize_record(r), sort_keys=True, ensure_ascii=False) for r in records)
    return sorted(json.dumps(r, ensure_ascii=False) for r in records)
