"""Находки ревью генератора, политик и скриптов проверки (20.11).

1. seed 5 и 5.0 дают один эпизод, bool и строка отклоняются;
2. pair_check --only принимает только P01-P20;
3. scripts.jury: --seeds 0 - ошибка аргумента, воспроизводимость проверяется прогоном;
4. SmartAuditor: усечение до 30 выводов не выкидывает тип целиком; без курса не гадает;
5. для валюты вне встроенного списка цены счетов согласованы с курсами.
"""
import csv
import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from common.jsonio import read_jsonl
from generator.build import generate_episode
from policies.rule_based import MAX_FINDINGS, RuleBasedPolicy, _limit_findings


def _sha(ep):
    return json.loads((Path(ep) / "private" / "generation.json").read_text(encoding="utf-8"))["episode_sha256"]


# ---------------------------------------------------------------- seed

def test_integral_float_seed_gives_same_episode(tmp_path):
    a = generate_episode(5, {}, tmp_path / "a")
    b = generate_episode(5.0, {}, tmp_path / "b")
    c = generate_episode(5, {}, tmp_path / "c", id_seed=7.0)
    d = generate_episode(5, {}, tmp_path / "d", id_seed=7)
    assert _sha(a) == _sha(b)
    assert _sha(c) == _sha(d)


@pytest.mark.parametrize("seed", [True, "5", 5.5, None])
def test_non_integer_seed_is_rejected(tmp_path, seed):
    with pytest.raises(ValueError):
        generate_episode(seed, {}, tmp_path)


# ---------------------------------------------------------------- pair_check --only

def test_pair_check_only_parses_valid_pairs():
    import argparse
    from scripts.pair_check import _parse_only
    assert _parse_only("P01, p6,P20", argparse.ArgumentParser()) == [1, 6, 20]


@pytest.mark.parametrize("bad", ["P0", "P21", "Px", "P-1", ""])
def test_pair_check_only_rejects_out_of_range(bad):
    import argparse
    from scripts.pair_check import _parse_only
    with pytest.raises(SystemExit):
        _parse_only(bad, argparse.ArgumentParser())


# ---------------------------------------------------------------- jury

def test_jury_rejects_zero_seeds(monkeypatch):
    from scripts import jury
    monkeypatch.setattr(sys, "argv", ["scripts.jury", "--seeds", "0"])
    with pytest.raises(SystemExit) as e:
        jury.main()
    assert e.value.code == 2


def test_jury_reproducibility_is_measured(tmp_path):
    from scripts import jury
    ok, note = jury._check_reproducibility(tmp_path, "grader.evaluate:evaluate")
    assert ok, note


def test_jury_has_no_hardcoded_repro_score():
    src = Path("scripts/jury.py").read_text(encoding="utf-8")
    assert "pts_repro = 15\n" not in src


# ---------------------------------------------------------------- SmartAuditor

def _f(kind, i):
    return {"type": kind, "affected_ids": [f"X-{i}"]}


def test_limit_keeps_short_list_unchanged():
    items = [_f("period_cutoff", i) for i in range(MAX_FINDINGS)]
    assert _limit_findings(items) is items


def test_limit_does_not_drop_last_type():
    items = [_f("period_cutoff", i) for i in range(35)] + [_f("fx_conversion", i) for i in range(5)]
    out = _limit_findings(items)
    assert len(out) == MAX_FINDINGS
    assert sum(f["type"] == "fx_conversion" for f in out) == 5


def _load_into_policy(ep):
    pub = Path(ep) / "public"
    pol = RuleBasedPolicy(naive=False)
    pol.manifest = json.loads((pub / "manifest.json").read_text(encoding="utf-8"))
    pol.episode_id = pol.manifest["episode_id"]
    pol.invoices = read_jsonl(pub / "invoices.jsonl")
    pol.payments = read_jsonl(pub / "payments.jsonl")
    with open(pub / "ledger.csv", encoding="utf-8", newline="") as f:
        pol.ledger = [dict(r, debit_minor=int(r["debit_minor"]), credit_minor=int(r["credit_minor"]),
                           invoice_id=r["invoice_id"] or None, payment_id=r["payment_id"] or None)
                      for r in csv.DictReader(f)]
    with open(pub / "fx.csv", encoding="utf-8", newline="") as f:
        pol.fx = {f"FX:{r['date']}:{r['source_currency']}:{r['target_currency']}": r for r in csv.DictReader(f)}
    return pol


def test_missing_fx_does_not_produce_guessed_allocation(tmp_path):
    cfg = {"errors": {"duplicate_invoice": {"enabled": False}, "fx_conversion": {"enabled": False},
                      "period_cutoff": {"enabled": False},
                      "payment_allocation": {"enabled": True, "count": [3, 3]}},
           "distractors": {k: {"enabled": False} for k in
                           ("split_payment", "refund", "legit_recurring", "late_evening_local")}}
    ep = generate_episode(21, cfg, tmp_path)
    pol = _load_into_policy(ep)
    base = pol.manifest["base_currency"]
    full = [f for f in pol._audit() if f["type"] == "payment_allocation"]
    assert full  # с курсами ошибки находятся
    pol.fx = {}
    blind = [f for f in pol._audit() if f["type"] == "payment_allocation"]
    # без курсов остаются только платежи в базовой валюте, и их суммы те же
    assert all(f["currency"] == base for f in blind)
    assert blind == [f for f in full if f["currency"] == base]


# ---------------------------------------------------------------- цены и курсы

def test_unknown_currency_prices_match_rates(tmp_path):
    cfg = {"company": {"foreign_currencies": ["AED"]}}
    for seed in range(3):
        ep = generate_episode(seed, cfg, tmp_path / str(seed))
        pub = ep / "public"
        with open(pub / "fx.csv", encoding="utf-8", newline="") as f:
            rates = {r["date"]: Decimal(r["rate"]) for r in csv.DictReader(f) if r["source_currency"] == "AED"}
        for inv in read_jsonl(pub / "invoices.jsonl"):
            if inv["currency"] != "AED":
                continue
            rate = rates[inv["issued_at"][:10]]
            for li in inv["line_items"]:
                unit_kzt = Decimal(li["unit_price_minor"]) / 100 * rate
                # цена задумана 2 000-300 000 KZT; дрейф курса за период - проценты, не разы
                assert Decimal(1000) <= unit_kzt <= Decimal(600000), (seed, unit_kzt)
