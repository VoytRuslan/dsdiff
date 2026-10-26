/* dsdiff ui: one page over the JSON API (dsdiff/ui/API.md). Data from manifests only ever goes into the
   page as text (h() sets textContent), never as HTML. */
"use strict";

const TYPE_LABELS = {
  renamed: "переименовано", relabeled: "перемечено", content_replaced: "заменено содержимое",
  group_moved: "перенесено", metadata_changed: "метаданные", cosmetic: "косметика",
  added: "добавлено", deleted: "удалено", ambiguous: "неоднозначно",
};
const TYPE_ORDER = ["renamed", "relabeled", "content_replaced", "group_moved", "metadata_changed", "cosmetic",
                    "added", "deleted"];
const VERDICT_ICON = ["✓", "!", "!", "✕"];
const cap = (s) => s.charAt(0).toUpperCase() + s.slice(1);
const PAGE = 200;
const fmt = new Intl.NumberFormat("ru-RU");
const n = (x) => (x === null || x === undefined ? "—" : fmt.format(x));

/* ---------- small DOM helper ---------- */
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "dataset") Object.assign(el.dataset, v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}
const view = () => document.getElementById("view");
const kept = (kids) => kids.flat(Infinity).filter((k) => k !== null && k !== undefined && k !== false);
function fill(el, ...kids) { el.replaceChildren(...kept(kids)); }  // the DOM's own would print "null"
function mount(...kids) { fill(view(), ...kids); }

/* ---------- API ---------- */
class ApiError extends Error {
  constructor(status, body) {
    super((body && body.message) || `Ошибка ${status}`);
    this.status = status; this.body = body || {};
  }
}
async function api(method, path, body) {
  const opts = { method, headers: { "X-Dsdiff-Token": window.DSDIFF_TOKEN } };
  if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  let r;
  try { r = await fetch(path, opts); }
  catch (e) { throw new ApiError(0, { message: "Сервер dsdiff не отвечает: он остановлен? Запустите `python -m dsdiff ui` снова." }); }
  const text = await r.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch (e) { data = { message: text }; }
  if (r.status >= 400) throw new ApiError(r.status, data);
  return { status: r.status, data };
}
const get = (p) => api("GET", p).then((r) => r.data);
const sleep = (ms) => new Promise((res) => setTimeout(res, ms));

/* ---------- errors ---------- */
function showError(e) {
  const box = document.getElementById("error");
  const body = (e && e.body) || {};
  const detail = body.detail && Object.keys(body.detail).length && body.detail.text
    ? h("pre", {}, body.detail.text) : null;
  fill(box, h("button", { class: "link x", onclick: hideError, "aria-label": "Скрыть" }, "Скрыть"),
                      h("b", {}, (e && e.message) || String(e)), detail);
  box.hidden = false;
  if (!(e instanceof ApiError)) console.error(e);
}
function hideError() { document.getElementById("error").hidden = true; }
const guard = (fn) => async (...a) => { try { hideError(); await fn(...a); } catch (e) { showError(e); } };

/* ---------- theme ---------- */
const THEMES = ["auto", "light", "dark"];
const THEME_NAMES = { auto: "авто", light: "светлая", dark: "тёмная" };
function applyTheme(t) {
  if (t === "auto") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = t;
  document.getElementById("theme").textContent = `Тема: ${THEME_NAMES[t]}`;
  try { localStorage.setItem("dsdiff-theme", t); } catch (e) {}
}
function currentTheme() { return document.documentElement.dataset.theme || "auto"; }

/* ---------- app state, header, jobs ---------- */
const S = { state: null, jobs: new Map(), lastReport: null };

async function refreshState() {
  S.state = await get("/api/state");
  const st = S.state;
  const base = (p) => p.split("/").filter(Boolean).pop() || "/";
  fill(document.getElementById("where"),
    "папка ", h("b", { title: st.root }, base(st.root)), "  ·  репозиторий ",
    st.repo ? h("b", { title: st.repo }, st.repo === st.root ? "в этой папке" : base(st.repo)) : h("span", { class: "faint" }, "не открыт"));
  return st;
}

function jobText(j) {
  if (j.state === "queued") return j.queue_position ? `в очереди (${j.queue_position} перед ней)` : "в очереди";
  if (j.stage === "wait_lock") return "ждёт: в репозиторий пишет другой процесс";
  if (j.stage === "read") {
    if (j.kind === "compare") {
      const a = j.lines_read_a || 0, b = j.lines_read_b || 0;
      return `чтение: A ${n(a)} строк${j.lines_read_b !== undefined ? `, B ${n(b)}` : ""}`;
    }
    return `прочитано ${n(j.lines_read || 0)} из ~${n(j.lines_total || 0)} строк`;
  }
  if (j.stage === "commit") return j.objects_written ? `записано объектов: ${n(j.objects_written)}` : "подготовка: нормализация и бакеты";
  if (j.stage === "diff") return "сравнение…";
  if (j.stage === "create") return "создание репозитория";
  return j.stage;
}
function jobFraction(j) {
  if (j.stage === "read" && j.kind !== "compare" && j.lines_total) return Math.min(1, j.lines_read / j.lines_total);
  if (j.stage === "read" && j.kind === "compare") {  // B is assumed as long as A until it is being read
    const ta = j.lines_total_a || 0, tb = j.lines_total_b ?? ta;
    return ta + tb ? Math.min(1, ((j.lines_read_a || 0) + (j.lines_read_b || 0)) / (ta + tb)) : null;
  }
  return null;
}
function bar(frac) {
  const b = h("div", { class: "bar" + (frac === null ? " indet" : "") }, h("i"));
  if (frac !== null) b.firstChild.style.width = `${Math.round(frac * 100)}%`;
  return b;
}
const JOB_TITLES = { commit: "Версия", create_repo: "Репозиторий", compare: "Сравнение", trace: "История" };

/* Track a job in the header until it ends; resolves with the finished job. */
function track(jobId, title) {
  return new Promise((resolve, reject) => {
    const entry = { id: jobId, title, el: h("div", { class: "job" }) };
    S.jobs.set(jobId, entry);
    const tick = async () => {
      let j;
      try { j = await get(`/api/jobs/${jobId}`); }
      catch (e) { S.jobs.delete(jobId); renderJobs(); reject(e); return; }
      entry.job = j;
      renderJobs();
      if (j.state === "queued" || j.state === "running") { setTimeout(tick, 300); return; }
      S.jobs.delete(jobId); renderJobs();
      if (j.state === "done") resolve(j);
      else if (j.state === "cancelled") reject(new ApiError(0, { message: `${title}: отменено.` }));
      else reject(new ApiError(0, j.error || { message: `${title}: ошибка.` }));
    };
    tick();
  });
}
function renderJobs() {
  const box = document.getElementById("jobs");
  fill(box, ...[...S.jobs.values()].filter((e) => e.job).map((e) => {
    const j = e.job;
    return h("div", { class: "job" },
      h("span", { class: "jt" }, h("b", {}, e.title), ": ", jobText(j), ` · ${Math.round(j.elapsed)} с`),
      bar(jobFraction(j)),
      h("button", { class: "link", onclick: () => api("DELETE", `/api/jobs/${j.id}`).catch(showError) }, "Отменить"));
  }));
}

/* ---------- file browser ---------- */
function fileBrowser(onPick, { dirs = false } = {}) {
  const box = h("div", { class: "browser" });
  let selected = null;
  async function open(dir) {
    const d = await get(`/api/files?dir=${encodeURIComponent(dir)}`);
    const rows = [];
    if (d.parent !== null) rows.push(h("button", { onclick: guard(() => open(d.parent)) }, h("span", {}, "↑ .."), h("span")));
    for (const e of d.entries) {
      const path = d.dir ? `${d.dir}/${e.name}` : e.name;
      if (e.type === "dir") {
        rows.push(h("button", { onclick: guard(() => { if (dirs) pick(path, null); return open(path); }) },
                    h("span", {}, `📁 ${e.name}`), h("span")));
      } else if (!dirs) {
        const b = h("button", { onclick: () => pick(path, b) },
                    h("span", { class: "mono" }, e.name), h("span", { class: "size" }, size(e.size)));
        if (selected === path) b.classList.add("sel");
        rows.push(b);
      }
    }
    if (!d.entries.length) rows.push(h("div", { class: "empty" }, dirs ? "Вложенных папок нет" : "Здесь нет файлов .jsonl"));
    const rootName = S.state.root.split("/").pop() || "/";
    fill(box, h("div", { class: "crumb", title: `${S.state.root}/${d.dir}` }, d.dir ? `${rootName}/${d.dir}` : rootName), ...rows);
    if (dirs) pick(d.dir, null);
  }
  function pick(path, btn) {
    selected = path;
    box.querySelectorAll("button.sel").forEach((x) => x.classList.remove("sel"));
    if (btn) btn.classList.add("sel");
    onPick(path);
  }
  open("").catch(showError);
  return box;
}
function size(b) {
  if (b === undefined) return "";
  const u = ["Б", "КБ", "МБ", "ГБ"]; let i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return `${b.toLocaleString("ru-RU", { maximumFractionDigits: i ? 1 : 0 })} ${u[i]}`;
}

/* ---------- screen: versions ---------- */
async function screenVersions() {
  const st = await refreshState();
  if (!st.repo) return mount(noRepo());
  const { items } = await get("/api/commits?limit=1000");
  const sel = { a: items[1] ? items[1].hash : null, b: items[0] ? items[0].hash : null };
  const addPanel = h("div");
  const compareBtn = h("button", { class: "primary", onclick: guard(() => compareRevs(sel.a, sel.b)) }, "Сравнить A → B");
  const sync = () => { compareBtn.disabled = !sel.a || !sel.b || sel.a === sel.b; };
  const radio = (side, hash) => {
    const r = h("input", { type: "radio", name: `sel-${side}`, "aria-label": `${side.toUpperCase()}: ${hash.slice(0, 12)}`,
                           onchange: () => { sel[side] = hash; sync(); } });
    r.checked = sel[side] === hash;
    return r;
  };
  const table = items.length ? h("div", { class: "card tablewrap" }, h("table", {},
    h("thead", {}, h("tr", {}, h("th", {}, "A"), h("th", {}, "B"), h("th", {}, "Версия"), h("th", {}, "Хеш"),
      h("th", { class: "num" }, "Записей"), h("th", { class: "num" }, "train / val / test"),
      h("th", { class: "num" }, "Утечки"), h("th", {}, "Валидация"), h("th", {}, "Добавлена"))),
    h("tbody", {}, items.map((c, i) => h("tr", {},
      h("td", { class: "sel-ab" }, radio("a", c.hash)), h("td", { class: "sel-ab" }, radio("b", c.hash)),
      h("td", {}, c.message, i === 0 ? h("span", { class: "badge-head" }, "HEAD") : null),
      h("td", { class: "mono" }, c.short),
      h("td", { class: "num" }, n(c.records)),
      h("td", { class: "num" }, `${n(c.groups.train)} / ${n(c.groups.val)} / ${n(c.groups.test)}`),
      h("td", { class: "num" }, c.leaks ? h("span", { class: "pill bad", title: "train/test: одно содержимое в обеих группах" }, n(c.leaks)) : "0"),
      h("td", {}, validationCell(c.validation)),
      h("td", {}, c.log ? [new Date(c.log.time).toLocaleString("ru-RU"), h("div", { class: "faint mono" }, c.log.file)]
                        : h("span", { class: "faint" }, "из CLI")))))))
    : h("div", { class: "card empty" }, "В репозитории ещё нет версий. Добавьте первую.");
  sync();
  mount(h("div", { class: "stack" },
    h("div", { class: "row between" }, h("h1", {}, "Версии"),
      h("div", { class: "row" },
        h("button", { onclick: () => fill(addPanel, addVersion(addPanel)) }, "Добавить версию"),
        compareBtn)),
    addPanel, table,
    h("div", { class: "faint" }, "Сравнить два файла без репозитория: ",
      h("button", { class: "link", onclick: () => fill(addPanel, compareFilesCard()) }, "выбрать файлы"))));
}
function validationCell(v) {
  const parts = [];
  if (v.duplicate_sample_id) parts.push(h("span", { class: "pill warn", title: "повторяющиеся sample_id" }, `дубли ${n(v.duplicate_sample_id)}`));
  if (v.conflicting_labels) parts.push(h("span", { class: "pill warn", title: "одно содержимое с разными метками" }, `метки ${n(v.conflicting_labels)}`));
  return parts.length ? h("span", { class: "row" }, parts) : h("span", { class: "faint" }, "—");
}

function addVersion(panel) {
  let file = null;
  const msg = h("input", { type: "text", placeholder: "Сообщение, например «v3: перемечены собаки»", style: "width: 420px" });
  const picked = h("div", { class: "picked faint" }, "файл не выбран");
  const save = h("button", { class: "primary", disabled: true }, "Сохранить версию");
  save.addEventListener("click", guard(async () => {
    const { data } = await api("POST", "/api/commit", { file, message: msg.value });
    fill(panel);
    await track(data.job, "Версия");
    await screenVersions();
  }));
  const browser = fileBrowser((p) => { file = p; picked.textContent = p; picked.classList.remove("faint"); save.disabled = false; });
  return h("div", { class: "card stack" }, h("h2", {}, "Добавить версию"),
    h("div", { class: "muted" }, "JSONL-файл из папки ", h("code", {}, S.state.root),
      ". Версия сохранится в фоне: можно продолжать работу, ход виден вверху справа."),
    browser, picked, h("div", { class: "row" }, msg, save,
      h("button", { onclick: () => fill(panel) }, "Закрыть")));
}

function noRepo() {
  let dir = "";
  const create = h("button", { class: "primary" }, "Создать репозиторий здесь");
  const shown = h("div", { class: "picked" }, `${S.state.root}/`);
  create.addEventListener("click", guard(async () => {
    const { data } = await api("POST", "/api/repo", { dir, create: true });
    await track(data.job, "Репозиторий");
    await screenVersions();
  }));
  const open = h("button", { onclick: guard(async () => { await api("POST", "/api/repo", { dir }); await screenVersions(); }) },
                 "Открыть существующий");
  return h("div", { class: "stack" }, h("h1", {}, "Версии"),
    h("div", { class: "muted" }, "Репозиторий не открыт. Версии хранятся в папке .dsdiff внутри выбранной папки."),
    h("div", { class: "grid2" },
      h("div", { class: "card stack" }, h("h2", {}, "Репозиторий в папке"),
        fileBrowser((p) => { dir = p; shown.textContent = `${S.state.root}/${p}`; }, { dirs: true }),
        shown, h("div", { class: "row" }, create, open)),
      compareFilesCard()));
}

function compareFilesCard() {
  const sel = { a: null, b: null };
  const go = h("button", { class: "primary", disabled: true }, "Сравнить");
  const out = h("div");
  const label = (side) => h("div", { class: "picked faint", id: `pick-${side}` }, "не выбран");
  const la = label("a"), lb = label("b");
  const pick = (side, el) => (p) => { sel[side] = p; el.textContent = p; el.classList.remove("faint"); go.disabled = !(sel.a && sel.b); };
  go.addEventListener("click", guard(async () => {
    const est = (await api("POST", "/api/compare/estimate", { file_a: sel.a, file_b: sel.b })).data;
    if (!est.warn) return compareFiles(sel.a, sel.b, false);
    fill(out, h("div", { class: "warnbox stack" },
      h("div", {}, h("b", {}, `Плоское сравнение займёт ${est.seconds >= 1 ? `~${n(est.seconds)} с` : "меньше секунды"}`),
        ` (A: ${n(est.lines_a)} строк, B: ${n(est.lines_b)}). Быстрее — добавить обе версии в репозиторий: `,
        "потом любое их сравнение займёт доли секунды."),
      h("div", { class: "row" },
        h("button", { onclick: guard(() => compareFiles(sel.a, sel.b, true)) }, "Всё равно сравнить"),
        h("button", { class: "primary", onclick: guard(() => addBothAndCompare(sel.a, sel.b)) }, "Добавить в репозиторий"))));
  }));
  return h("div", { class: "card stack" }, h("h2", {}, "Сравнить два файла без репозитория"),
    h("div", { class: "grid2" },
      h("div", { class: "stack" }, h("b", {}, "A — старая версия"), fileBrowser(pick("a", la)), la),
      h("div", { class: "stack" }, h("b", {}, "B — новая версия"), fileBrowser(pick("b", lb)), lb)),
    h("div", { class: "row" }, go), out);
}

async function addBothAndCompare(a, b) {
  let st = await refreshState();
  if (!st.repo) {
    const { data } = await api("POST", "/api/repo", { dir: "", create: true });
    await track(data.job, "Репозиторий");
    st = await refreshState();
  }
  const name = (p) => p.split("/").pop();
  const ja = (await api("POST", "/api/commit", { file: a, message: name(a) })).data.job;
  const jb = (await api("POST", "/api/commit", { file: b, message: name(b) })).data.job;  // queued after A
  const [ra, rb] = await Promise.all([track(ja, "Версия A"), track(jb, "Версия B")]);
  await compareRevs(ra.result.commit, rb.result.commit);
}

async function compareRevs(a, b) {
  const { data } = await api("POST", "/api/compare", { a, b });
  location.hash = `#/compare/${data.report}`;
}
async function compareFiles(a, b, confirm) {
  const { data } = await api("POST", "/api/compare", { file_a: a, file_b: b, confirm });
  location.hash = `#/compare/${data.report}`;
}

/* ---------- screen: compare ---------- */
async function screenCompare(rid) {
  if (!rid) {
    if (S.lastReport) { location.replace(`#/compare/${S.lastReport}`); return; }
    return mount(h("div", { class: "card empty" }, "Сравнения пока нет. Выберите две версии на экране ",
                   h("a", { href: "#/versions" }, "«Версии»"), " или два файла."));
  }
  await refreshState();
  let first = true;
  for (;;) {  // wait for the report, showing the job
    let r;
    try { r = await api("GET", `/api/report/${rid}/summary`); }
    catch (e) {
      const text = e.body && e.body.detail && e.body.detail.text;
      mount(h("div", { class: "card empty" }, h("b", {}, "Сравнение не получилось. "), e.message, " ",
            h("a", { href: "#/versions" }, "К версиям"), text ? h("pre", { class: "mono faint" }, text) : null));
      return;
    }
    if (r.status === 200) { S.lastReport = rid; return renderReport(r.data); }
    if (location.hash !== `#/compare/${rid}`) return;  // the user went elsewhere
    const j = r.data.job ? await get(`/api/jobs/${r.data.job}`).catch(() => null) : null;
    if (first || j) {
      mount(h("div", { class: "card progress-big" }, h("h2", {}, "Сравнение считается"),
        h("div", { class: "muted" }, j ? jobText(j) : "…"), bar(j ? jobFraction(j) : null),
        h("div", { class: "faint" }, j ? `прошло ${Math.round(j.elapsed)} с` : ""),
        j ? h("button", { onclick: () => api("DELETE", `/api/jobs/${j.id}`).catch(showError) }, "Отменить") : null));
      first = false;
    }
    await sleep(300);
  }
}

function sourceTitle(src) {
  if (src.kind === "rev") {
    return [h("span", {}, "A: ", h("b", {}, src.a_label), " ", h("code", {}, src.a.slice(0, 12))),
            h("span", { class: "arrow" }, " → "),
            h("span", {}, "B: ", h("b", {}, src.b_label), " ", h("code", {}, src.b.slice(0, 12)))];
  }
  return [h("span", {}, "A: ", h("code", {}, src.a)), h("span", { class: "arrow" }, " → "), h("span", {}, "B: ", h("code", {}, src.b))];
}
function idOf(example) { return String(example).replace(/ \(.*\)$/, ""); }  // "img_1.jpg (relabeled)" -> id
function recordLink(id, rid) {
  return h("a", { href: `#/record?id=${encodeURIComponent(id)}${rid ? `&report=${rid}` : ""}` }, id);
}

function renderReport(s) {
  const rid = s.id;
  const v = s.verdict;
  const main = v.reasons.find((r) => r.code === v.code) || v.reasons[0];
  const others = v.reasons.filter((r) => r !== main);
  const verdict = h("section", { class: `verdict s${v.severity}`, "aria-label": "Вердикт" },
    h("div", { class: "label" }, h("span", { class: "icon", "aria-hidden": "true" }, VERDICT_ICON[v.severity]), cap(v.label)),
    h("div", { class: "why" }, reasonText(main)),
    main.examples.length ? h("div", { class: "ex" }, "Например: ", main.examples.map((x) => [recordLink(idOf(x), rid), " "])) : null,
    others.length ? h("details", {}, h("summary", {}, `Ещё причины: ${others.length}`),
      h("ul", {}, others.map((r) => h("li", {}, `${reasonText(r)}. `,
        r.examples.length ? ["Например: ", r.examples.map((x) => [recordLink(idOf(x), rid), " "])] : null)))) : null);

  const table = changesTable(s);
  const tile = (k, v2, extra = {}) => h(extra.button ? "button" : "div",
    { class: `tile${extra.cls ? " " + extra.cls : ""}`, onclick: extra.onclick, "aria-pressed": extra.pressed, title: extra.title },
    h("div", { class: "v" }, n(v2)), h("div", { class: "k" }, k));
  const typeTiles = [...TYPE_ORDER, "ambiguous"].map((t) => {
    const count = t === "ambiguous" ? s.ambiguous_renames : (s.changes_by_type[t] || 0);
    const b = tile(TYPE_LABELS[t], count, { button: true, cls: count ? "" : "zero", title: "Показать только этот тип; ещё раз — убрать фильтр",
      onclick: () => { table.toggleType(t); syncTiles(); } });
    b.dataset.type = t;
    return b;
  });
  const syncTiles = () => typeTiles.forEach((b) => {
    const on = table.filters.types.has(b.dataset.type);
    b.classList.toggle("on", on); b.setAttribute("aria-pressed", on);
  });
  table.onFiltersChange = syncTiles;
  const st = s.stats;
  const total = Object.values(s.timings).reduce((a, b) => a + b, 0);
  const meta = h("div", { class: "meta" },
    s.sides ? `по дереву: прочитано до ${n(Math.max(s.sides.a, s.sides.b))} из ${n(s.sides.buckets)} ${plural(s.sides.buckets, "бакета", "бакетов", "бакетов")} в каждой версии (A: ${n(s.sides.a)}, B: ${n(s.sides.b)}), записей ${n(st.records_read)}`
      : st && st.mode ? `${modeText(st.mode)}: прочитаны обе версии целиком`
      : "плоское сравнение файлов: прочитаны обе версии целиком",
    ` · ${seconds(total)}`);

  const tabsBox = h("div");
  const tabs = [["changes", `Изменения (${n(s.changes_total)})`], ["leaks", `Утечки (${n(s.leaks.within + s.leaks.by_move)})`],
                ["validation", `Валидация (${n(s.validation.old + s.validation.new)})`]];
  const tabBar = h("div", { class: "tabs", role: "tablist" });
  const show = (key) => {
    tabBar.querySelectorAll("button").forEach((b) => b.classList.toggle("on", b.dataset.k === key));
    if (key === "changes") fill(tabsBox, table.el);
    if (key === "leaks") fill(tabsBox, leaksTab(s));
    if (key === "validation") fill(tabsBox, validationTab(s));
  };
  tabs.forEach(([k, label]) => tabBar.append(h("button", { role: "tab", dataset: { k }, onclick: () => show(k) }, label)));
  show("changes");

  mount(h("div", { class: "stack" },
    h("div", { class: "row between" }, h("div", {}, h("h1", {}, "Сравнение"), h("div", { class: "muted" }, sourceTitle(s.source))),
      h("div", { class: "row" }, copyCli(s), exportMenu(rid))),
    verdict,
    h("div", { class: "tiles" }, tile("записей в A", s.n_old), tile("записей в B", s.n_new), tile("без изменений", s.unchanged),
      ...typeTiles),
    meta, h("div", {}, tabBar, tabsBox)));
  table.load();
}

function reasonText(r) {
  return r.count ? `${n(r.count)} ${plural(r.count, "запись", "записи", "записей")}: ${r.reason}` : cap(r.reason);
}
function seconds(t) {
  return t < 1 ? `${Math.round(t * 1000)} мс` : `${t.toLocaleString("ru-RU", { maximumFractionDigits: 1 })} с`;
}
function modeText(mode) {  // "tree: 8192 buckets" / "flat: 64 vs 4096 buckets" from tree_diff
  const nums = (mode.match(/\d+/g) || []).map((x) => n(+x));
  if (mode.startsWith("tree")) return `по дереву, ${nums[0]} ${plural(+mode.match(/\d+/)[0], "бакет", "бакета", "бакетов")}`;
  return `плоское: у версий разное число бакетов (${nums.join(" и ")})`;
}
function plural(k, one, few, many) {
  const m10 = k % 10, m100 = k % 100;
  return m10 === 1 && m100 !== 11 ? one : m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14) ? few : many;
}

function copyCli(s) {
  const btn = h("button", { title: s.cli.check }, "Скопировать команду CLI");
  btn.addEventListener("click", async () => {
    const text = s.cli.check;
    try { await navigator.clipboard.writeText(text); }
    catch (e) {
      const t = h("textarea", {}, text); document.body.append(t); t.select(); document.execCommand("copy"); t.remove();
    }
    btn.textContent = "Скопировано"; setTimeout(() => (btn.textContent = "Скопировать команду CLI"), 1500);
  });
  return btn;
}
function exportMenu(rid) {
  const items = h("div", { class: "items", hidden: true });
  const formats = [["report.json", "Отчёт целиком — report.json"], ["patch.jsonl", "Патч A → B — patch.jsonl"],
                   ["changes.csv", "Таблица изменений — changes.csv"], ["summary.md", "Сводка для тикета — summary.md"]];
  formats.forEach(([f, label]) => items.append(h("button", { onclick: guard(() => { items.hidden = true; return download(rid, f); }) }, label)));
  const btn = h("button", { "aria-haspopup": "true", onclick: (e) => { e.stopPropagation(); items.hidden = !items.hidden; } }, "Выгрузить ▾");
  document.addEventListener("click", () => (items.hidden = true));
  return h("div", { class: "menu" }, btn, items);
}
async function download(rid, fmt) {
  // a one-time link (60 s): the browser saves the file as it streams, the session token stays out of URLs
  const { data } = await api("POST", `/api/report/${rid}/export-link`, { format: fmt });
  const a = h("a", { href: data.url, download: data.filename });
  document.body.append(a); a.click(); a.remove();
}

/* What changed, in a few words per row. */
function whatChanged(r) {
  const d = r.details || {};
  const out = [];
  const line = (k, ...v) => out.push(h("div", {}, h("span", { class: "k" }, `${k}: `), ...v));
  if (d.labels_old) line("метки", labelsDiff(d.labels_old, d.labels_new));
  if (d.group_old) line("группа", `${d.group_old} → ${d.group_new}`);
  if (d.hash_old) line("хеш", h("code", {}, d.hash_old.slice(0, 10)), " → ", h("code", {}, r.content_hash.slice(0, 10)));
  if (d.metadata_old) {
    const keys = [...new Set([...Object.keys(d.metadata_old), ...Object.keys(d.metadata_new)])]
      .filter((k) => JSON.stringify(d.metadata_old[k]) !== JSON.stringify(d.metadata_new[k]));
    line("метаданные", keys.map((k) => `${k}: ${fmtVal(d.metadata_old[k])} → ${fmtVal(d.metadata_new[k])}`).join("; "));
  }
  if (d.path_old) line("путь", h("code", {}, d.path_old), " → ", h("code", {}, d.path_new));
  if (r.types.includes("added")) line("группа", r.group_new || "—");
  if (r.types.includes("deleted")) line("была в", r.group_old || "—");
  if (r.types.includes("cosmetic")) {
    if (r.old_id_raw !== r.new_id_raw) line("id записан иначе", h("code", {}, JSON.stringify(r.old_id_raw)), " → ", h("code", {}, JSON.stringify(r.new_id_raw)));
    else line("только написание", h("span", { class: "faint" }, "порядок меток или ключей, регистр хеша, export_time"));
  }
  if (r.types.length === 1 && r.types[0] === "renamed") line("содержимое", h("span", { class: "faint" }, "то же"));
  return h("div", { class: "what" }, out);
}
const fmtVal = (v) => (v === undefined ? "—" : typeof v === "string" ? v : JSON.stringify(v));
function labelsDiff(a, b) {
  const sa = new Set(a), sb = new Set(b);
  const delta = [...b.filter((x) => !sa.has(x)).map((x) => `+${x}`), ...a.filter((x) => !sb.has(x)).map((x) => `−${x}`)];
  return [h("span", {}, a.length ? a.join(", ") : "—"), " → ", h("span", {}, b.length ? b.join(", ") : "—"),
          delta.length ? h("span", { class: "faint" }, `  (${delta.join(", ")})`) : null];
}

function changesTable(s) {
  const rid = s.id;
  const filters = { types: new Set(), group: "", q: "", sort: "importance", offset: 0, cosmetic: false };
  const cosmeticBox = h("input", { type: "checkbox" });
  const cosmeticLabel = h("span", {}, "показать косметику");
  const cosmeticToggle = h("label", { class: "row", style: "gap: 6px", title: "Изменения только написания: регистр, разделители, порядок ключей" },
                           cosmeticBox, cosmeticLabel);
  const body = h("tbody");
  const counter = h("span");
  const prev = h("button", {}, "← Назад"), next = h("button", {}, "Дальше →");
  const typeSel = h("div", { class: "row" });
  const group = h("select", { "aria-label": "Группа" }, h("option", { value: "" }, "все группы"),
    ["train", "val", "test"].map((g) => h("option", { value: g }, g)));
  const q = h("input", { type: "search", placeholder: "Поиск по id (подстрока)", "aria-label": "Поиск по id" });
  const sort = h("select", { "aria-label": "Сортировка" },
    h("option", { value: "importance" }, "по важности"), h("option", { value: "report" }, "как в отчёте"),
    h("option", { value: "old_id" }, "старый id ↑"),
    h("option", { value: "-old_id" }, "старый id ↓"), h("option", { value: "new_id" }, "новый id ↑"),
    h("option", { value: "-new_id" }, "новый id ↓"));
  let seq = 0, timer = null;
  const api_ = {
    filters, onFiltersChange: null,
    el: h("div", { class: "stack" },
      h("div", { class: "filters" }, typeSel, group, q, sort, cosmeticToggle),
      h("div", { class: "card tablewrap", style: "padding: 0" }, h("table", {},
        h("thead", {}, h("tr", {}, h("th", { style: "width: 190px" }, "Тип"), h("th", {}, "Старый id → новый id"),
                                   h("th", {}, "Что поменялось"), h("th", { style: "width: 120px" }, ""))), body)),
      h("div", { class: "pager" }, counter, prev, next)),
    toggleType(t) { filters.types.has(t) ? filters.types.delete(t) : filters.types.add(t); filters.offset = 0; renderChips(); api_.load(); },
    async load() {
      const my = ++seq;
      const p = new URLSearchParams({ offset: filters.offset, limit: PAGE, sort: filters.sort });
      if (!filters.cosmetic) p.set("hide_cosmetic", "1");
      if (filters.types.size) p.set("types", [...filters.types].join(","));
      if (filters.group) p.set("group", filters.group);
      if (filters.q) p.set("q", filters.q);
      let page;
      try { page = await get(`/api/report/${rid}/changes?${p}`); } catch (e) { showError(e); return; }
      if (my !== seq) return;  // a newer request is on its way
      fill(body, ...page.items.map((r) => row(r, rid)));
      if (!page.items.length) body.append(h("tr", {}, h("td", { colspan: 4, class: "empty" }, "Ничего не нашлось")));
      const from = page.total ? page.offset + 1 : 0, to = page.offset + page.items.length;
      counter.textContent = `${n(from)}–${n(to)} из ${n(page.total)}`;
      if (page.hidden_cosmetic) cosmeticLabel.textContent = `показать косметику (${n(page.hidden_cosmetic)})`;
      else if (!filters.cosmetic) cosmeticLabel.textContent = "показать косметику (0)";
      prev.disabled = page.offset === 0; next.disabled = to >= page.total;
      if (api_.onFiltersChange) api_.onFiltersChange();
    },
  };
  function renderChips() {
    fill(typeSel, ...[...filters.types].map((t) =>
      h("button", { class: "chip", title: "Убрать фильтр", onclick: () => api_.toggleType(t) }, `${TYPE_LABELS[t]} ✕`)));
  }
  prev.addEventListener("click", () => { filters.offset = Math.max(0, filters.offset - PAGE); api_.load(); });
  next.addEventListener("click", () => { filters.offset += PAGE; api_.load(); });
  group.addEventListener("change", () => { filters.group = group.value; filters.offset = 0; api_.load(); });
  sort.addEventListener("change", () => { filters.sort = sort.value; filters.offset = 0; api_.load(); });
  cosmeticBox.addEventListener("change", () => { filters.cosmetic = cosmeticBox.checked; filters.offset = 0; api_.load(); });
  q.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(() => { filters.q = q.value.trim(); filters.offset = 0; api_.load(); }, 250);
  });
  return api_;
}
function row(r, rid) {
  const ids = r.old_id && r.new_id && r.old_id !== r.new_id
    ? [recordLink(r.old_id_raw ?? r.old_id, rid), h("span", { class: "arrow" }, "→"), recordLink(r.new_id_raw ?? r.new_id, rid)]
    : [recordLink((r.new_id_raw ?? r.old_id_raw) || r.new_id || r.old_id, rid)];
  const amb = r.ambiguous
    ? h("div", {}, h("span", { class: "flag", title: `кандидаты: ${(r.details.candidates || []).join(", ")}` }, "неоднозначно"),
        h("div", { class: "cands" }, (r.details.candidates || []).slice(0, 4).join(", ")))
    : null;
  return h("tr", {},
    h("td", {}, r.types.map((t) => h("span", { class: "chip" }, TYPE_LABELS[t] || t)),
      r.affects_verdict ? h("div", { class: "drives", title: "Эта строка — одна из причин вердикта" }, "влияет на вердикт") : null),
    h("td", { class: "ids" }, ids),
    h("td", {}, whatChanged(r)),
    h("td", {}, amb));
}

function leaksTab(s) {
  const rid = s.id;
  const box = h("div");
  let kind = s.leaks.within || !s.leaks.by_move ? "within" : "by_move", offset = 0;
  const bar_ = h("div", { class: "row" });
  const pager = h("div", { class: "pager" });
  const load = guard(async () => {
    const p = await get(`/api/report/${rid}/leaks?kind=${kind}&offset=${offset}&limit=${PAGE}`);
    fill(bar_, ...[["within", `Внутри версии B (${n(p.totals.within)})`], ["by_move", `Переносом train → test (${n(p.totals.by_move)})`]]
      .map(([k, label]) => h("button", { class: k === kind ? "primary" : "", onclick: () => { kind = k; offset = 0; load(); } }, label)));
    const rows = kind === "within"
      ? p.items.map((l) => h("tr", {}, h("td", { class: "mono" }, l.content_hash.slice(0, 16)),
          h("td", { class: "ids" }, l.records.filter((x) => x.group === "train").map((x) => h("div", {}, recordLink(x.id, rid)))),
          h("td", { class: "ids" }, l.records.filter((x) => x.group === "test").map((x) => h("div", {}, recordLink(x.id, rid)))),
          h("td", {}, l.causes ? l.causes.map((c) => h("div", {}, `${c.cause === "copy" ? "копия" : "перенос"} ${c.direction.replace("->", " → ")}: `,
                                                       h("span", { class: "ids" }, recordLink(c.id, rid))))
                               : h("span", { class: "faint" }, "была и в A"))))
      : p.items.map((l) => h("tr", {}, h("td", { class: "mono" }, l.content_hash.slice(0, 16)),
          h("td", { class: "ids" }, recordLink(l.old_id, rid)), h("td", { class: "ids" }, recordLink(l.new_id, rid)),
          h("td", {}, "train → test")));
    const head = kind === "within" ? ["Содержимое", "В train", "В test", "Как появилась"] : ["Содержимое", "id в A (train)", "id в B (test)", "Направление"];
    fill(box, h("div", { class: "card tablewrap", style: "padding: 0" },
      p.total ? h("table", {}, h("thead", {}, h("tr", {}, head.map((x) => h("th", {}, x)))), h("tbody", {}, rows))
              : h("div", { class: "empty" }, "Утечек этого вида нет")));
    fill(pager, `${n(p.total ? p.offset + 1 : 0)}–${n(p.offset + p.items.length)} из ${n(p.total)}`,
      h("button", { disabled: p.offset === 0, onclick: () => { offset -= PAGE; load(); } }, "← Назад"),
      h("button", { disabled: p.offset + p.items.length >= p.total, onclick: () => { offset += PAGE; load(); } }, "Дальше →"));
  });
  load();
  return h("div", { class: "stack" }, bar_, box, pager);
}

function validationTab(s) {
  const rid = s.id;
  const box = h("div");
  let side = s.validation.new || !s.validation.old ? "new" : "old", offset = 0;
  const bar_ = h("div", { class: "row" });
  const pager = h("div", { class: "pager" });
  const load = guard(async () => {
    const p = await get(`/api/report/${rid}/validation?side=${side}&offset=${offset}&limit=${PAGE}`);
    fill(bar_, ...[["old", `A (${n(p.totals.old)})`], ["new", `B (${n(p.totals.new)})`]]
      .map(([k, label]) => h("button", { class: k === side ? "primary" : "", onclick: () => { side = k; offset = 0; load(); } }, label)));
    const rows = p.items.map((e) => e.type === "duplicate_sample_id"
      ? h("tr", {}, h("td", {}, "дубль sample_id"), h("td", { class: "ids" }, recordLink(e.sample_id, rid)),
          h("td", {}, `${n(e.count)} записи; хеши: `, h("code", {}, e.content_hashes.map((x) => x.slice(0, 10)).join(", "))))
      : h("tr", {}, h("td", {}, "противоречивые метки"),
          h("td", { class: "ids" }, e.sample_ids.map((x) => h("div", {}, recordLink(x, rid)))),
          h("td", {}, "содержимое ", h("code", {}, e.content_hash.slice(0, 10)), ": ", e.labels.map((l) => `[${l.join(", ")}]`).join(" / "))));
    fill(box, h("div", { class: "card tablewrap", style: "padding: 0" },
      p.total ? h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Ошибка"), h("th", {}, "sample_id"), h("th", {}, "Подробности"))), h("tbody", {}, rows))
              : h("div", { class: "empty" }, `В версии ${side === "old" ? "A" : "B"} ошибок валидации нет`)));
    fill(pager, `${n(p.total ? p.offset + 1 : 0)}–${n(p.offset + p.items.length)} из ${n(p.total)}`,
      h("button", { disabled: p.offset === 0, onclick: () => { offset -= PAGE; load(); } }, "← Назад"),
      h("button", { disabled: p.offset + p.items.length >= p.total, onclick: () => { offset += PAGE; load(); } }, "Дальше →"));
  });
  load();
  return h("div", { class: "stack" }, bar_, box, pager);
}

/* ---------- source picker: a commit or a file ---------- */
async function commitsList() {
  const st = S.state || (await refreshState());
  return st.repo && st.has_commits ? (await get("/api/commits?limit=1000")).items : [];
}
function sourcePicker(title, commits, defaultIndex) {
  const useRev = commits.length > 0;
  let mode = useRev ? "rev" : "file", file = null;
  const select = h("select", { "aria-label": `${title}: версия` },
    commits.map((c, i) => { const o = h("option", { value: c.hash }, `${c.message} · ${c.short}${i === 0 ? " · HEAD" : ""}`);
                            if (i === Math.min(defaultIndex, commits.length - 1)) o.selected = true; return o; }));
  const picked = h("div", { class: "picked faint" }, "файл не выбран");
  const fileBox = h("div", { class: "stack" });
  const body = h("div");
  const modeBtns = h("div", { class: "row" });
  const onChange = [];
  const render = () => {
    fill(modeBtns, ...[["rev", "Версия"], ["file", "Файл"]].map(([k, label]) =>
      h("button", { class: k === mode ? "primary" : "", disabled: k === "rev" && !useRev,
                    title: k === "rev" && !useRev ? "Репозиторий не открыт или пуст" : null,
                    onclick: () => { mode = k; render(); onChange.forEach((f) => f()); } }, label)));
    if (mode === "rev") fill(body, select);
    else {
      if (!fileBox.childNodes.length) fileBox.append(fileBrowser((p) => { file = p; picked.textContent = p; picked.classList.remove("faint"); onChange.forEach((f) => f()); }), picked);
      fill(body, fileBox);
    }
  };
  select.addEventListener("change", () => onChange.forEach((f) => f()));
  render();
  return {
    el: h("div", { class: "stack" }, h("b", {}, title), modeBtns, body),
    value: () => (mode === "rev" ? { rev: select.value } : file ? { file } : null),
    onChange: (f) => onChange.push(f),
  };
}
const sourceQuery = (v) => (v.rev ? `rev=${encodeURIComponent(v.rev)}` : `file=${encodeURIComponent(v.file)}`);

/* ---------- screen: record ---------- */
async function screenRecord(params) {
  const st = await refreshState();
  const commits = await commitsList();
  let ctx = null;  // the compare report the user came from
  if (params.get("report")) {
    try { ctx = (await api("GET", `/api/report/${params.get("report")}/summary`)).data; } catch (e) { ctx = null; }
    if (ctx && !ctx.source) ctx = null;
  }
  const q = h("input", { type: "search", placeholder: "sample_id, например images/00/img_001234.jpg", style: "width: 460px",
                         "aria-label": "sample_id", value: params.get("id") || "" });
  const pa = sourcePicker("A", commits, 1), pb = sourcePicker("B", commits, 0);
  const pickers = h("div", { class: "grid2" }, pa.el, pb.el);
  let where = ctx ? "report" : "pick";
  const whereBox = h("div", { class: "row" });
  const renderWhere = () => {
    fill(whereBox, 
      ctx ? h("label", { class: "row" }, h("input", { type: "radio", name: "where", checked: where === "report",
            onchange: () => { where = "report"; renderWhere(); } }), "в текущем сравнении: ", sourceTitle(ctx.source)) : null,
      h("label", { class: "row" }, h("input", { type: "radio", name: "where", checked: where === "pick",
            onchange: () => { where = "pick"; renderWhere(); } }), ctx ? "выбрать другие версии или файлы" : "в версиях или файлах:"));
    pickers.hidden = where !== "pick";
  };
  renderWhere();
  const result = h("div");
  const hist = h("div");
  const find = guard(async () => {
    const id = q.value.trim();
    if (!id) return;
    history.replaceState(null, "", `#/record?id=${encodeURIComponent(id)}${ctx ? `&report=${ctx.id}` : ""}`);
    let sides;
    if (where === "report") {
      const src = ctx.source;
      sides = src.kind === "rev" ? [{ rev: src.a }, { rev: src.b }] : [{ file: src.a }, { file: src.b }];
    } else {
      sides = [pa.value(), pb.value()];
      if (!sides[0] || !sides[1]) throw new ApiError(0, { message: "Выберите, где искать: версию или файл для A и для B." });
    }
    const [a, b] = await Promise.all(sides.map((v) => get(`/api/record?id=${encodeURIComponent(id)}&${sourceQuery(v)}`)));
    fill(result, recordCompare(a, b));
    if (st.repo && commits.length) fill(hist, historyBlock(id));
    else fill(hist);
  });
  q.addEventListener("keydown", (e) => { if (e.key === "Enter") find(); });
  mount(h("div", { class: "stack" }, h("h1", {}, "Запись"),
    h("div", { class: "card stack" }, h("div", { class: "row" }, q, h("button", { class: "primary", onclick: find }, "Найти")),
      whereBox, pickers),
    result, hist));
  if (q.value) find();
}

function sideLabel(r) {
  return r.source.rev ? [h("b", {}, r.source.message), " ", h("code", {}, r.source.rev.slice(0, 12))] : h("code", {}, r.source.file);
}
function recordCompare(a, b) {
  const ra = a.records[0], rb = b.records[0];
  const na = ra && ra.normalized, nb = rb && rb.normalized;
  const fields = ["sample_id", "path", "content_hash", "group", "labels"];
  const metaKeys = [...new Set([...Object.keys((na && na.metadata) || {}), ...Object.keys((nb && nb.metadata) || {})])].sort();
  const val = (rec, f) => (rec ? (f.startsWith("metadata.") ? rec.metadata[f.slice(9)] : rec[f]) : undefined);
  const rows = [...fields.filter((f) => val(na, f) !== undefined || val(nb, f) !== undefined), ...metaKeys.map((k) => `metadata.${k}`)];
  const cell = (rec, raw, other, f) => {
    if (!rec) return h("td", { class: "faint" }, "—");
    const v = val(rec, f);
    if (f === "labels") {
      const o = new Set(other ? other.labels : []);
      return h("td", {}, v.map((x) => h("span", { class: "chip", style: other && !o.has(x) ? "background: var(--mark-add)" : null,
                                                  title: other && !o.has(x) ? "нет на другой стороне" : null }, x)));
    }
    const rawV = f.startsWith("metadata.") ? raw.metadata[f.slice(9)] : raw[f];
    return h("td", { class: "mono" }, fmtVal(v), rawV !== undefined && JSON.stringify(rawV) !== JSON.stringify(v)
      ? h("div", { class: "faint" }, `записано: ${JSON.stringify(rawV)}`) : null);
  };
  const header = (r, side) => h("th", {}, `${side}: `, sideLabel(r),
    r.objects_read !== undefined ? h("span", { class: "faint" }, ` · прочитано объектов: ${r.objects_read}`) : null);
  const table = h("table", {},
    h("thead", {}, h("tr", {}, h("th", { style: "width: 170px" }, "Поле"), header(a, "A"), header(b, "B"))),
    h("tbody", {}, rows.map((f) => {
      const differ = JSON.stringify(val(na, f)) !== JSON.stringify(val(nb, f));
      return h("tr", { style: differ && na && nb ? "background: var(--warning-soft)" : null },
        h("td", {}, f, differ && na && nb ? h("span", { class: "faint" }, " · отличается") : null),
        cell(na, ra && ra.raw, nb, f), cell(nb, rb && rb.raw, na, f));
    })));
  const notes = [];
  if (!ra) notes.push(h("div", { class: "warnbox" }, `В A записи ${a.id} нет. `, "Если её переименовали, прежнее имя покажет «Проследить переименования» ниже."));
  if (!rb) notes.push(h("div", { class: "warnbox" }, `В B записи ${b.id} нет.`));
  if (a.records.length > 1 || b.records.length > 1)
    notes.push(h("div", { class: "warnbox" }, `id продублирован: в A записей ${a.records.length}, в B ${b.records.length}; показаны первые.`));
  if (!ra && !rb) return h("div", { class: "card empty" }, `Записи ${a.id} нет ни в A, ни в B.`);
  return h("div", { class: "stack" }, ...notes, h("div", { class: "card tablewrap", style: "padding: 0" }, table));
}

const CHANGE_WORDS = { added: "появилась", deleted: "удалена" };
function changeText(item) {
  if (!item.present) return h("span", { class: "faint" }, "нет в этой версии");
  if (!item.change) return h("span", { class: "faint" }, "—");
  if (!item.change.length) return h("span", { class: "faint" }, "без изменений");
  return item.change.map((t) => h("span", { class: "chip" }, CHANGE_WORDS[t] || TYPE_LABELS[t] || t));
}
function historyBlock(id) {
  const box = h("div", { class: "card stack" });
  const traceBtn = h("button", {}, "Проследить переименования");
  const render = (hst, traced) => {
    const stopped = { first_commit: "дальше версий нет", added: "здесь запись появилась впервые",
                      ambiguous: "переименование неоднозначно: продолжить нельзя", limit: "показаны последние 100 версий" };
    fill(box, 
      h("div", { class: "row between" }, h("h2", {}, traced ? "История с переименованиями" : "История по версиям"), traced ? null : traceBtn),
      h("div", { class: "muted" }, `Для каждой версии прочитан только бакет записи: всего объектов ${n(hst.objects_read)}, версии целиком не читались`,
        traced ? "; где имя появлялось впервые, соседние версии сравнены по дереву." : "."),
      h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Версия"), h("th", {}, "Хеш"), traced ? h("th", {}, "Имя в этой версии") : null,
                                                h("th", {}, "Что случилось"), h("th", {}, "Группа · метки"))),
        h("tbody", {}, hst.items.map((x) => {
          const r = x.records[0] && x.records[0].normalized;
          return h("tr", {}, h("td", {}, x.message), h("td", { class: "mono" }, x.short),
            traced ? h("td", { class: "ids" }, x.id, x.renamed_from ? h("div", { class: "faint" }, `раньше: ${x.renamed_from}`) : null) : null,
            h("td", {}, changeText(x), x.ambiguous ? h("div", { class: "cands" }, `кандидаты: ${x.ambiguous.candidates.join(", ")}`) : null),
            h("td", {}, r ? `${r.group} · ${r.labels.join(", ") || "—"}` : ""));
        }))),
      hst.stopped ? h("div", { class: "faint" }, stopped[hst.stopped] || hst.stopped) : null,
      !traced && hst.items.some((x) => !x.present) ? h("div", { class: "faint" }, "История идёт по id: если запись переименовывали, в старых версиях её «нет». Нажмите «Проследить переименования».") : null);
  };
  traceBtn.addEventListener("click", guard(async () => {
    traceBtn.disabled = true; traceBtn.textContent = "Ищу прежние имена…";
    const { data } = await api("POST", "/api/record/trace", { id });
    const job = await track(data.job, "История");
    render(job.result, true);
  }));
  get(`/api/record/history?id=${encodeURIComponent(id)}`).then((x) => render(x, false)).catch(showError);
  return box;
}

/* ---------- screen: patch check ---------- */
async function screenPatch() {
  await refreshState();
  const commits = await commitsList();
  const base = sourcePicker("База — версия, к которой применяется патч", commits, 1);
  const target = sourcePicker("Цель — версия, которая должна получиться", commits, 0);
  let patch = null;
  const picked = h("div", { class: "picked faint" }, "файл не выбран");
  const out = h("div");
  const go = h("button", { class: "primary" }, "Проверить");
  go.addEventListener("click", guard(async () => {
    const b = base.value(), t = target.value();
    if (!b || !t || !patch) throw new ApiError(0, { message: "Выберите базу, патч и цель." });
    go.disabled = true;
    fill(out, h("div", { class: "card progress-big" }, "Применяю патч…", bar(null)));
    try {
      const { data } = await api("POST", "/api/patch/verify", { base: b, patch, target: t });
      const job = await track(data.job, "Патч");
      fill(out, patchResult(job.result));
    } catch (e) { fill(out); throw e; }
    finally { go.disabled = false; }
  }));
  mount(h("div", { class: "stack" }, h("h1", {}, "Проверка патча"),
    h("div", { class: "muted" }, "Применяет patch.jsonl к базе и сравнивает результат с целью: strict — каждая строка побайтно (порядок не важен), semantic — после нормализации. То же делает ", h("code", {}, "python -m dsdiff verify"), "."),
    h("div", { class: "grid2" }, h("div", { class: "card" }, base.el), h("div", { class: "card" }, target.el)),
    h("div", { class: "card stack" }, h("b", {}, "Патч (patch.jsonl)"),
      fileBrowser((p) => { patch = p; picked.textContent = p; picked.classList.remove("faint"); }), picked),
    h("div", { class: "row" }, go), out));
}
function patchResult(r) {
  const head = h("div", { class: "muted" }, "база ", h("b", {}, r.base), " + патч ", h("code", {}, r.patch), " → цель ", h("b", {}, r.target));
  if (!r.applied) {
    return h("div", { class: "stack" }, h("section", { class: "verdict s3" },
      h("div", { class: "label" }, h("span", { class: "icon" }, "✕"), "Патч не применился"),
      h("div", { class: "why" }, r.error.message),
      h("details", {}, h("summary", {}, "подробности"), h("div", { class: "mono" }, r.error.detail.core))), head);
  }
  const ok = r.strict && r.semantic;
  const mark = (x) => (x ? "✓" : "✕");
  return h("div", { class: "stack" },
    h("section", { class: `verdict ${ok ? "s0" : r.semantic ? "s1" : "s3"}` },
      h("div", { class: "label" }, h("span", { class: "icon" }, ok ? "✓" : "✕"),
        ok ? "Патч воспроизводит цель" : r.semantic ? "Совпадает только по смыслу" : "Результат не совпадает с целью"),
      h("div", { class: "why" }, `strict ${mark(r.strict)} · semantic ${mark(r.semantic)}`,
        r.semantic && !r.strict ? " — строки отличаются только написанием (регистр, разделители, порядок ключей)" : "")),
    head,
    h("div", { class: "tiles" },
      Object.entries(r.ops).map(([k, v]) => h("div", { class: "tile" }, h("div", { class: "v" }, n(v)), h("div", { class: "k" }, `операций ${k}`))),
      h("div", { class: "tile" }, h("div", { class: "v" }, n(r.n_base)), h("div", { class: "k" }, "записей в базе")),
      h("div", { class: "tile" }, h("div", { class: "v" }, n(r.n_result)), h("div", { class: "k" }, "в результате")),
      h("div", { class: "tile" }, h("div", { class: "v" }, n(r.n_target)), h("div", { class: "k" }, "в цели"))),
    ok ? null : h("div", { class: "card stack" },
      h("b", {}, `Только в результате: ${n(r.mismatch.only_in_result)} строк, только в цели: ${n(r.mismatch.only_in_target)}`),
      r.mismatch.examples.map((x) => h("div", { class: "mono" }, h("span", { class: "chip" }, x.side === "result" ? "результат" : "цель"), x.line))));
}

/* ---------- router ---------- */
async function route() {
  const hash = location.hash || "#/versions";
  const [path, query] = hash.slice(1).split("?");
  const parts = path.split("/").filter(Boolean);
  const screen = parts[0] || "versions";
  document.querySelectorAll(".side a").forEach((a) => a.classList.toggle("on", a.dataset.nav === screen));
  hideError();
  try {
    if (screen === "versions") await screenVersions();
    else if (screen === "compare") await screenCompare(parts[1]);
    else if (screen === "record") await screenRecord(new URLSearchParams(query || ""));
    else if (screen === "patch") await screenPatch();
    else location.replace("#/versions");
  } catch (e) { showError(e); }
}

document.getElementById("theme").addEventListener("click", () => {
  applyTheme(THEMES[(THEMES.indexOf(currentTheme()) + 1) % THEMES.length]);
});
applyTheme(currentTheme());
window.addEventListener("hashchange", route);
route();
