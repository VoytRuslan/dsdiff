"""Olya's small dataset ("файлы Оли/data_small"): our diff, patch and verdict on data we did not generate."""
import os

import pytest

from dsdiff.baseline_idfirst import diff_idfirst
from dsdiff.diff import diff
from dsdiff.jsonl import dumps, read_jsonl
from dsdiff.patch import apply, build_patch

DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "файлы Оли", "data_small")
pytestmark = pytest.mark.skipif(not os.path.isdir(DATA), reason="Olya's data is not in the repo")


@pytest.fixture(scope="module")
def versions():
    return read_jsonl(os.path.join(DATA, "v1.jsonl")), read_jsonl(os.path.join(DATA, "v2.jsonl"))


def test_diff(versions):
    r = diff(*versions).to_report()
    assert len(r["changes"]) == 175
    assert len(r["leaks"]["within"]) == 1 and len(r["leaks"]["by_move"]) == 9
    assert r["verdict"]["code"] == "do_not_trust"


def test_apply_reproduces_v2_bytes(versions):
    old, new = versions
    out = apply(old, build_patch(diff(old, new)))
    assert sorted(map(dumps, out)) == sorted(map(dumps, new))
    with open(os.path.join(DATA, "v2.jsonl"), encoding="utf-8") as f:
        assert sorted(f.read().splitlines()) == sorted(map(dumps, out))


def test_baseline_agrees_on_clean_ids(versions):
    """Her ids carry no noise, so the id-first baseline finds the same 175 changes."""
    assert len(diff_idfirst(*versions)["changes"]) == 175
