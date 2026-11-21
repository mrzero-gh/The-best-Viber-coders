"""Любой seed и любая компания (жюри скрывает seed, валюты, часовой пояс и размеры): заказанные
валютные ошибки вносятся и проходят сертификат даже в неудобных валютах."""
import json

import pytest

from generator.build import generate_episode
from grader.certificate import certify_episode

OFF = {"enabled": False}
ONE = {"enabled": True, "count": [1, 1]}


def _cfg(company, errors=None, interactions=None):
    return {"company": company, "clean": False,
            "errors": {t: OFF for t in ("duplicate_invoice", "payment_allocation", "fx_conversion",
                                        "period_cutoff")} | (errors or {}),
            "interactions": {s: OFF for s in ("partial_fx_payment_at_cutoff",
                                              "duplicate_invoice_wrong_fx")} | (interactions or {}),
            "distractors": {d: OFF for d in ("split_payment", "refund", "legit_recurring",
                                             "late_evening_local")}}


def _check(ep, planned_key, name):
    gen = json.loads((ep / "private" / "generation.json").read_text(encoding="utf-8"))
    injected_key = planned_key.replace("_planned", "_injected")
    assert gen[injected_key][name] == gen[planned_key][name] == 1
    report = certify_episode(ep)
    assert report["valid"], report
    return json.loads((ep / "private" / "truth.json").read_text(encoding="utf-8"))


# Случаи из свипа: saas без валютных контрагентов не давал fx_conversion (seed 5012, 5021, 5031)
@pytest.mark.parametrize("seed,company", [
    (5012, {"archetype": "saas", "base_currency": "KGS", "timezone": "-08:45",
            "foreign_currencies": ["TRY", "CNY", "RUB", "EUR", "JPY", "UZS"]}),
    (5021, {"archetype": "distributor", "base_currency": "USD", "timezone": "+06:00",
            "foreign_currencies": ["TRY", "GBP", "KZT", "JPY"]}),
    (5031, {"archetype": "saas", "base_currency": "EUR", "timezone": "+14:00",
            "foreign_currencies": ["UZS", "USD", "KZT"]}),
])
def test_fx_conversion_without_foreign_counterparty(tmp_path, seed, company):
    ep = generate_episode(seed, _cfg(company, errors={"fx_conversion": ONE}), tmp_path)
    truth = _check(ep, "errors_planned", "fx_conversion")
    assert [i["type"] for i in truth["issues"]] == ["fx_conversion"]


# База дороже всех иностранных валют: частичную оплату не восстановить, сценарий 1 вносится полным пропуском
@pytest.mark.parametrize("seed", [5014, 1, 2])
def test_partial_fx_scenario_with_cheap_foreign(tmp_path, seed):
    company = {"archetype": "importer", "base_currency": "GBP", "timezone": "+00:00",
               "foreign_currencies": ["JPY", "TRY"]}
    ep = generate_episode(seed, _cfg(company, interactions={"partial_fx_payment_at_cutoff": ONE}), tmp_path)
    truth = _check(ep, "interactions_planned", "partial_fx_payment_at_cutoff")
    alloc = [i for i in truth["issues"] if i["type"] == "payment_allocation"]
    assert len(alloc) == 1 and alloc[0]["scenario"] == "partial_fx_payment_at_cutoff"


# База GBP, единственная валюта EUR и 20-30 документов: все платежи в EUR, 1 цент дешевле 1 пенни (seed 8003)
@pytest.mark.parametrize("variant", ["missing", "partial", "any"])
def test_payment_allocation_when_payments_are_cheap(tmp_path, variant):
    company = {"archetype": "importer", "base_currency": "GBP", "timezone": "+10:00", "foreign_currencies": ["EUR"]}
    cfg = _cfg(company, errors={"payment_allocation": dict(ONE, variant=variant)})
    cfg["size"] = {"ledger_rows": [200, 260], "documents": [20, 30]}
    ep = generate_episode(8003, cfg, tmp_path)
    truth = _check(ep, "errors_planned", "payment_allocation")
    assert [i["type"] for i in truth["issues"]] == ["payment_allocation"]


# База USD, единственная валюта KGS: малый счёт не давал расхождения курса для дубля с неверным курсом (seed 9080)
def test_duplicate_wrong_fx_with_cheap_currency(tmp_path):
    company = {"archetype": "saas", "base_currency": "USD", "timezone": "+06:45", "foreign_currencies": ["KGS"]}
    two = {"enabled": True, "count": [2, 2]}
    ep = generate_episode(9080, _cfg(company, interactions={"duplicate_invoice_wrong_fx": two}), tmp_path)
    gen = json.loads((ep / "private" / "generation.json").read_text(encoding="utf-8"))
    assert gen["interactions_injected"]["duplicate_invoice_wrong_fx"] == 2
    assert certify_episode(ep)["valid"]


# Кейс: 20-200 документов и 0-12 ошибок. В малом эпизоде отвлекающие случаи занимали документы
# раньше ошибок, и 12 ошибок на 20-30 документах теряли до трети; теперь ошибки важнее
def test_small_episode_prefers_errors_over_distractors(tmp_path):
    from generator.distractors import cap_distractors
    full = {"split_payment": 2, "refund": 2, "legit_recurring": 2, "late_evening_local": 2}
    assert cap_distractors(full, 40, 8) == full  # настройки по умолчанию не урезаются
    assert sum(cap_distractors(full, 20, 12).values()) == 1
    lost = 0
    for seed in range(6):
        cfg = {"size": {"ledger_rows": [200, 2000], "documents": [20, 30]},
               "errors": {t: {"enabled": True, "count": [3, 3]} for t in
                          ("duplicate_invoice", "payment_allocation", "fx_conversion", "period_cutoff")}}
        ep = generate_episode(seed, cfg, tmp_path / str(seed))
        gen = json.loads((ep / "private" / "generation.json").read_text(encoding="utf-8"))
        lost += sum(gen["errors_planned"].values()) - sum(gen["errors_injected"].values())
        assert sum(gen["distractors_planned"].values()) <= 1
    assert lost <= 1
