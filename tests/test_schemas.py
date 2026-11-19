"""Проверка схем контракта и примеров к ним."""
import json
from pathlib import Path

import pytest

jsonschema = pytest.importorskip("jsonschema")
pytest.importorskip("referencing")
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

SCHEMAS = Path(__file__).resolve().parent.parent / "schemas"
SCHEMA_FILES = sorted(SCHEMAS.glob("*.schema.json"))
EXAMPLE_FILES = sorted((SCHEMAS / "examples").glob("*.json")) if (SCHEMAS / "examples").is_dir() else []


def _load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _registry():
    """Регистрирует все схемы по их $id для разрешения $ref между файлами."""
    reg = Registry()
    for p in SCHEMA_FILES:
        s = _load(p)
        if "$id" in s:
            reg = reg.with_resource(s["$id"], Resource.from_contents(s, default_specification=DRAFT202012))
    return reg


@pytest.mark.parametrize("path", SCHEMA_FILES, ids=lambda p: p.name)
def test_schema_is_valid(path):
    jsonschema.Draft202012Validator.check_schema(_load(path))


@pytest.mark.parametrize("path", EXAMPLE_FILES, ids=lambda p: p.name)
def test_example(path):
    name = path.name.split(".", 1)[0]
    schema_path = SCHEMAS / (name + ".schema.json")
    assert schema_path.exists(), "нет схемы для примера " + path.name
    validator = jsonschema.Draft202012Validator(_load(schema_path), registry=_registry())
    valid = validator.is_valid(_load(path))
    if ".invalid." in path.name:
        assert not valid, "пример должен быть невалиден"
    else:
        assert valid, "пример должен быть валиден"


def test_schemas_present():
    if not SCHEMA_FILES:
        pytest.skip("схем пока нет")
