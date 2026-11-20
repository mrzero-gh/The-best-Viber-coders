"""Преобразование эпизода как у жюри (scripts.transform_episode) и парные проверки (scripts.pair_check).

Полный прогон 20 пар: `python -m scripts.pair_check` (~20 с). Здесь по одной паре каждого вида
и свойства самого преобразования, включая негативные случаи.
"""
import hashlib
import json
import re
import shutil
import tempfile
from pathlib import Path

import pytest

import judge_adapter as ja
from grader.certificate import certify
from grader.evidence import read_public
from scripts import pair_check
from scripts.transform_episode import REMAP_PREFIXES, invert, remap_value, transform_episode

TOKEN = re.compile(r"\b(?:INV|PAY|DOC|L|E)-[0-9A-Z]{8}\b")


@pytest.fixture(scope="module")
def base():
    d = Path(tempfile.mkdtemp(prefix="pair-test-"))
    cfg = pair_check._inter("duplicate_invoice_wrong_fx")
    ja.generate_episode(42, cfg, str(d / "src"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _digest(root: Path) -> dict:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def _tokens(root: Path) -> set:
    out = set()
    for p in root.rglob("*"):
        if p.is_file() and p.suffix in (".json", ".jsonl", ".csv"):
            out |= set(TOKEN.findall(p.read_text(encoding="utf-8")))
    return out


def _lines(path: Path):
    return path.read_text(encoding="utf-8").splitlines()


def _cert_ok(ep: Path) -> bool:
    truth = json.loads((ep / "private" / "truth.json").read_text(encoding="utf-8"))
    return certify(read_public(ep / "public"), truth)["valid"]


def test_source_untouched_and_ids_fully_remapped(base):
    src = base / "src"
    before = _digest(src)
    var = base / "var-full"
    mapping = transform_episode(src, var, seed=1, permute=True, remap=True)
    assert _digest(src) == before                       # исходный комплект не перезаписан
    old = _tokens(src)
    assert set(mapping) == old                          # заменены все ID с нужными префиксами
    assert len(set(mapping.values())) == len(mapping)   # взаимно однозначно
    assert not (old & _tokens(var))                      # ни одного старого ID в копии
    assert all(k.split("-")[0] == v.split("-")[0] for k, v in mapping.items())  # пространства ID раздельны
    assert {k.split("-")[0] for k in mapping} <= set(REMAP_PREFIXES)


def test_fx_keys_counts_and_line_items_preserved(base):
    src, var = base / "src", base / "var-fx"
    mapping = transform_episode(src, var, seed=2, permute=True, remap=True)
    # курсы: те же строки (ключ дата:пара не меняется), но в другом порядке
    s_fx, v_fx = _lines(src / "public" / "fx.csv"), _lines(var / "public" / "fx.csv")
    assert s_fx[0] == v_fx[0] and sorted(s_fx[1:]) == sorted(v_fx[1:]) and s_fx != v_fx
    # число записей и manifest (кроме ссылок) не меняются
    for name in ("ledger.csv", "invoices.jsonl", "payments.jsonl"):
        assert len(_lines(src / "public" / name)) == len(_lines(var / "public" / name))
    assert (src / "public" / "manifest.json").read_text(encoding="utf-8") == \
           (var / "public" / "manifest.json").read_text(encoding="utf-8")
    # порядок line_items внутри счёта сохранён
    inv = invert(mapping)
    src_inv = {json.loads(x)["invoice_id"]: json.loads(x) for x in _lines(src / "public" / "invoices.jsonl")}
    for x in _lines(var / "public" / "invoices.jsonl"):
        back = remap_value(json.loads(x), inv)
        assert back == src_inv[back["invoice_id"]]


def test_permute_only_and_remap_only(base):
    src = base / "src"
    perm = base / "var-perm"
    assert transform_episode(src, perm, seed=3, permute=True, remap=False) == {}
    s, p = _lines(src / "public" / "ledger.csv"), _lines(perm / "public" / "ledger.csv")
    assert sorted(s) == sorted(p) and s != p
    assert (src / "private" / "truth.json").read_bytes() == (perm / "private" / "truth.json").read_bytes()
    rem = base / "var-rem"
    mapping = transform_episode(src, rem, seed=3, permute=False, remap=True)
    r = _lines(rem / "public" / "ledger.csv")
    assert r != s
    assert [TOKEN.sub(lambda m: invert(mapping)[m.group(0)], x) for x in r] == s  # тот же порядок


def test_stale_private_part_is_detected(base):
    """Негатив: если поменять ID только в public, независимый сертификат это ловит."""
    src, var = base / "src", base / "var-stale"
    transform_episode(src, var, seed=4, permute=True, remap=True)
    assert _cert_ok(var)
    shutil.copy(src / "private" / "truth.json", var / "private" / "truth.json")
    assert not _cert_ok(var)


@pytest.mark.parametrize("n", [1, 6, 11, 16, 19])
def test_pair_rows(n, monkeypatch):
    runs = Path(tempfile.mkdtemp(prefix="pair-runs-"))
    monkeypatch.setenv("AUDIT_GYM_RUNS_DIR", str(runs))
    work = Path(tempfile.mkdtemp(prefix="pair-row-"))
    try:
        with ja.make_env() as env:
            row = pair_check.check_pair(n, work, env)
    finally:
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(runs, ignore_errors=True)
    assert row["ok"], row["problems"]
    assert (row["permute"], row["remap"]) == pair_check.transform_kind(n)
    assert row["score"]["f1"] == 1.0


def test_matrix_matches_jury_definition():
    """20 сценариев: 5 чистых, 3+3+2+2 по типам, 3+2 взаимодействия; виды преобразований 5/5/10."""
    codes = [s[0] for s in pair_check.SCENARIOS]
    assert codes == ["D%02d" % i for i in range(1, 21)]
    kinds = [pair_check.transform_kind(n) for n in range(1, 21)]
    assert kinds.count((True, False)) == 5 and kinds.count((False, True)) == 5
    assert kinds.count((True, True)) == 10
