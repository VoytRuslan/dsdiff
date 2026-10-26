"""generator.py — генератор синтетических версий датасета.
    
    Генерирует две версии манифеста (v1, v2) с известной разметкой изменений
    (truth.json). Сначала строится v1, затем к её копии применяются правки,
    дающие v2. Поэтому truth.json — «золотой стандарт» для проверки diff и патча.
    
    Формат правки (патча):
        {"op": "add",   "record": {...}}
        {"op": "delete", "sample_id": "..."}
        {"op": "rename", "from": "...", "to": "..."}          # тот же content_hash
        {"op": "relabel", "sample_id": "...", "labels": [...]}
        {"op": "content_replace", "sample_id": "...", "content_hash": "..."}
        {"op": "group_move", "sample_id": "...", "group": "..."}
        {"op": "metadata_change", "sample_id": "...", "metadata": {...}}
        {"op": "cosmetic", "sample_id": "...", "field": "...", "old": "...", "new": "..."}
    """
    import json
    import os
    import random
    from typing import Any, Dict, List
    
    # Общие определения (GROUPS, content_hash_of, save_truth, write_manifest)
    # загружаются из параграфа «model.py — общие типы и канонизация»,
    # который должен быть запущен первым (общий in-memory контекст).
    
    # --- Синтетические «изображения» --------------------------------------------
    _POOL = 2000  # пул уникальных содержимых
    
    
    def _make_content(i: int) -> bytes:
        # детерминированный «байтовый образ» примера
        return f"img-{i:06d}".encode("utf-8")
    
    
    def _make_labels(rng: random.Random) -> List[str]:
        n = rng.randint(1, 3)
        return [f"label_{rng.randint(0, 9)}" for _ in range(n)]
    
    
    def _make_metadata(rng: random.Random, i: int) -> Dict[str, str]:
        return {
            "source": f"src_{rng.randint(0, 4)}",
            "width": str(rng.randint(100, 1000)),
            "height": str(rng.randint(100, 1000)),
            "idx": str(i),
        }
    
    
    def _make_record(rng: random.Random, i: int, group: str) -> Dict[str, Any]:
        content = _make_content(i)
        return {
            "sample_id": f"sample_{i:06d}",
            "content_hash": content_hash_of(content),
            "group": group,
            "labels": _make_labels(rng),
            "metadata": _make_metadata(rng, i),
        }
    
    
    def _apply(records: Dict[str, Dict[str, Any]], op: Dict[str, Any]) -> None:
        """Применяет одну правку к dict записей (мутирует records)."""
        kind = op["op"]
        if kind == "add":
            rec = dict(op["record"])
            sid = rec["sample_id"]
            if sid in records:
                raise ValueError(f"add: дубликат sample_id {sid}")
            records[sid] = rec
        elif kind == "delete":
            records.pop(op["sample_id"], None)
        elif kind == "rename":
            rec = records.pop(op["from"])
            rec["sample_id"] = op["to"]
            records[op["to"]] = rec
        elif kind == "relabel":
            records[op["sample_id"]]["labels"] = list(op["labels"])
        elif kind == "content_replace":
            records[op["sample_id"]]["content_hash"] = op["content_hash"]
        elif kind == "group_move":
            records[op["sample_id"]]["group"] = op["group"]
        elif kind == "metadata_change":
            records[op["sample_id"]]["metadata"] = dict(op["metadata"])
        elif kind == "cosmetic":
            rec = records[op["sample_id"]]
            # field может быть путём через точку, например "metadata.source"
            parts = op["field"].split(".")
            obj = rec
            for p in parts[:-1]:
                obj = obj[p]
            obj[parts[-1]] = op["new"]
        else:
            raise ValueError(f"Неизвестная операция: {kind}")
    
    
    def generate(
        n: int = 100_000,
        seed: int = 42,
        out_dir: str = "data",
        leak: bool = True,
    ) -> Dict[str, Any]:
        """Генерирует v1.jsonl, v2.jsonl и truth.json в out_dir.
    
        Возвращает truth (словарь разметки изменений).
        """
        os.makedirs(out_dir, exist_ok=True)
        rng = random.Random(seed)
    
        # --- v1: базовый набор (фиксируем сразу) ------------------------------
        v1: Dict[str, Dict[str, Any]] = {}
        for i in range(n):
            group = rng.choices(GROUPS, weights=[0.7, 0.1, 0.2])[0]
            v1[f"sample_{i:06d}"] = _make_record(rng, i, group)
    
        # --- v2: копия v1, к которой применяем правки -------------------------
        v2: Dict[str, Dict[str, Any]] = {sid: dict(r) for sid, r in v1.items()}
    
        changes: List[Dict[str, Any]] = []
        leaks: List[Dict[str, Any]] = []
    
        # 1) rename: часть записей переименовываем (тот же content_hash)
        n_rename = int(n * 0.02)
        rename_ids = rng.sample(sorted(v2), n_rename)
        for old in rename_ids:
            new = f"renamed_{old}"
            changes.append({"op": "rename", "from": old, "to": new})
            _apply(v2, {"op": "rename", "from": old, "to": new})
    
        # 2) relabel: меняем метки при том же содержимом
        n_relabel = int(n * 0.02)
        relabel_ids = rng.sample(sorted(v2), n_relabel)
        for sid in relabel_ids:
            new_labels = _make_labels(rng)
            changes.append({"op": "relabel", "sample_id": sid, "labels": new_labels})
            _apply(v2, {"op": "relabel", "sample_id": sid, "labels": new_labels})
    
        # 3) content_replace: тот же id, другое содержимое
        n_replace = int(n * 0.01)
        replace_ids = rng.sample(sorted(v2), n_replace)
        for sid in replace_ids:
            new_content = _make_content(rng.randint(_POOL, _POOL * 2))
            new_hash = content_hash_of(new_content)
            changes.append({"op": "content_replace", "sample_id": sid, "content_hash": new_hash})
            _apply(v2, {"op": "content_replace", "sample_id": sid, "content_hash": new_hash})
    
        # 4) group_move: переезд между train/val/test
        n_move = int(n * 0.01)
        move_ids = rng.sample(sorted(v2), n_move)
        for sid in move_ids:
            cur = v2[sid]["group"]
            new_group = rng.choice([g for g in GROUPS if g != cur])
            changes.append({"op": "group_move", "sample_id": sid, "group": new_group})
            _apply(v2, {"op": "group_move", "sample_id": sid, "group": new_group})
    
        # 5) metadata_change
        n_meta = int(n * 0.01)
        meta_ids = rng.sample(sorted(v2), n_meta)
        for sid in meta_ids:
            new_meta = _make_metadata(rng, rng.randint(0, 10 ** 6))
            changes.append({"op": "metadata_change", "sample_id": sid, "metadata": new_meta})
            _apply(v2, {"op": "metadata_change", "sample_id": sid, "metadata": new_meta})
    
        # 6) cosmetic: отличие только в написании (регистр/слэши)
        n_cosmetic = int(n * 0.005)
        cosmetic_ids = rng.sample(sorted(v2), n_cosmetic)
        for sid in cosmetic_ids:
            rec = v2[sid]
            old = rec["metadata"]["source"]
            new = old.upper()  # регистр — косметика
            changes.append({"op": "cosmetic", "sample_id": sid, "field": "metadata.source",
                            "old": old, "new": new})
            _apply(v2, {"op": "cosmetic", "sample_id": sid, "field": "metadata.source",
                        "old": old, "new": new})
    
        # 7) add: новые записи
        n_add = int(n * 0.01)
        for k in range(n_add):
            i = n + k
            rec = _make_record(rng, i, rng.choices(GROUPS, weights=[0.7, 0.1, 0.2])[0])
            changes.append({"op": "add", "record": rec})
            _apply(v2, {"op": "add", "record": rec})
    
        # 8) delete: удаляем записи
        n_del = int(n * 0.01)
        del_ids = rng.sample(sorted(v2), n_del)
        for sid in del_ids:
            changes.append({"op": "delete", "sample_id": sid})
            _apply(v2, {"op": "delete", "sample_id": sid})
    
        # 9) утечка: одно и то же содержимое в train и test
        if leak:
            train_ids = [sid for sid, r in v2.items() if r["group"] == "train"]
            if train_ids:
                src = rng.choice(train_ids)
                src_rec = v2[src]
                new_id = f"leak_{src}"
                leak_rec = {
                    "sample_id": new_id,
                    "content_hash": src_rec["content_hash"],
                    "group": "test",
                    "labels": list(src_rec["labels"]),
                    "metadata": dict(src_rec["metadata"]),
                }
                changes.append({"op": "add", "record": leak_rec})
                _apply(v2, {"op": "add", "record": leak_rec})
                leaks.append({
                    "content_hash": src_rec["content_hash"],
                    "train_sample_id": src,
                    "test_sample_id": new_id,
                })
    
        # --- запись ------------------------------------------------------------
        write_manifest(os.path.join(out_dir, "v1.jsonl"), v1)
        write_manifest(os.path.join(out_dir, "v2.jsonl"), v2)
    
        truth = {
            "changes": changes,
            "leaks": leaks,
            "counts": _count_ops(changes),
            "meta": {"n": n, "seed": seed, "leak": leak},
        }
        save_truth(os.path.join(out_dir, "truth.json"), truth)
        return truth
    
    
    def _count_ops(changes):
        counts = {}
        for op in changes:
            counts[op["op"]] = counts.get(op["op"], 0) + 1
        return counts
    