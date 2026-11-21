"""Массовый перебор конфигов генератора: python -m scripts.fuzz_generator [--n 300] [--seed 0].

Для каждого случайного конфига (как «новые сочетания параметров» у жюри):
1. judge_adapter.generate_episode; ConfigError - допустимый отказ, любое другое исключение - баг;
2. независимый сертификат с проверкой чистой истории (grader.certificate + grader.clean_history);
3. размеры: число строк в manifest совпадает с файлами, ledger и documents в пределах конфига;
4. oracle через judge_adapter.score_episode: F1 = 1, return = 1;
5. копия с перестановкой строк и сменой ID: сертификат и оценка oracle те же.
Не входит в CI (долго); итог печатается таблицей, проблемы - списком.
"""
import argparse
import json
import random
import shutil
import sys
import tempfile
import time
import traceback
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import judge_adapter as ja  # noqa: E402
from generator.config import ARCHETYPES, DISTRACTORS, ERROR_TYPES, INTERACTIONS, ConfigError  # noqa: E402
from grader.certificate import certify  # noqa: E402
from grader.evidence import read_public  # noqa: E402
from scripts.transform_episode import invert, remap_value, transform_episode  # noqa: E402

KNOWN = ["KZT", "USD", "EUR", "CNY", "RUB", "JPY", "GBP", "TRY", "KGS", "UZS"]
ODD = ["KRW", "KWD", "BHD", "XAU", "AAA"]  # нет в таблице курсов или необычная разрядность


def _tz(rng):
    h = rng.randint(-12, 14)
    m = rng.choice([0, 0, 0, 30, 45])
    if h == 14 or h == -12:
        m = 0
    return f"{'+' if h >= 0 else '-'}{abs(h):02d}:{m:02d}"


def _range(rng, lo, hi):
    a, b = sorted(rng.randint(lo, hi) for _ in range(2))
    return [a, b]


def _toggle(rng, hi=3):
    return {"enabled": rng.random() < 0.7, "count": _range(rng, 0, hi)}


def random_config(rng) -> dict:
    base = rng.choice(KNOWN if rng.random() < 0.9 else ODD)
    pool = KNOWN + (ODD if rng.random() < 0.1 else [])
    foreign = rng.sample(pool, rng.randint(0, 6))
    cfg = {"config_version": "fuzz-v1",
           "company": {"archetype": rng.choice(ARCHETYPES), "base_currency": base,
                       "timezone": _tz(rng), "foreign_currencies": foreign},
           "size": {"ledger_rows": _range(rng, 200, 2000), "documents": _range(rng, 20, 200)},
           "errors": {t: _toggle(rng) for t in ERROR_TYPES},
           "interactions": {s: _toggle(rng, 2) for s in INTERACTIONS},
           "distractors": {k: _toggle(rng) for k in DISTRACTORS},
           "clean": rng.random() < 0.15,
           "max_errors": rng.randint(0, 12)}
    # иногда - короткий конфиг (часть полей по умолчанию)
    for key in ("size", "interactions", "distractors", "max_errors", "company"):
        if rng.random() < 0.1:
            cfg.pop(key)
    return cfg


def _truth(ep):
    return json.loads((Path(ep) / "private" / "truth.json").read_text(encoding="utf-8"))


def _prov(ep):
    return json.loads((Path(ep) / "private" / "provenance.json").read_text(encoding="utf-8"))


def _oracle(ep):
    t = _truth(ep)
    return {"episode_id": t["episode_id"], "findings": [
        {"type": i["type"], "affected_ids": sorted(i["affected_ids"]),
         "claimed_amount_minor": i["expected_amount_minor"], "currency": i["currency"],
         "evidence_ids": sorted(i["evidence_sets"][0]), "explanation": "oracle"} for i in t["issues"]]}


def _canon(f):
    return sorted(json.dumps(dict(x, affected_ids=sorted(x["affected_ids"]),
                                  evidence_ids=sorted(x["evidence_ids"])), sort_keys=True) for x in f["findings"])


def check_one(case: tuple) -> dict:
    idx, seed, cfg = case
    row = {"idx": idx, "seed": seed, "cfg": cfg, "problems": [], "status": "ok"}
    bad = row["problems"].append
    tmp = Path(tempfile.mkdtemp(prefix="fuzzgen-"))
    try:
        t0 = time.perf_counter()
        try:
            ja.generate_episode(seed, cfg, str(tmp / "src"))
        except ConfigError as exc:
            row.update(status="rejected", reason=str(exc))
            return row
        row["gen_s"] = round(time.perf_counter() - t0, 3)
        src = tmp / "src"
        public, truth, prov = read_public(src / "public"), _truth(src), _prov(src)
        gen = json.loads((src / "private" / "generation.json").read_text(encoding="utf-8"))
        man = public["manifest"]
        # размеры
        c = man["counts"]
        if c["ledger_rows"] != len(public["ledger"]) or c["invoices"] != len(public["invoices"]) \
                or c["payments"] != len(public["payments"]) or c["fx_rates"] != len(public["fx"]):
            bad(f"manifest.counts не совпадает с файлами: {c}")
        size = cfg.get("size", {})
        lo, hi = size.get("ledger_rows", [300, 1200])
        if not lo <= c["ledger_rows"] <= hi:
            bad(f"ledger_rows {c['ledger_rows']} вне [{lo}, {hi}]")
        dlo, dhi = size.get("documents", [40, 120])
        if not dlo <= c["documents"] <= dhi:
            bad(f"documents {c['documents']} вне [{dlo}, {dhi}]")
        if cfg.get("clean") and truth["issues"]:
            bad("clean=true, но в truth есть ошибки")
        if len(truth["issues"]) > cfg.get("max_errors", 12):
            bad(f"ошибок {len(truth['issues'])} > max_errors")
        row["issues"] = len(truth["issues"])
        row["planned"] = sum(gen["errors_planned"].values()) + 2 * sum(gen["interactions_planned"].values())
        short = {t: gen["errors_planned"][t] - gen["errors_injected"][t] for t in gen["errors_planned"]}
        short.update({s: gen["interactions_planned"][s] - gen["interactions_injected"][s]
                      for s in gen["interactions_planned"]})
        short.update({d: gen["distractors_planned"][d] - gen["distractors_injected"][d]
                      for d in gen["distractors_planned"]})
        row["short"] = {k: v for k, v in short.items() if v}
        # сертификат + чистая история
        cert = certify(public, truth, prov)
        if not cert["valid"]:
            bad("сертификат: " + json.dumps({k: cert.get(k) for k in ("public_errors", "summary")},
                                            ensure_ascii=False)[:300])
        if cert["clean_history_balance"]["status"] != "ok":
            bad("чистая история: " + json.dumps(cert["clean_history_balance"], ensure_ascii=False)[:300])
        # oracle
        fin = _oracle(src)
        s = ja.score_episode(str(src), fin, 0)
        if s["f1"] != 1.0 or s["return"] != 1.0:
            bad(f"oracle: {s}")
        # копия как у жюри
        var = tmp / "var"
        mapping = transform_episode(src, var, seed=idx, permute=True, remap=True)
        if not certify(read_public(var / "public"), _truth(var), _prov(var))["valid"]:
            bad("сертификат копии не прошёл")
        fin_v = _oracle(var)
        if _canon(remap_value(fin_v, invert(mapping))) != _canon(fin):
            bad("oracle копии после обратного отображения ID отличается")
        sv = ja.score_episode(str(var), fin_v, 0)
        if sv != s:
            bad(f"оценка копии {sv} != {s}")
    except Exception as exc:  # noqa: BLE001 - любая авария генератора/оценщика - находка
        row.update(status="crash", error=f"{type(exc).__name__}: {exc}",
                   trace=traceback.format_exc()[-1500:])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if row["status"] == "ok" and row["problems"]:
        row["status"] = "problem"
    return row


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n", type=int, default=300)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--out", type=Path, help="JSON со всеми прогонами (проблемные помечены status)")
    a = ap.parse_args(argv)
    rng = random.Random(a.seed)
    cases = [(i, rng.randint(0, 10**6), random_config(rng)) for i in range(a.n)]
    t0 = time.monotonic()
    with ProcessPoolExecutor(max_workers=a.workers) as ex:
        rows = list(ex.map(check_one, cases))
    status = Counter(r["status"] for r in rows)
    print(f"конфигов {a.n} за {time.monotonic() - t0:.0f} с: {dict(status)}")
    gen = [r["gen_s"] for r in rows if "gen_s" in r]
    if gen:
        print(f"генерация: медиана {sorted(gen)[len(gen) // 2]:.2f} с, максимум {max(gen):.2f} с")
    short = Counter()
    for r in rows:
        short.update({k: 1 for k in r.get("short", {})})
    print("внесено меньше запланированного (эпизодов по типу):", dict(short) or "нет")
    reasons = Counter(r["reason"].split(":")[0] for r in rows if r["status"] == "rejected")
    if reasons:
        print("отказы ConfigError:", dict(reasons))
    found = [r for r in rows if r["status"] in ("crash", "problem")]
    kinds = Counter((r.get("error") or r["problems"][0])[:90] for r in found)
    for k, n in kinds.most_common():
        print(f"  {n:4d} x {k}")
    if a.out:  # все строки: проблемные помечены status
        a.out.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
