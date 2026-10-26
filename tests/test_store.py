"""Store and tree diff: the tree diff must give exactly the flat diff's report."""
import json

import pytest

from dsdiff.__main__ import main as cli
from dsdiff.diff import DiffConfig, diff
from dsdiff.gen import main as gen
from dsdiff.jsonl import dumps, read_jsonl
from dsdiff.store import Store, cbucket_body, choose_buckets, depth, parse_cbucket
from dsdiff.treediff import tree_diff

PRESETS = {  # every generator preset except large / xl; subdirectories for collisions
    "tiny": ([""], ["--preset", "tiny"]),
    "story": ([""], ["--preset", "story"]),
    "clean": ([""], ["--preset", "clean"]),
    "test_relabel": ([""], ["--preset", "test_relabel"]),
    "test_to_train": ([""], ["--preset", "test_to_train"]),
    "small": ([""], ["--preset", "small"]),
    "medium": ([""], ["--preset", "medium"]),
    "collisions": (["h16", "h24", "h32", "h64"], ["--preset", "collisions"]),
    "v3_path_dirty": ([""], ["--n", "20000", "--versions", "3", "--with-path", "--dirty"]),
}


@pytest.fixture(scope="module", params=list(PRESETS))
def stored(request, tmp_path_factory):
    """[(dir, store, [commit name per version])] for one preset."""
    subs, args = PRESETS[request.param]
    out = tmp_path_factory.mktemp(request.param)
    gen(args + ["--out", str(out)])
    runs = []
    for sub in subs:
        d = out / sub if sub else out
        k = json.loads((d / "truth_summary.json").read_text(encoding="utf-8"))["params"]["versions"]
        st = Store(str(d))
        names = [st.commit(read_jsonl(d / f"v{v}.jsonl"), f"v{v}")[0] for v in range(1, k + 1)]
        runs.append((d, st, names))
    return runs


def test_tree_diff_equals_flat_diff(stored):
    for d, st, names in stored:
        for s in range(1, len(names)):
            flat = diff(read_jsonl(d / f"v{s}.jsonl"), read_jsonl(d / f"v{s + 1}.jsonl"))
            tree = tree_diff(st, names[s - 1], names[s])
            assert tree.to_report() == flat.to_report(), f"{d.name} v{s}"
            assert (tree.old_digest, tree.new_digest) == (flat.old_digest, flat.new_digest)
            assert tree.stats["mode"].startswith("tree")


def test_examples_option_matches_flat(stored):
    """Commits keep up to 100 ids per validation error, so any --examples gives the flat report."""
    for d, st, names in stored:
        if st.commit_obj(names[0])["counts"]["records"] > 20_000:
            continue  # same code path as the small presets; the 100k ones only cost time
        for k in (1, 3):
            flat = diff(read_jsonl(d / "v1.jsonl"), read_jsonl(d / "v2.jsonl"), DiffConfig(examples=k))
            assert tree_diff(st, names[0], names[1], DiffConfig(examples=k)).to_report() == flat.to_report()


def test_checkout_and_leaks(stored):
    for d, st, names in stored:
        summary = json.loads((d / "truth_summary.json").read_text(encoding="utf-8"))
        for v, name in enumerate(names, 1):
            assert sorted(map(dumps, st.checkout(name))) == sorted(map(dumps, read_jsonl(d / f"v{v}.jsonl")))
            c = st.commit_obj(name)
            leaks = st.leaks(c["content_root"])
            assert c["leaks_count"] == len(leaks) == sum(n for _, _, n in st.tree(c["content_root"]).values())
            if not d.name.startswith("h"):  # with short hashes leaks_vK (by true content) and hashes differ
                assert leaks == summary[f"leaks_v{v}"]


def test_equal_roots_read_nothing(tmp_path):
    gen(["--preset", "clean", "--out", str(tmp_path)])
    st = Store(str(tmp_path))
    a, _ = st.commit(read_jsonl(tmp_path / "v1.jsonl"), "a")
    written = st.stats["objects_written"]
    b, _ = st.commit(read_jsonl(tmp_path / "v1.jsonl"), "same file again")
    assert st.stats["objects_written"] == written + 1  # only the new commit object
    st.stats.update(buckets_read=0, records_read=0)
    r = tree_diff(st, a, b)
    assert r.changes == [] and r.unchanged == r.n_old and r.stats["buckets_read"] == 0
    assert r.verdict["code"] == "trust"


def test_unchanged_buckets_shared(tmp_path):
    gen(["--n", "100000", "--p-noise", "0", "--p-rename", "0.0001", "--p-relabel", "0", "--p-rename-relabel", "0",
         "--p-replace", "0", "--p-move", "0", "--p-meta", "0", "--p-delete", "0", "--p-add", "0",
         "--n-leaks", "0", "--n-rename-cycles", "0", "--out", str(tmp_path)])
    st = Store(str(tmp_path))
    a, ca = st.commit(read_jsonl(tmp_path / "v1.jsonl"), "a")
    b, cb = st.commit(read_jsonl(tmp_path / "v2.jsonl"), "b")
    la, lb = st.leaves(ca["id_root"]), st.leaves(cb["id_root"])
    shared = sum(la[k] == lb.get(k) for k in la)
    assert shared > 0.99 * len(la)
    r = tree_diff(st, a, b)
    assert r.stats["buckets_differing"] <= 2 * len(r.changes)


def test_cbucket_escaping():
    rows = {"ab": [("train", "a,b%c\td"), ("test", "x:y")]}
    body, leaks = cbucket_body(rows)
    assert leaks == 1
    assert parse_cbucket(body) == [("ab", sorted(rows["ab"]))]


def test_cli_rev(tmp_path, capsys):
    gen(["--preset", "tiny", "--out", str(tmp_path / "d")])
    repo = str(tmp_path)
    for v in (1, 2):
        assert cli(["commit", str(tmp_path / "d" / f"v{v}.jsonl"), "-m", f"v{v}", "--repo", repo]) == 0
    assert cli(["log", "--repo", repo]) == 0
    assert "  v2  " in capsys.readouterr().out
    rep = tmp_path / "r.json"
    assert cli(["diff", "--rev", "HEAD~1", "HEAD", "--repo", repo, "--report", str(rep)]) == 0
    assert "buckets_read" in capsys.readouterr().out
    flat = diff(read_jsonl(tmp_path / "d" / "v1.jsonl"), read_jsonl(tmp_path / "d" / "v2.jsonl")).to_report()
    assert json.loads(rep.read_text(encoding="utf-8")) == flat
    assert cli(["check", "--rev", "HEAD~1", "HEAD", "--repo", repo]) == 3


def test_choose_buckets():
    assert choose_buckets(32) == 64
    assert choose_buckets(100_000) == 8192 and depth(8192) == 3
    assert choose_buckets(1_000_000) == 65536 and depth(65536) == 3
    assert choose_buckets(10_000) == 512 and depth(512) == 2 and depth(64) == 1
    assert choose_buckets(150_000, parent=8192) == 8192   # n/16 within 2x of the parent: keep it
    assert choose_buckets(400_000, parent=8192) == 32768


def test_different_bucket_counts_fall_back_to_flat(tmp_path):
    gen(["--preset", "tiny", "--out", str(tmp_path)])
    st = Store(str(tmp_path))
    a, _ = st.commit(read_jsonl(tmp_path / "v1.jsonl"), "v1", buckets=64)
    b, cb = st.commit(read_jsonl(tmp_path / "v2.jsonl"), "v2", buckets=4096)
    assert cb["buckets"] == 4096 and depth(4096) == 2
    r = tree_diff(st, a, b)
    assert r.stats["mode"] == "flat: 64 vs 4096 buckets"
    assert r.to_report() == diff(read_jsonl(tmp_path / "v1.jsonl"), read_jsonl(tmp_path / "v2.jsonl")).to_report()


def test_bucket_count_follows_parent(tmp_path):
    gen(["--preset", "small", "--out", str(tmp_path)])
    st = Store(str(tmp_path))
    _, c1 = st.commit(read_jsonl(tmp_path / "v1.jsonl"), "v1")
    _, c2 = st.commit(read_jsonl(tmp_path / "v2.jsonl"), "v2")
    assert c1["buckets"] == c2["buckets"] == 512
