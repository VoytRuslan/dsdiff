"""JSON Lines in the one serialization every dsdiff file uses."""
import json


def dumps(rec):
    return json.dumps(rec, ensure_ascii=False)


def read_jsonl(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f]


def write_jsonl(path, records):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for r in records:
            f.write(dumps(r) + "\n")
