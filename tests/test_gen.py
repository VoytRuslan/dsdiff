"""Checks of the generator against its own truth: reference_apply, leaks, determinism, tiny coverage."""
import filecmp
import json
import os

import pytest

from dsdiff.gen import (NOISE_RULES, canonical_lines, main, normalize_name, read_jsonl,
                        reference_apply)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

RUNS = {
    "tiny": ["--preset", "tiny"],
    "story": ["--preset", "story"],
    "clean": ["--preset", "clean"],
    "test_relabel": ["--preset", "test_relabel"],
    "test_to_train": ["--preset", "test_to_train"],
    "small": ["--preset", "small"],
    "v3_path_dirty": ["--n", "20000", "--versions", "3", "--with-path", "--dirty"],
}
ALL_TYPES = {"added", "deleted", "renamed", "relabeled", "content_replaced", "group_moved",
             "metadata_changed", "cosmetic", "rename_cycle", "leak", "leak_by_move"}


def generate(args, out):
    main(args + ["--out", str(out)])
    return out


@pytest.fixture(scope="module", params=list(RUNS))
def run(request, tmp_path_factory):
    out = generate(RUNS[request.param], tmp_path_factory.mktemp(request.param))
    with open(out / "truth_summary.json", encoding="utf-8") as f:
        summary = json.load(f)
    return request.param, out, summary


def versions(out, summary):
    return [read_jsonl(out / f"v{v}.jsonl") for v in range(1, summary["params"]["versions"] + 1)]


def test_reference_apply(run):
    _, out, summary = run
    vs = versions(out, summary)
    for s in range(1, len(vs)):
        truth = read_jsonl(out / f"truth_v{s}_v{s + 1}.jsonl")
        got = reference_apply(vs[s - 1], truth)
        assert canonical_lines(got, True) == canonical_lines(vs[s], True), f"v{s} -> v{s + 1}"


def test_leaks_match_files(run):
    """leaks_v{i} equals a recount of the version file: contents present in both train and test."""
    _, out, summary = run
    for v, recs in enumerate(versions(out, summary), 1):
        by_hash = {}
        for r in recs:
            by_hash.setdefault(r["content_hash"].lower(), []).append((normalize_name(r["sample_id"]), r["group"]))
        expected = sorted(
            ({"content_hash": h, "records": [{"id": i, "group": g} for i, g in sorted(m)]}
             for h, m in by_hash.items() if {"train", "test"} <= {g for _, g in m}),
            key=lambda x: (x["content_hash"], [r["id"] for r in x["records"]]))
        assert summary[f"leaks_v{v}"] == expected, f"v{v}"


def test_summary_counts_match_truth(run):
    _, out, summary = run
    for s in range(1, summary["params"]["versions"]):
        combos = {}
        for row in read_jsonl(out / f"truth_v{s}_v{s + 1}.jsonl"):
            key = "+".join(row["types"])
            combos[key] = combos.get(key, 0) + 1
        assert summary["changes_by_combination"][f"v{s}_v{s + 1}"] == dict(sorted(combos.items()))


def test_deterministic(run, tmp_path):
    name, out, _ = run
    again = generate(RUNS[name], tmp_path / "again")
    files = sorted(os.listdir(out))
    assert files == sorted(os.listdir(again))
    _, mismatch, errors = filecmp.cmpfiles(out, again, files, shallow=False)
    assert not mismatch and not errors


@pytest.mark.parametrize("name", ["tiny", "story"])
def test_examples_up_to_date(name, tmp_path):
    """The checked-in examples/ match a fresh generation."""
    fresh = generate(RUNS[name], tmp_path / name)
    ref = os.path.join(ROOT, "examples", name)
    files = sorted(os.listdir(fresh))
    _, mismatch, errors = filecmp.cmpfiles(ref, fresh, files, shallow=False)
    assert not mismatch and not errors, "regenerate with python -m dsdiff.gen --preset " + name


def test_tiny_coverage(tmp_path):
    out = generate(RUNS["tiny"], tmp_path)
    truth = read_jsonl(out / "truth_v1_v2.jsonl")
    types = {t for row in truth for t in row["types"]}
    assert ALL_TYPES <= types, ALL_TYPES - types
    rules = {row["details"]["rule"] for row in truth if "cosmetic" in row["types"]}
    assert set(NOISE_RULES) <= rules, set(NOISE_RULES) - rules
    renames = [row["details"] for row in truth if "renamed" in row["types"]]
    assert {(d["content_shared"], d["ambiguous"]) for d in renames} == {(False, False), (True, False), (True, True)}
    directions = {row["details"]["direction"] for row in truth if row["types"] == ["added", "leak"]}
    assert directions == {"test->train", "train->test"}
    assert ["group_moved", "leak_by_move", "leak"] in [row["types"] for row in truth]


def test_hash_case_never_noop(tmp_path):
    out = generate(["--n", "5000", "--p-noise", "0.8"], tmp_path)
    rows = [r for r in read_jsonl(out / "truth_v1_v2.jsonl") if r["details"].get("rule") == "hash_case"]
    assert rows and all(any(c in "abcdef" for c in r["content_hash"]) for r in rows)


def test_ambiguous_pairs_off_same_id_and_hash(tmp_path):
    """Records keeping id and hash (relabel, move, with-path rename) are not rename candidates."""
    out = generate(["--n", "20000", "--versions", "3", "--with-path", "--dirty"], tmp_path)
    for s in (1, 2):
        for row in read_jsonl(out / f"truth_v{s}_v{s + 1}.jsonl"):
            if "renamed" in row["types"]:
                assert not row["details"]["ambiguous"], (s, row["change_id"])
