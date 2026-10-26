"""dsdiff ui in a real browser (Playwright, a dev-only dependency; skipped without it): screens render,
the compare page is fast on medium and never holds more than one page of changes.
Screenshots go to $DSDIFF_SCREENSHOTS if set, else to a temp dir."""
import json
import os
import threading
import time

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

from dsdiff.gen import main as gen  # noqa: E402
from dsdiff.ui.server import make_server  # noqa: E402

from test_ui import Client  # noqa: E402

VIEWPORT = {"width": 1440, "height": 900}


@pytest.fixture(scope="module")
def shots(tmp_path_factory):
    d = os.environ.get("DSDIFF_SCREENSHOTS") or str(tmp_path_factory.mktemp("shots"))
    os.makedirs(d, exist_ok=True)
    return d


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:  # browser not downloaded
            pytest.skip(f"Chromium for Playwright is not installed: {e}")
        yield b
        b.close()


def serve(root, **kw):
    server, app = make_server(str(root), **kw)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, Client(app)


def repo_with(root, preset):
    gen(["--preset", preset, "--out", str(root / "data")])
    server, ui = serve(root)
    os.makedirs(root / "repo", exist_ok=True)
    status, out = ui.post("/api/repo", {"dir": "repo", "create": True})
    assert ui.wait(out["job"])["state"] == "done"
    for v in (1, 2):
        assert ui.wait(ui.commit(f"data/v{v}.jsonl", f"{preset} v{v}"))["state"] == "done"
    return server, ui


def page_for(browser, ui, theme="light"):
    ctx = browser.new_context(viewport=VIEWPORT, color_scheme=theme)
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: m.type == "error" and not m.text.startswith("Failed to load resource")
            and errors.append(m.text))  # 4xx answers the page handles are logged by the browser, not errors
    return ctx, page, errors


def test_story_screens(browser, tmp_path, shots):
    server, ui = repo_with(tmp_path, "story")
    try:
        ctx, page, errors = page_for(browser, ui)
        page.goto(ui.base + "/#/versions")
        page.wait_for_selector("table tbody tr")
        assert page.locator("tbody tr").count() == 2
        assert "из CLI" not in page.inner_text("tbody")  # both committed through the ui: dated, with a source
        page.screenshot(path=f"{shots}/story-1-versions.png", full_page=True)
        page.click("text=Добавить версию")
        page.click(".browser button >> text=data")
        page.click('.browser button >> text="v2.jsonl"')
        page.fill("input[type=text]", "v3: та же v2 ещё раз")
        page.screenshot(path=f"{shots}/story-2-add-version.png", full_page=True)
        page.click("text=Закрыть")
        page.click("text=Сравнить A → B")
        page.wait_for_selector(".verdict")
        page.wait_for_selector("tbody tr td .chip")
        assert "Не доверять" in page.inner_text(".verdict .label")
        assert page.locator("tbody tr").count() == 124  # 314 changes, 190 of them pure cosmetic: hidden
        assert "показать косметику (190)" in page.inner_text(".filters")
        first = page.locator("tbody tr").first.inner_text()
        assert "влияет на вердикт" in first  # the rows behind the verdict come first
        page.screenshot(path=f"{shots}/story-3-compare.png")
        moved = page.inner_text("button.tile[data-type=group_moved] .v")
        page.click("button.tile[data-type=group_moved]")
        page.wait_for_function(f"document.querySelector('.pager span').textContent.endsWith('из {moved}')")
        rows = page.locator("tbody tr")
        assert all("перенесено" in rows.nth(i).inner_text() for i in range(rows.count()))
        page.screenshot(path=f"{shots}/story-4-filter-moved.png")
        page.click("text=Утечки")
        page.wait_for_selector("text=Как появилась")
        page.screenshot(path=f"{shots}/story-5-leaks.png")
        ctx.close()
        ctx, page, errors_dark = page_for(browser, ui, theme="dark")
        page.goto(ui.base + "/#/versions")
        page.wait_for_selector("table tbody tr")
        page.click("text=Сравнить A → B")
        page.wait_for_selector("tbody tr td .chip")
        bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
        assert bg == "rgb(18, 18, 17)", bg
        page.screenshot(path=f"{shots}/story-6-compare-dark.png")
        ctx.close()
        assert not errors and not errors_dark, errors + errors_dark
    finally:
        server.shutdown()
        server.server_close()


def test_medium_compare_fast_one_page(browser, tmp_path, shots):
    server, ui = repo_with(tmp_path, "medium")
    try:
        rid = ui.compare({"a": "HEAD~1", "b": "HEAD"})  # the report is ready before the page opens
        ctx, page, errors = page_for(browser, ui)
        sizes = []

        def on_response(r):
            if "/changes?" in r.url:
                sizes.append(len(json.loads(r.body())["items"]))
        page.on("response", on_response)
        t = time.perf_counter()
        page.goto(f"{ui.base}/#/compare/{rid}")
        page.wait_for_selector("tbody tr td .chip")
        elapsed = time.perf_counter() - t
        assert elapsed < 2.0, elapsed
        assert sizes == [200]  # one page, nothing prefetched
        page.screenshot(path=f"{shots}/medium-1-compare.png")
        pager = lambda: page.inner_text(".pager span")
        before = pager()
        page.fill("input[type=search]", "img_00")
        page.wait_for_function("t => document.querySelector('.pager span').textContent !== t", arg=before)
        searched = pager()
        page.select_option("select[aria-label=Группа]", "test")
        page.wait_for_function("t => document.querySelector('.pager span').textContent !== t", arg=searched)
        assert all(s <= 200 for s in sizes)
        page.screenshot(path=f"{shots}/medium-2-search.png")
        page.goto(ui.base + "/#/versions")  # a commit in the background: progress in the header
        page.click("text=Добавить версию")
        page.click('.browser button >> text="📁 data"')
        page.click('.browser button >> text="v2.jsonl"')
        page.fill("input[type=text]", "medium v2 ещё раз")
        page.click("text=Сохранить версию")
        page.wait_for_selector(".job")
        page.click("text=Сравнение")  # the page stays usable while the commit runs
        page.wait_for_selector(".verdict")
        page.screenshot(path=f"{shots}/medium-3-commit-in-background.png")
        page.click("text=Версии")
        page.wait_for_function("document.querySelectorAll('tbody tr').length === 3", timeout=120_000)
        ctx.close()
        assert not errors, errors
        print(f"medium compare page: {elapsed:.2f} s to the first rows; /changes responses: {sizes}")
    finally:
        server.shutdown()
        server.server_close()


def test_no_repo_and_big_files_warning(browser, tmp_path, shots):
    gen(["--preset", "story", "--out", str(tmp_path / "data")])
    server, ui = serve(tmp_path, confirm_seconds=0.1)  # story's ~0.4 s counts as long here
    try:
        ctx, page, errors = page_for(browser, ui)
        page.goto(ui.base + "/")
        page.wait_for_selector("text=Репозиторий не открыт")
        page.screenshot(path=f"{shots}/norepo-1.png", full_page=True)
        cards = page.locator(".card", has_text="Сравнить два файла")
        cards.locator(".browser").nth(0).locator("text=data").click()
        cards.locator(".browser").nth(0).locator('text="v1.jsonl"').click()
        cards.locator(".browser").nth(1).locator("text=data").click()
        cards.locator(".browser").nth(1).locator('text="v2.jsonl"').click()
        cards.locator("button.primary", has_text="Сравнить").click()
        page.wait_for_selector(".warnbox")
        assert "Плоское сравнение займёт" in page.inner_text(".warnbox")
        page.screenshot(path=f"{shots}/norepo-2-warning.png", full_page=True)
        page.click("text=Всё равно сравнить")
        page.wait_for_selector(".verdict")
        lines = (tmp_path / "data/v1.jsonl").read_text(encoding="utf-8").splitlines()
        lines[6] = lines[6][:40]  # line 7 cut in the middle
        (tmp_path / "data/broken.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
        rid = ui.post("/api/compare", {"file_a": "data/broken.jsonl", "file_b": "data/v2.jsonl", "confirm": True})[1]
        page.goto(f"{ui.base}/#/compare/{rid['report']}")
        page.wait_for_selector("text=Сравнение не получилось")
        assert "строка 7" in page.inner_text("#view") and "столбец 41" in page.inner_text("#view")
        assert "Traceback" not in page.content()
        page.screenshot(path=f"{shots}/norepo-3-broken-jsonl.png")
        ctx.close()
        assert not errors, errors
    finally:
        server.shutdown()
        server.server_close()


def test_record_and_patch_screens(browser, tmp_path, shots):
    """tiny v1, v2 and v3 (r1_000 renamed again to r2_000 and relabeled); record screen with the trace;
    patch check, good and with the wrong base; a download through the one-time link."""
    gen(["--preset", "tiny", "--out", str(tmp_path / "data")])
    v2 = [json.loads(x) for x in (tmp_path / "data/v2.jsonl").read_text(encoding="utf-8").splitlines()]
    v3 = [dict(r, sample_id="images/r2_000.jpg", labels=["zebra"]) if r["sample_id"] == "images/r1_000.jpg" else r
          for r in v2]
    (tmp_path / "data/v3.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in v3),
                                            encoding="utf-8")
    server, ui = serve(tmp_path)
    try:
        os.makedirs(tmp_path / "repo")
        assert ui.wait(ui.post("/api/repo", {"dir": "repo", "create": True})[1]["job"])["state"] == "done"
        for v in (1, 2, 3):
            assert ui.wait(ui.commit(f"data/v{v}.jsonl", f"tiny v{v}"))["state"] == "done"
        rid12 = ui.compare({"a": "HEAD~2", "b": "HEAD~1"})
        _, patch = ui.get(f"/api/report/{rid12}/export/patch.jsonl", raw=True)
        (tmp_path / "data/patch_v1_v2.jsonl").write_bytes(patch)
        rid23 = ui.compare({"a": "HEAD~1", "b": "HEAD"})

        ctx, page, errors = page_for(browser, ui)
        page.goto(f"{ui.base}/#/compare/{rid23}")
        page.wait_for_selector("tbody tr td .chip")
        page.click("a >> text=images/r2_000.jpg")  # from the changes table to the record
        page.wait_for_selector("text=В A записи images/r2_000.jpg нет")
        page.wait_for_selector("text=История по версиям")
        assert "zebra" in page.inner_text("table") and "Проследить переименования" in page.inner_text("#view")
        page.screenshot(path=f"{shots}/step4-1-record-renamed.png", full_page=True)
        page.click("button >> text=Проследить переименования")
        page.wait_for_selector("text=История с переименованиями")
        text = page.inner_text("#view")
        assert "раньше: images/r1_000.jpg" in text and "раньше: images/img_000.jpg" in text
        page.screenshot(path=f"{shots}/step4-2-record-traced.png", full_page=True)

        page.goto(f"{ui.base}/#/record?id=images/img_001.jpg&report={rid12}")
        page.wait_for_selector("text=отличается")
        page.screenshot(path=f"{shots}/step4-3-record-relabeled.png", full_page=True)

        page.goto(f"{ui.base}/#/compare/{rid12}")
        page.wait_for_selector("tbody tr td .chip")
        page.click("text=Выгрузить")
        with page.expect_download() as dl:
            page.click("text=Патч A → B")
        assert open(dl.value.path(), "rb").read() == patch

        page.goto(f"{ui.base}/#/patch")
        page.wait_for_selector("text=Проверка патча")
        selects = page.locator("select")
        selects.nth(0).select_option(label=next(o for o in selects.nth(0).locator("option").all_inner_texts()
                                                if o.startswith("tiny v1")))
        selects.nth(1).select_option(label=next(o for o in selects.nth(1).locator("option").all_inner_texts()
                                                if o.startswith("tiny v2")))
        page.click('.browser button >> text="📁 data"')
        page.click('.browser button >> text="patch_v1_v2.jsonl"')
        page.click("button.primary >> text=Проверить")
        page.wait_for_selector("text=Патч воспроизводит цель")
        page.screenshot(path=f"{shots}/step4-4-patch-ok.png", full_page=True)
        selects.nth(0).select_option(label=next(o for o in selects.nth(0).locator("option").all_inner_texts()
                                                if o.startswith("tiny v3")))
        page.click("button.primary >> text=Проверить")
        page.wait_for_selector("text=Патч не применился")
        assert "другой базовой версии" in page.inner_text(".verdict")
        page.screenshot(path=f"{shots}/step4-5-patch-wrong-base.png", full_page=True)
        ctx.close()
        assert not errors, errors
    finally:
        server.shutdown()
        server.server_close()
