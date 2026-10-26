"""diff / patch against the generator's truth."""
import json
import os

import pytest

from dsdiff.__main__ import main as cli
from dsdiff.diff import diff
from dsdiff.gen import main as gen
from dsdiff.jsonl import dumps, read_jsonl
from dsdiff.normalize import canonical_lines, keys
from dsdiff.patch import PatchError, apply, build_patch

RUNS = {
    "tiny": ["--preset", "tiny"],
    "story": ["--preset", "story"],
    "clean": ["--preset", "clean"],
    "test_relabel": ["--preset", "test_relabel"],
    "small": ["--preset", "small"],
    "seed3": ["--n", "30000", "--seed", "3"],
    "seed4": ["--n", "30000", "--seed", "4"],
    "seed5": ["--n", "30000", "--seed", "5"],
    "v3_path_dirty": ["--n", "20000", "--versions", "3", "--with-path", "--dirty"],
    "test_to_train": ["--preset", "test_to_train"],
}
VERDICT = {"story": "do_not_trust", "clean": "trust", "test_relabel": "metrics_not_comparable",
           "test_to_train": "do_not_retrain"}
TRUTH_ONLY = {"leak", "leak_by_move", "rename_cycle"}  # truth markers the diff reports elsewhere


class Step:
    def __init__(self, out, s, summary):
        self.s = s
        self.old = read_jsonl(out / f"v{s}.jsonl")
        self.new = read_jsonl(out / f"v{s + 1}.jsonl")
        self.truth = read_jsonl(out / f"truth_v{s}_v{s + 1}.jsonl")
        self.summary = summary
        self.result = diff(self.old, self.new)
        self.report = self.result.to_report()


@pytest.fixture(scope="module", params=list(RUNS))
def run(request, tmp_path_factory):
    out = tmp_path_factory.mktemp(request.param)
    gen(RUNS[request.param] + ["--out", str(out)])
    with open(out / "truth_summary.json", encoding="utf-8") as f:
        summary = json.load(f)
    return request.param, [Step(out, s, summary) for s in range(1, summary["params"]["versions"])]


def ambiguous_hashes(rows):
    return {r["content_hash"] for r in rows if r["details"].get("ambiguous")}


def in_group(row, hashes):
    return row["content_hash"] in hashes or row["details"].get("hash_old") in hashes


def test_ambiguity_agrees_with_truth(run):
    for st in run[1]:
        assert ambiguous_hashes(st.report["changes"]) == ambiguous_hashes(st.truth), f"v{st.s}"


def test_matches_truth_outside_ambiguous_groups(run):
    for st in run[1]:
        amb = ambiguous_hashes(st.truth) | ambiguous_hashes(st.report["changes"])
        want = {(r["old_id"], r["new_id"]): sorted(set(r["types"]) - TRUTH_ONLY)
                for r in st.truth if not in_group(r, amb)}
        got = {(r["old_id"], r["new_id"]): sorted(r["types"])
               for r in st.report["changes"] if not in_group(r, amb)}
        assert got == want, f"v{st.s}"


def test_patch_strict(run):
    """apply(old, build_patch(diff(old, new))) == new, every line byte-identical (ambiguous groups included)."""
    for st in run[1]:
        out = apply(st.old, build_patch(st.result))
        assert sorted(map(dumps, out)) == sorted(map(dumps, st.new)), f"v{st.s}"
        assert canonical_lines(out, True) == canonical_lines(st.new, True)


def test_patch_refuses_other_base(run):
    st = run[1][0]
    with pytest.raises(PatchError, match="another base"):
        apply(st.new, build_patch(st.result))


def test_leaks_match_truth(run):
    for st in run[1]:
        v = st.s + 1
        leaks = st.report["leaks"]
        within = [{"content_hash": x["content_hash"], "records": x["records"]} for x in leaks["within"]]
        assert within == st.summary[f"leaks_v{v}"], f"v{v}"
        truth = [x for x in st.summary["leaks"] if x["version"] == v]
        want_copies = {(x["direction"], x["train_id"] if x["direction"] == "test->train" else x["test_id"])
                       for x in truth if x["kind"] == "copy"}
        got_copies = {(c["direction"], c["id"]) for x in leaks["within"] for c in x["causes"] or ()
                      if c["cause"] == "copy"}
        assert got_copies == want_copies, f"v{v}"
        assert {x["old_id"] for x in leaks["by_move"]} == {x["id"] for x in truth if x["kind"] == "move"}


def test_verdict(run):
    name, steps = run
    if name in VERDICT:
        assert steps[0].report["verdict"]["code"] == VERDICT[name]


def test_every_verdict_covered():
    assert set(VERDICT.values()) == {"trust", "metrics_not_comparable", "do_not_retrain", "do_not_trust"}


def test_preexisting_leak_reason():
    """A train/test pair already present in the old version keeps do_not_trust but says so."""
    base = {"content_hash": "ab", "labels": ["cat"], "metadata": {}}
    old = [dict(base, sample_id="a", group="train"), dict(base, sample_id="b", group="test")]
    v = diff(old, old).verdict
    assert v["code"] == "do_not_trust"
    assert [r["reason_code"] for r in v["reasons"]] == ["leak_preexisting"]
    assert "старой версии" in v["reasons"][0]["reason"]


def test_rename_cycle_goes_through_tmp():
    rec = lambda i, h: {"sample_id": i, "content_hash": h, "group": "train", "labels": ["cat"], "metadata": {}}
    old = [rec("a", "01"), rec("b", "02"), rec("c", "03")]
    new = [rec("b", "01"), rec("c", "02"), rec("a", "03")]
    ops = build_patch(diff(old, new))
    assert any(op["op"] == "rename" and op["to"].startswith("__tmp__/") for op in ops)
    assert sorted(map(dumps, apply(old, ops))) == sorted(map(dumps, new))


def test_whitespace_is_trimmed():
    rec = {"sample_id": " a/b.jpg ", "path": "\timg/b.jpg ", "content_hash": " AB12\n", "group": "train",
           "labels": [], "metadata": {}}
    norm, id_key, content_key, _, _ = keys(rec)
    assert (id_key, norm["path"], content_key) == ("a/b.jpg", "img/b.jpg", "ab12")
    r = diff([dict(rec, sample_id="a/b.jpg", path="img/b.jpg", content_hash="ab12")], [rec]).to_report()
    assert [c["types"] for c in r["changes"]] == [["cosmetic"]]


def test_update_kind():
    rec = lambda **kw: dict({"sample_id": "a", "content_hash": "01", "group": "train", "labels": ["cat"],
                             "metadata": {"w": 1}}, **kw)
    cases = [(rec(labels=["dog"]), "relabel"), (rec(group="test"), "group_move"),
             (rec(content_hash="02"), "content_replace"), (rec(metadata={"w": 2}), "metadata_change"),
             (rec(sample_id="A"), "cosmetic"), (rec(labels=["dog"], group="val"), ["relabel", "group_move"])]
    for new, kind in cases:
        ops = build_patch(diff([rec()], [new]))
        assert [op["kind"] for op in ops if op["op"] == "update"] == [kind]
        stripped = [{k: v for k, v in op.items() if k != "kind"} for op in ops]
        assert apply([rec()], ops) == apply([rec()], stripped) == [new]  # apply ignores "kind"


def test_cli(tmp_path, capsys):
    d = tmp_path / "tiny"
    gen(["--preset", "tiny", "--out", str(d)])
    rep, patch, out = tmp_path / "out" / "report.json", tmp_path / "out" / "patch.jsonl", tmp_path / "v2.jsonl"
    assert cli(["diff", str(d / "v1.jsonl"), str(d / "v2.jsonl"), "--report", str(rep), "--patch", str(patch)]) == 0
    printed = capsys.readouterr().out
    assert "time: read" in printed and "normalize" in printed and "match" in printed and "leaks_verdict" in printed
    assert json.loads(rep.read_text(encoding="utf-8"))["verdict"]["code"] == "do_not_trust"
    assert cli(["verify", str(d / "v1.jsonl"), str(d / "v2.jsonl"), str(patch)]) == 0
    assert cli(["apply", str(d / "v1.jsonl"), str(patch), "--out", str(out)]) == 0
    assert sorted(out.read_text(encoding="utf-8").splitlines()) == \
        sorted((d / "v2.jsonl").read_text(encoding="utf-8").splitlines())
    assert cli(["apply", str(d / "v2.jsonl"), str(patch), "--out", str(tmp_path / "x.jsonl")]) == 2
    assert cli(["check", str(d / "v1.jsonl"), str(d / "v2.jsonl")]) == 3
    assert os.path.exists(rep)


@pytest.fixture(scope="module")
def seeds(tmp_path_factory):
    out = {}
    for sd in (3, 4):
        d = tmp_path_factory.mktemp(f"s{sd}")
        gen(["--n", "30000", "--seed", str(sd), "--out", str(d)])
        out[sd] = d
    return out


def test_rename_tiebreak_by_name(seeds):
    """seed 3: two old candidates for one renamed content; the one sharing the number wins."""
    d = seeds[3]
    rows = diff(read_jsonl(d / "v1.jsonl"), read_jsonl(d / "v2.jsonl")).to_report()["changes"]
    by_old = {r["old_id"]: r for r in rows}
    r = by_old["images/a8/img_004467.jpg"]
    assert (r["types"], r["new_id"]) == (["renamed"], "images/a8/r1_004467.jpg")
    assert by_old["images/77/img_003800.jpg"]["types"] == ["deleted"]


@pytest.mark.parametrize("seed,name", [(3, "img_001989"), (4, "img_001348")])
def test_truth_ambiguous_ignores_ids_kept_in_new(seeds, seed, name):
    rows = [r for r in read_jsonl(seeds[seed] / "truth_v1_v2.jsonl")
            if "renamed" in r["types"] and name in r["old_id"]]
    assert rows and not any(r["details"]["ambiguous"] for r in rows)
