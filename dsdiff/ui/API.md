# dsdiff ui — JSON API

Локальный сервер (`http.server.ThreadingHTTPServer`, только 127.0.0.1) отдаёт одну страницу
(`/`, файлы из `dsdiff/ui/static/`) и JSON API под `/api/`. Все функции ядра вызываются как есть
(`diff`, `tree_diff`, `Store`, `build_patch`, `apply`, `canonical_lines`); всё, чего в ядре нет
(фоновые задачи, пагинация, фильтры, история записи, проверка путей), живёт в `dsdiff/ui/`.

## Общие правила

- **Защита локального сервера.** Запрос принимается, только если заголовок `Host` равен
  `127.0.0.1:<порт>` или `localhost:<порт>` (против DNS rebinding), а запросы к `/api/` несут
  заголовок `X-Dsdiff-Token: <токен>`. Токен случайный, создаётся при запуске и вписывается в
  отдаваемую страницу; чужой сайт не может ни прочитать его, ни отправить нестандартный
  заголовок без CORS-preflight, на который сервер не отвечает разрешением. Без токена — `403`.
- **Пути.** Любой путь от клиента (`file`, `dir`, `repo`, `patch`) — относительно `--root`.
  Сервер делает `realpath` и требует, чтобы результат лежал внутри `realpath(--root)`; `..`,
  абсолютные пути наружу и симлинки, ведущие наружу, дают `403 {"error": "path_outside_root"}`.
  `--repo` из командной строки доверенный и может лежать где угодно.
- **Ошибки** — всегда JSON: `{"error": "<код>", "message": "<понятный текст по-русски>",
  "detail": {...}}` с кодом HTTP 400 / 403 / 404 / 409 / 500. Трейсбек в ответ не попадает (пишется
  в консоль сервера). Коды: `bad_jsonl` (detail: file, line, column, текст ошибки), `not_found`,
  `unknown_revision`, `no_repo`, `path_outside_root`, `not_a_file`, `job_running`,
  `patch_base_mismatch`, `patch_bad_op`, `patch_not_a_patch`, `bad_request`, `internal`.
- **Ревизия** (`rev`) — как в CLI: `HEAD`, `HEAD~k`, полный хеш или уникальный префикс от 4 символов.
- **Числа** — как в отчёте ядра; `id` в ответах — канонические (`old_id`, `new_id`) и сырые
  (`old_id_raw`, `new_id_raw`), как в `report.json`.

## Запись в репозиторий: очередь и журнал

- **Строго по одному.** Всё, что пишет в репозиторий (commit, создание), идёт через очередь
  сервера: на каждый репозиторий один поток-исполнитель, задачи по порядку поступления. Сам
  исполнитель на время задачи берёт блокировку файла `.dsdiff/ui.lock` (`fcntl.flock`, без
  ожидания), так что и второй экземпляр `dsdiff ui` на том же репозитории не пишет одновременно:
  его задача остаётся в состоянии `queued` и повторяет попытку раз в секунду. `dsdiff commit` из
  CLI блокировку не берёт (ядро не меняем) — одновременный commit из CLI и из ui не защищён.
- **Сравнения** не пишут в репозиторий и идут параллельно, каждое в своём потоке.
- **Журнал** `.dsdiff/ui-log.jsonl` — только слой ui, в хеши и объекты не входит. После каждого
  удачного commit из ui дописывается строка:
  `{"commit": "<хеш>", "time": "2026-10-27T14:03:12+03:00", "file": "data/v2.jsonl",
  "size": 61234567, "lines": 100000, "message": "v2"}`. Коммиты из CLI в журнале не появляются.

## Состояние и репозиторий

`GET /api/state` →
```json
{"root": "/abs/root", "repo": "/abs/repo" | null, "has_commits": true, "head": "<хеш>" | null,
 "version": "1.1-ui", "jobs_running": 1}
```

`POST /api/repo {"dir": "<папка внутри root>", "create": false}` — открыть репозиторий в папке
(`<dir>/.dsdiff`) → то же, что `/api/state`; `404 no_repo`, если `.dsdiff` нет. С `"create": true`
создание идёт через очередь записи: `202 {"job": "<id>"}`, задача создаёт пустой `.dsdiff/`
(первый commit наполнит его) и делает репозиторий текущим.

## Файлы

`GET /api/files?dir=<папка>` → содержимое папки внутри `--root`, без рекурсии:
```json
{"dir": "data/medium", "parent": "data" | null,
 "entries": [{"name": "v1.jsonl", "type": "file", "size": 61234567, "mtime": 1761480000.0},
             {"name": "sub", "type": "dir"}]}
```
Показываются папки и файлы `*.jsonl` (остальные скрыты); симлинки наружу не показываются.

## Коммиты

`GET /api/commits?offset=0&limit=50` → коммиты от HEAD по цепочке родителей, новые сверху:
```json
{"total": 10, "items": [
  {"hash": "…", "short": "3b5a302c1d2e", "message": "v2", "parent": "…" | null,
   "records": 100000, "groups": {"train": 70012, "val": 9988, "test": 20000},
   "leaks": 5, "validation": {"duplicate_sample_id": 2, "conflicting_labels": 3},
   "buckets": 8192,
   "log": {"time": "2026-10-27T14:03:12+03:00", "file": "data/v2.jsonl", "size": 61234567,
           "lines": 100000} | null}]}
```
Всё берётся из объекта commit (`counts`, `leaks_count`, `validation`, `buckets`), бакеты не читаются;
`log` — из журнала ui, `null` для коммитов из CLI.

`POST /api/commit {"file": "<jsonl внутри root>", "message": "…"}` → `202 {"job": "<id>"}`.
Задача в фоне: читает файл построчно (прогресс — строки), затем `Store.commit` с теми же
аргументами, что `dsdiff commit` (тот же корень дерева). Если запись в этот репозиторий уже идёт,
задача ждёт в очереди (`state: "queued"`, `queue_position`); два одновременных запроса дают два
коммита подряд, второй — потомок первого.

## Фоновые задачи

`GET /api/jobs/<id>` →
```json
{"id": "…", "kind": "commit" | "create_repo" | "compare" | "trace" | "patch",
 "state": "queued" | "running" | "done" | "failed" | "cancelled", "queue_position": 0,
 "stage": "wait_lock" | "read" | "commit" | "diff" | "done", "lines_read": 412000, "lines_total": 1000000,
 "objects_written": 51234, "elapsed": 12.4,
 "result": {"commit": "<хеш>", "records": 1000000, "objects_written": 140213} | {"report": "<id>"} | null,
 "error": {"error": "bad_jsonl", "message": "…", "detail": {…}} | null}
```
`lines_total` — оценка по размеру файла и средней длине прочитанных строк (уточняется).

Для `trace`: `commits_done`, `commits_total`, `stage: "history" | "diff"`; для `patch`:
`stage: "read" | "apply" | "compare"`.

`DELETE /api/jobs/<id>` — отмена. Задача из очереди просто убирается. Чтение файла останавливается на следующей строке; commit —
на следующей записи объекта (через подкласс `Store` в ui, ядро не меняется). HEAD при отмене не
сдвигается: он пишется только в конце commit. Уже записанные объекты остаются в `objects/`
(они адресуются содержимым и безвредны; следующий commit той же версии их переиспользует).
Сравнение отменяется на этапе чтения; если уже идёт `diff`, его результат просто выбрасывается.

## Сравнение

`POST /api/compare/estimate {"file_a": "…", "file_b": "…"}` →
`{"lines_a": 1000000, "lines_b": 1000050, "seconds": 39, "warn": true, "threshold": 100000}` —
строки считаются точно (поблочное чтение и подсчёт `\n`, без разбора JSON); `seconds` — оценка
плоского сравнения по замеру из `bench/` (≈ 19,5 мкс на запись обеих версий), `warn` — если
хотя бы в одном файле больше 100 000 строк.

`POST /api/compare` {"a": "<rev>", "b": "<rev>"}` или `{"file_a": "…", "file_b": "…"}` →
`200 {"report": "<id>", "job": null}`, если отчёт уже в кэше, иначе `202 {"report": "<id>",
"job": "<id>"}`. Коммиты сравниваются `tree_diff` (при разном числе бакетов он сам уходит в
плоский), файлы — `diff`. Кэш в памяти по ключу: пара хешей коммитов или пара
`(realpath, size, mtime)`. Это LRU с лимитом по суммарному числу изменений во всех отчётах
(`--cache-changes`, по умолчанию 2 000 000; в отчёте только изменённые записи, не версии целиком):
при превышении вытесняются отчёты, к которым дольше всего не обращались. Последний открытый отчёт
не вытесняется, даже если он один больше лимита; считающиеся отчёты — тоже. Вытесненный отчёт
даёт `404` с просьбой запустить сравнение снова. `GET /api/state` → `cache: {changes_held, limit,
reports}`. Сравнение файлов, где хотя бы в одном больше 100 000 строк, без
`"confirm": true` даёт `409 {"error": "needs_confirmation", "detail": <то же, что estimate>}` —
страница показывает предупреждение и спрашивает.

`GET /api/report/<id>/summary` → пока задача идёт: `202 {"job": "<id>"}`; потом:
```json
{"id": "…", "source": {"kind": "rev", "a": "<хеш>", "b": "<хеш>", "a_label": "v1", "b_label": "v2"}
          | {"kind": "file", "a": "data/v1.jsonl", "b": "data/v2.jsonl"},
 "verdict": {"code": "do_not_trust", "label": "…", "severity": 3,
             "reasons": [{"code", "reason_code", "count", "examples", "reason"}]},
 "n_old": 10000, "n_new": 10050, "unchanged": 9700,
 "changes_by_type": {"renamed": 30, …}, "changes_by_combination": {…}, "ambiguous_renames": 2,
 "leaks": {"within": 51, "by_move": 51},
 "validation": {"old": 0, "new": 3},
 "stats": {"mode": "tree: 8192 buckets", "buckets_read": 230, "records_read": 2994,
           "objects_read": 512} | null,
 "timings": {"read": 0.4, "normalize": …, "match": …, "leaks_verdict": …},
 "cli": {"diff": "python -m dsdiff diff --rev 3b5a302c HEAD --repo … --report report.json --patch patch.jsonl",
         "check": "python -m dsdiff check --rev 3b5a302c HEAD --repo …"}}
```
`verdict`, счётчики и `timings` — ровно из `DiffResult.to_report()` и `DiffResult.timings`.

`GET /api/report/<id>/changes?offset=0&limit=200&types=relabeled,group_moved&group=test&q=img_01&sort=type`
```json
{"total": 1805, "offset": 0, "limit": 200,
 "items": [{"n": 17, "types": ["relabeled"], "old_id": "…", "new_id": "…", "old_id_raw": "…",
            "new_id_raw": "…", "content_hash": "…", "details": {…},
            "group_old": "train", "group_new": "train"}]}
```
- `limit` не больше 200; строки — `Change.to_json()` из ядра плюс группы обеих сторон и номер `n`
  в полном списке.
- `types` — изменение подходит, если у него есть хотя бы один из типов (`renamed`, `relabeled`,
  `content_replaced`, `group_moved`, `metadata_changed`, `cosmetic`, `added`, `deleted`) или,
  для `ambiguous`, флаг неоднозначности.
- `group` — `train` / `val` / `test`: группа старой или новой стороны.
- `q` — подстрока канонического или сырого id любой стороны, без учёта регистра.
- `sort` — `type` (по умолчанию: порядок отчёта), `old_id`, `new_id`, `-old_id`, `-new_id`.

`GET /api/report/<id>/leaks?kind=within|by_move&offset&limit` — утечки отчёта с пагинацией:
`within` — `{"content_hash", "records": [{"id", "group"}], "causes": [{"id", "cause", "direction"}] | null}`,
`by_move` — `{"old_id", "new_id", "content_hash"}`; `total` — для каждого вида.

`GET /api/report/<id>/validation?side=old|new&offset&limit` — ошибки валидации версии:
`duplicate_sample_id` (`sample_id`, `count`, `content_hashes`) и `conflicting_labels`
(`content_hash`, `sample_ids` — до 5 примеров, как в отчёте; для коммитов до 100 хранится в commit,
отдаются тоже 5, `labels`).

`POST /api/report/<id>/export-link {"format": "patch.jsonl"}` →
`{"url": "/dl/<случайный токен>", "expires_in": 60, "filename": "patch.jsonl"}` — одноразовая
ссылка на выгрузку: страница переходит по ней, браузер сохраняет файл. Ссылка живёт 60 с и работает
один раз; токен сессии в URL не попадает (сама ссылка — разрешение). `GET /dl/<токен>`: файл
потоком (`Content-Disposition: attachment`, без `Content-Length`, конец — закрытие соединения);
использованная или просроченная ссылка — `410 gone`, несуществующая — `403`.

`GET /api/report/<id>/export/<format>` (с заголовком токена, для скриптов) — тот же файл потоком.
Форматы:
- `report.json` — `to_report()` целиком (как `dsdiff diff --report`);
- `patch.jsonl` — `build_patch(result)` (как `dsdiff diff --patch`);
- `changes.csv` — `n, types, old_id, new_id, old_id_raw, new_id_raw, group_old, group_new,
  content_hash, labels_old, labels_new, hash_old, ambiguous, candidates`;
- `summary.md` — для тикета: источники A и B, вердикт и причины с примерами, счётчики по
  типам, 10 первых изменений таблицей, команда CLI.

## Запись

`GET /api/record?id=<sample_id>&rev=<rev>` или `&file=<jsonl>` →
```json
{"id": "images/img_001.jpg", "found": true, "records": [{"raw": {…}, "normalized": {…}}],
 "source": {"rev": "<хеш>"} | {"file": "…"}, "bucket": 1234, "objects_read": 4}
```
`id` нормализуется как в ядре (`normalize_name`); `records` — список, потому что id может быть
продублирован. Для коммита читается один бакет: спуск по дереву id (`depth(B)` узлов) к бакету
`bucket_of(id, B)`. Для файла — индекс `id → смещения строк` строится один раз на файл
(`realpath, size, mtime`) и кэшируется.

`GET /api/record/history?id=<sample_id>&limit=100` → по цепочке коммитов от HEAD, новые сверху:
```json
{"id": "…", "items": [{"commit": "<хеш>", "message": "v3", "present": true,
                       "change": ["relabeled"] | ["added"] | ["deleted"] | [] ,
                       "records": [{"raw": {…}}], "objects_read": 4}],
 "objects_read": 40}
```
`change` — `classify` ядра между записью в этом коммите и в предыдущем (по тому же id). История
идёт по id: переименованная запись в старых коммитах «отсутствует» — это видно и подписано на
экране, рядом кнопка «Проследить переименования».

`POST /api/record/trace {"id": "<sample_id>", "limit": 100}` → `202 {"job": "<id>"}`. Идёт по
цепочке от HEAD так же, как history, но там, где запись есть в коммите и нет в его родителе,
считает `tree_diff(родитель, коммит)` (через кэш отчётов) и ищет изменение с `new_id` = текущему
имени и типом `renamed`; если нашлось — дальше история идёт под `old_id`. Неоднозначное
переименование (`ambiguous`) прерывает след с пометкой и списком кандидатов. Результат задачи —
как у history, плюс у каждого элемента `"id"` — имя записи в этом коммите и `"renamed_from"`
там, где имя сменилось; `"stopped": "added" | "ambiguous" | "first_commit" | "limit"`.

## Проверка патча

`POST /api/patch/verify {"base": {"rev": "…"} | {"file": "…"}, "patch": "<jsonl>",
                         "target": {"rev": "…"} | {"file": "…"}}` → `202 {"job": "<id>"}` (версию на 1e6
записей надо прочитать целиком, поэтому в фоне). Результат задачи:
```json
{"base": "v1 (3b5a302c1d2e)", "patch": "out/patch.jsonl", "target": "data/v2.jsonl",
 "applied": true, "strict": true, "semantic": true, "ops": {"delete": 10, "rename": 30, …},
 "n_base": 10000, "n_result": 10050, "n_target": 10050,
 "mismatch": {"only_in_result": 0, "only_in_target": 0, "examples": [{"side": "result", "line": "…"}]}}
```
Не применился — `"applied": false, "error": {"error": "patch_base_mismatch" | "patch_bad_op" |
"patch_not_a_patch", "message": "…", "detail": {"core": "<текст PatchError>"}}`. Для цели-коммита
strict сравнивает со строками в том виде, как их хранит commit (`dumps` записи).
То же, что `dsdiff verify`: `apply(base, ops, check_target=False)`, затем strict (строки побайтно,
порядок не важен) и semantic (`canonical_lines(…, True)`). Ошибка применения —
`400 patch_base_mismatch` / `patch_bad_op` / `patch_not_a_patch` с текстом по-русски («Патч
сделан для другой базовой версии: ожидался хеш …, у выбранной …»). Сравниваются до 10 примеров
несовпадающих строк.

## Ограничения

- `dsdiff commit` из CLI не берёт блокировку `.dsdiff/ui.lock` (ядро заморожено): одновременный
  commit из CLI и из ui в один репозиторий не защищён. Исправим после заморозки.
- Сравнение отменяется сразу только на этапе чтения файлов; идущий `diff` / `tree_diff` дорабатывает
  до конца, и его результат выбрасывается.
- История записи идёт по id; переименования прослеживаются только по кнопке (`/api/record/trace`).

## Не делает

- Не меняет и не удаляет коммиты, не переключает HEAD (кроме нового commit).
- Не слушает ничего, кроме 127.0.0.1; не ходит в сеть.
