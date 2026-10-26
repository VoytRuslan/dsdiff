# KIP-пакет: diff/patch для версий датасета

## Состав

- `source/` — исходный код функций (model, generator, diff, patch_apply)
- `data_small/` — сгенерированные данные (v1.jsonl, v2.jsonl, truth.json)

## Результаты прогона

### Ручной пример

- counts: `{"deleted": 2, "added": 3, "relabeled": 1, "content_replaced": 1, "group_moved": 1, "renamed_ambiguous": 1}`
- verdict: `not_trust`
- apply verified: `True`

### Синтетика (n=2000)

- diff counts: `{"added": 22, "relabeled": 40, "group_moved": 19, "deleted": 21, "content_replaced": 19, "metadata_changed": 20, "renamed": 38}`
- truth counts: `{"rename": 40, "relabel": 40, "content_replace": 20, "group_move": 20, "metadata_change": 20, "cosmetic": 10, "add": 21, "delete": 20}`
- diff leaks: `1`
- truth leaks: `1`
- synthetic apply verified: `True`