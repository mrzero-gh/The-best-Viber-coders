"""Границы размера эпизода на крайних конфигах: 200..2000 проводок, 20..200 документов.

Раньше при узком диапазоне ([2000, 2000] / [200, 200]) запас под ошибки и отвлекающие случаи
возвращался нижней границей, и эпизод выходил за предел кейса (например 2020 строк, 207 документов).
"""
import json

import pytest

from generator.build import generate_episode
from generator.config import DISTRACTORS, DOCUMENT_BOUNDS, ERROR_TYPES, INTERACTIONS, LEDGER_BOUNDS

HEAVY = {"errors": {t: {"enabled": True, "count": [3, 3]} for t in ERROR_TYPES},
         "interactions": {s: {"enabled": True, "count": [2, 2]} for s in INTERACTIONS},
         "distractors": {k: {"enabled": True, "count": [3, 3]} for k in DISTRACTORS}}

CASES = {
    "max": {"size": {"ledger_rows": [2000, 2000], "documents": [200, 200]}},
    "max_heavy": dict(HEAVY, size={"ledger_rows": [2000, 2000], "documents": [200, 200]}),
    "near_max_heavy": dict(HEAVY, size={"ledger_rows": [1990, 2000], "documents": [190, 200]}),
    "mid_narrow_heavy": dict(HEAVY, size={"ledger_rows": [1000, 1000], "documents": [100, 100]}),
    "min_heavy": dict(HEAVY, size={"ledger_rows": [200, 200], "documents": [20, 20]}),
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_size_within_case_bounds(name, tmp_path):
    cfg = CASES[name]
    r_hi, d_hi = cfg["size"]["ledger_rows"][1], cfg["size"]["documents"][1]
    for seed in range(12):
        ep = generate_episode(seed, cfg, tmp_path / str(seed))
        counts = json.loads((ep / "public" / "manifest.json").read_text(encoding="utf-8"))["counts"]
        rows, docs = counts["ledger_rows"], counts["documents"]
        where = (name, seed, rows, docs)
        assert LEDGER_BOUNDS[0] <= rows <= LEDGER_BOUNDS[1], where
        assert DOCUMENT_BOUNDS[0] <= docs <= DOCUMENT_BOUNDS[1], where
        if r_hi > LEDGER_BOUNDS[0] + 100:  # у минимального конфига ошибкам негде поместиться ниже 200
            assert rows <= r_hi and docs <= d_hi, where
