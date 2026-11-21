"""Свип узких диапазонов размера: python -m scripts.size_sweep [--n 300] [--seed 77].

Случайные конфиги (как в scripts.fuzz_generator), но диапазон ledger_rows шириной 0-18 строк и
documents шириной 0-30, совместимые между собой. Печатает, сколько эпизодов попало в диапазон,
наибольший промах по строкам и самое долгое время генерации. Замеры - docs/design.md, раздел 6.5.
"""
import argparse
import json
import random
import tempfile
import time
from pathlib import Path

from generator.build import generate_episode
from scripts.fuzz_generator import random_config

DEFAULT_COMPANY = {"archetype": "importer", "base_currency": "KZT", "timezone": "+05:00",
                   "foreign_currencies": ["USD", "EUR"]}


def narrow_config(rng: random.Random) -> dict:
    cfg = random_config(rng)
    cfg.setdefault("company", dict(DEFAULT_COMPANY))
    width = rng.choice([0, 1, 2, 3, 5, 8, 12, 18])
    lo = rng.randint(200, 2000 - width)
    d_lo = rng.randint(20, min(200, max(20, lo // 3)))  # документы помещаются в строки
    cfg["size"] = {"ledger_rows": [lo, lo + width],
                   "documents": [d_lo, min(200, d_lo + rng.choice([0, 2, 5, 10, 30]))]}
    cfg["max_errors"] = 12
    return cfg


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=300)
    parser.add_argument("--seed", type=int, default=77)
    args = parser.parse_args(argv)
    rng = random.Random(args.seed)
    stats = {"n": args.n, "rows_in": 0, "docs_in": 0, "both_in": 0, "max_rows_miss": 0, "max_s": 0.0}
    misses = []
    with tempfile.TemporaryDirectory() as tmp:
        for i in range(args.n):
            cfg = narrow_config(rng)
            started = time.perf_counter()
            ep = generate_episode(rng.randint(0, 10**6), cfg, Path(tmp) / str(i))
            stats["max_s"] = max(stats["max_s"], round(time.perf_counter() - started, 2))
            counts = json.loads((ep / "public" / "manifest.json").read_text(encoding="utf-8"))["counts"]
            (lo, hi), (d_lo, d_hi) = cfg["size"]["ledger_rows"], cfg["size"]["documents"]
            rows_in = lo <= counts["ledger_rows"] <= hi
            docs_in = d_lo <= counts["documents"] <= d_hi
            stats["rows_in"] += rows_in
            stats["docs_in"] += docs_in
            stats["both_in"] += rows_in and docs_in
            if not rows_in:
                miss = min(abs(counts["ledger_rows"] - lo), abs(counts["ledger_rows"] - hi))
                stats["max_rows_miss"] = max(stats["max_rows_miss"], miss)
                misses.append(f"  ширина {hi - lo}: {counts['ledger_rows']} вне [{lo}, {hi}]")
    print(f"узких конфигов {stats['n']}: строки в диапазоне {stats['rows_in']}, документы {stats['docs_in']}, "
          f"оба {stats['both_in']}; наибольший промах {stats['max_rows_miss']} стр.; "
          f"генерация не дольше {stats['max_s']} с")
    print("\n".join(misses))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
