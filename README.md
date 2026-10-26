# dsdiff — семантический дифф версий манифеста датасета

- `python -m dsdiff` — дифф двух версий (сопоставление сначала по хешу содержимого, потом по id),
  утечки train/test, вердикт «можно ли доверять метрике», патч и его проверка. См. «Дифф и патч».
  Версии можно хранить в git-подобном хранилище и сравнивать по дереву хешей. См. «Хранилище».
- `python -m dsdiff.gen` — генератор версий с известной истиной для проверки диффа.

## Генератор

`dsdiff/gen.py` строит несколько версий манифеста (`v1.jsonl`, `v2.jsonl`, …) и для каждой
пары соседних версий — эталонный дифф `truth_vS_vS+1.jsonl`, плюс сводку `truth_summary.json`.
Всё детерминировано: одинаковые параметры дают побайтно одинаковые файлы. Запись `n` выводится
из `(seed, n)`, её изменение на шаге `s` — из `(seed, s, n)`, поэтому в памяти ничего крупного
не держится и один код обслуживает и `tiny`, и `xl`.

### Запуск

```sh
python -m dsdiff.gen --preset tiny          # -> examples/tiny
python -m dsdiff.gen --preset story         # -> examples/story
python -m dsdiff.gen --preset clean         # -> data/clean
python -m dsdiff.gen --preset test_relabel  # -> data/test_relabel
python -m dsdiff.gen --preset test_to_train # -> data/test_to_train
python -m dsdiff.gen --preset small         # -> data/small   (10 000 записей)
python -m dsdiff.gen --preset medium        # -> data/medium  (100 000)
python -m dsdiff.gen --preset large         # -> data/large   (1 000 000)
python -m dsdiff.gen --preset xl            # -> data/xl      (10 000 000)
python -m dsdiff.gen --preset collisions    # -> data/collisions/h16, h24, h32, h64 (100 000, разная длина хеша)
python -m dsdiff.gen --n 20000 --versions 3 --with-path --dirty --out out/custom
```

| пресет | что это |
|---|---|
| `tiny` | 33 записи, все изменения заданы вручную: каждый тип изменения и каждое правило шума встречается хотя бы раз. Строки отсортированы. |
| `story` | История заказчика: модель не переобучали, а метрика на v2 выросла. ~10% теста v1 (102 записи) — обучающие примеры: половина перенесена из train (`group_moved+leak_by_move`), половина скопирована (`added+leak`, `direction: train->test`). |
| `clean` | 10 000 записей; меняется только train (без переносов и утечек), test и val не трогаются. Вердикт diff — «доверять». |
| `test_relabel` | Как `clean`, плюс у 5% теста сменены метки и больше в test ничего. Вердикт — «метрики несопоставимы». |
| `test_to_train` | 10 000 записей, 5 тестовых примеров скопированы в train, переносов нет. Вердикт — «не переобучать на этой версии». |
| `small` / `medium` / `large` / `xl` | Параметры по умолчанию, отличаются только размером. |
| `collisions` | Хеши 16/24/32/64 бит — много случайных совпадений хешей у разного содержимого. |
| `break_swaps` | 10 000 записей, одно явление: 100 обменов и цепочек имён. Вердикт — «доверять». |
| `break_id_noise` | 10 000 записей, одно явление: шум в id у 5% записей (`id_case`, `backslash`, `dot_slash`, `nfd`, `id_whitespace`). Вердикт — «доверять». |
| `break_train_to_test` | 10 000 записей, одно явление: 2% записей переходят в другую группу, в том числе из train в test. Вердикт — «не доверять». |
| `break_test_relabel` | `test_relabel` без прочих изменений: перемечено 5% test. Вердикт — «метрики несопоставимы». |

У пресетов с задуманным вердиктом (`story`, `clean`, `test_relabel`, `test_to_train`, `break_*`) он
записан в конфиге (`expected_verdict`) и попадает в `truth_summary.json` → `params.expected_verdict`;
он задан смыслом пресета, а не вычислен из truth.

Флаги командной строки переопределяют пресет:

| флаг | по умолчанию | смысл |
|---|---|---|
| `--out DIR` | см. выше, без пресета `out` | куда писать |
| `--n`, `--seed`, `--versions` | 10000, 1, 2 | размер v1, зерно, число версий |
| `--hash-bits` | 64 | длина `content_hash` |
| `--with-path` | выкл. | `sample_id` — стабильный `s_NNNNNN`, имя файла уходит в поле `path` |
| `--sorted` | выкл. | строки в порядке генерации, а не перемешаны |
| `--dirty` | выкл. | добавить ошибки валидации (см. `expected_validation_errors`) |
| `--p-rename`, `--p-relabel`, `--p-rename-relabel`, `--p-replace`, `--p-move`, `--p-meta`, `--p-delete`, `--p-noise` | 0.03, 0.03, 0.005, 0.01, 0.01, 0.01, 0.01, 0.10 | вероятность изменения записи на шаге (в сумме ≤ 1) |
| `--p-add` | 0.02 | доля новых записей на шаге (от `n`) |
| `--p-dup-content` | 0.005 | доля записей v1, дублирующих содержимое (и группу/метки/метаданные) другой записи |
| `--n-leaks` | 5 | копий-утечек на шаге |
| `--leak-direction` | `both` | направление копий: `test->train`, `train->test` или `both` (чередуются, начиная с `test->train`) |
| `--leak-at-version` | все шаги | утечки-копии только в этой версии (2…versions) |
| `--n-rename-cycles` | 2 | циклов переименований на шаге (чётные — обмен двух имён, нечётные — цепочка из трёх) |
| `--n-dup-ids`, `--n-conflicts` | 2, 3 | число ошибок каждого вида в режиме `--dirty` |

### Формат версии: `vK.jsonl`

Одна запись на строку:

```json
{"sample_id": "images/3f/img_000123.jpg", "content_hash": "9c1e…", "group": "train",
 "labels": ["cat", "dog"], "metadata": {"width": 640, "height": 480, "source": "cam3", "captured_at": "2026-04-02"}}
```

- `sample_id` — имя файла; с `--with-path` — `s_000123`, а имя файла лежит в `path`.
- `content_hash` — хеш содержимого; одинаковый хеш у разных записей — либо общее содержимое
  (дубль, утечка), либо случайная коллизия (заметна при малом `--hash-bits`).
- `group` — `train` / `val` / `test`.
- Косметический шум (тип `cosmetic`) меняет только представление: пробел в начале или конце id
  (`id_whitespace`), регистр id (`id_case`) или хеша
  (`hash_case`), `\` вместо `/` (`backslash`), префикс `./` (`dot_slash`), NFD-нормализация
  (`nfd`), порядок меток (`label_order`) или ключей метаданных (`meta_order`), поле
  `metadata.export_time` (`export_time`). `normalize_record` снимает весь этот шум. `--noise-rules
  R …` ограничивает выбор правил (по умолчанию все).

Канонический id — `normalize_name(sample_id)`: без пробелов по краям, NFC, `\` → `/`, нижний регистр, без `./`.

### Формат диффа: `truth_vS_vS+1.jsonl`

```json
{"change_id": 5, "types": ["group_moved", "leak_by_move"],
 "old_id": "images/img_004.jpg", "new_id": "images/img_004.jpg",
 "old_id_raw": "images/img_004.jpg", "new_id_raw": "images/img_004.jpg",
 "content_hash": "97a5eb00",
 "details": {"group_old": "train", "group_new": "test", "direction": "train->test"}}
```

- `old_id` / `new_id` — канонические id (`null` для добавленных / удалённых), `*_raw` — как в файле.
- `content_hash` — хеш в новой версии (для удалённых — в старой).
- Записи без строки в truth не изменились. Применение truth к старой версии — `reference_apply`
  (сравнивать с новой версией после `normalize_record`).

### Типы

Строка может нести несколько типов (`renamed+relabeled`, `group_moved+leak_by_move+leak`, …).

| тип | смысл | details |
|---|---|---|
| `added` | новая запись | `record` — запись целиком |
| `deleted` | запись удалена | — |
| `renamed` | то же содержимое под другим id (с `--with-path` — другой `path`, `sample_id` прежний) | `content_shared`, `ambiguous`; с `--with-path` — `path_old`, `path_new`, `*_raw` |
| `rename_cycle` | переименование внутри обмена/цепочки: новое имя записи — старое имя другой | `cycle_id`, `kind` (`swap` / `chain`), `position` |
| `relabeled` | изменились метки | `labels_old`, `labels_new` |
| `content_replaced` | тот же id, другое содержимое | `hash_old` |
| `group_moved` | запись сменила группу | `group_old`, `group_new`, `direction` (`"<old>-><new>"`) |
| `leak_by_move` | перенос `train → test`: модель уже видела этот пример при обучении | вместе с `group_moved` |
| `leak` | в новой версии содержимое есть одновременно в train и test. На `added`: копия-утечка. На `group_moved+leak_by_move`: перенесённая запись осталась в паре с дублем в train | для копии: `source_id`, `source_id_raw`, `source_group`, `direction`, `leak_version` |
| `metadata_changed` | изменились метаданные | `metadata_old`, `metadata_new` |
| `cosmetic` | только шум представления, семантически запись та же | `rule` |

### Флаги

- **`direction`**
  - у `group_moved` — откуда и куда, `"train->test"` и т. п.;
  - у копии-утечки (`added+leak`):
    - `"test->train"` — тестовый пример скопирован в train: опасно при следующем обучении
      (модель переобучится на тест);
    - `"train->test"` — обучающий пример скопирован в test: завышает метрику уже обученной модели.
- **`content_shared`** (у `renamed`) — содержимое записи есть у нескольких id в старой или новой
  версии (дубль, утечка, конфликт меток). Информативно: переименование может быть вполне однозначным.
- **`ambiguous`** (у `renamed`) — по хешу нельзя однозначно сопоставить старую и новую запись.
  Считается так: берутся некосметические строки шага с этим хешем (изменённые, удалённые,
  добавленные; неизменённые и `cosmetic` не участвуют); снимаются пары «тот же канонический id и
  тот же хеш» в старой и новой версии; среди оставшихся старых кандидатов оставляются те, чей
  канонический id исчез из новой версии (остальные будут `content_replaced`), если такие есть;
  `ambiguous = true`, если после этого у хеша больше одного кандидата на старой или на новой
  стороне. Это то же правило, что в `diff`. Пример в `tiny`: обе копии дубля (`img_026`, `img_027`)
  переименованы — `true`; у `img_016` дубль `img_015` не менялся — `content_shared: true`,
  `ambiguous: false`.

### Сводка: `truth_summary.json`

| ключ | смысл |
|---|---|
| `preset`, `seed`, `params` | параметры генерации |
| `n_records` | число строк в каждой версии |
| `changes_by_combination` / `changes_by_type` | счётчики строк truth по сочетанию типов / по отдельным типам |
| `duplicate_content_records_v1` | сколько записей v1 — дубли содержимого |
| `leaks` | все утечки: `{"kind": "copy", "direction", "version", "content_hash", "train_id", "test_id"}` и `{"kind": "move", "version", "content_hash", "id"}` |
| `leaks_vK` | содержимое (по истинному содержимому, не по хешу), которое в версии K есть и в train, и в test: `{"content_hash", "records": [{"id", "group"}, …]}` |
| `duplicates_vK` | группы id с одинаковым содержимым (истинные дубли, не коллизии хеша) |
| `expected_validation_errors` | с `--dirty`: `duplicate_sample_id` (один id, два содержимого) и `conflicting_labels` (одно содержимое, разные метки); во всех версиях |
| `skipped` | что не удалось разместить (цикл или утечка не нашли подходящих записей) |

## Дифф и патч

```sh
python -m dsdiff diff OLD NEW --report out/report.json --patch out/patch.jsonl
python -m dsdiff apply OLD out/patch.jsonl --out out/new.jsonl
python -m dsdiff verify OLD NEW out/patch.jsonl   # strict и semantic; код выхода 0 / 1, 2 — патч не применился
python -m dsdiff check OLD NEW                    # только вердикт; код выхода 0 доверять … 3 не доверять
```

`diff` печатает сводку изменений, вердикт с причинами и примерами и время по стадиям
(`read`, `normalize`, `validate`, `match`, `leaks_verdict`, `write`).

### Как сопоставляются записи (`dsdiff/diff.py`)

Каждая запись нормализуется (`dsdiff/normalize.py`, общая с генератором) и получает ключи:
`id_key` (канонический `sample_id`), `content_key` (хеш в нижнем регистре), `payload_hash`
(group + labels + metadata) и `raw_hash` (запись как записана).

1. Внутри группы одного `content_key` в пару сначала встают записи с тем же `id_key`.
2. Оставшиеся — переименования. Из старых кандидатов берутся те, чей `id_key` исчез из новой
   версии (если такие есть); пары подбираются по похожести имени (общий номер или стем, затем
   общая директория), потом по `id_key`. Если кандидатов на одной из сторон больше одного —
   `ambiguous: true` и список `candidates`.
3. Оставшиеся без пары сопоставляются по `id_key` (`content_replaced`), прочие — `deleted` / `added`.

Сначала хеш, потом id: иначе обмены именами a↔b и цепочки выглядели бы как замена содержимого.
Класс пары — набор флагов `renamed`, `relabeled`, `content_replaced`, `group_moved`,
`metadata_changed`, `cosmetic` (по смыслу равны, `raw_hash` разный), `added`, `deleted`.
Дубли id и противоречивые метки внутри версии попадают в `validation`, дифф при этом не падает.

### Отчёт (`--report`)

```json
{"summary": {"n_old", "n_new", "unchanged", "changes_by_type", "changes_by_combination", "ambiguous_renames"},
 "verdict": {"code", "label", "reasons": [{"code", "reason_code", "count", "examples", "reason"}]},
 "leaks": {"within": [{"content_hash", "records": [{"id", "group"}], "causes": [{"id", "cause", "direction"}] | null}],
           "by_move": [{"old_id", "new_id", "content_hash"}]},
 "validation": {"old": [...], "new": [...]},
 "changes": [{"types", "old_id", "new_id", "old_id_raw", "new_id_raw", "content_hash", "details"}]}
```

`changes[].details` — те же поля, что в truth (`labels_old/new`, `group_old/new`, `direction`,
`hash_old`, `metadata_old/new`, `path_old/new`, `record`, у переименований `ambiguous` и `candidates`).

Утечки:
- `within` — `content_key` одновременно в train и test новой версии. `causes` — записи, которые
  в этой версии пришли в свою группу: `copy` (добавлена) или `move` (перенесена); `direction` —
  откуда пришло содержимое (`train->test`: запись в test, `test->train`: запись в train).
  `causes: null` — пара была и в старой версии.
- `by_move` — запись была в train старой версии и перенесена в test новой.

Вердикт — самая тяжёлая из причин:

| code | label | reason_code | когда |
|---|---|---|---|
| `do_not_trust` | не доверять | `leak_train_to_test` | в test попали примеры из train (перенос или копия): модель на них училась |
| `do_not_trust` | не доверять | `leak_preexisting` | пара train/test была уже в старой версии: метрика была завышена и раньше, скачок этим не объясняется |
| `do_not_retrain` | не переобучать на этой версии | `leak_test_to_train` | в train попали примеры из test |
| `metrics_not_comparable` | метрики несопоставимы | `test_changed` | в test что-то добавлено, удалено, заменено, перемечено или перенесено в test / из test (переименования, метаданные и косметика не считаются) |
| `trust` | доверять | `no_test_change` | test не менялся, утечек нет |

### Патч (`--patch`, `dsdiff/patch.py`)

JSON Lines; первая строка — заголовок
`{"op": "header", "format": "dsdiff-patch/1", "base", "target", "n_base", "n_target"}`, где `base` и
`target` — хеш мультимножества строк старой и новой версии. Дальше операции в порядке
`delete` → `rename` → `update` → `add`:

```json
{"op": "delete", "id": "<sample_id>", "h": "<content_hash>"}
{"op": "rename", "id": "...", "h": "...", "to": "<sample_id>"}
{"op": "update", "id": "...", "h": "...", "kind": "relabel", "set": {"labels": [...]}}
{"op": "add", "record": {...}}
```

`kind` у `update` — класс изменения: `relabel`, `group_move`, `content_replace`, `metadata_change`,
`cosmetic`; при нескольких классах — список (`["relabel", "group_move"]`). Он для читателя патча:
`apply` его не смотрит, значения в `set` / `record` по-прежнему сырые.

Пример — `examples/tiny/patch_v1_v2.jsonl`:

```json
{"op": "update", "id": "images/img_001.jpg", "h": "97c1d6b6", "kind": "relabel", "set": {"labels": ["café"]}}
{"op": "update", "id": "images/img_003.jpg", "h": "cb16f3c9", "kind": "content_replace", "set": {"content_hash": "c7ed2da9"}}
{"op": "update", "id": "images/img_004.jpg", "h": "97a5eb00", "kind": "group_move", "set": {"group": "test"}}
{"op": "update", "id": "images/img_032.jpg", "h": "2cdc493b", "kind": "cosmetic", "set": {"sample_id": "images/img_032.jpg "}}
```

Запись адресуется сырыми `sample_id` и `content_hash` на момент операции. Переименование не может
занять id, который ещё занят, поэтому переименования упорядочены, а циклы идут через
`__tmp__/<n>`. Значения в `update` / `add` сырые, так что результат совпадает с новой версией
построчно побайтно. `apply` сохраняет порядок базы и дописывает добавленные записи в конец, поэтому
strict сравнивает строки как мультимножество. На чужой базе (`base` не совпадает) `apply` отказывает.

## Хранилище и diff по дереву

```sh
python -m dsdiff commit v1.jsonl -m "v1"          # .dsdiff/ в текущей папке (или --repo DIR); --buckets B
python -m dsdiff commit v2.jsonl -m "v2"
python -m dsdiff log
python -m dsdiff diff --rev HEAD~1 HEAD --report out/report.json --patch out/patch.jsonl
python -m dsdiff check --rev HEAD~1 HEAD
python -m dsdiff cat-file HEAD                    # любой объект: ревизия или хеш
```

Ревизия — `HEAD`, `HEAD~k` или префикс хеша коммита (от 4 символов).

### Объекты (`dsdiff/store.py`)

Как в git: объект — `"<type> <size>\0<body>"`, имя — sha256 от этих байтов, хранится сжатым zlib
в `.dsdiff/objects/<2 hex>/<62 hex>`. Одинаковые объекты хранятся один раз, поэтому неизменённые
бакеты общие у всех версий.

| тип | тело |
|---|---|
| `bucket` | сырые строки записей одного из B бакетов (бакет = старшие log2 B бит blake2b(`id_key`)), по `id_key` |
| `cbucket` | строки `<content_key>\t<group>:<id>,<group>:<id>…` для B бакетов по `content_key` (в id экранируются `% , \t \n`) |
| `tree` | строки `<slot> <type> <hash> <leaks>` для непустых детей; `<leaks>` — число содержимых, которые в поддереве есть и в train, и в test (`-` в дереве по id) |
| `commit` | JSON: `buckets`, `id_root`, `content_root`, `parent`, `message`, `counts`, `leaks_count`, `digest`, `validation` |

Число бакетов B — степень двойки около n / 16 (не меньше 64), хранится в коммите. Новый коммит
оставляет B родителя, пока n / 16 в пределах ×2 от него, чтобы соседние версии сравнивались по
дереву; `--buckets` задаёт B явно. Оба дерева — с разветвлением 64 и глубиной ceil(log64 B): путь к
бакету — его цифры в системе по основанию 64 (1e4 записей → 512 бакетов, глубина 2; 1e5 → 8192,
глубина 3; 1e6 → 65 536, глубина 3). Коммиты без поля `buckets` читаются как 4096. Имя узла — хеш от имён (и счётчиков) детей, имя корня —
отпечаток версии. Число утечек версии — сумма по строкам корня, сами утечки находятся спуском только
в поддеревья с ненулевым счётчиком. `digest` — тот же хеш мультимножества строк, что в заголовке
патча. `validation` хранится со 100 id на ошибку, так что `diff --rev` с любым `--examples` до 100
даёт отчёт плоского diff. Времени в коммите нет: один и тот же файл на том же родителе даёт тот же
коммит.

### `tree_diff` (`dsdiff/treediff.py`)

Спуск только по узлам с разными хешами; записи всех отличающихся бакетов обеих версий собираются
вместе и идут через тот же `match`, что и плоский diff (переименование обычно переносит запись в
другой бакет, поэтому сопоставление по хешу должно видеть все отличающиеся бакеты сразу). Записи в
одинаковых бакетах не изменились по построению. Утечки `within` берутся из дерева содержимого
нового коммита, `by_move` — из изменений, `validation` — из коммитов. Отчёт совпадает с плоским diff
байт в байт; при равных корнях не читается ни один бакет. У коммитов с разным B общих поддеревьев
нет, поэтому они сравниваются плоским diff по записям обоих коммитов (`mode: flat: …` в выводе).
`diff --rev` печатает режим и сколько бакетов, записей и объектов прочитано.

## Бенчмарк

```sh
python -m dsdiff.bench [--quick] [--only flat_vs_tree storage baseline collisions] [--work DIR]   # -> bench/results.csv
python -m dsdiff.bench --only baseline --out bench/baseline.csv   # ~20 с; plots.py берёт его вместо строк baseline из results.csv
python bench/plots.py                                                       # -> bench/*.png, bench/summary.md
```

`results.csv` — длинный формат: `experiment, scenario, label, n, d, buckets, hash_bits, version, method, metric, value`.

- `flat_vs_tree`: n ∈ {1e5, 1e6}, d ∈ {10, 100, 1000, 10000} изменённых записей (по кругу relabel,
  rename, move, replace, meta, delete, add); время плоского diff (с чтением файлов) и `tree_diff`
  (с чтением объектов) при 4096 бакетах и при ~n/16, прочитанные бакеты, записи и объекты, время
  commit каждой версии (`d = 0` — базовая). Отчёты дерева сверяются с плоским.
- `storage`: 10 версий по 1e5 записей подряд, при изменениях генератора по умолчанию (~20% записей
  за версию) и в 20 раз реже (~1%), при 4096 бакетах и при ~n/16; сумма размеров объектов, их число,
  `du` папки `.dsdiff`, размер 10 полных JSONL и 10 JSONL в gzip.
- `baseline`: v0 / плоский / дерево на `tiny`, `story`, `clean`, `test_relabel`, `test_to_train`,
  `--n 30000 --seed 3` и на пресетах `break_*`. Метрики: строки изменений, не совпавшие с truth
  (пропущено + лишних, без групп неоднозначных переименований), вердикт и ожидаемый вердикт из
  пресета (у `tiny` и `--n 30000` его нет — он вычисляется по truth правилами `make_verdict` и в
  таблице помечен ¹), число утечек, время самого diff (один прогон).
- `collisions`: hash_bits ∈ {16, 20, 24, 32, 64}, n = 1e5; переименования из diff, которых нет в
  truth (ложные), пропущенные и неоднозначные.

`bench/plots.py` рисует по одной мысли на график: `time_vs_d.png` (время от d при n = 1e6, с
подписью «commit — O(n) один раз на версию, сравнение пары — O(d)»), `extrapolation.png` (оценка
для n = 1e6 … 1e9 при d = 100: плоский — линейно по замерам, дерево — по модели «время = a·объекты +
b·записи» с a, b ≥ 0, подобранной на замерах по относительной ошибке; сейчас a = 0 и выходит
≈ 12 мкс на прочитанную запись), `storage.png` (хранилище против 10 gzip-копий), `collisions.png`;
`breaks.png` («что ломается в v0»: по сценарию — верно найденные изменения, лишние строки, вердикт,
утечки для v0 и нашего diff); и пишет `summary.md` — таблицы «v0 / плоский / дерево» и «4096 бакетов
против ~n/16».

### Базовая линия v0 (`dsdiff/baseline_idfirst.py`)

Подход первой версии кейса (Оля, `source/diff.py`): записи по `sample_id` как есть, слияние двух
отсортированных списков id, остаток — по `content_hash` (один кандидат — переименование, несколько —
`renamed_ambiguous`), утечка — хеш и в train, и в test в любой из версий, вердикт: утечки → «не
доверять», изменился набор id в test → «метрики несопоставимы», иначе «доверять». Значения (не id)
канонизируются как у неё. Чем это кончается:

- обмен имён двух записей она видит как замену содержимого у обеих;
- id с шумом (`IMG.JPG`, `\`, `./`, пробел) — как удаление и добавление, а потом как переименование;
- запись, перенесённая из train в test, утечкой не считается (сравниваются группы только внутри версии);
- перемеченный test не меняет набор id в test, поэтому вердикт «доверять»;
- вердикта «не переобучать» нет: утечка test→train даёт «не доверять».

## Тесты

```sh
python -m pytest
```

`tests/test_gen.py` генерирует `tiny`, `story`, `clean`, `test_relabel`, `test_to_train`, `small` и прогон
`--n 20000 --versions 3 --with-path --dirty` и проверяет: `reference_apply` даёт следующую
версию; `leaks_vK` совпадает с пересчётом по файлам версий; счётчики сводки совпадают с truth;
повторная генерация побайтно равна; `examples/` актуальны; в `tiny` есть каждый тип изменения и
каждое правило шума (в том числе `id_whitespace`).

`tests/test_diff.py` на тех же пресетах и трёх сидах по 30 000
проверяет: вне неоднозначных групп (все строки с `content_hash` или `hash_old` неоднозначного
переименования) вывод diff совпадает с truth по `types` (без `leak`, `leak_by_move`, `rename_cycle`),
`old_id`, `new_id`; флаги `ambiguous` совпадают с truth; `apply(old, build_patch(diff(old, new)))`
равен new побайтно (включая неоднозначные группы); патч отказывает на чужой базе; утечки совпадают
с `leaks` и `leaks_vK` из сводки; каждый из четырёх вердиктов получается хотя бы на одном пресете;
пробелы по краям `sample_id`, `path`, `content_hash` снимаются; у `update` правильный `kind`, и `apply`
без `kind` даёт то же самое.

`tests/test_baseline.py` на пресетах `break_*`: ожидаемый вердикт в пресете тот, что задуман
(обмены и шум в id — «доверять», перенос train→test — «не доверять», смена меток в test — «метрики
несопоставимы»); наш diff совпадает с truth и даёт этот вердикт, v0 в каждом даёт другой, а на
обменах и шуме в id не находит ни одного изменения правильно. `tests/test_diff.py` проверяет, что
`expected_verdict` у `story`, `clean`, `test_relabel`, `test_to_train` совпадает с вердиктом diff.

`tests/test_olya_data.py` на `файлы Оли/data_small` (`v1.jsonl`, `v2.jsonl`): 175 строк изменений,
`apply` воспроизводит v2 побайтно, утечек within 1, by_move 9, вердикт «не доверять» (пропускается,
если папки нет).

`tests/test_store.py` коммитит все версии каждого пресета (кроме `large` и `xl`, вместе с `medium`
и `collisions`) и проверяет, что отчёт `tree_diff` равен отчёту плоского diff, `checkout` даёт
исходные строки, утечки из дерева равны `leaks_vK`, при равных корнях бакеты не читаются, а при
малом числе изменений почти все бакеты общие.
