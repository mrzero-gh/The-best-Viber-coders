"""Нулевые проводки не доказывают нулевое распределение исходной валюты."""
from copy import deepcopy
from decimal import Decimal, ROUND_HALF_EVEN
import json

import pytest

from generator.build import generate_episode
from grader.certificate import certify
from grader.clean_history import TABLES, digest
from grader.evidence import PublicEvidence, read_public
from tests.test_grader_certificate import declared, invoice, payment, post, public_data, rate
from tests.test_grader_fixtures import truth_for


def _manual_missing(rate_value):
    public = public_data()
    inv = invoice(public, amount=1000, currency="USD")
    pay = payment(public, inv, amount=1000)
    rate(public, "2026-09-15", value=rate_value)
    rate(public, "2026-09-20", value=rate_value)
    converted = int((Decimal(1000) * Decimal(rate_value)).to_integral_value(rounding=ROUND_HALF_EVEN))
    post(public, inv, [("1200", converted), ("4000", -converted)])
    rows = post(public, pay, [("1010", converted), ("2900", -converted)])
    item = declared("payment_allocation", [inv["invoice_id"], pay["payment_id"]], 1000,
                    [pay["doc_id"], *(row["row_id"] for row in rows)], "USD")
    truth = truth_for(item)
    # До ошибки этот же платёж целиком закрывал счёт. Только эти два поля
    # отличают чистую проводку 1200 от конечного нераспределённого остатка 2900.
    clean = deepcopy({name: public[name] for name in TABLES})
    clean["ledger"][-1].update(account="1200", invoice_id=inv["invoice_id"])
    diff = {name: {"added": [], "removed": [], "modified": []} for name in TABLES}
    diff["ledger"]["modified"] = [
        {"id": rows[-1]["row_id"], "field": "account", "before": "1200", "after": "2900"},
        {"id": rows[-1]["row_id"], "field": "invoice_id", "before": inv["invoice_id"], "after": None},
    ]
    provenance = {"episode_id": truth["episode_id"], "clean_history_sha256": digest(clean),
                  "clean_history_diff": diff,
                  "mutations": [{"issue_id": item["issue_id"], "type": item["type"], "changes": [
                      {"table": "ledger", "record_id": change["id"], "field": change["field"],
                       "before": change["before"], "after": change["after"]}
                      for change in diff["ledger"]["modified"]]}]}
    return public, truth, provenance, inv, pay


def _assert_ambiguous_issue(report, issue_id):
    assert report["valid"] is False
    assert report["fully_verified"] is False
    item = next(item for item in report["issues"] if item["issue_id"] == issue_id)
    assert item["valid"] is False
    assert all("cannot be uniquely recovered" in check["error"] for check in item["evidence_sets"])


@pytest.mark.parametrize("with_provenance", [False, True])
def test_same_public_book_cannot_prove_zero_allocation(with_provenance):
    public, truth, provenance, inv, pay = _manual_missing("0.001")
    data = PublicEvidence(public)
    iid = inv["invoice_id"]
    assert data.book(pay) == data.payment_book(pay, {iid: 0}) == data.payment_book(pay, {iid: 100})
    assert ("1200", iid) not in data.book(pay)
    with pytest.raises(ValueError, match="cannot be uniquely recovered"):
        data.source_amount(0, pay["currency"], inv["issued_at"])
    report = certify(public, truth, provenance if with_provenance else None)
    if with_provenance:
        assert report["clean_history_balance"]["valid"] is True
    _assert_ambiguous_issue(report, truth["issues"][0]["issue_id"])


@pytest.mark.parametrize("rate_value", ["0.5001", "1.0", "2.5"])
@pytest.mark.parametrize("with_provenance", [False, True])
def test_uniquely_recoverable_missing_allocation_is_valid(rate_value, with_provenance):
    public, truth, provenance, inv, pay = _manual_missing(rate_value)
    data = PublicEvidence(public)
    assert data.source_amount(0, pay["currency"], inv["issued_at"]) == 0
    report = certify(public, truth, provenance if with_provenance else None)
    assert report["valid"], report
    assert report["fully_verified"] is with_provenance


def test_half_even_boundary_does_not_make_zero_unique():
    public, truth, _, inv, pay = _manual_missing("0.5")
    data = PublicEvidence(public)
    # Ровно 0.5 округляется к чётному нулю: исходные 0 и 1 неразличимы.
    assert data.convert(0, pay["currency"], inv["issued_at"]) == 0
    assert data.convert(1, pay["currency"], inv["issued_at"]) == 0
    with pytest.raises(ValueError, match="cannot be uniquely recovered"):
        data.source_amount(0, pay["currency"], inv["issued_at"])
    _assert_ambiguous_issue(certify(public, truth), truth["issues"][0]["issue_id"])


@pytest.fixture
def cheap_foreign_episode(tmp_path):
    config = {
        "company": {"archetype": "importer", "base_currency": "USD", "timezone": "+05:00",
                    "foreign_currencies": ["KZT"]},
        "errors": {kind: {"enabled": False} for kind in
                   ("duplicate_invoice", "payment_allocation", "fx_conversion", "period_cutoff")},
        "interactions": {"partial_fx_payment_at_cutoff": {"enabled": True, "count": [1, 1]},
                         "duplicate_invoice_wrong_fx": {"enabled": False}},
        "distractors": {kind: {"enabled": False} for kind in
                        ("split_payment", "refund", "legit_recurring", "late_evening_local")},
    }
    episode = generate_episode(1, config, tmp_path)
    public = read_public(episode / "public")
    read = lambda name: json.loads((episode / "private" / name).read_text(encoding="utf-8"))
    return public, read("truth.json"), read("provenance.json")


@pytest.mark.parametrize("with_provenance", [False, True])
def test_real_cheap_currency_episode_rejects_ambiguous_missing_allocation(cheap_foreign_episode,
                                                                         with_provenance):
    public, truth, provenance = cheap_foreign_episode
    item = next(item for item in truth["issues"] if item["type"] == "payment_allocation")
    data = PublicEvidence(public)
    inv = data.invoices[next(identifier for identifier in item["affected_ids"] if identifier in data.invoices)]
    pay = data.payments[next(identifier for identifier in item["affected_ids"] if identifier in data.payments)]
    iid = inv["invoice_id"]
    assert pay["currency"] == "KZT" and data.base == "USD"
    assert data.book(pay) == data.payment_book(pay, {iid: 0}) == data.payment_book(pay, {iid: 233})
    with pytest.raises(ValueError, match="cannot be uniquely recovered"):
        data.source_amount(0, pay["currency"], inv["issued_at"])
    report = certify(public, truth, provenance if with_provenance else None)
    if with_provenance:
        assert report["clean_history_balance"]["valid"] is True
    _assert_ambiguous_issue(report, item["issue_id"])
