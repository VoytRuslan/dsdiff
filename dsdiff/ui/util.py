"""Errors, paths confined to --root, and reading a version file with progress and clear messages."""
import json
import os

REQUIRED = ("sample_id", "content_hash", "group", "labels", "metadata")


class ApiError(Exception):
    """An error the page shows as is: HTTP status, code, message in Russian, details."""

    def __init__(self, status, code, message, detail=None):
        super().__init__(message)
        self.status, self.code, self.message, self.detail = status, code, message, detail or {}

    def to_json(self):
        return {"error": self.code, "message": self.message, "detail": self.detail}


class Cancelled(Exception):
    pass


class Root:
    """Paths from the client are resolved inside --root; anything that lands outside it is refused."""

    def __init__(self, path):
        self.real = os.path.realpath(path)

    def resolve(self, rel, kind=None):
        """kind: None (may not exist), "file" or "dir" (must exist and be one)."""
        if not isinstance(rel, str):
            raise ApiError(400, "bad_request", "Не указан путь.")
        path = os.path.realpath(os.path.join(self.real, rel))  # an absolute rel replaces the root
        if os.path.commonpath([path, self.real]) != self.real:
            raise ApiError(403, "path_outside_root", f"Путь «{rel}» ведёт за пределы папки {self.real}.",
                           {"path": rel, "root": self.real})
        if kind == "file" and not os.path.isfile(path):
            raise ApiError(404, "not_a_file", f"Файл «{rel}» не найден.", {"path": rel})
        if kind == "dir" and not os.path.isdir(path):
            raise ApiError(404, "not_found", f"Папка «{rel}» не найдена.", {"path": rel})
        return path

    def rel(self, path):
        r = os.path.relpath(path, self.real)
        return "" if r == "." else r

    def inside(self, path):
        real = os.path.realpath(path)
        return os.path.commonpath([real, self.real]) == self.real


def count_lines(path, chunk=1 << 20):
    """Lines in a file without parsing them (a last line without a newline counts too)."""
    n, last = 0, b"\n"
    with open(path, "rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            n += block.count(b"\n")
            last = block[-1:]
    return n + (last != b"\n")


def read_records(path, shown, progress=None):
    """The records of a version file, exactly as dsdiff.jsonl.read_jsonl reads them (one json.loads per
    line, text mode), with a message naming the line on a bad one. progress(lines read, estimated total)
    is called every 2000 lines and at the end; it may raise Cancelled."""
    size = os.path.getsize(path) or 1
    records, chars = [], 0
    with open(path, encoding="utf-8") as f:
        try:
            for n, line in enumerate(f, 1):
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError as e:
                    col = min(e.pos, len(line.rstrip("\r\n"))) + 1  # within the line, not past its end
                    raise ApiError(400, "bad_jsonl", f"Файл {shown}, строка {n}: неверный JSON ({e.msg}, "
                                                     f"столбец {col}).",
                                   {"file": shown, "line": n, "column": col, "text": line[:200]}) from None
                if not isinstance(rec, dict) or any(k not in rec for k in REQUIRED):
                    missing = [k for k in REQUIRED if not isinstance(rec, dict) or k not in rec]
                    raise ApiError(400, "bad_record", f"Файл {shown}, строка {n}: в записи нет полей "
                                                      f"{', '.join(missing)}.",
                                   {"file": shown, "line": n, "missing": missing})
                records.append(rec)
                chars += len(line)
                if progress and n % 2000 == 0:
                    progress(n, int(n * size / max(chars, 1)))
        except UnicodeDecodeError as e:
            raise ApiError(400, "bad_jsonl", f"Файл {shown}: не UTF-8 ({e.reason}).", {"file": shown}) from None
    if progress:
        progress(len(records), len(records))
    return records
