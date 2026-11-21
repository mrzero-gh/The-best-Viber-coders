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


# База KWD, валюта UZS: оплаты в базовой валюте заняты отвлекающими случаями и period_cutoff, остальные
# платежи в дешёвой UZS. payment_allocation вносится полным пропуском (фазз, seed 583444)
def test_payment_allocation_missing_fallback_in_cheap_currency(tmp_path):
    cfg = {"company": {"archetype": "importer", "base_currency": "KWD", "timezone": "-04:30",
                       "foreign_currencies": ["UZS"]},
           "size": {"ledger_rows": [1379, 1899], "documents": [27, 69]},
           "errors": {"duplicate_invoice": OFF, "fx_conversion": OFF,
                      "payment_allocation": ONE, "period_cutoff": {"enabled": True, "count": [3, 3]}},
           "distractors": {"split_payment": {"enabled": True, "count": [2, 2]},
                           "refund": {"enabled": True, "count": [2, 2]},
                           "legit_recurring": ONE, "late_evening_local": ONE}}
    ep = generate_episode(583444, cfg, tmp_path)
    truth = _check(ep, "errors_planned", "payment_allocation")
    alloc = [i for i in truth["issues"] if i["type"] == "payment_allocation"]
    assert len(alloc) == 1 and alloc[0]["currency"] == "UZS"


# Скрытые валюты жюри: разрядность минимальной единицы по ISO 4217, а не всегда 2
@pytest.mark.parametrize("code,exp", [("VND", 0), ("CLP", 0), ("ISK", 0), ("OMR", 3), ("TND", 3), ("CLF", 4),
                                      ("INR", 2), ("KZT", 2)])
def test_iso_4217_exponents_in_manifest(tmp_path, code, exp):
    other = "USD" if code != "USD" else "EUR"
    ep = generate_episode(1, {"company": {"archetype": "importer", "base_currency": other, "timezone": "+05:30",
                                          "foreign_currencies": [code]}}, tmp_path)
    manifest = json.loads((ep / "public" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["currency_exponents"][code] == exp
    assert certify_episode(ep)["valid"]


# База KRW или CLP (без копеек): вымышленный курс 1-500 KZT за единицу делал валютный счёт копеечным
# в пересчёте, и дубль с неверным курсом не вносился (свип на экзотических валютах)
@pytest.mark.parametrize("seed,count,company", [
    (170030, 1, {"archetype": "distributor", "base_currency": "KRW", "timezone": "-09:00",
                 "foreign_currencies": ["ISK", "CLP", "AED", "INR"]}),
    (170047, 2, {"archetype": "distributor", "base_currency": "KRW", "timezone": "-03:00",
                 "foreign_currencies": ["AED", "VND", "ISK", "KWD"]}),
    (170056, 2, {"archetype": "saas", "base_currency": "CLP", "timezone": "+14:00",
                 "foreign_currencies": ["UGX", "KRW", "VND"]}),
])
def test_duplicate_wrong_fx_with_zero_decimal_base(tmp_path, seed, count, company):
    planned = {"enabled": True, "count": [count, count]}
    ep = generate_episode(seed, _cfg(company, interactions={"duplicate_invoice_wrong_fx": planned}), tmp_path)
    gen = json.loads((ep / "private" / "generation.json").read_text(encoding="utf-8"))
    assert gen["interactions_injected"]["duplicate_invoice_wrong_fx"] == count
    assert certify_episode(ep)["valid"]


# D02: база EUR, дешёвые CLP/ISK/VND - суммы частей разбитого платежа после пересчёта не сходились
# с суммой счёта, и отвлекающий случай не вносился; теперь берётся другой платёж (seed 190038)
def test_split_payment_tries_other_payments(tmp_path):
    company = {"archetype": "importer", "base_currency": "EUR", "timezone": "-04:30",
               "foreign_currencies": ["USD", "CLP", "ISK", "OMR", "VND"]}
    cfg = _cfg(company)
    cfg["clean"] = True
    cfg["distractors"]["split_payment"] = ONE
    ep = generate_episode(190038, cfg, tmp_path)
    gen = json.loads((ep / "private" / "generation.json").read_text(encoding="utf-8"))
    assert gen["distractors_injected"]["split_payment"] == 1
    assert certify_episode(ep)["valid"]


# saas c 4 валютными счетами: сценарий 1 и запас под него забирали все валютные счета, и fx_conversion
# не вносился; резерв валютных счетов теперь учитывает и совмещённые сценарии (фазз, seed 61578)
def test_fx_reserve_counts_interactions(tmp_path):
    cfg = {"config_version": "fuzz-v1",
           "company": {"archetype": "saas", "base_currency": "GBP", "timezone": "+06:00",
                       "foreign_currencies": ["EUR", "KGS", "JPY", "RUB", "UZS"]},
           "size": {"ledger_rows": [218, 1548], "documents": [42, 134]},
           "errors": {"duplicate_invoice": {"enabled": False, "count": [0, 3]},
                      "payment_allocation": {"enabled": True, "count": [1, 1]},
                      "fx_conversion": {"enabled": True, "count": [1, 2]},
                      "period_cutoff": {"enabled": False, "count": [3, 3]}},
           "interactions": {"partial_fx_payment_at_cutoff": {"enabled": True, "count": [1, 2]},
                            "duplicate_invoice_wrong_fx": {"enabled": False, "count": [1, 2]}},
           "distractors": {"split_payment": {"enabled": False, "count": [0, 2]},
                           "refund": {"enabled": True, "count": [0, 2]},
                           "legit_recurring": {"enabled": True, "count": [1, 2]},
                           "late_evening_local": {"enabled": False, "count": [1, 2]}},
           "clean": False, "max_errors": 12}
    ep = generate_episode(61578, cfg, tmp_path)
    gen = json.loads((ep / "private" / "generation.json").read_text(encoding="utf-8"))
    assert gen["errors_injected"]["fx_conversion"] == gen["errors_planned"]["fx_conversion"] == 2
    assert certify_episode(ep)["valid"]


# Мало валютных счетов (26-40 документов): отвлекающие случаи и сценарий 1 забирали валютные счета,
# нужные fx_conversion и дублю с неверным курсом. Конфиги из фазза генератора (seed 339563, 746808)
@pytest.mark.parametrize("seed", ["339563", "746808"])
def test_errors_keep_scarce_foreign_invoices(tmp_path, seed):
    from pathlib import Path
    cfgs = json.loads((Path(__file__).parent / "fixtures" / "fuzz_foreign_scarce.json").read_text(encoding="utf-8"))
    ep = generate_episode(int(seed), cfgs[seed], tmp_path)
    gen = json.loads((ep / "private" / "generation.json").read_text(encoding="utf-8"))
    for kind in ("errors", "interactions"):
        for name, n in gen[f"{kind}_planned"].items():
            assert gen[f"{kind}_injected"][name] >= n, (kind, name)
    assert certify_episode(ep)["valid"]
