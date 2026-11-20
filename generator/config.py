"""Конфиг генератора: значения по умолчанию и проверка (контракт, раздел 13).

В рантайме нет jsonschema, поэтому проверка своя и повторяет config.schema.json.
"""
import copy
import re

ERROR_TYPES = ("duplicate_invoice", "payment_allocation", "fx_conversion", "period_cutoff")
INTERACTIONS = ("partial_fx_payment_at_cutoff", "duplicate_invoice_wrong_fx")
DISTRACTORS = ("split_payment", "refund", "legit_recurring", "late_evening_local")
ARCHETYPES = ("importer", "saas", "distributor")

LEDGER_BOUNDS = (200, 2000)
DOCUMENT_BOUNDS = (20, 200)
MAX_ERRORS = 12

DEFAULT_CONFIG = {
    "config_version": "default-v1",
    "mode": "train",
    "company": {
        "archetype": "importer",
        "base_currency": "KZT",
        "timezone": "+05:00",
        "foreign_currencies": ["USD", "CNY", "EUR", "RUB", "JPY"],
    },
    "size": {"ledger_rows": [300, 1200], "documents": [40, 120]},
    "errors": {t: {"enabled": True, "count": [0, 2]} for t in ERROR_TYPES},
    "interactions": {s: {"enabled": False, "count": [0, 1]} for s in INTERACTIONS},
    "distractors": {k: {"enabled": True, "count": [0, 2]} for k in DISTRACTORS},
    "clean": False,
    "max_errors": MAX_ERRORS,
}

_CUR = re.compile(r"^[A-Z]{3}$")
_TZ = re.compile(r"^[+-][0-9]{2}:[0-9]{2}$")
_VER = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


class ConfigError(ValueError):
    """Конфиг не соответствует контракту."""


def _check_keys(obj, allowed, where):
    if not isinstance(obj, dict):
        raise ConfigError(f"{where}: ожидается объект")
    extra = sorted(set(obj) - set(allowed))
    if extra:
        raise ConfigError(f"{where}: лишние поля {extra}")


def _check_range(value, lo, hi, where):
    ok = (isinstance(value, list) and len(value) == 2
          and all(isinstance(v, int) and not isinstance(v, bool) for v in value))
    if not ok or not (lo <= value[0] <= value[1] <= hi):
        raise ConfigError(f"{where}: нужен [min, max] в пределах [{lo}, {hi}], min <= max")


def _merge_toggles(section_default, given, names, where):
    _check_keys(given, names, where)
    out = copy.deepcopy(section_default)
    for name, toggle in given.items():
        _check_keys(toggle, ("enabled", "count"), f"{where}.{name}")
        if not isinstance(toggle.get("enabled"), bool):
            raise ConfigError(f"{where}.{name}.enabled: нужен true/false")
        out[name] = {**out[name], **copy.deepcopy(toggle)}
        _check_range(out[name]["count"], 0, MAX_ERRORS, f"{where}.{name}.count")
    return out


def normalize_config(config) -> dict:
    """Проверяет конфиг и дополняет его значениями по умолчанию. Вход не меняется."""
    if config is None:
        config = {}
    _check_keys(config, DEFAULT_CONFIG.keys(), "config")
    cfg = copy.deepcopy(DEFAULT_CONFIG)

    for key in ("config_version", "mode", "clean", "max_errors"):
        if key in config:
            cfg[key] = config[key]
    if not isinstance(cfg["config_version"], str) or not _VER.match(cfg["config_version"]):
        raise ConfigError("config_version: неверный формат")
    if cfg["mode"] not in ("train", "eval"):
        raise ConfigError("mode: train или eval")
    if not isinstance(cfg["clean"], bool):
        raise ConfigError("clean: нужен true/false")
    me = cfg["max_errors"]
    if not isinstance(me, int) or isinstance(me, bool) or not 0 <= me <= MAX_ERRORS:
        raise ConfigError(f"max_errors: целое 0..{MAX_ERRORS}")

    company = config.get("company", {})
    _check_keys(company, cfg["company"].keys(), "company")
    cfg["company"].update(copy.deepcopy(company))
    c = cfg["company"]
    if c["archetype"] not in ARCHETYPES:
        raise ConfigError(f"company.archetype: одно из {ARCHETYPES}")
    if not isinstance(c["base_currency"], str) or not _CUR.match(c["base_currency"]):
        raise ConfigError("company.base_currency: три заглавные буквы")
    if not isinstance(c["timezone"], str) or not _TZ.match(c["timezone"]):
        raise ConfigError("company.timezone: формат +05:00")
    fc = c["foreign_currencies"]
    if (not isinstance(fc, list) or len(fc) > 6 or len(set(fc)) != len(fc)
            or not all(isinstance(x, str) and _CUR.match(x) for x in fc)):
        raise ConfigError("company.foreign_currencies: до 6 разных кодов валют")
    # базовая валюта среди иностранных не нужна: молча убираем
    c["foreign_currencies"] = [x for x in fc if x != c["base_currency"]]

    size = config.get("size", {})
    _check_keys(size, ("ledger_rows", "documents"), "size")
    cfg["size"].update(copy.deepcopy(size))
    _check_range(cfg["size"]["ledger_rows"], *LEDGER_BOUNDS, "size.ledger_rows")
    _check_range(cfg["size"]["documents"], *DOCUMENT_BOUNDS, "size.documents")

    cfg["errors"] = _merge_toggles(cfg["errors"], config.get("errors", {}), ERROR_TYPES, "errors")
    cfg["interactions"] = _merge_toggles(cfg["interactions"], config.get("interactions", {}),
                                         INTERACTIONS, "interactions")
    cfg["distractors"] = _merge_toggles(cfg["distractors"], config.get("distractors", {}),
                                        DISTRACTORS, "distractors")
    return cfg
