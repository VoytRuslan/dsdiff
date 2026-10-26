"""patch_apply.py — построение патча из diff и применение к версии.
    
    Патч — упорядоченный список правок, превращающий старую версию в новую.
    Порядок: delete -> rename -> update -> add (как в git apply).
    
    Циклы переименований (a -> b, b -> a) разрываются временным именем:
        rename a -> __tmp, rename b -> a, rename __tmp -> b
    
    Формат правки (совпадает с генератором):
        {"op": "add",   "record": {...}}
        {"op": "delete", "sample_id": "..."}
        {"op": "rename", "from": "...", "to": "..."}
        {"op": "relabel", "sample_id": "...", "labels": [...]}
        {"op": "content_replace", "sample_id": "...", "content_hash": "..."}
        {"op": "group_move", "sample_id": "...", "group": "..."}
        {"op": "metadata_change", "sample_id": "...", "metadata": {...}}
        {"op": "cosmetic", "sample_id": "...", "field": "...", "old": "...", "new": "..."}
    """
    import json
    from typing import Any, Dict, List
    
    # Общие определения (canonical_record, read_manifest, write_manifest)
    # загружаются из параграфа «model.py — общие типы и канонизация»,
    # который должен быть запущен первым (общий in-memory контекст).
    
    
    def build_patch(
        v1: Dict[str, Dict[str, Any]],
        v2: Dict[str, Dict[str, Any]],
        diff_result: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Строит упорядоченный список правок из результата diff.
    
        diff_result — результат diff.diff(v1, v2).
        """
        changes = diff_result["changes"]
    
        deletes: List[Dict[str, Any]] = []
        renames: List[Dict[str, Any]] = []
        updates: List[Dict[str, Any]] = []
        adds: List[Dict[str, Any]] = []
    
        for c in changes:
            t = c["type"]
            if t == "deleted":
                deletes.append({"op": "delete", "sample_id": c["sample_id"]})
            elif t == "renamed":
                renames.append({"op": "rename", "from": c["from"], "to": c["to"]})
            elif t == "renamed_ambiguous":
                # неоднозначный rename — не угадываем, пропускаем (останется add/delete)
                pass
            elif t == "added":
                sid = c["sample_id"]
                rec = v2[sid].copy()
                rec.pop("sample_id", None)
                adds.append({"op": "add", "record": {**rec, "sample_id": sid}})
            elif t == "relabeled":
                updates.append({"op": "relabel", "sample_id": c["sample_id"],
                                "labels": list(v2[c["sample_id"]]["labels"])})
            elif t == "content_replaced":
                updates.append({"op": "content_replace", "sample_id": c["sample_id"],
                                "content_hash": v2[c["sample_id"]]["content_hash"]})
            elif t == "group_moved":
                updates.append({"op": "group_move", "sample_id": c["sample_id"],
                                "group": v2[c["sample_id"]]["group"]})
            elif t == "metadata_changed":
                updates.append({"op": "metadata_change", "sample_id": c["sample_id"],
                                "metadata": v2[c["sample_id"]]["metadata"].copy()})
            elif t == "cosmetic":
                # косметика уже учтена канонизацией; для побайтового восстановления
                # нужна правка, но в базовом diff мы её не детализируем.
                pass
    
        # --- разрыв циклов переименований -------------------------------------
        renames = _break_rename_cycles(renames)
    
        # порядок: delete -> rename -> update -> add
        return deletes + renames + updates + adds
    
    
    def _break_rename_cycles(renames: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Разрывает циклы переименований временным именем.
    
        Пример: a->b, b->a  =>  a->__tmp, b->a, __tmp->b
        """
        # строим граф from -> to
        fwd: Dict[str, str] = {}
        for r in renames:
            fwd[r["from"]] = r["to"]
    
        result: List[Dict[str, Any]] = []
        visited: set = set()
        tmp_counter = 0
    
        for r in renames:
            if r["from"] in visited:
                continue
            # ищем цикл, начиная с r["from"]
            cycle = _find_cycle(r["from"], fwd)
            if cycle:
                # разрываем цикл
                tmp = f"__dsdiff_tmp_{tmp_counter}"
                tmp_counter += 1
                # первый шаг цикла через временное имя
                first_from = cycle[0]
                first_to = fwd[first_from]
                result.append({"op": "rename", "from": first_from, "to": tmp})
                # остальные шаги цикла напрямую
                for i in range(1, len(cycle)):
                    result.append({"op": "rename", "from": cycle[i], "to": fwd[cycle[i]]})
                # возврат из временного имени
                result.append({"op": "rename", "from": tmp, "to": first_to})
                visited.update(cycle)
            else:
                result.append(r)
                visited.add(r["from"])
        return result
    
    
    def _find_cycle(start: str, fwd: Dict[str, str]) -> List[str]:
        """Возвращает список узлов цикла, если start входит в цикл, иначе [].
    
        Идём по fwd, пока не упрёмся в повтор или в отсутствие ребра.
        """
        path: List[str] = []
        seen: set = set()
        cur = start
        while cur in fwd and cur not in seen:
            seen.add(cur)
            path.append(cur)
            cur = fwd[cur]
        if cur == start and path:
            return path
        return []
    
    
    def apply_patch(
        v1: Dict[str, Dict[str, Any]],
        patch: List[Dict[str, Any]],
    ) -> Dict[str, Dict[str, Any]]:
        """Применяет патч к v1 и возвращает новую версию (dict {sample_id: record})."""
        result = {sid: dict(r) for sid, r in v1.items()}
    
        for op in patch:
            kind = op["op"]
            if kind == "delete":
                result.pop(op["sample_id"], None)
            elif kind == "rename":
                rec = result.pop(op["from"])
                rec["sample_id"] = op["to"]
                result[op["to"]] = rec
            elif kind == "add":
                rec = dict(op["record"])
                sid = rec["sample_id"]
                result[sid] = rec
            elif kind == "relabel":
                result[op["sample_id"]]["labels"] = list(op["labels"])
            elif kind == "content_replace":
                result[op["sample_id"]]["content_hash"] = op["content_hash"]
            elif kind == "group_move":
                result[op["sample_id"]]["group"] = op["group"]
            elif kind == "metadata_change":
                result[op["sample_id"]]["metadata"] = dict(op["metadata"])
            elif kind == "cosmetic":
                result[op["sample_id"]][op["field"]] = op["new"]
            else:
                raise ValueError(f"Неизвестная операция: {kind}")
        return result
    
    
    def verify(
        applied: Dict[str, Dict[str, Any]],
        v2: Dict[str, Dict[str, Any]],
    ) -> bool:
        """Проверяет, что применённая версия совпадает с целевой (v2).
    
        Сравнение по канонической форме записи (без sample_id как ключа).
        """
        if set(applied) != set(v2):
            return False
        for sid in applied:
            if canonical_record(applied[sid]) != canonical_record(v2[sid]):
                return False
        return True
    