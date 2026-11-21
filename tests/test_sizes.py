"""Узкие диапазоны размеров: повторная сборка со сдвигом цели (generator.build.generate_episode).

Конфиги из fixtures/fuzz_narrow_sizes.json - те, что в фаззе генератора (500 конфигов, seed 31337)
выходили за диапазон ledger_rows на 1-17 строк до повторной сборки.
"""
import json
from pathlib import Path

import pytest

from generator.build import generate_episode
from generator.config import DOCUMENT_BOUNDS, LEDGER_BOUNDS
from grader.certificate import certify_episode

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "fuzz_narrow_sizes.json").read_text(encoding="utf-8"))
NARROW = sorted(key for key in FIXTURE if not key.startswith("_"))


def _counts(ep):
    return json.loads((ep / "public" / "manifest.json").read_text(encoding="utf-8"))["counts"]


def _generation(ep):
    return json.loads((ep / "private" / "generation.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("seed", NARROW)
def test_narrow_ledger_range_is_respected(tmp_path, seed):
    cfg = FIXTURE[seed]
    ep = generate_episode(int(seed), cfg, tmp_path)
    counts = _counts(ep)
    lo, hi = cfg["size"]["ledger_rows"]
    assert lo <= counts["ledger_rows"] <= hi
    d_lo, d_hi = cfg["size"]["documents"]
    assert d_lo <= counts["documents"] <= d_hi
    assert certify_episode(ep)["valid"]


def test_retry_is_deterministic_and_recorded(tmp_path):
    seed = NARROW[0]
    first = generate_episode(int(seed), FIXTURE[seed], tmp_path / "a")
    second = generate_episode(int(seed), FIXTURE[seed], tmp_path / "b")
    files = sorted(p.relative_to(first) for p in first.rglob("*") if p.is_file())
    assert files == sorted(p.relative_to(second) for p in second.rglob("*") if p.is_file())
    for name in files:
        assert (first / name).read_bytes() == (second / name).read_bytes(), name
    retry = _generation(first)["size_retry"]
    assert retry["attempt"] >= 1 and (retry["shift_rows"], retry["shift_documents"]) != (0, 0)


def test_first_attempt_episode_has_no_retry_field(tmp_path):
    # обычный конфиг собирается с первой попытки: generation.json как раньше
    ep = generate_episode(42, {}, tmp_path)
    assert "size_retry" not in _generation(ep)


def test_incompatible_ranges_stay_within_case_bounds(tmp_path):
    # 144-193 документа не помещаются в 216-282 проводки: документов меньше, но пределы кейса соблюдены
    case = FIXTURE["_incompatible"]
    ep = generate_episode(case["seed"], case["config"], tmp_path)
    counts = _counts(ep)
    lo, hi = case["config"]["size"]["ledger_rows"]
    assert lo <= counts["ledger_rows"] <= hi
    assert LEDGER_BOUNDS[0] <= counts["ledger_rows"] <= LEDGER_BOUNDS[1]
    assert DOCUMENT_BOUNDS[0] <= counts["documents"] <= DOCUMENT_BOUNDS[1]
    assert certify_episode(ep)["valid"]


@pytest.mark.parametrize("seed", range(6))
def test_exact_minimum_with_all_errors(tmp_path, seed):
    # [200, 200] со всеми ошибками: до повторной сборки выходило 205-212 проводок
    from tests.test_generator_size_bounds import CASES
    ep = generate_episode(seed, CASES["min_heavy"], tmp_path)
    assert _counts(ep)["ledger_rows"] == 200
    assert certify_episode(ep)["valid"]


def test_large_heavy_episode_is_fast(tmp_path):
    # 2000 проводок со всеми ошибками: подсчёт свободных валютных счетов раз на отбор, а не на документ
    import time
    from tests.test_generator_size_bounds import CASES
    started = time.perf_counter()
    generate_episode(0, CASES["max_heavy"], tmp_path)
    assert time.perf_counter() - started < 15  # реально ~0.5 с, было ~2 с; запас на медленный CI


def test_size_sweep_script_runs(capsys):
    from scripts.size_sweep import main
    assert main(["--n", "3", "--seed", "1"]) == 0
    assert "узких конфигов 3" in capsys.readouterr().out
