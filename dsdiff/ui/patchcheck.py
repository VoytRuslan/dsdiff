"""Patch check, as `dsdiff verify`: apply to the base, then strict and semantic comparison with the target;
the core's PatchError messages become plain Russian."""
import json
import re
from collections import Counter

from ..jsonl import dumps
from ..normalize import canonical_lines
from ..patch import PatchError, apply
from ..store import Store
from .util import ApiError, read_records

EXAMPLES = 10


def patch_message(e):
    """(code, Russian message) for a PatchError from dsdiff.patch.apply."""
    text = str(e)
    if text.startswith("not a dsdiff patch"):
        return "patch_not_a_patch", "Это не патч dsdiff: первая строка должна быть заголовком {\"op\": \"header\", …}."
    m = re.match(r"patch is for another base version \(digest (\w+), got (\w+)\)", text)
    if m:
        return "patch_base_mismatch", (f"Патч сделан для другой базовой версии: ожидалась версия с хешем "
                                       f"{m[1][:12]}…, у выбранной — {m[2][:12]}…; выберите ту версию, из которой "
                                       f"делали патч.")
    m = re.match(r"(\w+): no record (.+) with hash (.+)$", text)
    if m:
        return "patch_bad_op", f"Операция {m[1]}: в базе нет записи {m[2]} с хешем {m[3]}."
    m = re.match(r"rename: target id (.+) is in use", text)
    if m:
        return "patch_bad_op", f"Операция rename: id {m[1]} уже занят другой записью."
    if text.startswith("unknown op"):
        return "patch_bad_op", f"Неизвестная операция в патче: {text.removeprefix('unknown op ')}."
    if text.startswith("temporary ids left"):
        return "patch_bad_op", "После применения остались временные id (__tmp__/…): патч неполный."
    return "patch_bad_op", f"Патч не применился: {text}."


def read_patch(path, shown_name):
    ops = []
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            try:
                ops.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise ApiError(400, "bad_jsonl", f"Файл {shown_name}, строка {n}: неверный JSON ({e.msg}).",
                               {"file": shown_name, "line": n}) from None
    return ops


def side_records(side, repo, root, progress=None):
    """(records, label, lines exactly as compared strictly) of {"rev": …} or {"file": …}."""
    if "rev" in side:
        if repo is None:
            raise ApiError(409, "no_repo", "Репозиторий не открыт: выберите файл вместо версии.")
        st = Store(repo)
        try:
            name = st.resolve(str(side["rev"]))
        except KeyError:
            raise ApiError(404, "unknown_revision", f"Версия «{side['rev']}» не найдена в репозитории.") from None
        recs = st.checkout(name)
        return recs, f"{st.commit_obj(name)['message']} ({name[:12]})", [dumps(r) for r in recs]
    path = root.resolve(side.get("file"), kind="file")
    shown_name = root.rel(path)
    recs = read_records(path, shown_name, progress)
    with open(path, encoding="utf-8") as f:
        lines = [line.rstrip("\n") for line in f]  # as `dsdiff verify` reads the target
    return recs, shown_name, lines


def verify_job(repo, root, base, patch, target):
    def fn(job):
        job.stage = "read"
        base_recs, base_label, _ = side_records(base, repo, root)
        patch_path = root.resolve(patch, kind="file")
        ops = read_patch(patch_path, root.rel(patch_path))
        target_recs, target_label, target_lines = side_records(target, repo, root)
        job.check()
        job.stage = "apply"
        counts = Counter(op.get("op") for op in ops[1:])
        out = {"base": base_label, "patch": root.rel(patch_path), "target": target_label,
               "ops": dict(sorted(counts.items())), "n_base": len(base_recs), "n_target": len(target_recs)}
        try:
            result = apply(base_recs, ops, check_target=False)
        except PatchError as e:
            code, message = patch_message(e)
            return dict(out, applied=False, error={"error": code, "message": message, "detail": {"core": str(e)}})
        job.stage = "compare"
        got = Counter(dumps(r) for r in result)
        want = Counter(target_lines)
        only_result, only_target = got - want, want - got
        examples = ([{"side": "result", "line": x} for x in list(only_result.elements())[:EXAMPLES]]
                    + [{"side": "target", "line": x} for x in list(only_target.elements())[:EXAMPLES]])
        return dict(out, applied=True, n_result=len(result),
                    strict=not only_result and not only_target,
                    semantic=canonical_lines(result, True) == canonical_lines(target_recs, True),
                    mismatch={"only_in_result": sum(only_result.values()),
                              "only_in_target": sum(only_target.values()), "examples": examples})
    return fn
