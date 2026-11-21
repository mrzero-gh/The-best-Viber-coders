"""Closed-history proofs built from hand-calculated books, without generator imports."""
from copy import deepcopy
from datetime import datetime
import hashlib

import pytest

from common.jsonio import canonical_dumps
from grader.history_proof import verify_history
from tests.test_grader_certificate import declared, invoice, payment, post, public_data, rate
from tests.test_grader_fixtures import truth_for


TABLES = ("ledger", "invoices", "payments", "fx")


def snapshot_of(public):
    return {"schema_version": "1.0", "episode_id": public["manifest"]["episode_id"],
            "tables": {table: deepcopy(public[table]) for table in TABLES}}


def digest(snapshot):
    tables = deepcopy(snapshot["tables"])
    for table, time_key, id_key in (("ledger", "timestamp", "row_id"),
                                   ("invoices", "issued_at", "invoice_id"),
                                   ("payments", "timestamp", "payment_id")):
        tables[table].sort(key=lambda r: (datetime.fromisoformat(r[time_key]), r[id_key]))
    tables["fx"].sort(key=lambda r: (r["date"], r["source_currency"], r["target_currency"]))
    return hashlib.sha256(canonical_dumps(tables).encode("utf-8")).hexdigest()


def change(table, before, after):
    key = {"ledger": "row_id", "invoices": "invoice_id", "payments": "payment_id"}[table]
    return {"table": table, "record_id": (after if after is not None else before)[key],
            "before": deepcopy(before), "after": deepcopy(after)}


def journal(public, snapshot, items):
    return {"episode_id": public["manifest"]["episode_id"], "generator_version": "manual-proof",
            "clean_history_sha256": digest(snapshot), "mutations": items}


def mutation(issue, changes):
    return {"issue_id": issue["issue_id"], "type": issue["type"],
            "changes": [], "record_changes": changes}


def allocation_case():
    clean = public_data()
    inv = invoice(clean)
    post(clean, inv, [("1200", 1000), ("4000", -1000)])
    pay = payment(clean, inv)
    post(clean, pay, [("1010", 1000), ("1200", -1000)])
    snapshot = snapshot_of(clean)
    public = deepcopy(clean)
    public["ledger"][3]["credit_minor"] = 500
    remainder = dict(public["ledger"][3], row_id="L-00000005", account="2900", invoice_id=None)
    public["ledger"].append(remainder)
    issue = declared("payment_allocation", [inv["invoice_id"], pay["payment_id"]], 500,
                     [pay["doc_id"], *(row["row_id"] for row in public["ledger"][2:])])
    mutations = [mutation(issue, [change("ledger", clean["ledger"][3], public["ledger"][3]),
                                  change("ledger", None, remainder)])]
    return public, truth_for(issue), journal(public, snapshot, mutations), snapshot


def duplicate_fx_case():
    clean = public_data()
    src = invoice(clean, currency="USD")
    rate(clean, "2026-09-15")
    fx = rate(clean, "2026-09-19")
    post(clean, src, [("1200", 2500), ("4000", -2500)])
    snapshot = snapshot_of(clean)
    public = deepcopy(clean)
    duplicate = invoice(public, number=2, currency="USD", timestamp="2026-09-19T12:00:00+05:00")
    intermediate = post(public, duplicate, [("1200", 2500), ("4000", -2500)])
    dupe = declared("duplicate_invoice", [src["invoice_id"], duplicate["invoice_id"]], 1000,
                    [src["doc_id"], duplicate["doc_id"]], "USD", number=2)
    duplicated = mutation(dupe, [change("invoices", None, duplicate),
                                *(change("ledger", None, row) for row in intermediate)])
    final_rows = [dict(intermediate[0], row_id="L-00000005", debit_minor=2600),
                  dict(intermediate[1], row_id="L-00000006", credit_minor=2600)]
    public["ledger"] = public["ledger"][:2] + final_rows
    conversion = declared("fx_conversion", [duplicate["invoice_id"]], 100,
                          [duplicate["doc_id"], final_rows[0]["row_id"], fx], number=1)
    wrong_fx = mutation(conversion, [*(change("ledger", row, None) for row in intermediate),
                                    *(change("ledger", None, row) for row in final_rows)])
    dupe["scenario"] = conversion["scenario"] = "duplicate_invoice_wrong_fx"
    return public, truth_for(dupe, conversion), journal(public, snapshot, [duplicated, wrong_fx]), snapshot


def combined_payment_case():
    clean = public_data()
    inv = invoice(clean)
    post(clean, inv, [("1200", 1000), ("4000", -1000)])
    pay = payment(clean, inv)
    post(clean, pay, [("1010", 1000), ("1200", -1000)])
    snapshot = snapshot_of(clean)
    public = deepcopy(clean)
    public["payments"][0]["timestamp"] = "2026-10-01T12:00:00+05:00"
    public["ledger"][3]["credit_minor"] = 400
    public["ledger"].append(dict(public["ledger"][3], row_id="L-00000005", account="2900",
                                 credit_minor=600, invoice_id=None))
    cutoff = declared("period_cutoff", [pay["payment_id"]], 1000,
                      [pay["doc_id"], public["ledger"][2]["row_id"]])
    alloc = declared("payment_allocation", [inv["invoice_id"], pay["payment_id"]], 600,
                     [pay["doc_id"], *(row["row_id"] for row in public["ledger"][2:])], number=2)
    cutoff["scenario"] = alloc["scenario"] = "partial_fx_payment_at_cutoff"
    changes = [change("payments", clean["payments"][0], public["payments"][0]),
               change("ledger", clean["ledger"][3], public["ledger"][3]),
               change("ledger", None, public["ledger"][4])]
    return public, truth_for(cutoff, alloc), journal(public, snapshot, [mutation(cutoff, changes),
                                                                 mutation(alloc, [])]), snapshot


@pytest.mark.parametrize("factory", [allocation_case, duplicate_fx_case, combined_payment_case])
def test_replays_real_changes_and_transient_rows_without_mutating_inputs(factory):
    args = factory()
    original = deepcopy(args)
    report = verify_history(*args)
    assert report["status"] == "verified", report
    assert report["valid"] is True
    assert report["clean_ledger_balance_minor"] == 0
    assert report["clean_documents_checked"] > 0
    assert report["mutations_checked"] == len(args[2]["mutations"])
    assert args == original


def test_clean_episode_has_no_mutations_and_is_fully_verified():
    public = public_data()
    doc = invoice(public)
    post(public, doc, [("1200", 1000), ("4000", -1000)])
    snapshot = snapshot_of(public)
    report = verify_history(public, truth_for(), journal(public, snapshot, []), snapshot)
    assert report["status"] == "verified", report
    assert report["mutations_checked"] == 0


@pytest.mark.parametrize("missing", ["snapshot", "provenance", "both"])
def test_missing_closed_proof_is_not_a_verified_certificate(missing):
    public, truth, provenance, snapshot = allocation_case()
    if missing in ("snapshot", "both"):
        snapshot = None
    if missing in ("provenance", "both"):
        provenance = None
    report = verify_history(public, truth, provenance, snapshot)
    assert report["status"] == "not_checked"
    assert report["valid"] is None


@pytest.mark.parametrize("provenance,snapshot", [([], None), ({}, None), (None, []),
                                                (None, {"schema_version": "bad"})])
def test_malformed_supplied_proof_is_not_hidden_by_missing_other_input(provenance, snapshot):
    public, truth, _, _ = allocation_case()
    report = verify_history(public, truth, provenance, snapshot)
    assert report["status"] == "failed"
    assert report["valid"] is False


@pytest.mark.parametrize("timestamp,description,valid", [
    ("2026-09-15T12:00:00+05:00", "Услуги за сентябрь", False),
    ("2026-09-20T12:00:00+05:00", "Услуги за сентябрь", False),
    ("2026-08-15T12:00:00+05:00", "Услуги за август", True),
    ("2026-09-16T12:00:00+05:00", "Другая услуга", True),
])
def test_clean_snapshot_rejects_duplicate_obligations_but_accepts_recurring_invoices(timestamp, description, valid):
    public = public_data()
    source = invoice(public)
    post(public, source, [("1200", 1000), ("4000", -1000)])
    other = invoice(public, number=2, timestamp=timestamp, description=description)
    post(public, other, [("1200", 1000), ("4000", -1000)])
    snapshot = snapshot_of(public)
    report = verify_history(public, truth_for(), journal(public, snapshot, []), snapshot)
    assert report["valid"] is valid, report
    if not valid:
        assert "already contains duplicate" in report["errors"][0]


def test_table_order_is_irrelevant_but_journal_order_is_required():
    args = duplicate_fx_case()
    for table in TABLES:
        args[0][table].reverse()
        args[3]["tables"][table].reverse()
    assert verify_history(*args)["status"] == "verified"
    args[2]["mutations"].reverse()
    report = verify_history(*args)
    assert report["status"] == "failed"
    assert "before differs" in report["errors"][0]


def test_hash_tampering_and_balanced_but_incorrect_original_books_fail():
    args = allocation_case()
    args[3]["tables"]["ledger"][0]["debit_minor"] += 1
    assert "hash mismatch" in verify_history(*args)["errors"][0]
    args[3]["tables"]["ledger"][1]["credit_minor"] += 1
    args[2]["clean_history_sha256"] = digest(args[3])
    report = verify_history(*args)
    assert report["status"] == "failed"
    assert "postings do not follow" in report["errors"][0]


def test_wrong_original_record_cannot_be_replaced_by_a_claim_about_before():
    args = allocation_case()
    args[2]["mutations"][0]["record_changes"][0]["before"]["credit_minor"] = 999
    report = verify_history(*args)
    assert report["status"] == "failed"
    assert "before differs" in report["errors"][0]


def test_unlogged_public_changes_are_detected_even_if_books_still_balance():
    args = allocation_case()
    args[0]["ledger"][0]["debit_minor"] += 1
    args[0]["ledger"][1]["credit_minor"] += 1
    report = verify_history(*args)
    assert report["status"] == "failed"
    assert "differ from final" in report["errors"][0]


def test_mutation_must_not_change_unaffected_document():
    args = allocation_case()
    other = invoice(args[0], number=2)
    extra = post(args[0], other, [("1200", 1000), ("4000", -1000)])
    args[2]["mutations"][0]["record_changes"].extend(
        [change("invoices", None, other), *(change("ledger", None, row) for row in extra)])
    report = verify_history(*args)
    assert report["status"] == "failed"
    assert "outside affected_ids" in report["errors"][0]


@pytest.mark.parametrize("damage", ["missing_journal", "missing_issue", "extra_issue", "duplicate_issue",
                                   "wrong_type", "empty_change", "renamed_record", "incomplete_record",
                                   "boolean_money", "wrong_episode", "wrong_schema", "duplicate_public_id"])
def test_malformed_or_incomplete_proof_fails_closed(damage):
    args = allocation_case()
    public, truth, provenance, snapshot = args
    mut = provenance["mutations"][0]
    if damage == "missing_journal":
        del mut["record_changes"]
    elif damage == "missing_issue":
        provenance["mutations"] = []
    elif damage == "extra_issue":
        mut["issue_id"] = "ISS-99999999"
    elif damage == "duplicate_issue":
        provenance["mutations"].append(deepcopy(mut))
    elif damage == "wrong_type":
        mut["type"] = "period_cutoff"
    elif damage == "empty_change":
        mut["record_changes"][0]["after"] = deepcopy(mut["record_changes"][0]["before"])
    elif damage == "renamed_record":
        mut["record_changes"][0]["after"]["row_id"] = "L-99999999"
    elif damage == "incomplete_record":
        del mut["record_changes"][0]["after"]["currency"]
    elif damage == "boolean_money":
        snapshot["tables"]["ledger"][0]["debit_minor"] = True
        provenance["clean_history_sha256"] = digest(snapshot)
    elif damage == "wrong_episode":
        snapshot["episode_id"] = "EP-99999999"
    elif damage == "wrong_schema":
        snapshot["schema_version"] = "2.0"
    else:
        snapshot["tables"]["ledger"].append(deepcopy(snapshot["tables"]["ledger"][0]))
        provenance["clean_history_sha256"] = digest(snapshot)
    report = verify_history(*args)
    assert report["status"] == "failed", report
    assert report["valid"] is False
    assert report["errors"]


@pytest.mark.parametrize("scenario", [None, "unrelated_scenario"])
def test_empty_mutation_needs_a_shared_real_change_in_the_same_scenario(scenario):
    args = combined_payment_case()
    args[1]["issues"][1]["scenario"] = scenario
    report = verify_history(*args)
    assert report["status"] == "failed"
    assert "empty mutation" in report["errors"][0]


def test_full_line_item_order_is_preserved_in_hash_and_replay():
    args = allocation_case()
    original = args[3]["tables"]["invoices"][0]["line_items"][0]
    lines = [dict(original, description="first", unit_price_minor=400, amount_minor=400),
             dict(original, description="second", unit_price_minor=600, amount_minor=600)]
    args[3]["tables"]["invoices"][0]["line_items"] = deepcopy(lines)
    args[0]["invoices"][0]["line_items"] = deepcopy(lines)
    args[2]["clean_history_sha256"] = digest(args[3])
    assert verify_history(*args)["status"] == "verified"
    args[0]["invoices"][0]["line_items"].reverse()
    assert verify_history(*args)["status"] == "failed"
