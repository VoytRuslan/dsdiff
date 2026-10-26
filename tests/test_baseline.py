"""The id-first baseline (v0): what it gets right and where it breaks, on the gen.BREAKS presets."""
import json

import pytest

from dsdiff.baseline_idfirst import diff_idfirst
from dsdiff.bench import score
from dsdiff.diff import diff
from dsdiff.gen import BREAKS
from dsdiff.gen import main as gen
from dsdiff.jsonl import read_jsonl

# what each scenario is meant to show, by its meaning; the presets must say the same
MEANT = {"break_swaps": "trust", "break_id_noise": "trust", "break_train_to_test": "do_not_trust",
         "break_test_relabel": "metrics_not_comparable"}


@pytest.fixture(scope="module", params=list(BREAKS))
def scenario(request, tmp_path_factory):
    out = tmp_path_factory.mktemp(request.param)
    gen(["--preset", request.param, "--out", str(out)])
    summary = json.loads((out / "truth_summary.json").read_text(encoding="utf-8"))
    old, new, truth = (read_jsonl(out / f) for f in ("v1.jsonl", "v2.jsonl", "truth_v1_v2.jsonl"))
    return request.param, old, new, truth, summary["params"]["expected_verdict"]


def test_expected_verdict_in_preset(scenario):
    name, *_, expected = scenario
    assert expected == MEANT[name]


def test_ours_exact(scenario):
    _, old, new, truth, expected = scenario
    r = diff(old, new).to_report()
    assert score(r["changes"], truth) == (0, 0)
    assert r["verdict"]["code"] == expected


def test_v0_breaks(scenario):
    name, old, new, truth, expected = scenario
    v0 = diff_idfirst(old, new)
    assert v0["verdict"]["code"] != expected
    missed, false = score(v0["changes"], truth)
    if name in ("break_swaps", "break_id_noise"):
        assert missed == len(truth) and false > 0  # every swap / noisy id becomes something else


def test_v0_simple_cases():
    rec = lambda i, h, **kw: dict({"sample_id": i, "content_hash": h, "group": "train", "labels": ["cat"],
                                   "metadata": {}}, **kw)
    old = [rec("a", "01"), rec("b", "02"), rec("c", "03")]
    new = [rec("a", "01", labels=["dog"]), rec("b2", "02"), rec("d", "04")]
    r = diff_idfirst(old, new)
    assert sorted((c["old_id"] or "", c["new_id"] or "", tuple(c["types"])) for c in r["changes"]) == [
        ("", "d", ("added",)), ("a", "a", ("relabeled",)), ("b", "b2", ("renamed",)), ("c", "", ("deleted",))]
    assert r["verdict"]["code"] == "trust"
