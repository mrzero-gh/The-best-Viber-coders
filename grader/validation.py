"""Проверки findings/truth по существующим схемам, только stdlib.

Это обработчик подмножества ключей двух схем проекта, не общий JSON Schema
движок. Денежные integer намеренно строже JSON Schema: 1.0 и bool запрещены.
"""
from functools import lru_cache
import re

from common.schemas import load_schema
from grader.matcher import canonical_ids


@lru_cache(maxsize=2)
def _schema(name: str) -> dict:
    return load_schema(name)


def _error(value, rule: dict, root: dict, path: str) -> str | None:
    if "$ref" in rule:
        target = root
        for part in rule["$ref"].removeprefix("#/").split("/"):
            target = target[part]
        return _error(value, target, root, path)
    if "enum" in rule and value not in rule["enum"]:
        return f"{path}: unexpected value"
    if "const" in rule and value != rule["const"]:
        return f"{path}: unexpected value"
    kind = rule.get("type")
    types = {"object": dict, "array": list, "string": str, "integer": int}
    if kind in types and type(value) is not types[kind]:
        return f"{path}: expected {kind}"
    if kind == "object":
        properties = rule["properties"]
        if any(key not in value for key in rule.get("required", [])):
            return f"{path}: missing field"
        if rule.get("additionalProperties") is False and any(key not in properties for key in value):
            return f"{path}: extra field"
        for key, field_rule in properties.items():
            if key in value:
                error = _error(value[key], field_rule, root, f"{path}.{key}")
                if error:
                    return error
    elif kind == "array":
        if not rule.get("minItems", 0) <= len(value) <= rule.get("maxItems", len(value)):
            return f"{path}: invalid item count"
        for index, item in enumerate(value):
            error = _error(item, rule["items"], root, f"{path}[{index}]")
            if error:
                return error
            if rule.get("uniqueItems") and item in value[:index]:
                return f"{path}: duplicate item"
    elif kind == "string":
        if not rule.get("minLength", 0) <= len(value) <= rule.get("maxLength", len(value)):
            return f"{path}: invalid string length"
        if "pattern" in rule and re.fullmatch(rule["pattern"], value) is None:
            return f"{path}: invalid string format"
    elif kind == "integer" and value < rule.get("minimum", value):
        return f"{path}: below minimum"
    return None


def findings_error(findings_obj, episode_id: str) -> str | None:
    """Возвращает фиксированную диагностику без данных эталона."""
    schema = _schema("findings")
    error = _error(findings_obj, schema, schema, "findings")
    if error:
        return error
    if findings_obj["episode_id"] != episode_id:
        return "findings.episode_id: wrong episode"
    return None


def validate_truth(truth: dict, episode_id: str) -> None:
    """Дефект эталона — ошибка сервера, а не штраф агенту."""
    schema = _schema("truth")
    error = _error(truth, schema, schema, "truth")
    if error:
        raise ValueError(error)
    if truth["episode_id"] != episode_id:
        raise ValueError("truth.episode_id: wrong episode")
    # Уникален только issue_id. Две разные ошибки с одной парой (type, affected_ids)
    # допустимы (графы M10/M11 у жюри): их разводит максимальное паросочетание.
    seen_ids = set()
    for issue in truth["issues"]:
        if issue["issue_id"] in seen_ids:
            raise ValueError("truth: duplicate issue ID")
        seen_ids.add(issue["issue_id"])
        for ids in [issue["affected_ids"], *issue["evidence_sets"]]:
            # повторы допустимы: это множество (правило 2), а после смены ID жюри одинаковые
            # строки могут оказаться в разных таблицах (счёт и платёж одной ошибки)
            if "" in canonical_ids(ids):
                raise ValueError("truth: empty normalized ID")
    distractor_ids = [item["distractor_id"] for item in truth["distractors"]]
    if len(distractor_ids) != len(set(distractor_ids)):
        raise ValueError("truth: duplicate distractor ID")
