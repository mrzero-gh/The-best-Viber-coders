"""Преобразование эпизода для парных проверок, как его делает жюри (fintech-checks.html, блок 4).

    python -m scripts.transform_episode SRC DST [--seed N] [--permute] [--remap] [--remap-episode-id]

Исходный эпизод не меняется: создаётся копия, в которой
- переставлен порядок записей в таблицах (`ledger.csv`, `fx.csv`, `invoices.jsonl`,
  `payments.jsonl`); порядок внутри документа (`line_items`) не трогается;
- взаимно однозначно заменены ID записей и все ссылки на них во всех файлах, в том числе
  в `private/truth.json` и `private/provenance.json`: row_id (`L-`), invoice_id (`INV-`),
  payment_id (`PAY-`), doc_id (`DOC-`), entity_id (`E-`). У каждого префикса своё пространство ID.
  Ключи курсов `FX:<дата>:<валюта>:<база>` не меняются: это не суррогатные ID.

`private/generation.json` намеренно не пересчитывается (жюри его тоже не обновляет), поэтому
копию загружают с `--trust-episode` (так делает judge_adapter). При смене ID пересчитывается
только `provenance.clean_history_sha256`: ID в журнале чистой истории заменены, значит и хэш
чистой истории другой; баланс и согласованность журнала `grader.clean_history` проверяет заново.
"""
import argparse
import csv
import io
import json
import random
import re
import shutil
from pathlib import Path

REMAP_PREFIXES = ("L", "INV", "PAY", "DOC", "E")
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32, как в генераторе
_TEXT = (".json", ".jsonl", ".csv")
_TABLES = ("ledger.csv", "fx.csv", "invoices.jsonl", "payments.jsonl")


def _token_re(prefixes):
    return re.compile(r"\b(%s)-([0-9A-Z]{8})\b" % "|".join(sorted(prefixes, key=len, reverse=True)))


def _files(root: Path):
    return sorted(p for p in root.rglob("*") if p.is_file() and p.suffix in _TEXT)


def build_mapping(episode_dir, rng: random.Random, prefixes=REMAP_PREFIXES) -> dict:
    """Собирает все ID с данными префиксами во всех файлах эпизода и даёт каждому новый."""
    tok = _token_re(prefixes)
    old = set()
    for f in _files(Path(episode_dir)):
        old.update(m.group(0) for m in tok.finditer(f.read_text(encoding="utf-8")))
    mapping, used = {}, set(old)
    for oid in sorted(old):
        prefix = oid.split("-", 1)[0]
        while True:
            nid = prefix + "-" + "".join(rng.choice(_ALPHABET) for _ in range(8))
            if nid not in used:
                break
        used.add(nid)
        mapping[oid] = nid
    return mapping


def _apply_mapping(root: Path, mapping: dict, prefixes):
    tok = _token_re(prefixes)
    for f in _files(root):
        text = f.read_text(encoding="utf-8")
        new = tok.sub(lambda m: mapping.get(m.group(0), m.group(0)), text)
        if new != text:
            f.write_text(new, encoding="utf-8", newline="")


def _shuffle(rows: list, rng: random.Random) -> list:
    """Перестановка, гарантированно отличная от исходного порядка (если строк больше одной)."""
    out = list(rows)
    if len(set(out)) < 2:
        return out
    while out == rows:
        rng.shuffle(out)
    return out


def _permute_table(path: Path, rng: random.Random):
    text = path.read_text(encoding="utf-8")
    lines = text.split("\n")
    trailing = lines and lines[-1] == ""
    if trailing:
        lines = lines[:-1]
    if path.suffix == ".csv":
        header, rows = lines[0], lines[1:]
        # каждая запись CSV должна занимать ровно одну строку, иначе перестановка строк сломает файл
        parsed = list(csv.reader(io.StringIO("\n".join(lines) + "\n")))
        if len(parsed) != len(lines):
            raise ValueError("%s: многострочные поля CSV не поддерживаются" % path.name)
        lines = [header] + _shuffle(rows, rng)
    else:
        lines = _shuffle(lines, rng)
    path.write_text("\n".join(lines) + ("\n" if trailing else ""), encoding="utf-8", newline="")


def transform_episode(src, dst, *, seed: int = 0, permute: bool = True, remap: bool = True,
                      remap_episode_id: bool = False) -> dict:
    """Копирует эпизод src в dst и преобразует копию. Возвращает отображение старый ID -> новый."""
    src, dst = Path(src), Path(dst)
    shutil.copytree(src, dst)
    rng = random.Random(seed)
    mapping = {}
    if remap:
        prefixes = REMAP_PREFIXES + (("EP",) if remap_episode_id else ())
        mapping = build_mapping(dst, rng, prefixes)
        _apply_mapping(dst, mapping, prefixes)
        _rehash_clean_history(dst)
    if permute:
        for name in _TABLES:
            _permute_table(dst / "public" / name, rng)
    return mapping


def _rehash_clean_history(ep: Path) -> None:
    """После смены ID: хэш чистой истории, восстановленной уже с новыми ID."""
    from common.jsonio import canonical_dumps
    from grader import clean_history
    from grader.evidence import read_public
    path = ep / "private" / "provenance.json"
    if not path.exists():
        return
    prov = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(prov.get("clean_history_diff"), dict) or not prov.get("clean_history_sha256"):
        return
    tables, errors = clean_history.reconstruct(read_public(ep / "public"), prov["clean_history_diff"])
    if errors:  # журнал не сходится с копией: оставляем старый хэш, проверка честно упадёт
        return
    prov["clean_history_sha256"] = clean_history.digest(tables)
    path.write_text(canonical_dumps(prov) + "\n", encoding="utf-8", newline="\n")


def invert(mapping: dict) -> dict:
    return {v: k for k, v in mapping.items()}


def remap_value(obj, mapping: dict):
    """Заменяет ID внутри JSON-значения (для обратного отображения findings)."""
    if isinstance(obj, str):
        return mapping.get(obj, obj)
    if isinstance(obj, list):
        return [remap_value(x, mapping) for x in obj]
    if isinstance(obj, dict):
        return {k: remap_value(v, mapping) for k, v in obj.items()}
    return obj


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="scripts.transform_episode")
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--permute", action="store_true", help="переставить записи")
    ap.add_argument("--remap", action="store_true", help="заменить ID")
    ap.add_argument("--remap-episode-id", action="store_true", help="заменить и episode_id")
    ap.add_argument("--mapping-out", default=None, help="куда сохранить отображение ID (JSON)")
    a = ap.parse_args(argv)
    permute, remap = a.permute, a.remap
    if not (permute or remap):
        permute = remap = True  # по умолчанию оба преобразования
    mapping = transform_episode(a.src, a.dst, seed=a.seed, permute=permute, remap=remap,
                                remap_episode_id=a.remap_episode_id)
    if a.mapping_out:
        Path(a.mapping_out).write_text(json.dumps(mapping, indent=1, sort_keys=True), encoding="utf-8")
    print("Готово: %s (переставлено: %s, заменено ID: %d)" % (a.dst, permute, len(mapping)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
