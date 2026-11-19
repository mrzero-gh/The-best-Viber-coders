"""Загрузка JSON-схем контракта из папки schemas/ (без jsonschema в рантайме)."""
import json
from pathlib import Path

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"
_SUFFIX = ".schema.json"


def load_schema(name: str) -> dict:
    """Читает schemas/{name}.schema.json (UTF-8) и возвращает словарь."""
    path = SCHEMA_DIR / (name + _SUFFIX)
    return json.loads(path.read_text(encoding="utf-8"))


def list_schemas() -> list[str]:
    """Имена всех схем без суффикса .schema.json, отсортированы."""
    if not SCHEMA_DIR.is_dir():
        return []
    return sorted(p.name[: -len(_SUFFIX)] for p in SCHEMA_DIR.glob("*" + _SUFFIX))
