"""Slide charts and a comparison table from bench/results.csv, one message per chart.

python bench/plots.py [--csv bench/results.csv] [--out bench]
    time_vs_d.png       flat vs tree diff time against the number of differences (largest n)
    extrapolation.png   estimate for n = 1e6 … 1e9 at d = 100, extrapolated from the measurements
    storage.png         10 versions: the store at two change rates against 10 gzipped copies
    collisions.png      false renames against hash length
    breaks.png          what breaks in v0 (id-first baseline) on each phenomenon, against our diff
    summary.md          4096 buckets against about n / 16: diff time, buckets read, storage;
                        v0 / flat / tree on the presets (baseline rows; bench/baseline.csv overrides)
"""
import argparse
import csv
import os
import sys
import textwrap

import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.ticker import FuncFormatter, MaxNLocator  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dsdiff.store import choose_buckets, depth  # noqa: E402

# reference palette (light): surface, ink, categorical slots 1-3, recessive grid
SURFACE, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
BLUE, ORANGE, AQUA = "#2a78d6", "#eb6834", "#1baf7a"
GOOD, CRITICAL = "#0ca30c", "#d03b3b"  # status: always with a mark and a label
VERDICTS = {"do_not_trust": "не доверять", "do_not_retrain": "не переобучать",
            "metrics_not_comparable": "метрики несопоставимы", "trust": "доверять"}
FIXED = 4096  # the bucket count before it became adaptive
BYTES_PER_RECORD = 2.5e3  # measured: resident memory of one normalized record in the flat diff


def load(path):
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def val(rows, metric, **keys):
    """The value of the one row with this metric and these columns."""
    hits = [r for r in rows if r["metric"] == metric and all(r[k] == str(v) for k, v in keys.items())]
    if len(hits) != 1:
        raise KeyError(f"{len(hits)} rows for {metric} {keys}")
    return float(hits[0]["value"])


def values(rows, column, **keys):
    return sorted({int(r[column]) for r in rows if r[column] and all(r[k] == str(v) for k, v in keys.items())})


def num(n):
    return f"{int(n):,}".replace(",", "\u00a0")  # no-break space: a wrapped caption keeps the number whole


def human_seconds(s):
    if s < 1:
        return f"{s * 1000:.0f} мс"
    if s < 600:
        return f"{s:.1f} с"
    if s < 3 * 3600:
        return f"{s / 60:.0f} мин"
    return f"{s / 3600:.0f} ч"


TIME_TICKS = [(1e-3, "1 мс"), (1e-2, "10 мс"), (0.1, "100 мс"), (1, "1 с"), (10, "10 с"), (60, "1 мин"),
              (600, "10 мин"), (3600, "1 ч"), (36000, "10 ч"), (360000, "100 ч")]


def time_axis(ax, ys):
    """Log time axis with round ticks covering ys."""
    lo, hi = min(ys) / 1.5, max(ys) * 1.5
    ticks = [(t, s) for t, s in TIME_TICKS if lo <= t <= hi]
    ax.set_yscale("log")
    ax.set_ylim(lo, hi)
    ax.set_yticks([t for t, _ in ticks], [s for _, s in ticks])
    ax.minorticks_off()


def style(ax, title, subtitle, note=None):
    fig = ax.figure
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=INK2, labelsize=11, length=0, pad=6)
    ax.grid(True, axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    fig.text(0.06, 0.95, title, fontsize=17, fontweight="bold", color=INK, ha="left", va="top")
    fig.text(0.06, 0.885, textwrap.fill(subtitle, 100), fontsize=11.5, color=INK2, ha="left", va="top",
             linespacing=1.4)
    if ax.get_legend_handles_labels()[0]:
        fig.legend(loc="upper left", bbox_to_anchor=(0.055, 0.79), ncol=2, frameon=False, fontsize=11,
                   labelcolor=INK2, handlelength=2.2)
    bottom = 0.13
    if note:
        fig.text(0.06, 0.03, textwrap.fill(note, 105), fontsize=11, color=INK, ha="left", va="bottom",
                 linespacing=1.4, bbox=dict(boxstyle="round,pad=0.5", facecolor="#f0efec", edgecolor="none"))
        bottom = 0.22
    fig.subplots_adjust(left=0.1, right=0.86, top=0.7, bottom=bottom)


def line(ax, xs, ys, color, label, end_label=None, dy=0, dashed=False):
    ax.plot(xs, ys, color=color, linewidth=2.2, marker="o", markersize=8, linestyle="--" if dashed else "-",
            markeredgecolor=color if dashed else SURFACE, markeredgewidth=2,
            markerfacecolor=SURFACE if dashed else color, label=label, solid_capstyle="round")
    ax.annotate(end_label or label, (xs[-1], ys[-1]), xytext=(10, dy), textcoords="offset points",
                color=INK, fontsize=11.5, va="center")


def adaptive_buckets(rows, n):
    return next(b for b in values(rows, "buckets", experiment="flat_vs_tree", n=n) if b != FIXED)


def plot_time(rows, out):
    n = max(values(rows, "n", experiment="flat_vs_tree"))
    b = adaptive_buckets(rows, n)
    ds = [d for d in values(rows, "d", experiment="flat_vs_tree", n=n) if d]
    get = lambda method, d, **k: val(rows, "seconds", experiment="flat_vs_tree", n=n, d=d, method=method, **k)
    flat = [get("flat", d) for d in ds]
    tree = [get("tree", d, buckets=b) for d in ds]
    first, nexts = get("commit", 0, buckets=b), [get("commit", d, buckets=b) for d in ds]
    mid = ds[len(ds) // 2 - 1] if len(ds) > 1 else ds[0]
    fig, ax = plt.subplots(figsize=(10, 6.2), dpi=200)
    close = tree[-1] > flat[-1] / 3  # end labels would collide: push them apart
    line(ax, ds, flat, ORANGE, "плоский diff", dy=9 if close else 0)
    line(ax, ds, tree, BLUE, "diff по дереву", dy=-9 if close else 0)
    ax.set_xscale("log")
    ax.set_xticks(ds, [num(d) for d in ds])
    time_axis(ax, flat + tree)
    ax.set_xlabel("число изменённых записей", color=INK2, fontsize=11.5)
    style(ax, f"При {mid} отличиях дерево находит их в {get('flat', mid) / get('tree', mid, buckets=b):.0f} раз быстрее",
          f"{num(n)} записей, {num(b)} бакетов по ~{n / b:.0f} записей; плоский diff читает обе версии целиком, "
          f"дерево — только отличающиеся бакеты. Время — минимум из 3 прогонов после прогрева",
          f"commit — один раз на версию, O(n): {human_seconds(first)} для первой версии и ≈ "
          f"{human_seconds(sum(nexts) / len(nexts))} для каждой следующей при n = {num(n)}. "
          f"Сравнение любой пары закоммиченных версий — O(d).")
    fig.savefig(os.path.join(out, "time_vs_d.png"), facecolor=SURFACE)
    plt.close(fig)


def fit_nonneg(A, t):
    """Least squares in relative error (rows weighted by 1 / t) with non-negative coefficients.
    Objects and records read grow together, so a free fit can trade one for a negative other."""
    W, y = A / t[:, None], np.ones(len(t))
    best = None
    for cols in ([0, 1], [0], [1]):
        c = np.zeros(A.shape[1])
        c[cols] = np.linalg.lstsq(W[:, cols], y, rcond=None)[0]
        if (c >= 0).all():
            err = np.sum((W @ c - y) ** 2)
            if best is None or err < best[0]:
                best = (err, c)
    return best[1]


def plot_extrapolation(rows, out, d=100):
    ns = values(rows, "n", experiment="flat_vs_tree")
    n0 = max(ns)
    # flat: linear in n, from the measurements at every d
    flat_rows = [(n, val(rows, "seconds", experiment="flat_vs_tree", n=n, d=dd, method="flat"))
                 for n in ns for dd in values(rows, "d", experiment="flat_vs_tree", n=n) if dd]
    c_flat = sum(t for _, t in flat_rows) / sum(n for n, _ in flat_rows)
    commits = [val(rows, "seconds", experiment="flat_vs_tree", n=n0, d=dd, buckets=adaptive_buckets(rows, n0),
                   method="commit") for dd in values(rows, "d", experiment="flat_vs_tree", n=n0) if dd]
    c_commit = sum(commits) / len(commits) / n0  # seconds per record of a commit on top of a parent
    # tree: seconds = a * objects read + b * records read (a, b >= 0), fitted on the adaptive runs
    fit = []
    for n in ns:
        b = adaptive_buckets(rows, n)
        for dd in values(rows, "d", experiment="flat_vs_tree", n=n, buckets=b):
            k = dict(experiment="flat_vs_tree", n=n, d=dd, buckets=b, method="tree")
            if dd:
                fit.append((val(rows, "objects_read", **k), val(rows, "records_read", **k), val(rows, "seconds", **k)))
    coef = fit_nonneg(np.array([[o, r] for o, r, _ in fit]), np.array([t for _, _, t in fit]))
    b0 = adaptive_buckets(rows, n0)
    k0 = dict(experiment="flat_vs_tree", n=n0, d=d, buckets=b0, method="tree")
    obj0, rec0 = val(rows, "objects_read", **k0), val(rows, "records_read", **k0)

    def tree_est(n):
        b = choose_buckets(n)
        objects = obj0 + 2 * d * (depth(b) - depth(b0))  # one more tree node per differing bucket and side
        records = rec0 * (n / b) / (n0 / b0)              # same buckets touched, ~16 records each
        return coef @ [objects, records]

    xs = [10 ** k for k in range(6, 10)]
    flat_est, tree_ests = [c_flat * n for n in xs], [tree_est(n) for n in xs]
    fig, ax = plt.subplots(figsize=(10, 6.2), dpi=200)
    line(ax, xs, flat_est, ORANGE, "плоский diff (оценка)", "плоский", dashed=True)
    line(ax, xs, tree_ests, BLUE, "diff по дереву (оценка)", "дерево", dashed=True)
    ax.plot([n0], [val(rows, "seconds", experiment="flat_vs_tree", n=n0, d=d, method="flat")], "o",
            color=ORANGE, markersize=8, markeredgecolor=SURFACE, markeredgewidth=2)
    ax.plot([n0], [val(rows, "seconds", **k0)], "o", color=BLUE, markersize=8,
            markeredgecolor=SURFACE, markeredgewidth=2)
    ax.annotate("замер", (n0, val(rows, "seconds", **k0)), xytext=(0, 14), textcoords="offset points",
                ha="center", color=INK2, fontsize=10.5)
    ax.set_xscale("log")
    ax.set_xticks(xs, ["1 млн", "10 млн", "100 млн", "1 млрд"])
    time_axis(ax, flat_est + tree_ests)
    ax.set_xlabel("записей в версии", color=INK2, fontsize=11.5)
    style(ax, f"Оценка, d = {d}: дерево ≈ {human_seconds(tree_ests[-1])} и на 1 млрд записей",
          f"Экстраполяция замеров при n = {', '.join(num(n) for n in ns)}: плоский ∝ n, дерево ≈ "
          + (f"{coef[0] * 1e6:.0f} мкс на прочитанный узел + " if coef[0] else "")
          + f"{coef[1] * 1e6:.1f} мкс на запись отличающихся бакетов. Плоскому на 1 млрд нужно ещё ~"
          f"{2 * BYTES_PER_RECORD * xs[-1] / 1e12:.0f} ТБ памяти. Замеры — минимум из 3 прогонов",
          f"commit миллиардной версии — O(n), один раз при поступлении (≈ {human_seconds(c_commit * xs[-1])} "
          f"по линейной оценке от замера при n = {num(n0)}); потом любое сравнение с ней — по дереву")
    fig.savefig(os.path.join(out, "extrapolation.png"), facecolor=SURFACE)
    plt.close(fig)


def plot_storage(rows, out):
    vs = values(rows, "version", experiment="storage", method="copies", scenario="default")
    n = values(rows, "n", experiment="storage")[0]
    b = next(x for x in values(rows, "buckets", experiment="storage") if x != FIXED)
    mb = lambda metric, **k: [val(rows, metric, experiment="storage", version=v, **k) / 2**20 for v in vs]
    gz = mb("jsonl_gz_bytes", scenario="default", method="copies")
    gz_low = mb("jsonl_gz_bytes", scenario="low", method="copies")
    default = mb("object_bytes", scenario="default", method="store", buckets=b)
    low = mb("object_bytes", scenario="low", method="store", buckets=b)
    step = lambda ys: (ys[-1] - ys[0]) / (len(ys) - 1)
    def versus(store, copies):
        r = store / copies
        if abs(r - 1) < 0.1:
            return "наравне"
        return f"в {r:.1f} раза меньше".replace(".", ",") if r < 1 else f"в {r:.1f} раза больше".replace(".", ",")

    title = f"Против gzip: {versus(low[-1], gz_low[-1])} при ~1%, {versus(default[-1], gz[-1])} при ~20%"
    fig, ax = plt.subplots(figsize=(10, 5.6), dpi=200)
    close = abs(gz[-1] - low[-1]) < 0.06 * max(gz + default + low)  # end labels would collide
    up = gz[-1] >= low[-1]
    line(ax, vs, gz, ORANGE, "10 копий JSONL в gzip", "gzip-копии", dy=(8 if up else -8) if close else 0)
    line(ax, vs, default, BLUE, "хранилище, ~20% записей меняется за версию", "~20%")
    line(ax, vs, low, AQUA, "хранилище, ~1% записей меняется за версию", "~1%", dy=(-8 if up else 8) if close else 0)
    ax.set_xticks(vs)
    ax.set_ylim(0, max(gz + default + low) * 1.08)
    top = max(gz + default + low)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:.0f} МБ" if top >= 10 else f"{v:.1f} МБ"))
    ax.set_xlabel("версия", color=INK2, fontsize=11.5)
    style(ax, title,
          f"{num(n)} записей, {num(b)} бакетов. Первая версия — {low[0]:.1f} МБ против {gz[0]:.1f} МБ gzip; "
          f"каждая следующая: +{step(low):.1f} МБ при ~1%, +{step(default):.1f} МБ при ~20%, у gzip-копии "
          f"+{step(gz):.1f} МБ")
    fig.savefig(os.path.join(out, "storage.png"), facecolor=SURFACE)
    plt.close(fig)


def plot_collisions(rows, out):
    bits = values(rows, "hash_bits", experiment="collisions")
    n = values(rows, "n", experiment="collisions")[0]
    get = lambda b, m: val(rows, m, experiment="collisions", hash_bits=b)
    false = [get(b, "false_renames") for b in bits]
    true = get(bits[-1], "renames_true")
    safe = next((b for i, b in enumerate(bits) if all(x == 0 for x in false[i:])), None)
    fig, ax = plt.subplots(figsize=(10, 5.6), dpi=200)
    xs = range(len(bits))
    ax.bar(xs, false, width=0.6, color=BLUE, edgecolor=SURFACE, linewidth=2)
    for x, f in zip(xs, false):
        ax.annotate(f"{f:.0f}", (x, f), xytext=(0, 6), textcoords="offset points",
                    ha="center", color=INK, fontsize=12)
    ax.set_xticks(list(xs), [f"{b} бит" for b in bits])
    ax.set_ylim(0, max(max(false) * 1.15, 1))
    ax.yaxis.set_major_locator(MaxNLocator(integer=True))
    title = (f"С {safe} бит хеша ложных переименований нет" if safe is not None
             else "Ложные переименования при любой длине хеша")
    style(ax, title, f"{num(n)} записей, {true:.0f} настоящих переименований; ложное — diff связал записи "
                     f"с разным содержимым, у которых совпал хеш")
    ax.set_ylabel("ложных переименований", color=INK2, fontsize=11.5)
    fig.savefig(os.path.join(out, "collisions.png"), facecolor=SURFACE)
    plt.close(fig)


def cell(rows, metric, scenario, method):
    hits = [r for r in rows if r["experiment"] == "baseline" and r["scenario"] == scenario
            and r["method"] == method and r["metric"] == metric]
    return hits[0]["value"]


def mark(fig, x, y, ok, label):
    fig.text(x, y, "✓" if ok else "✗", fontsize=13, fontweight="bold", color=GOOD if ok else CRITICAL,
             va="center")
    fig.text(x + 0.02, y, label, fontsize=12, color=INK, va="center")


def plot_breaks(rows, out):
    """A table as a picture: per phenomenon, v0's and our change rows, verdict and leaks."""
    scenarios = list(dict.fromkeys(r["scenario"] for r in rows
                                   if r["experiment"] == "baseline" and r["scenario"].startswith("break_")))
    fig = plt.figure(figsize=(13.5, 1.9 + 0.95 * len(scenarios)), dpi=200)
    fig.patch.set_facecolor(SURFACE)
    cols = [0.04, 0.23, 0.41, 0.63, 0.81]
    top, step = 0.66, 0.6 / len(scenarios)
    for x, h in zip(cols, ["", "изменения", "вердикт", "изменения", "вердикт"]):
        fig.text(x + (0.02 if h else 0), top + 0.02, h, fontsize=11, color=INK2, va="bottom")
    fig.text(cols[1] + 0.02, top + 0.075, "v0: сопоставление по id", fontsize=12.5, color=INK, fontweight="bold")
    fig.text(cols[3] + 0.02, top + 0.075, "наш diff", fontsize=12.5, color=INK, fontweight="bold")
    fig.add_artist(plt.Line2D([0.04, 0.97], [top + 0.005] * 2, color=GRID, linewidth=1))
    wrong_v0 = wrong_ours = 0
    for i, sc in enumerate(scenarios):
        y = top - step * (i + 0.5)
        want = cell(rows, "verdict", sc, "expected")
        truth_leaks = int(float(cell(rows, "leaks", sc, "truth")))
        fig.text(cols[0], y + 0.018, next(r["label"] for r in rows if r["scenario"] == sc), fontsize=13,
                 color=INK, va="center")
        fig.text(cols[0], y - 0.03, f"ожидается: {VERDICTS[want]}", fontsize=10.5, color=INK2, va="center")
        for k, m in enumerate(("v0", "flat")):
            errors = int(float(cell(rows, "errors", sc, m)))
            changes = int(float(cell(rows, "changes", sc, "truth")))
            got = cell(rows, "verdict", sc, m)
            leaks = int(float(cell(rows, "leaks", sc, m)))
            bad = errors > 0 or got != want or leaks != truth_leaks
            if m == "v0":
                wrong_v0 += bad
            else:
                wrong_ours += bad
            missed = int(float(cell(rows, "missed", sc, m)))
            false = int(float(cell(rows, "false", sc, m)))
            mark(fig, cols[1 + 2 * k], y + 0.018, errors == 0,
                 f"верно {num(changes - missed)} из {num(changes)}")
            if false:
                fig.text(cols[1 + 2 * k] + 0.02, y - 0.03, f"и {num(false)} лишних строк", fontsize=10.5,
                         color=INK2, va="center")
            mark(fig, cols[2 + 2 * k], y + 0.018, got == want, VERDICTS[got])
            if truth_leaks or leaks:
                fig.text(cols[2 + 2 * k] + 0.02, y - 0.03, f"утечек найдено {num(leaks)} из {num(truth_leaks)}",
                         fontsize=10.5, color=INK2, va="center")
        if i:
            fig.add_artist(plt.Line2D([0.04, 0.97], [y + step / 2] * 2, color=GRID, linewidth=0.6))
    title = (f"v0 ошибается в {wrong_v0} сценариях из {len(scenarios)}, наш diff — "
             + ("ни в одном" if not wrong_ours else f"в {wrong_ours}"))
    fig.text(0.04, 0.95, title, fontsize=17, fontweight="bold", color=INK, va="top")
    n = next(r["n"] for r in rows if r["scenario"] == scenarios[0])
    fig.text(0.04, 0.885, textwrap.fill(f"Каждый сценарий — одно явление на спокойной базе из {num(n)} "
                                        f"записей. Верно — изменение найдено с тем же классом, что в truth; "
                                        f"лишние — строки, которых в truth нет", 125),
             fontsize=11.5, color=INK2, va="top")
    fig.savefig(os.path.join(out, "breaks.png"), facecolor=SURFACE)
    plt.close(fig)


def baseline_table(rows):
    scenarios = list(dict.fromkeys(r["scenario"] for r in rows if r["experiment"] == "baseline"))
    lines = ["# v0 (сопоставление по id) / наш плоский / дерево", "",
             "Ошибок — строк изменений, не совпавших с truth (пропущено + лишних; группы неоднозначных "
             "переименований не считаются). Время — сам diff, без чтения файлов; для дерева без commit.", "",
             "| набор | записей | изменений | ожидаемый вердикт | v0: ошибок / вердикт / время "
             "| плоский | дерево |", "|---|---|---|---|---|---|---|"]
    for sc in scenarios:
        label = next(r["label"] for r in rows if r["scenario"] == sc) or sc
        want = cell(rows, "verdict", sc, "expected")
        computed = cell(rows, "verdict_source", sc, "expected") != "preset"

        def method(m):
            got = cell(rows, "verdict", sc, m)
            mark = "" if got == want else " ✗"
            return (f"{num(float(cell(rows, 'errors', sc, m)))} / {VERDICTS[got]}{mark} / "
                    f"{human_seconds(float(cell(rows, 'seconds', sc, m)))}")
        n = next(r["n"] for r in rows if r["scenario"] == sc)
        lines.append(f"| {label} | {num(n)} | {num(float(cell(rows, 'changes', sc, 'truth')))} "
                     f"| {VERDICTS[want]}{' ¹' if computed else ''} | {method('v0')} | {method('flat')} "
                     f"| {method('tree')} |")
    return lines + ["", "Ожидаемый вердикт задан в пресете генератора (`expected_verdict`); ¹ — у пресета его нет, "
                         "он вычислен по truth правилами `make_verdict`.", ""]


def summary_table(rows, out):
    lines = ["# 4096 бакетов против ~n/16", ""]
    for n in values(rows, "n", experiment="flat_vs_tree"):
        b = adaptive_buckets(rows, n)
        lines += [f"## Время diff, n = {num(n)} ({num(b)} бакетов в адаптивном варианте)", "",
                  "| d | плоский | дерево 4096 | дерево ~n/16 | бакетов / записей прочитано, 4096 | "
                  "бакетов / записей прочитано, ~n/16 | commit 4096 | commit ~n/16 |",
                  "|---|---|---|---|---|---|---|---|"]
        for d in [x for x in values(rows, "d", experiment="flat_vs_tree", n=n) if x]:
            k = dict(experiment="flat_vs_tree", n=n, d=d)
            cell = lambda bb, m: val(rows, m, method="tree", buckets=bb, **k)
            lines.append(
                f"| {num(d)} | {human_seconds(val(rows, 'seconds', method='flat', **k))} "
                f"| {human_seconds(cell(FIXED, 'seconds'))} | {human_seconds(cell(b, 'seconds'))} "
                f"| {num(cell(FIXED, 'buckets_read'))} / {num(cell(FIXED, 'records_read'))} "
                f"| {num(cell(b, 'buckets_read'))} / {num(cell(b, 'records_read'))} "
                f"| {human_seconds(val(rows, 'seconds', method='commit', buckets=FIXED, **k))} "
                f"| {human_seconds(val(rows, 'seconds', method='commit', buckets=b, **k))} |")
        first = lambda bb: human_seconds(val(rows, "seconds", experiment="flat_vs_tree", n=n, d=0,
                                             method="commit", buckets=bb))
        lines += ["", f"Первый commit, в пустое хранилище: {first(FIXED)} при 4096 бакетах, {first(b)} при "
                      f"{num(b)}. Время — минимум из 3 прогонов после прогрева.", ""]
    vs = values(rows, "version", experiment="storage", method="copies", scenario="default")
    n = values(rows, "n", experiment="storage")[0]
    b = next(x for x in values(rows, "buckets", experiment="storage") if x != FIXED)
    last = vs[-1]
    lines += [f"## Хранилище, {len(vs)} версий по {num(n)} записей", "",
              "| изменений за версию | бакетов | объекты, МБ | объектов, шт. | du, МБ | JSONL-копии, МБ | gzip-копии, МБ |",
              "|---|---|---|---|---|---|---|"]
    for sc, label in (("low", "~1%"), ("default", "~20%")):
        k = dict(experiment="storage", scenario=sc, version=last)
        for bb in (FIXED, b):
            s = dict(method="store", buckets=bb, **k)
            lines.append(f"| {label} | {num(bb)} | {val(rows, 'object_bytes', **s) / 2**20:.1f} "
                         f"| {num(val(rows, 'objects', **s))} | {val(rows, 'du_bytes', **s) / 2**20:.0f} "
                         f"| {val(rows, 'jsonl_bytes', method='copies', **k) / 2**20:.0f} "
                         f"| {val(rows, 'jsonl_gz_bytes', method='copies', **k) / 2**20:.1f} |")
    if any(r["experiment"] == "baseline" for r in rows):
        lines = baseline_table(rows) + lines
    with open(os.path.join(out, "summary.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    here = os.path.dirname(os.path.abspath(__file__))
    ap.add_argument("--csv", default=os.path.join(here, "results.csv"))
    ap.add_argument("--baseline-csv", default=os.path.join(here, "baseline.csv"),
                    help="if present, its baseline rows replace those in --csv (bench --only baseline output)")
    ap.add_argument("--out", default=here)
    a = ap.parse_args(argv)
    rows = load(a.csv)
    if os.path.exists(a.baseline_csv):
        rows = [r for r in rows if r["experiment"] != "baseline"] + load(a.baseline_csv)
    base = any(r["experiment"] == "baseline" for r in rows)
    os.makedirs(a.out, exist_ok=True)
    plot_time(rows, a.out)
    plot_extrapolation(rows, a.out)
    plot_storage(rows, a.out)
    plot_collisions(rows, a.out)
    if base:
        plot_breaks(rows, a.out)
    summary_table(rows, a.out)
    print(f"-> {a.out}/time_vs_d.png, extrapolation.png, storage.png, collisions.png"
          f"{', breaks.png' if base else ''}, summary.md")


if __name__ == "__main__":
    main()
