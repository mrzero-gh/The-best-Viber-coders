"""Full certificate regressions: journal replay alone is not financial proof."""
from copy import deepcopy
import json
import subprocess
import sys

import pytest

from grader.certificate import certify, certify_episode
from tests.test_grader_certificate import declared, invoice, payment, post, public_data, rate, write_episode
from tests.test_grader_fixtures import truth_for
from tests.test_grader_history_proof import (
    allocation_case, change, combined_payment_case, duplicate_fx_case, journal, mutation, snapshot_of,
)


def full_report(args, require_history=True):
    public, truth, provenance, snapshot = args
    return certify(public, truth, provenance=provenance, clean_history=snapshot,
                   require_history=require_history)


@pytest.mark.parametrize("factory", [allocation_case, duplicate_fx_case, combined_payment_case])
def test_supported_manual_histories_get_complete_certificate(factory):
    args = factory()
    before = deepcopy(args)
    report = full_report(args)
    assert report["valid"] and report["fully_verified"], report
    assert report["clean_history_balance"]["status"] == "verified"
    assert args == before


def cutoff_case(posted):
    clean = public_data()
    doc = invoice(clean, currency="USD")
    rate(clean, "2026-09-15", value="2.5000")
    rate(clean, "2026-10-01", value="3.0000")
    post(clean, doc, [("1200", 2500), ("4000", -2500)])
    snapshot = snapshot_of(clean)
    public = deepcopy(clean)
    public["invoices"][0].update(issued_at="2026-10-01T12:00:00+05:00",
                                  due_at="2026-10-31T12:00:00+05:00")
    public["ledger"][0]["debit_minor"] = posted
    public["ledger"][1]["credit_minor"] = posted
    issue = declared("period_cutoff", [doc["invoice_id"]], 1000,
                     [doc["doc_id"], public["ledger"][0]["row_id"]], "USD")
    changes = [change("invoices", clean["invoices"][0], public["invoices"][0])]
    changes += [change("ledger", before, after) for before, after in zip(clean["ledger"], public["ledger"])
                if before != after]
    return public, truth_for(issue), journal(public, snapshot, [mutation(issue, changes)]), snapshot


@pytest.mark.parametrize("posted,valid", [(3000, True), (3100, False), (2500, False)])
def test_cutoff_allows_new_date_conversion_but_not_an_undeclared_fx_error(posted, valid):
    report = full_report(cutoff_case(posted))
    assert report["clean_history_balance"]["status"] == "verified", report
    assert report["issues"][0]["valid"]  # The minimal cutoff evidence alone would pass.
    assert report["valid"] is valid, report
    assert report["fully_verified"] is valid
    if not valid:
        assert "outside the declared error types" in report["public_errors"][0]


@pytest.mark.parametrize("credit_account,valid", [("4000", True), ("2900", False)])
def test_fx_proof_does_not_allow_an_unrelated_account_error(credit_account, valid):
    clean = public_data()
    doc = invoice(clean, currency="USD")
    fx = rate(clean, "2026-09-15")
    post(clean, doc, [("1200", 2500), ("4000", -2500)])
    snapshot = snapshot_of(clean)
    public = deepcopy(clean)
    public["ledger"][0]["debit_minor"] = 2600
    public["ledger"][1].update(credit_minor=2600, account=credit_account)
    issue = declared("fx_conversion", [doc["invoice_id"]], 100,
                     [doc["doc_id"], public["ledger"][0]["row_id"], fx])
    changes = [change("ledger", before, after) for before, after in zip(clean["ledger"], public["ledger"])]
    report = full_report((public, truth_for(issue), journal(public, snapshot, [mutation(issue, changes)]), snapshot))
    assert report["issues"][0]["valid"]
    assert report["valid"] is valid, report
    assert report["fully_verified"] is valid


def test_fx_payment_allows_correct_balancing_exchange_difference():
    clean = public_data()
    inv = invoice(clean, currency="USD")
    rate(clean, "2026-09-15")
    fx = rate(clean, "2026-09-20")
    post(clean, inv, [("1200", 2500), ("4000", -2500)])
    pay = payment(clean, inv)
    post(clean, pay, [("1010", 2500), ("1200", -2500)])
    snapshot = snapshot_of(clean)
    public = deepcopy(clean)
    public["ledger"][2]["debit_minor"] = 2600
    difference = dict(public["ledger"][3], row_id="L-00000005", account="7900", credit_minor=100, invoice_id=None)
    public["ledger"].append(difference)
    issue = declared("fx_conversion", [pay["payment_id"]], 100,
                     [pay["doc_id"], public["ledger"][2]["row_id"], fx])
    changes = [change("ledger", clean["ledger"][2], public["ledger"][2]), change("ledger", None, difference)]
    report = full_report((public, truth_for(issue), journal(public, snapshot, [mutation(issue, changes)]), snapshot))
    assert report["valid"] and report["fully_verified"], report


def test_fx_payment_with_remainder_is_explicitly_unverified_without_applied_rate():
    clean = public_data()
    inv = invoice(clean, currency="USD")
    rate(clean, "2026-09-15")
    fx = rate(clean, "2026-09-20")
    post(clean, inv, [("1200", 2500), ("4000", -2500)])
    pay = payment(clean, inv, amount=1500)
    pay["invoice_refs"][0]["amount_minor"] = 1000
    post(clean, pay, [("1010", 3750), ("1200", -2500), ("2900", -1250)])
    snapshot = snapshot_of(clean)
    public = deepcopy(clean)
    public["ledger"][2]["debit_minor"] = 3900
    public["ledger"][4]["credit_minor"] = 1300
    difference = dict(public["ledger"][4], row_id="L-00000006", account="7900", credit_minor=100)
    public["ledger"].append(difference)
    issue = declared("fx_conversion", [pay["payment_id"]], 150,
                     [pay["doc_id"], public["ledger"][2]["row_id"], fx])
    changes = [change("ledger", clean["ledger"][i], public["ledger"][i]) for i in (2, 4)]
    changes.append(change("ledger", None, difference))
    report = full_report((public, truth_for(issue), journal(public, snapshot, [mutation(issue, changes)]), snapshot))
    assert report["clean_history_balance"]["status"] == "verified"
    assert report["issues"][0]["valid"]
    assert report["valid"] is False and report["fully_verified"] is False
    assert "applied-rate proof" in report["public_errors"][0]


def test_disk_and_cli_require_and_verify_closed_proof(tmp_path):
    public, truth, provenance, snapshot = duplicate_fx_case()
    write_episode(tmp_path, public, truth)
    for name, value in (("provenance", provenance), ("clean_history", snapshot)):
        (tmp_path / "private" / f"{name}.json").write_text(json.dumps(value), encoding="utf-8")
    public_before = {path.name: path.read_bytes() for path in (tmp_path / "public").iterdir()}
    report = certify_episode(tmp_path, require_history=True)
    assert report["valid"] and report["fully_verified"], report
    command = [sys.executable, "-m", "grader.certificate", str(tmp_path)]
    completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", check=False)
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["fully_verified"] is True
    provenance["clean_history_sha256"] = "0" * 64
    (tmp_path / "private" / "provenance.json").write_text(json.dumps(provenance), encoding="utf-8")
    for suffix in ([], ["--public-only"]):
        completed = subprocess.run(command + suffix, capture_output=True, text=True, encoding="utf-8", check=False)
        assert completed.returncode == 1, completed.stderr
        assert json.loads(completed.stdout)["fully_verified"] is False
    assert {path.name: path.read_bytes() for path in (tmp_path / "public").iterdir()} == public_before


@pytest.mark.parametrize("require_history", [True, False])
def test_valid_history_does_not_override_false_truth_amount(require_history):
    args = duplicate_fx_case()
    args[1]["issues"][1]["expected_amount_minor"] += 1
    report = full_report(args, require_history)
    assert report["clean_history_balance"]["status"] == "verified"
    assert report["valid"] is False
    assert report["fully_verified"] is False
