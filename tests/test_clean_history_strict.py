"""Hand-calculated before/after proofs; no generator or matcher is needed."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from grader.clean_history import TABLES, check, digest, reconstruct


EXAMPLES = Path(__file__).resolve().parents[1] / "schemas" / "examples"


def clean_company(amount=1000):
    invoice = {"invoice_id": "INV-00000001", "doc_id": "DOC-00000001", "entity_id": "E-00000001",
               "issued_at": "2026-09-15T12:00:00+05:00", "due_at": "2026-10-15T12:00:00+05:00",
               "currency": "KZT", "total_minor": amount,
               "line_items": [{"description": "Service", "quantity": 1,
                               "unit_price_minor": amount, "amount_minor": amount}]}
    common = {"timestamp": invoice["issued_at"], "currency": "KZT", "entity_id": invoice["entity_id"],
              "invoice_id": invoice["invoice_id"], "payment_id": None}
    return {
        "manifest": json.loads((EXAMPLES / "manifest.example.json").read_text(encoding="utf-8")),
        "policy": json.loads((EXAMPLES / "records.policy.json").read_text(encoding="utf-8")),
        "invoices": [invoice], "payments": [], "fx": [],
        "ledger": [dict(common, row_id="L-00000001", account="1200", debit_minor=amount, credit_minor=0),
                   dict(common, row_id="L-00000002", account="4000", debit_minor=0, credit_minor=amount)],
    }


def proof(clean):
    return {"episode_id": clean["manifest"]["episode_id"], "generator_version": "manual-proof",
            "clean_history_sha256": digest({table: clean[table] for table in TABLES}),
            "clean_history_diff": {table: {"added": [], "removed": [], "modified": []} for table in TABLES}}


def period_case():
    public = clean_company()
    provenance = proof(public)
    doc = public["invoices"][0]
    for field, after in (("issued_at", "2026-10-01T12:00:00+05:00"), ("due_at", "2026-11-01T12:00:00+05:00")):
        provenance["clean_history_diff"]["invoices"]["modified"].append(
            {"id": doc["invoice_id"], "field": field, "before": doc[field], "after": after})
        doc[field] = after
    return public, provenance


def test_small_clean_books_and_real_period_change_pass_without_mutating_inputs():
    public, provenance = period_case()
    original = deepcopy((public, provenance))
    report = check(public, provenance)
    assert report["status"] == "ok", report
    assert report["documents_checked"] == 1
    assert report["ledger_balance_minor"] == 0
    assert (public, provenance) == original


def test_reconstruction_restores_deleted_rows_and_removes_new_rows():
    public = clean_company()
    provenance = proof(public)
    original = deepcopy(public["ledger"])
    public["ledger"] = [dict(row, row_id=f"L-{n + 10:08d}") for n, row in enumerate(original)]
    provenance["clean_history_diff"]["ledger"].update(
        added=[row["row_id"] for row in public["ledger"]], removed=original)
    restored, errors = reconstruct(public, provenance["clean_history_diff"])
    assert not errors
    assert restored["ledger"] == original
    assert check(public, provenance)["status"] == "ok"


def test_outer_order_does_not_change_digest_or_verification():
    public = clean_company()
    provenance = proof(public)
    public["ledger"].reverse()
    assert digest({table: public[table] for table in TABLES}) == provenance["clean_history_sha256"]
    assert check(public, provenance)["status"] == "ok"


def test_inner_line_item_order_is_part_of_the_proof():
    public = clean_company()
    public["invoices"][0]["line_items"] = [
        {"description": "first", "quantity": 1, "unit_price_minor": 400, "amount_minor": 400},
        {"description": "second", "quantity": 1, "unit_price_minor": 600, "amount_minor": 600}]
    provenance = proof(public)
    public["invoices"][0]["line_items"].reverse()
    report = check(public, provenance)
    assert report["status"] == "failed" and not report["sha256_matches"]


@pytest.mark.parametrize("after", [True, 1.0])
def test_after_must_match_json_type_not_python_numeric_equality(after):
    public = clean_company(amount=1)
    provenance = proof(public)
    provenance["clean_history_diff"]["ledger"]["modified"] = [
        {"id": "L-00000001", "field": "debit_minor", "before": 2, "after": after}]
    _, errors = reconstruct(public, provenance["clean_history_diff"])
    assert errors and "after differs" in errors[0]


def test_missing_field_is_not_an_explicit_null():
    public = clean_company()
    provenance = proof(public)
    provenance["clean_history_diff"]["ledger"]["modified"] = [
        {"id": "L-00000001", "field": "never_existed", "before": "x", "after": None}]
    assert check(public, provenance)["status"] == "failed"


@pytest.mark.parametrize("damage", ["missing_table", "extra_table", "bad_section", "bad_list", "extra_operation",
                                   "duplicate_public", "duplicate_added", "duplicate_removed", "duplicate_modified",
                                   "added_removed_overlap", "added_modified_overlap", "removed_modified_overlap",
                                   "unknown_added", "unknown_modified", "change_record_id", "noop_modified"])
def test_invalid_diff_operations_cannot_be_certified(damage):
    public, provenance = period_case()
    diff = provenance["clean_history_diff"]
    part = diff["ledger"]
    if damage == "missing_table":
        del diff["fx"]
    elif damage == "extra_table":
        diff["other"] = deepcopy(part)
    elif damage == "bad_section":
        diff["ledger"] = []
    elif damage == "bad_list":
        part["added"] = {}
    elif damage == "extra_operation":
        part["unknown"] = []
    elif damage == "duplicate_public":
        public["ledger"].append(deepcopy(public["ledger"][0]))
    elif damage == "duplicate_added":
        part["added"] = ["L-00000001", "L-00000001"]
    elif damage == "duplicate_removed":
        row = dict(public["ledger"][0], row_id="L-00000003")
        part["removed"] = [row, deepcopy(row)]
    elif damage == "duplicate_modified":
        diff["invoices"]["modified"].append(deepcopy(diff["invoices"]["modified"][0]))
    elif damage == "added_removed_overlap":
        part["added"] = ["L-00000001"]
        part["removed"] = [deepcopy(public["ledger"][0])]
    elif damage == "added_modified_overlap":
        diff["invoices"]["added"] = ["INV-00000001"]
    elif damage == "removed_modified_overlap":
        diff["invoices"]["removed"] = [deepcopy(public["invoices"][0])]
    elif damage == "unknown_added":
        part["added"] = ["L-99999999"]
    elif damage == "unknown_modified":
        diff["invoices"]["modified"][0]["id"] = "INV-99999999"
    elif damage == "change_record_id":
        part["modified"] = [{"id": "L-00000001", "field": "row_id", "before": "L-00000003", "after": "L-00000001"}]
    else:
        change = diff["invoices"]["modified"][0]
        change["before"] = change["after"]
    report = check(public, provenance)
    assert report["status"] == "failed", report
    assert report["errors"]


@pytest.mark.parametrize("damage", ["wrong_episode", "missing_episode", "missing_hash", "empty_hash", "wrong_hash",
                                   "null_diff", "list_provenance", "list_diff"])
def test_metadata_and_malformed_supplied_proofs_fail(damage):
    public, provenance = period_case()
    if damage == "wrong_episode":
        provenance["episode_id"] += "-other"
    elif damage == "missing_episode":
        del provenance["episode_id"]
    elif damage == "missing_hash":
        del provenance["clean_history_sha256"]
    elif damage == "empty_hash":
        provenance["clean_history_sha256"] = ""
    elif damage == "wrong_hash":
        provenance["clean_history_sha256"] = "0" * 64
    elif damage == "null_diff":
        provenance["clean_history_diff"] = None
    elif damage == "list_provenance":
        provenance = []
    else:
        provenance["clean_history_diff"] = []
    assert check(public, provenance)["status"] == "failed"


@pytest.mark.parametrize("provenance", [None, {}])
def test_missing_diff_is_not_checked(provenance):
    assert check(clean_company(), provenance)["status"] == "not_checked"


@pytest.mark.parametrize("damage", ["wrong_amount", "wrong_account", "wrong_entity", "bad_invoice_link",
                                   "naive_due_at", "naive_timestamp", "boolean_money", "float_money",
                                   "inconsistent_items", "period_boolean_as_int", "invalid_rounding", "invalid_timezone"])
def test_balanced_but_invalid_original_books_are_rejected(damage):
    public = clean_company()
    if damage == "wrong_amount":
        public["ledger"][0]["debit_minor"] = public["ledger"][1]["credit_minor"] = 999
    elif damage == "wrong_account":
        public["ledger"][1]["account"] = "2900"
    elif damage == "wrong_entity":
        public["ledger"][0]["entity_id"] = "E-99999999"
    elif damage == "bad_invoice_link":
        public["ledger"][0]["invoice_id"] = "INV-99999999"
    elif damage == "naive_due_at":
        public["invoices"][0]["due_at"] = "2026-10-15T12:00:00"
    elif damage == "naive_timestamp":
        public["ledger"][0]["timestamp"] = "2026-09-15T12:00:00"
    elif damage == "boolean_money":
        public["ledger"][0]["debit_minor"] = True
    elif damage == "float_money":
        public["ledger"][0]["debit_minor"] = 1000.0
    elif damage == "inconsistent_items":
        public["invoices"][0]["line_items"][0]["quantity"] = 2
    elif damage == "period_boolean_as_int":
        public["policy"]["period"]["start_inclusive"] = 1
    elif damage == "invalid_timezone":
        public["manifest"]["timezone"] = ""
    else:
        public["policy"]["rounding"]["mode"] = "ROUND_DOWN"
    # A matching hash authenticates bytes, not their financial correctness.
    try:
        provenance = proof(public)
    except ValueError:  # malformed timestamps cannot even be canonically ordered
        provenance = proof(clean_company())
    report = check(public, provenance)
    assert report["status"] == "failed", report


@pytest.mark.parametrize("days,description,valid", [(0, "Service", False), (5, "Service", False),
                                                   (6, "Service", True), (1, "Another service", True)])
def test_clean_duplicate_detection_follows_the_published_definition(days, description, valid):
    from datetime import datetime, timedelta
    public = clean_company()
    other = deepcopy(public["invoices"][0])
    other.update(invoice_id="INV-00000002", doc_id="DOC-00000002")
    other["issued_at"] = (datetime.fromisoformat(other["issued_at"]) + timedelta(days=days)).isoformat()
    other["line_items"][0]["description"] = description
    public["invoices"].append(other)
    public["ledger"].extend(dict(row, row_id=f"L-{n + 3:08d}", invoice_id=other["invoice_id"],
                                 timestamp=other["issued_at"]) for n, row in enumerate(deepcopy(public["ledger"])))
    report = check(public, proof(public))
    assert (report["status"] == "ok") is valid, report


def test_nonfinite_or_non_json_values_are_rejected():
    public = clean_company()
    provenance = proof(public)
    for value in (float("nan"), float("inf"), {1: "not a JSON key"}):
        provenance["clean_history_diff"]["invoices"]["modified"] = [
            {"id": "INV-00000001", "field": "line_items", "before": value, "after": public["invoices"][0]["line_items"]}]
        assert check(public, provenance)["status"] == "failed"
