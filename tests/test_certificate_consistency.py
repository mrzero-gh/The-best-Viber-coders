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
