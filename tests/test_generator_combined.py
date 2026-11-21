"""Генератор, этап 5: совмещённые сценарии (задача #19) и сверка с сертификатом оценщика."""
import json
from collections import Counter
from pathlib import Path

import pytest

from generator.build import generate_episode
from tests.test_generator_clean import load
from tests import test_generator_errors as checks  # модулем: иначе pytest соберёт проверки повторно

OFF = {"enabled": False}
ON2 = {"enabled": True, "count": [2, 2]}
ERRORS = ("duplicate_invoice", "payment_allocation", "fx_conversion", "period_cutoff")
COMBINED = {"config_version": "t-comb", "mode": "train",
            "errors": {t: OFF for t in ERRORS},
            "interactions": {"partial_fx_payment_at_cutoff": ON2, "duplicate_invoice_wrong_fx": ON2}}
MIXED = dict(COMBINED, errors={t: {"enabled": True, "count": [1, 2]} for t in ERRORS})


@pytest.fixture(scope="module", params=list(range(400, 410)))
def ep(request, tmp_path_factory):
    return load(generate_episode(request.param, COMBINED, tmp_path_factory.mktemp("c")))


def _by_scenario(d, name):
    return [i for i in d["truth"]["issues"] if i["scenario"] == name]


def test_both_scenarios_give_two_issues_each(ep):
    assert ep["generation"]["interactions_injected"] == {"partial_fx_payment_at_cutoff": 2,
                                                         "duplicate_invoice_wrong_fx": 2}
    assert len(ep["truth"]["issues"]) == 8
    keys = [(i["type"], tuple(i["affected_ids"])) for i in ep["truth"]["issues"]]
    assert len(keys) == len(set(keys)), "каждая ошибка сценария — отдельный issue"


def test_partial_fx_payment_at_cutoff_pairs(ep):
    issues = _by_scenario(ep, "partial_fx_payment_at_cutoff")
    assert Counter(i["type"] for i in issues) == {"period_cutoff": 2, "payment_allocation": 2}
    base = ep["manifest"]["base_currency"]
    for cut in (i for i in issues if i["type"] == "period_cutoff"):
        (pid,) = cut["affected_ids"]
        assert pid.startswith("PAY-") and cut["currency"] != base
        alloc = [i for i in issues if i["type"] == "payment_allocation" and pid in i["affected_ids"]]
        assert len(alloc) == 1, "к каждому платежу вне периода — своя ошибка распределения"


def test_duplicate_invoice_wrong_fx_pairs(ep):
    issues = _by_scenario(ep, "duplicate_invoice_wrong_fx")
    assert Counter(i["type"] for i in issues) == {"duplicate_invoice": 2, "fx_conversion": 2}
    for fx in (i for i in issues if i["type"] == "fx_conversion"):
        (dup,) = fx["affected_ids"]
        assert any(dup in i["affected_ids"] for i in issues if i["type"] == "duplicate_invoice")


def test_each_issue_still_provable(ep):
    for name in ("test_books_still_balanced", "test_evidence_is_public_and_small",
                 "test_duplicate_is_provable_from_public", "test_allocation_is_provable_from_public",
                 "test_fx_is_provable_from_public", "test_cutoff_is_provable_from_public"):
        getattr(checks, name)(ep)


def test_max_errors_counts_scenarios_as_two(tmp_path):
    d = load(generate_episode(7, dict(MIXED, max_errors=5), tmp_path / "m"))
    assert len(d["truth"]["issues"]) <= 5
    d = load(generate_episode(7, dict(COMBINED, max_errors=3), tmp_path / "n"))
    assert len(d["truth"]["issues"]) == 2  # один сценарий из двух ошибок


def test_clean_disables_scenarios(tmp_path):
    d = load(generate_episode(8, dict(COMBINED, clean=True), tmp_path / "c"))
    assert d["truth"]["issues"] == []


# ---------- сверка с независимым проверщиком и оценщиком (grader/, роль C) ----------

CERT_CONFIGS = [
    MIXED,
    dict(MIXED, company={"archetype": "saas"}),
    dict(MIXED, company={"archetype": "distributor"}, size={"ledger_rows": [200, 240], "documents": [20, 25]}),
    dict(MIXED, size={"ledger_rows": [1900, 2000], "documents": [180, 200]}),
    dict(MIXED, clean=True),
    dict(MIXED, company={"archetype": "saas", "foreign_currencies": []}),
]


@pytest.mark.parametrize("seed", range(500, 512))
def test_certificate_and_oracle(seed, tmp_path):
    certificate = pytest.importorskip("grader.certificate")
    from grader.evaluate import evaluate
    ep = generate_episode(seed, CERT_CONFIGS[seed % len(CERT_CONFIGS)], tmp_path)
    cert = certificate.certify_episode(ep)
    assert cert["valid"], [x for x in cert["issues"] + cert["distractors"] if not x.get("valid")][:2]
    truth = json.loads((Path(ep) / "private" / "truth.json").read_text(encoding="utf-8"))
    findings = {"episode_id": truth["episode_id"], "findings": [
        {"type": i["type"], "affected_ids": i["affected_ids"], "claimed_amount_minor": i["expected_amount_minor"],
         "currency": i["currency"], "evidence_ids": i["evidence_sets"][0], "explanation": "oracle"}
        for i in truth["issues"]]}
    result = evaluate(findings, truth, truth["episode_id"], 0)
    assert result["valid"] and result["metrics"]["f1"] == 1.0 and result["reward"] == 1.0


# Фазз генератора (scripts/fuzz_generator.py, 21.11): у saas всего 20% валютных контрагентов, и
# в части сидов не было ни валютного контрагента, ни оплаченного валютного счёта - совмещённые
# сценарии и fx_conversion вносились меньше плана. Сиды ниже до исправления недовносили все три.
@pytest.mark.parametrize("seed,archetype", [(1, "saas"), (13, "saas"), (45, "saas"), (69, "distributor")])
def test_foreign_scenarios_injected_for_low_foreign_archetypes(seed, archetype, tmp_path):
    cfg = {"company": {"archetype": archetype},
           "errors": {t: {"enabled": True, "count": [1, 2]} for t in ERRORS},
           "interactions": {s: {"enabled": True, "count": [1, 1]}
                            for s in ("partial_fx_payment_at_cutoff", "duplicate_invoice_wrong_fx")}}
    g = load(generate_episode(seed, cfg, tmp_path))["generation"]
    assert g["interactions_injected"] == g["interactions_planned"]
    assert all(g["errors_injected"][t] >= g["errors_planned"][t] for t in ERRORS), g


def test_recoverable_foreign_entity_reserved(tmp_path):
    """База CNY, валюты KZT/UZS/KGS дешевле базы (частичную оплату не восстановить), EUR дороже:
    контрагент в EUR должен появиться, иначе partial_fx_payment_at_cutoff не внести (фазз, idx 251)."""
    cfg = {"company": {"archetype": "saas", "base_currency": "CNY",
                       "foreign_currencies": ["KZT", "UZS", "KGS", "EUR"]},
           "errors": {t: OFF for t in ERRORS},
           "interactions": {"partial_fx_payment_at_cutoff": {"enabled": True, "count": [1, 1]},
                            "duplicate_invoice_wrong_fx": OFF}}
    for seed in range(6):
        g = load(generate_episode(seed, cfg, tmp_path / str(seed)))["generation"]
        assert g["interactions_injected"]["partial_fx_payment_at_cutoff"] == 1, seed
