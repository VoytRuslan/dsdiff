"""Compare reports: computed once per pair, kept in memory (last few), served page by page."""
import csv
import io
import itertools
import json
import secrets
import shlex
import threading
from collections import OrderedDict

from ..diff import VERDICTS, diff
from ..jsonl import dumps
from ..patch import build_patch
from ..store import Store
from ..treediff import differing_buckets, tree_diff
from .util import ApiError, read_records

CACHE_CHANGES = 2_000_000  # changes held in all cached reports together (--cache-changes)
PAGE_MAX = 200
TYPES = ("renamed", "relabeled", "content_replaced", "group_moved", "metadata_changed", "cosmetic",
         "added", "deleted")
SORTS = ("importance", "report", "type", "old_id", "new_id", "-old_id", "-new_id")  # "type": old name of "report"
# "importance": rows that drive the verdict first, then by class; within a rank, the report's order
TYPE_RANK = {"content_replaced": 1, "relabeled": 2, "renamed": 3, "added": 4, "deleted": 4, "group_moved": 4,
             "metadata_changed": 5, "cosmetic": 6}
NOT_TEST_CHANGES = (["cosmetic"], ["metadata_changed"], ["renamed"])  # as make_verdict skips them


def importance(row, copies):
    """0 for a row that drives the verdict (a train/test move, a copy that makes a leak, a change in test,
    by the rules of dsdiff.diff.make_verdict); else the rank of its most important class."""
    groups = {row["group_old"], row["group_new"]}
    if "group_moved" in row["types"] and groups & {"train", "test"}:
        return 0
    if "added" in row["types"] and row["new_id"] in copies:
        return 0
    if row["types"] not in NOT_TEST_CHANGES and "test" in groups:
        return 0
    return min(TYPE_RANK.get(t, 4) for t in row["types"])


class Report:
    def __init__(self, key, source, owner=None):
        self.id = secrets.token_hex(6)
        self.key, self.source, self.owner = key, source, owner
        self.job = None
        self.result = self.report = self.rows = None
        self.sides = None  # tree diff: {"buckets": B, "a": buckets read in A, "b": in B}

    def ready(self):
        if self.report is None:
            raise ApiError(202, "pending", "Отчёт ещё считается.", {"job": self.job.id if self.job else None})

    def set_result(self, result):
        self.result = result
        report = result.to_report()
        copies = {c["id"] for leak in report["leaks"]["within"] for c in leak["causes"] or () if c["cause"] == "copy"}
        rows = []
        for n, (c, row) in enumerate(zip(result.changes, report["changes"])):
            row = dict(row, n=n, ambiguous=c.ambiguous,
                       group_old=c.old.norm["group"] if c.old else None,
                       group_new=c.new.norm["group"] if c.new else None)
            row["_q"] = " ".join(x.lower() for x in (row["old_id"], row["new_id"], row["old_id_raw"],
                                                     row["new_id_raw"]) if x)
            row["_rank"] = importance(row, copies)
            row["affects_verdict"] = row["_rank"] == 0
            rows.append(row)
        self.rows, self.report = rows, report
        if self.owner:
            self.owner.evict()

    @property
    def weight(self):
        return len(self.rows) if self.rows is not None else 0

    # ---- pages ---------------------------------------------------------

    def changes(self, offset=0, limit=PAGE_MAX, types=None, group=None, q=None, sort="report", hide_cosmetic=False):
        self.ready()
        rows = self.rows
        if types:
            wanted = set(types)
            bad = wanted - set(TYPES) - {"ambiguous"}
            if bad:
                raise ApiError(400, "bad_request", f"Неизвестный тип изменения: {', '.join(sorted(bad))}.")
            rows = [r for r in rows if wanted & set(r["types"]) or ("ambiguous" in wanted and r["ambiguous"])]
        if group:
            rows = [r for r in rows if group in (r["group_old"], r["group_new"])]
        if q:
            q = q.lower()
            rows = [r for r in rows if q in r["_q"]]
        hidden = 0
        if hide_cosmetic and not (types and "cosmetic" in types):  # asking for cosmetic shows it anyway
            kept = [r for r in rows if r["types"] != ["cosmetic"]]
            hidden, rows = len(rows) - len(kept), kept
        if sort not in SORTS:
            raise ApiError(400, "bad_request", f"Сортировка «{sort}» не поддерживается.")
        if sort == "importance":
            rows = sorted(rows, key=lambda r: (r["_rank"], r["n"]))
        elif sort not in ("report", "type"):
            field = sort.lstrip("-")
            present = sorted((r for r in rows if r[field] is not None), key=lambda r: (r[field], r["n"]),
                             reverse=sort.startswith("-"))
            rows = present + [r for r in rows if r[field] is None]  # added / deleted rows have no id there
        return dict(page(rows, offset, limit, strip=("_q", "_rank")), hidden_cosmetic=hidden)

    def leaks(self, kind, offset=0, limit=PAGE_MAX):
        self.ready()
        if kind not in ("within", "by_move"):
            raise ApiError(400, "bad_request", "Вид утечек — within или by_move.")
        out = page(self.report["leaks"][kind], offset, limit)
        out["totals"] = {k: len(v) for k, v in self.report["leaks"].items()}
        return out

    def validation(self, side, offset=0, limit=PAGE_MAX):
        self.ready()
        if side not in ("old", "new"):
            raise ApiError(400, "bad_request", "Сторона — old или new.")
        out = page(self.report["validation"][side], offset, limit)
        out["totals"] = {k: len(v) for k, v in self.report["validation"].items()}
        return out

    # ---- summary and exports --------------------------------------------

    def summary(self):
        self.ready()
        r, s = self.report, self.report["summary"]
        verdict = dict(r["verdict"], severity=VERDICTS[r["verdict"]["code"]][1])
        return {"id": self.id, "source": self.source, "verdict": verdict,
                "n_old": s["n_old"], "n_new": s["n_new"], "unchanged": s["unchanged"],
                "changes_total": len(self.rows), "changes_by_type": s["changes_by_type"],
                "changes_by_combination": s["changes_by_combination"],
                "ambiguous_renames": s["ambiguous_renames"],
                "leaks": {k: len(v) for k, v in r["leaks"].items()},
                "validation": {k: len(v) for k, v in r["validation"].items()},
                "stats": self.result.stats, "sides": self.sides, "timings": {k: round(v, 4) for k, v in self.result.timings.items()},
                "cli": self.cli()}

    def cli(self):
        src = self.source
        if src["kind"] == "rev":
            args = f"--rev {src['a'][:12]} {src['b'][:12]} --repo {shlex.quote(src['repo'])}"
        else:
            args = f"{shlex.quote(src['a_path'])} {shlex.quote(src['b_path'])}"
        return {"check": f"python -m dsdiff check {args}",
                "diff": f"python -m dsdiff diff {args} --report report.json --patch patch.jsonl"}

    def export(self, fmt):
        """(iterator of str pieces, content type): the file is produced while it is sent."""
        self.ready()
        if fmt == "report.json":  # the same bytes as json.dump(..., indent=2) in `dsdiff diff --report`
            enc = json.JSONEncoder(ensure_ascii=False, indent=2)
            return itertools.chain(enc.iterencode(self.report), ["\n"]), "application/json"
        if fmt == "patch.jsonl":
            return (dumps(op) + "\n" for op in build_patch(self.result)), "application/x-ndjson"
        if fmt == "changes.csv":
            return self.changes_csv(), "text/csv"
        if fmt == "summary.md":
            return iter([self.summary_md()]), "text/markdown"
        raise ApiError(404, "not_found", f"Нет выгрузки «{fmt}».")

    def changes_csv(self):
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(["n", "types", "old_id", "new_id", "old_id_raw", "new_id_raw", "group_old", "group_new",
                    "content_hash", "labels_old", "labels_new", "hash_old", "ambiguous", "candidates"])
        for r in self.rows:
            d = r["details"]
            w.writerow([r["n"], "+".join(r["types"]), r["old_id"] or "", r["new_id"] or "", r["old_id_raw"] or "",
                        r["new_id_raw"] or "", r["group_old"] or "", r["group_new"] or "", r["content_hash"],
                        "|".join(d.get("labels_old", [])), "|".join(d.get("labels_new", [])),
                        d.get("hash_old", ""), int(r["ambiguous"]), "|".join(d.get("candidates", []))])
            if buf.tell() > 1 << 16:
                yield buf.getvalue()
                buf.seek(0)
                buf.truncate()
        yield buf.getvalue()

    def summary_md(self):
        s = self.summary()
        src = self.source
        a, b = ((f"{src['a_label']} (`{src['a'][:12]}`)", f"{src['b_label']} (`{src['b'][:12]}`)")
                if src["kind"] == "rev" else (f"`{src['a_path']}`", f"`{src['b_path']}`"))
        v = s["verdict"]
        lines = [f"## dsdiff: {a} → {b}", "", f"**Вердикт: {v['label']}** (`{v['code']}`)", ""]
        for r in v["reasons"]:
            ex = f" Например: {', '.join(f'`{x}`' for x in r['examples'])}." if r["examples"] else ""
            head = f"{r['count']} {records_word(r['count'])}: {r['reason']}" if r["count"] else r["reason"]
            lines.append(f"- {head}.{ex}")
        lines += ["", f"Записей: {s['n_old']} → {s['n_new']}, без изменений {s['unchanged']}; "
                      f"утечек внутри версии {s['leaks']['within']}, переносом {s['leaks']['by_move']}.", "",
                  "| тип | записей |", "|---|---|"]
        lines += [f"| {t} | {n} |" for t, n in s["changes_by_type"].items()]
        lines += ["", "Первые изменения:", "", "| типы | было | стало |", "|---|---|---|"]
        lines += [f"| {'+'.join(r['types'])} | {r['old_id_raw'] or '—'} | {r['new_id_raw'] or '—'} |"
                  for r in self.rows[:10]]
        lines += ["", "Проверить из терминала:", "", "```sh", s["cli"]["check"], "```", ""]
        return "\n".join(lines)


def records_word(k):
    m10, m100 = k % 10, k % 100
    return "запись" if m10 == 1 and m100 != 11 else "записи" if 2 <= m10 <= 4 and not 12 <= m100 <= 14 else "записей"


def page(items, offset, limit, strip=()):
    offset, limit = max(0, int(offset)), max(1, min(int(limit), PAGE_MAX))
    out = items[offset:offset + limit]
    if strip:
        out = [{k: v for k, v in r.items() if k not in strip} for r in out]
    return {"total": len(items), "offset": offset, "limit": limit, "items": out}


class Reports:
    """LRU of reports limited by the changes they hold together. The most recently used report stays
    even when it alone is over the limit; reports still being computed are not dropped."""

    def __init__(self, limit=CACHE_CHANGES):
        self.limit = limit
        self.by_key, self.by_id, self.lock = OrderedDict(), {}, threading.RLock()

    def get(self, rid):
        with self.lock:
            r = self.by_id.get(rid)
            if r is None:
                raise ApiError(404, "not_found", "Отчёт не найден: его вытеснили из памяти или сервер "
                                                 "перезапускали. Запустите сравнение снова.", {"report": rid})
            self.by_key.move_to_end(r.key)
            return r

    def lookup_or_add(self, key, source):
        """(report, created) for the key, made the most recently used."""
        with self.lock:
            r = self.by_key.get(key)
            if r is not None and not (r.job and r.job.state in ("failed", "cancelled")):
                self.by_key.move_to_end(key)
                return r, False
            if r is not None:
                self.by_id.pop(r.id, None)
            r = Report(key, source, self)
            self.by_key[key] = r
            self.by_key.move_to_end(key)
            self.by_id[r.id] = r
            self.evict()
            return r, True

    def evict(self):
        with self.lock:
            total = sum(r.weight for r in self.by_key.values())
            for key in list(self.by_key)[:-1]:  # oldest first, never the most recent
                if total <= self.limit:
                    break
                r = self.by_key[key]
                if r.report is None and r.job and r.job.state in ("queued", "running"):
                    continue
                total -= r.weight
                del self.by_key[key]
                self.by_id.pop(r.id, None)

    def held(self):
        return sum(r.weight for r in self.by_key.values())


def compare_revs_job(report, repo, a, b):
    def fn(job):
        job.stage = "diff"
        st = Store(repo)
        result = tree_diff(st, a, b)
        job.check()  # cancelled while diffing: the result is thrown away
        ca, cb = st.commit_obj(a), st.commit_obj(b)
        if ca["buckets"] == cb["buckets"]:  # buckets read on each side: tree_diff's stats add A and B up
            leaves = differing_buckets(Store(repo), ca["id_root"], cb["id_root"])
            report.sides = {"buckets": cb["buckets"], "a": sum(x is not None for _, x, _ in leaves),
                            "b": sum(y is not None for _, _, y in leaves)}
        report.set_result(result)
        return {"report": report.id}
    return fn


def compare_files_job(report, paths, shown):
    def fn(job):
        job.stage = "read"
        sides = []
        for k, (path, name) in enumerate(zip(paths, shown)):
            def progress(n, total, k=k):
                job.check()
                job.progress(**{f"lines_read_{'ab'[k]}": n, f"lines_total_{'ab'[k]}": total})
            sides.append(read_records(path, name, progress))
        job.stage = "diff"
        result = diff(*sides)
        del sides
        job.check()
        report.set_result(result)
        return {"report": report.id}
    return fn
