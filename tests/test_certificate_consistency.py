"""Сертификат ловит неверную чистую историю и неполный эталон (условия 1–3 блока D жюри).

Четыре повреждённых входа из внешнего аудита 22.11: прежняя проверка (хэш + баланс) их принимала.
Хэш чистой истории в провенансе пересчитывается, то есть подделка согласована, как у сломанного
генератора: ловить её должны правила учёта и связь изменений с эталоном (grader.consistency).
"""
import csv
import json
import shutil
from pathlib import Path

import pytest
from fractions import Fraction

from common.jsonio import canonical_dumps, write_jsonl
from generator.build import generate_episode
from grader import clean_history
from grader.certificate import certify_episode
from grader.evidence import read_public

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "episodes" / "dev" / "EP-4A84A8A4"  # есть отдельная ошибка периода по счёту INV-D333VQYB


def _copy(tmp_path) -> Path:
    ep = tmp_path / SOURCE.name
    shutil.copytree(SOURCE, ep)
    (ep / "private" / "certificate.json").unlink(missing_ok=True)
    return ep


def _load(ep):
    truth = json.loads((ep / "private" / "truth.json").read_text(encoding="utf-8"))
    prov = json.loads((ep / "private" / "provenance.json").read_text(encoding="utf-8"))
    return read_public(ep / "public"), truth, prov


def _save(ep, public, truth=None, rehash=False):
    """Пишет изменённые открытые таблицы; rehash - согласовать хэш чистой истории с подделкой."""
    null = public["manifest"]["csv_null"]
    with (ep / "public" / "ledger.csv").open(encoding="utf-8", newline="") as stream:
        header = next(csv.reader(stream))
    with (ep / "public" / "ledger.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(header)
        for row in public["ledger"]:
            writer.writerow([null if row[key] is None else row[key] for key in header])
    write_jsonl(ep / "public" / "invoices.jsonl", public["invoices"])
    write_jsonl(ep / "public" / "payments.jsonl", public["payments"])
    if truth is not None:
        (ep / "private" / "truth.json").write_text(canonical_dumps(truth) + "\n", encoding="utf-8", newline="\n")
    if rehash:
        prov = json.loads((ep / "private" / "provenance.json").read_text(encoding="utf-8"))
        tables, _ = clean_history.reconstruct(read_public(ep / "public"), prov["clean_history_diff"])
        prov["clean_history_sha256"] = clean_history.digest(tables)
        (ep / "private" / "provenance.json").write_text(canonical_dumps(prov) + "\n", encoding="utf-8",
                                                        newline="\n")


def _untouched_base_invoice(public, truth, prov):
    """Неоплаченный счёт в базовой валюте, которого не касаются эталон, ловушки и журнал."""
    base = public["manifest"]["base_currency"]
    paid = {ref["invoice_id"] for pay in public["payments"] for ref in pay["invoice_refs"]}
    busy = {i for issue in truth["issues"] for i in issue["affected_ids"]}
    busy |= {i for item in truth["distractors"] for i in item["record_ids"]}
    diff = prov["clean_history_diff"]
    busy |= set(diff["invoices"]["added"]) | {c["id"] for c in diff["invoices"]["modified"]}
    for invoice in public["invoices"]:
        rows = [r for r in public["ledger"] if r["invoice_id"] == invoice["invoice_id"] and r["payment_id"] is None]
        if (invoice["currency"] == base and invoice["invoice_id"] not in paid | busy
                and not {r["row_id"] for r in rows} & set(diff["ledger"]["added"])):
            return invoice, rows
    raise AssertionError("нет подходящего счёта")


def _rules_errors(report) -> str:
    return " | ".join(report["clean_history_rules"]["errors"])


def test_committed_episodes_pass_rules():
    for ep in sorted((ROOT / "episodes").glob("*/EP-*")):
        public, truth, prov = _load(ep)
        report = clean_history.check(public, prov)
        assert report["status"] == "ok", ep.name
        from grader import consistency
        assert consistency.check(public, truth, prov)["status"] == "ok", ep.name


def test_invoice_posted_with_wrong_amount_in_clean_history(tmp_path):
    # счёт на 1000, а в книге ±999: документ сходится, хэш согласован, но учёт неверен
    ep = _copy(tmp_path)
    public, truth, prov = _load(ep)
    invoice, rows = _untouched_base_invoice(public, truth, prov)
    invoice["line_items"] = [dict(invoice["line_items"][0], quantity=1, unit_price_minor=1000, amount_minor=1000)]
    invoice["total_minor"] = 1000
    debit = next(r for r in rows if r["account"] == "1200")
    credits = [r for r in rows if r["account"] == "4000"]
    debit["debit_minor"] = 999
    credits[0]["credit_minor"] = 999
    drop = {r["row_id"] for r in credits[1:]}
    public["ledger"] = [r for r in public["ledger"] if r["row_id"] not in drop]
    _save(ep, public, rehash=True)
    public, truth, prov = _load(ep)
    assert clean_history.check(public, prov)["status"] == "ok"  # прежняя проверка обманута
    report = certify_episode(ep)
    assert not report["valid"]
    assert f"clean {invoice['invoice_id']}: document postings do not follow public policy" in _rules_errors(report)


def test_duplicate_already_in_clean_history(tmp_path):
    # дубль счёта есть уже в «чистой» истории: чистый эпизод тогда не чист
    ep = _copy(tmp_path)
    public, truth, prov = _load(ep)
    invoice, rows = _untouched_base_invoice(public, truth, prov)
    copy = dict(invoice, doc_id="DOC-TESTDUP1", invoice_id="INV-TESTDUP1")
    public["invoices"].append(copy)
    for n, row in enumerate(rows):
        public["ledger"].append(dict(row, row_id=f"L-TESTDUP{n}", invoice_id="INV-TESTDUP1"))
    _save(ep, public, rehash=True)
    public, truth, prov = _load(ep)
    assert clean_history.check(public, prov)["status"] == "ok"
    report = certify_episode(ep)
    assert not report["valid"]
    assert "duplicate of" in _rules_errors(report)


def test_undeclared_fx_change_inside_period_cutoff(tmp_path):
    # у ошибки периода проводки ещё и пересчитаны по другому курсу: такая ошибка не заявлена
    ep = _copy(tmp_path)
    public, truth, prov = _load(ep)
    rows = [r for r in public["ledger"] if r["invoice_id"] == "INV-D333VQYB" and r["payment_id"] is None]
    next(r for r in rows if r["account"] == "1200")["debit_minor"] += 100
    next(r for r in rows if r["account"] == "4000")["credit_minor"] += 100
    _save(ep, public)
    report = certify_episode(ep)
    assert all(item["valid"] for item in report["issues"])  # дата ошибки периода по-прежнему доказана
    assert not report["valid"]
    assert "INV-D333VQYB: postings do not follow policy for the document date" in _rules_errors(report)


def test_issue_removed_from_truth(tmp_path):
    # эталон неполон: изменение в данных есть, а ошибки в truth нет
    ep = _copy(tmp_path)
    public, truth, prov = _load(ep)
    truth["issues"] = [i for i in truth["issues"] if i["affected_ids"] != ["INV-D333VQYB"]]
    _save(ep, public, truth)
    report = certify_episode(ep)
    assert all(item["valid"] for item in report["issues"])  # оставшиеся ошибки доказаны
    assert not report["valid"]
    assert "INV-D333VQYB: document date changed without period_cutoff" in _rules_errors(report)


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_cheap_currency_full_skip_is_unambiguous(tmp_path, seed):
    # аудит 22.11: база USD, валюта KZT, сценарий 1. 1 тиын дешевле цента, поэтому сценарий вносится
    # полным пропуском: строки Кт 1200 по счёту нет, а правило «каждое ненулевое распределение даёт
    # строку 1200» сертификат проверяет на чистой истории - отсутствие строки однозначно значит 0
    off = {"enabled": False, "count": [0, 0]}
    cfg = {"config_version": "audit-usd-kzt", "clean": False,
           "company": {"archetype": "importer", "base_currency": "USD", "foreign_currencies": ["KZT"],
                       "timezone": "+05:00"},
           "errors": {t: off for t in ("duplicate_invoice", "payment_allocation", "fx_conversion", "period_cutoff")},
           "interactions": {"partial_fx_payment_at_cutoff": {"enabled": True, "count": [1, 1]},
                            "duplicate_invoice_wrong_fx": off},
           "distractors": {d: off for d in ("split_payment", "refund", "legit_recurring", "late_evening_local")}}
    ep = generate_episode(seed, cfg, tmp_path)
    report = certify_episode(ep)
    assert report["valid"], report
    assert report["clean_history_rules"]["status"] == "ok"
    public, truth, _ = _load(ep)
    issue = next(i for i in truth["issues"] if i["type"] == "payment_allocation")
    invoice_id, payment_id = issue["affected_ids"]
    payment = next(p for p in public["payments"] if p["payment_id"] == payment_id)
    assert not [r for r in public["ledger"] if r["payment_id"] == payment_id and r["account"] == "1200"
                and r["invoice_id"] == invoice_id]
    assert issue["expected_amount_minor"] == payment["invoice_refs"][0]["amount_minor"]


# ---------- ревью C 22.11: лазейки сертификата ----------

def _tables(ep):
    """Открытые таблицы и восстановленная чистая история (для проверки правил без пересчёта журнала)."""
    public, truth, prov = _load(ep)
    tables, _ = clean_history.reconstruct(public, prov["clean_history_diff"])
    clean = {"manifest": public["manifest"], "policy": public["policy"], **tables}
    return public, clean, truth


def _change_errors(public, clean, truth) -> str:
    from grader import consistency
    from grader.evidence import PublicEvidence
    errors = []
    consistency._check_changes(PublicEvidence(public), PublicEvidence(clean), truth, errors)
    return " | ".join(errors)


def test_fx_issue_cannot_hide_account_substitution(tmp_path):
    # у счёта с заявленной ошибкой курса выручка разнесена на другой счёт учёта
    ep = _copy(tmp_path)
    public, truth, prov = _load(ep)
    row = next(r for r in public["ledger"] if r["row_id"] == "L-1H8HN6B9")
    assert row["invoice_id"] == "INV-MCB947RJ" and row["account"] == "4000"
    row["account"] = "7900"
    _save(ep, public)
    report = certify_episode(ep)
    assert all(item["valid"] for item in report["issues"])  # строки-доказательства курса не тронуты
    assert not report["valid"]
    assert "INV-MCB947RJ: FX error changes more than the conversion rate" in _rules_errors(report)


def test_fx_issue_cannot_hide_allocation_change(tmp_path):
    # у платежа с заявленной ошибкой курса ещё и уменьшено распределение по счёту
    ep = _copy(tmp_path)
    public, truth, prov = _load(ep)
    rows = {r["row_id"]: r for r in public["ledger"] if r["payment_id"] == "PAY-SGXRNSKP"}
    assert rows["L-R4STX316"]["account"] == "1200" and rows["L-B8VY3SKB"]["account"] == "7900"
    rows["L-R4STX316"]["credit_minor"] -= 1000
    rows["L-B8VY3SKB"]["debit_minor"] -= 1000
    _save(ep, public)
    report = certify_episode(ep)
    assert all(item["valid"] for item in report["issues"])
    assert not report["valid"]
    assert "PAY-SGXRNSKP: FX error changes more than the conversion rate" in _rules_errors(report)


def test_period_cutoff_with_old_postings_is_undeclared_fx(tmp_path):
    # дата счёта перенесена, а проводки остались прежними (по курсу старой даты): раньше проверка
    # правил пропускалась, потому что проводки совпадают с чистыми
    public, clean, truth = _tables(_copy(tmp_path))
    old = [r for r in clean["ledger"] if r["invoice_id"] == "INV-D333VQYB" and r["payment_id"] is None]
    public["ledger"] = [r for r in public["ledger"]
                        if not (r["invoice_id"] == "INV-D333VQYB" and r["payment_id"] is None)] + old
    errors = _change_errors(public, clean, truth)
    assert "INV-D333VQYB: postings do not follow policy for the document date" in errors


def test_duplicate_created_by_date_shift(tmp_path):
    # перенос даты сблизил счёт с таким же счётом другой даты: возник незаявленный дубль
    from grader.evidence import PublicEvidence
    public, clean, truth = _tables(_copy(tmp_path))
    shifted = next(i for i in public["invoices"] if i["invoice_id"] == "INV-D333VQYB")
    twin = dict(shifted, doc_id="DOC-TESTTWIN", invoice_id="INV-TESTTWIN",
                issued_at=shifted["issued_at"].replace("2026-08-31", "2026-08-29"))
    assert twin["issued_at"] != shifted["issued_at"]
    value = PublicEvidence(public).convert(twin["total_minor"], twin["currency"], twin["issued_at"])
    base = next(r for r in clean["ledger"] if r["invoice_id"] == "INV-D333VQYB" and r["account"] == "1200")
    rows = [dict(base, row_id="L-TESTTW01", invoice_id="INV-TESTTWIN", timestamp=twin["issued_at"],
                 account="1200", debit_minor=value, credit_minor=0),
            dict(base, row_id="L-TESTTW02", invoice_id="INV-TESTTWIN", timestamp=twin["issued_at"],
                 account="4000", debit_minor=0, credit_minor=value)]
    for tables in (public, clean):
        tables["invoices"] = tables["invoices"] + [twin]
        tables["ledger"] = tables["ledger"] + rows
    errors = _change_errors(public, clean, truth)
    assert ("INV-TESTTWIN: undeclared duplicate of INV-D333VQYB" in errors
            or "INV-D333VQYB: undeclared duplicate of INV-TESTTWIN" in errors)


def test_multi_invoice_allocation_recovered_jointly():
    # платёж на два счёта: по отдельности строки 1200 неоднозначны (12 = 0.4 x 29..31,
    # 16 = 0.4 x 39..41), но остаток 2900 фиксирует сумму распределений 72 и делает их однозначными
    from decimal import Decimal
    from grader.evidence import PublicEvidence

    class Rates(PublicEvidence):
        def __init__(self):  # только то, что нужно пересчёту распределений
            self.invoices = {"INV-A": {"issued_at": "inv"}, "INV-B": {"issued_at": "inv"}}

        def factor(self, currency, timestamp):
            return Decimal("0.4") if timestamp == "inv" else Decimal(1)

    data = Rates()
    payment = {"payment_id": "PAY-X", "currency": "AAA", "timestamp": "pay", "amount_minor": 74,
               "invoice_refs": [{"invoice_id": "INV-A", "amount_minor": 32},
                                {"invoice_id": "INV-B", "amount_minor": 42}]}
    book = {("1010", None): 74, ("1200", "INV-A"): -12, ("1200", "INV-B"): -16, ("2900", None): -2,
            ("7900", None): -44}
    with pytest.raises(ValueError):
        data.source_amount(12, "AAA", "inv")  # прежняя проверка по одной строке отклоняла доказательство
    recovered = data.allocation_ranges(payment, book)
    assert recovered["ranges"] == {"INV-A": (31, 31), "INV-B": (41, 41)}
    assert data.payment_book(payment, recovered["assignment"]) == book


def test_provenance_of_other_episode_or_other_issues(tmp_path):
    from grader import consistency
    public, truth, prov = _load(_copy(tmp_path))
    assert consistency.check(public, truth, prov)["status"] == "ok"
    alien = dict(prov, episode_id="EP-OTHER001")
    assert "provenance episode_id differs from manifest" in consistency.check(public, truth, alien)["errors"]
    first = prov["mutations"][0]
    dropped = dict(prov, mutations=prov["mutations"][1:])
    assert f"{first['issue_id']}: no provenance mutation" in consistency.check(public, truth, dropped)["errors"]
    other = "fx_conversion" if first["type"] != "fx_conversion" else "period_cutoff"
    retyped = dict(prov, mutations=[dict(first, type=other)] + prov["mutations"][1:])
    assert (f"{first['issue_id']}: provenance mutation type differs from truth"
            in consistency.check(public, truth, retyped)["errors"])


def test_empty_journal_is_not_certified(tmp_path):
    # пустой журнал с согласованным хэшем: «чистая» история равна изменённой, ошибки эталона ничем не подкреплены
    ep = _copy(tmp_path)
    public, truth, prov = _load(ep)
    diff = prov["clean_history_diff"]
    prov["clean_history_diff"] = {table: {key: [] for key in part} for table, part in diff.items()}
    tables, _ = clean_history.reconstruct(public, prov["clean_history_diff"])
    prov["clean_history_sha256"] = clean_history.digest(tables)
    (ep / "private" / "provenance.json").write_text(canonical_dumps(prov) + "\n", encoding="utf-8", newline="\n")
    report = certify_episode(ep)
    assert report["clean_history_balance"]["status"] == "ok"
    assert not report["valid"]
    assert "declared issue has no change in public data" in _rules_errors(report)


# ---------- второй внешний отчёт 22.11: курс на границе округления, контрагент, пространства ID ----------

def _half_even(x):
    whole = x.numerator // x.denominator
    rest = x - whole
    return whole + (1 if rest > Fraction(1, 2) or (rest == Fraction(1, 2) and whole % 2) else 0)


def _rate_exists_brute(pairs) -> bool:
    points = sorted({Fraction(2 * c + d, 2 * n) for n, c in pairs for d in (-1, 1)})
    candidates = {p for p in points if p > 0} | {(a + b) / 2 for a, b in zip(points, points[1:]) if a + b > 0}
    candidates.add(points[-1] + 1)
    return any(all(_half_even(n * k) == c for n, c in pairs) for k in candidates)


def test_single_fx_rate_is_exact_at_rounding_boundary():
    # прежняя проверка «1010 и 2900 пропорциональны» пропускала невозможные пары: например,
    # платёж 1 и остаток 1 при 1010 = 1 и 2900 = 2 не даёт ни один общий курс
    from grader.consistency import _one_rate
    assert not _one_rate([(1, 1), (1, 2)])
    assert _one_rate([(3, 2), (1, 0)])  # k = 1/2: 1.5 -> 2 и 0.5 -> 0 (к чётному)
    for total in range(1, 10):
        for cash in range(1, 20):
            for remainder in range(1, total + 1):
                for rest in range(0, 20):
                    pairs = [(total, cash), (remainder, rest)]
                    assert _one_rate(pairs) == _rate_exists_brute(pairs), pairs


def test_counterparty_substitution_is_caught(tmp_path):
    # у строки проводки счёта с ошибкой курса подменён контрагент: суммы и счета прежние
    ep = _copy(tmp_path)
    public, truth, prov = _load(ep)
    other = next(i["entity_id"] for i in public["invoices"]
                 if i["entity_id"] != next(r for r in public["ledger"] if r["row_id"] == "L-1H8HN6B9")["entity_id"])
    next(r for r in public["ledger"] if r["row_id"] == "L-1H8HN6B9")["entity_id"] = other
    _save(ep, public)
    report = certify_episode(ep)
    assert not report["valid"]
    assert "INV-MCB947RJ: ledger rows name another counterparty" in _rules_errors(report)


def test_same_id_in_different_tables(tmp_path):
    # жюри меняет ID по пространствам независимо: одна и та же строка может стать ID строки книги,
    # документа, счёта, платежа и контрагента. Это разные ID, а не ложная связь
    from scripts.transform_episode import REMAP_PREFIXES, _apply_mapping, _rehash_clean_history, _token_re
    ep = _copy(tmp_path)
    token, mapping, counters = _token_re(REMAP_PREFIXES), {}, {}
    for path in sorted(ep.rglob("*")):
        if path.is_file() and path.suffix in (".json", ".jsonl", ".csv"):
            for found in sorted(set(m.group(0) for m in token.finditer(path.read_text(encoding="utf-8")))):
                if found not in mapping:
                    prefix = found.split("-", 1)[0]
                    counters[prefix] = counters.get(prefix, 0) + 1
                    mapping[found] = f"ID-{counters[prefix]:08d}"
    _apply_mapping(ep, mapping, REMAP_PREFIXES)
    _rehash_clean_history(ep)
    public, truth, prov = _load(ep)
    invoice_ids = {i["invoice_id"] for i in public["invoices"]}
    payment_ids = {p["payment_id"] for p in public["payments"]}
    assert invoice_ids & payment_ids and invoice_ids & {r["row_id"] for r in public["ledger"]}
    report = certify_episode(ep)
    assert report["valid"], (report["public_errors"], _rules_errors(report),
                             [i for i in report["issues"] if not i["valid"]][:2])
