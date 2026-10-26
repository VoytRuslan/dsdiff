"""model.py — общие типы, канонизация и утилиты для кейса 14.
    
    Манифест: JSONL, по строке на пример.
    Запись: {sample_id, content_hash, group, labels, metadata}
    """
    import hashlib
    import json
    from typing import Any, Dict, List, Optional
    
    # --- Классы изменений -------------------------------------------------------
    CHANGE_TYPES = [
        "added", "deleted", "renamed", "relabeled",
        "content_replaced", "group_moved", "metadata_changed", "cosmetic",
    ]
    
    # --- Вердикты ---------------------------------------------------------------
    TRUST = "trust"
    NOT_TRUST = "not_trust"
    INCOMPARABLE = "incomparable"
    
    # --- Группы -----------------------------------------------------------------
    GROUPS = ("train", "val", "test")
    
    
    def canonicalize(value: Any) -> Any:
        """Приводит значение к канонической форме.
    
        - строки: strip, нижний регистр, слэши -> '/'
        - списки: сортировка (для labels порядок не важен)
        - словари: рекурсивно, ключи отсортированы
        """
        if isinstance(value, str):
            return value.strip().lower().replace("\", "/")
        if isinstance(value, list):
            return sorted(canonicalize(v) for v in value)
        if isinstance(value, dict):
            return {k: canonicalize(v) for k, v in sorted(value.items())}
        return value
    
    
    def canonical_record(rec: Dict[str, Any]) -> Dict[str, Any]:
        """Каноническая форма записи (без sample_id — он ключ)."""
        return {
            "content_hash": canonicalize(rec.get("content_hash", "")),
            "group": canonicalize(rec.get("group", "")),
            "labels": canonicalize(rec.get("labels", [])),
            "metadata": canonicalize(rec.get("metadata", {})),
        }
    
    
    def record_hash(rec: Dict[str, Any]) -> str:
        """Хеш записи от канонического JSON (без sample_id).
    
        Одинаковые записи -> одинаковый хеш (идея ETag).
        """
        canon = canonical_record(rec)
        payload = json.dumps(canon, sort_keys=True, separators=(",", ":"))
        return hashlib.blake2b(payload.encode("utf-8"), digest_size=8).hexdigest()
    
    
    def content_hash_of(content: bytes) -> str:
        """Хеш содержимого (64-бит blake2b)."""
        return hashlib.blake2b(content, digest_size=8).hexdigest()
    
    
    def read_manifest(path: str) -> Dict[str, Dict[str, Any]]:
        """Читает JSONL-манифест в dict {sample_id: record}.
    
        Дубли sample_id -> ValueError (ошибка валидации).
        """
        records: Dict[str, Dict[str, Any]] = {}
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                sid = rec["sample_id"]
                if sid in records:
                    raise ValueError(f"Дубликат sample_id в манифесте: {sid}")
                records[sid] = rec
        return records
    
    
    def write_manifest(path: str, records: Dict[str, Dict[str, Any]]) -> None:
        """Пишет dict {sample_id: record} в JSONL-манифест."""
        with open(path, "w", encoding="utf-8") as f:
            for sid in sorted(records):
                rec = dict(records[sid])
                rec["sample_id"] = sid
                f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "
")
    
    
    def load_truth(path: str) -> Dict[str, Any]:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    
    
    def save_truth(path: str, truth: Dict[str, Any]) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(truth, f, ensure_ascii=False, indent=2)
    