"""Каноничный JSON и чтение/запись JSONL."""
import json
from pathlib import Path


def canonical_dumps(obj) -> str:
    """Детерминированная сериализация: сортировка ключей, без пробелов, не-ASCII как есть."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def read_jsonl(path) -> list[dict]:
    """Читает JSONL (UTF-8), пропуская пустые строки."""
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def write_jsonl(path, rows) -> None:
    """Пишет JSONL: UTF-8, переводы строк LF, каждая строка через canonical_dumps."""
    with open(Path(path), "w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(canonical_dumps(row) + "\n")
