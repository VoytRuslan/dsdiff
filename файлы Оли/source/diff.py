"""diff.py — базовое сравнение двух версий манифеста.
    
    Алгоритм (базовый вариант, без дерева Меркла):
    1. Сортировка записей обеих версий по sample_id.
    2. Слияние двумя указателями (merge-join, лекция Шиндарева).
    3. Классификация различий по типам.
    4. Поиск утечек: одинаковый content_hash в train и test.
    
    Это «первая попытка сравнения», которую ждёт Ефим. Дерево Меркла и
    спуск по отличиям — следующий шаг (поток C).
    """
    import json
    from typing import Any, Dict, List, Optional, Tuple
    
    # Общие определения (INCOMPARABLE, NOT_TRUST, TRUST, canonical_record,
    # read_manifest) загружаются из параграфа «model.py — общие типы и
    # канонизация», который должен быть запущен первым (общий контекст).
    
    
    def _classify(v1_rec: Dict[str, Any], v2_rec: Dict[str, Any]) -> List[str]:
        """Классифицирует изменение записи с одинаковым sample_id.
    
        v1_rec и v2_rec — записи без sample_id (или с ним, не важно).
        Возвращает список классов изменений (может быть несколько полей
        изменено одновременно). Пустой список — косметика/без изменений.
        """
        c1 = canonical_record(v1_rec)
        c2 = canonical_record(v2_rec)
    
        if c1 == c2:
            return []  # по смыслу то же (канонизация уже применилась)
    
        out: List[str] = []
        if c1["content_hash"] != c2["content_hash"]:
            out.append("content_replaced")
        if c1["group"] != c2["group"]:
            out.append("group_moved")
        if c1["labels"] != c2["labels"]:
            out.append("relabeled")
        if c1["metadata"] != c2["metadata"]:
            out.append("metadata_changed")
        return out
    
    
    def diff(
        v1: Dict[str, Dict[str, Any]],
        v2: Dict[str, Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Сравнивает две версии и возвращает разбор различий.
    
        Возвращает dict:
          - changes: список {type, sample_id, ...} по каждому изменению
          - counts: счётчики по типам
          - leaks: список утечек {content_hash, train_sample_id, test_sample_id}
          - verdict: вердикт для бизнеса
        """
        ids1 = sorted(v1)
        ids2 = sorted(v2)
    
        changes: List[Dict[str, Any]] = []
        i = j = 0
    
        # --- merge-join по sample_id -----------------------------------------
        while i < len(ids1) and j < len(ids2):
            a, b = ids1[i], ids2[j]
            if a == b:
                for cls in _classify(v1[a], v2[b]):
                    changes.append({"type": cls, "sample_id": a})
                i += 1
                j += 1
            elif a < b:
                changes.append({"type": "deleted", "sample_id": a})
                i += 1
            else:
                changes.append({"type": "added", "sample_id": b})
                j += 1
    
        while i < len(ids1):
            changes.append({"type": "deleted", "sample_id": ids1[i]})
            i += 1
        while j < len(ids2):
            changes.append({"type": "added", "sample_id": ids2[j]})
            j += 1
    
        # --- rename: то же содержимое под другим id --------------------------
        # записи без пары по id: deleted и added. Сопоставляем по content_hash.
        deleted = {c["sample_id"]: v1[c["sample_id"]] for c in changes if c["type"] == "deleted"}
        added = {c["sample_id"]: v2[c["sample_id"]] for c in changes if c["type"] == "added"}
    
        # индекс по content_hash для added (может быть несколько кандидатов)
        added_by_hash: Dict[str, List[str]] = {}
        for sid, rec in added.items():
            added_by_hash.setdefault(canonical_record(rec)["content_hash"], []).append(sid)
    
        rename_changes: List[Dict[str, Any]] = []
        used_added: set = set()
        for sid, rec in deleted.items():
            ch = canonical_record(rec)["content_hash"]
            cands = [a for a in added_by_hash.get(ch, []) if a not in used_added]
            if len(cands) == 1:
                # однозначный rename
                rename_changes.append({"type": "renamed", "from": sid, "to": cands[0]})
                used_added.add(cands[0])
                # после rename запись могла измениться и по другим полям
                # (group/labels/metadata) — сравниваем и добавляем изменения
                for cls in _classify(v1[sid], v2[cands[0]]):
                    if cls != "content_replaced":  # content_hash совпадает по условию rename
                        rename_changes.append({"type": cls, "sample_id": cands[0]})
            elif len(cands) > 1:
                # неоднозначно — помечаем, не угадываем
                rename_changes.append({"type": "renamed_ambiguous", "from": sid, "to": cands})
    
        # убираем из added/deleted те, что стали rename
        renamed_from = {c["from"] for c in rename_changes if c["type"] == "renamed"}
        renamed_to = {c["to"] for c in rename_changes if c["type"] == "renamed"}
    
        final_changes = [
            c for c in changes
            if not (c["type"] == "deleted" and c["sample_id"] in renamed_from)
            and not (c["type"] == "added" and c["sample_id"] in renamed_to)
        ]
        final_changes.extend(rename_changes)
    
        # --- утечки: одинаковый content_hash в train и test ------------------
        leaks = _find_leaks(v1, v2)
    
        # --- вердикт ----------------------------------------------------------
        verdict = _verdict(v1, v2, leaks)
    
        counts: Dict[str, int] = {}
        for c in final_changes:
            counts[c["type"]] = counts.get(c["type"], 0) + 1
    
        return {
            "changes": final_changes,
            "counts": counts,
            "leaks": leaks,
            "verdict": verdict,
        }
    
    
    def _find_leaks(
        v1: Dict[str, Dict[str, Any]],
        v2: Dict[str, Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Находит содержимое, встречающееся и в train, и в test (в любой версии)."""
        # по content_hash -> множество групп в каждой версии
        groups_v1: Dict[str, set] = {}
        for sid, rec in v1.items():
            ch = canonical_record(rec)["content_hash"]
            groups_v1.setdefault(ch, set()).add(canonical_record(rec)["group"])
    
        groups_v2: Dict[str, set] = {}
        for sid, rec in v2.items():
            ch = canonical_record(rec)["content_hash"]
            groups_v2.setdefault(ch, set()).add(canonical_record(rec)["group"])
    
        leaks: List[Dict[str, Any]] = []
        all_hashes = set(groups_v1) | set(groups_v2)
        for ch in all_hashes:
            g1 = groups_v1.get(ch, set())
            g2 = groups_v2.get(ch, set())
            if ("train" in g1 and "test" in g1) or ("train" in g2 and "test" in g2):
                # примеры для отчёта
                train_sid = _find_in_group(v1, ch, "train") or _find_in_group(v2, ch, "train")
                test_sid = _find_in_group(v1, ch, "test") or _find_in_group(v2, ch, "test")
                leaks.append({
                    "content_hash": ch,
                    "train_sample_id": train_sid,
                    "test_sample_id": test_sid,
                })
        return leaks
    
    
    def _find_in_group(version, content_hash, group):
        for sid, rec in version.items():
            if canonical_record(rec)["content_hash"] == content_hash and \
               canonical_record(rec)["group"] == group:
                return sid
        return None
    
    
    def _verdict(v1, v2, leaks) -> str:
        """Вердикт для бизнеса."""
        # изменилась ли тестовая выборка (по составу sample_id в test)
        test1 = {sid for sid, r in v1.items() if canonical_record(r)["group"] == "test"}
        test2 = {sid for sid, r in v2.items() if canonical_record(r)["group"] == "test"}
        test_changed = test1 != test2
    
        if leaks:
            return NOT_TRUST
        if test_changed:
            return INCOMPARABLE
        return TRUST
    