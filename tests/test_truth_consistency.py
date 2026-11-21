"""Truth coverage against real reversible histories and independent manual cases."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from generator.build import generate_episode, history_diff
from grader.clean_history import TABLES, digest
from grader.evidence import read_public
from grader.truth_consistency import _document_changes, check_consistency
from tests.test_grader_fixtures import EPISODE_ID, issue, truth_for


TYPES = ("duplicate_invoice", "payment_allocation", "fx_conversion", "period_cutoff")
INTERACTIONS = ("partial_fx_payment_at_cutoff", "duplicate_invoice_wrong_fx")
FULL = {"errors": {kind: {"enabled": True, "count": [1, 1]} for kind in TYPES},
        "interactions": {kind: {"enabled": True, "count": [1, 1]} for kind in INTERACTIONS}}
FX_ONLY = {"errors": {kind: {"enabled": kind == "fx_conversion", "count": [1, 1]} for kind in TYPES},
           "interactions": {kind: {"enabled": False} for kind in INTERACTIONS}}


def load(episode):
    def private(name):
        return json.loads((episode / "private" / f"{name}.json").read_text(encoding="utf-8"))
    return read_public(episode / "public"), private("truth"), private("provenance")


@pytest.mark.parametrize("config", [{}, FULL, {"clean": True}], ids=["default", "combined", "clean"])
@pytest.mark.parametrize("seed", [1, 7])
def test_generated_histories_are_consistent_and_inputs_unchanged(tmp_path, seed, config):
    args = load(generate_episode(seed, config, tmp_path))
    before = deepcopy(args)
    report = check_consistency(*args)
    assert report["valid"], report
    assert report["mutations_checked"] == len(args[1]["issues"])
    assert report["duplicate_pairs_checked"] == sum(i["type"] == "duplicate_invoice" for i in args[1]["issues"])
    assert report["historical_policy_verified"] is False
    assert args == before


@pytest.fixture(scope="module")
def fx_episode(tmp_path_factory):
    args = load(generate_episode(12, FX_ONLY, tmp_path_factory.mktemp("consistency-fx")))
    assert len(args[1]["issues"]) == 1 and args[1]["issues"][0]["type"] == "fx_conversion"
    return args


def test_d12_removing_both_truth_and_mutation_does_not_hide_error(fx_episode):
    public, truth, provenance = deepcopy(fx_episode)
    truth["issues"] = []
    provenance["mutations"] = []
    report = check_consistency(public, truth, provenance)
    assert report["valid"] is False
    assert "not covered by truth" in report["errors"][0]


@pytest.mark.parametrize("damage", ["missing", "extra", "duplicate", "wrong_type", "wrong_episode", "policy_change"])
def test_mutation_membership_and_type_are_checked(fx_episode, damage):
    public, truth, provenance = deepcopy(fx_episode)
    mutation = provenance["mutations"][0]
    if damage == "missing":
        provenance["mutations"] = []
    elif damage == "extra":
        provenance["mutations"].append(dict(mutation, issue_id="ISS-00000000"))
    elif damage == "duplicate":
        provenance["mutations"].append(deepcopy(mutation))
    elif damage == "wrong_type":
        mutation["type"] = "period_cutoff"
    elif damage == "wrong_episode":
        provenance["episode_id"] = "EP-00000000"
    else:
        mutation["changes"].append({"table": "policy", "record_id": "fx", "field": "rate_date",
                                    "before": "document_local_date", "after": "other"})
    assert check_consistency(public, truth, provenance)["valid"] is False


def manual_public():
    examples = Path(__file__).resolve().parents[1] / "schemas" / "examples"
    return {"manifest": json.loads((examples / "manifest.example.json").read_text(encoding="utf-8")),
            "policy": json.loads((examples / "records.policy.json").read_text(encoding="utf-8")),
            "ledger": [], "invoices": [], "payments": [], "fx": []}


def add_invoice(public, number, timestamp="2026-09-15T12:00:00+05:00", description="same services"):
    doc = {"invoice_id": f"INV-{number:08d}", "doc_id": f"DOC-{number:08d}", "entity_id": "E-00000001",
           "issued_at": timestamp, "due_at": timestamp, "currency": "KZT", "total_minor": 1000,
           "line_items": [{"description": description, "quantity": 1, "unit_price_minor": 1000,
                           "amount_minor": 1000}]}
    public["invoices"].append(doc)
    for account, debit, credit in (("1200", 1000, 0), ("4000", 0, 1000)):
        public["ledger"].append({"row_id": f"L-{len(public['ledger']) + 1:08d}", "timestamp": timestamp,
                                 "account": account, "debit_minor": debit, "credit_minor": credit,
                                 "currency": "KZT", "entity_id": doc["entity_id"],
                                 "invoice_id": doc["invoice_id"], "payment_id": None})
    return doc


def provenance_for(clean, public, truth):
    return {"episode_id": EPISODE_ID,
            "clean_history_sha256": digest({name: clean[name] for name in TABLES}),
            "clean_history_diff": history_diff([clean[name] for name in TABLES],
                                               [public[name] for name in TABLES]),
            "mutations": [{"issue_id": item["issue_id"], "type": item["type"], "changes": []}
                          for item in truth["issues"]]}


def duplicate_case():
    clean = manual_public()
    original = add_invoice(clean, 1)
    add_invoice(clean, 3, description="different services")
    public = deepcopy(clean)
    duplicate = add_invoice(public, 2)
    item = issue("duplicate_invoice")
    item["affected_ids"] = [original["invoice_id"], duplicate["invoice_id"]]
    truth = truth_for(item)
    return public, truth, provenance_for(clean, public, truth)


def test_declared_duplicate_pair_matches_actual_documents():
    args = duplicate_case()
    report = check_consistency(*args)
    assert report["valid"], report
    assert report["changed_documents"] == ["INV-00000002"]
    assert report["duplicate_pairs_checked"] == 1
    args[1]["issues"][0]["affected_ids"] = ["INV-00000003", "INV-00000002"]
    report = check_consistency(*args)
    assert report["valid"] is False
    assert "duplicate pairs" in report["errors"][0]


def test_cutoff_cannot_hide_a_new_duplicate_pair():
    clean = manual_public()
    add_invoice(clean, 1, timestamp="2026-08-29T12:00:00+05:00")
    doc = add_invoice(clean, 2, timestamp="2026-09-20T12:00:00+05:00")
    public = deepcopy(clean)
    public["invoices"][1].update(issued_at="2026-08-30T12:00:00+05:00", due_at="2026-09-30T12:00:00+05:00")
    item = issue("period_cutoff")
    item["affected_ids"] = [doc["invoice_id"]]
    truth = truth_for(item)
    report = check_consistency(public, truth, provenance_for(clean, public, truth))
    assert report["valid"] is False
    assert "duplicate pairs" in report["errors"][0]


def test_cutoff_cannot_add_null_metadata_to_source_document():
    clean = manual_public()
    doc = add_invoice(clean, 1)
    public = deepcopy(clean)
    public["invoices"][0].update(issued_at="2026-10-01T12:00:00+05:00",
                                  due_at="2026-10-31T12:00:00+05:00")
    item = issue("period_cutoff")
    item["affected_ids"] = [doc["invoice_id"]]
    truth = truth_for(item)
    provenance = provenance_for(clean, public, truth)
    assert check_consistency(public, truth, provenance)["valid"]
    public["invoices"][0]["extra_metadata"] = None
    # The net-diff format has no sentinel for an absent field; test the scope
    # comparison directly too, so hash rejection cannot conceal its regression.
    with pytest.raises(ValueError, match="outside declared cutoff dates"):
        _document_changes(clean, public, {item["issue_id"]: item})
    assert check_consistency(public, truth, provenance)["valid"] is False


def test_modified_fx_reference_is_rejected_even_with_consistent_diff_and_hash():
    clean = manual_public()
    add_invoice(clean, 1)
    clean["fx"] = [{"date": "2026-09-15", "source_currency": "USD", "target_currency": "KZT", "rate": "2.5"}]
    public = deepcopy(clean)
    public["fx"][0]["rate"] = "3.0"
    truth = truth_for()
    report = check_consistency(public, truth, provenance_for(clean, public, truth))
    assert report["valid"] is False
    assert "FX reference table changed" in report["errors"][0]


@pytest.mark.parametrize("damage", ["policy_rule", "policy_in_diff", "wrong_hash", "missing_diff", "removed_document"])
def test_reference_policy_and_proof_damage_fail_closed(damage):
    public, truth, provenance = duplicate_case()
    if damage == "policy_rule":
        public["policy"]["fx"]["rate_date"] = "previous_day"
    elif damage == "policy_in_diff":
        provenance["clean_history_diff"]["policy"] = {"added": [], "removed": [], "modified": []}
    elif damage == "wrong_hash":
        provenance["clean_history_sha256"] = "0" * 64
    elif damage == "missing_diff":
        del provenance["clean_history_diff"]
    else:
        clean = deepcopy(public)
        public["invoices"] = [doc for doc in public["invoices"] if doc["invoice_id"] != "INV-00000003"]
        public["ledger"] = [row for row in public["ledger"] if row["invoice_id"] != "INV-00000003"]
        provenance = provenance_for(clean, public, truth)
    assert check_consistency(public, truth, provenance)["valid"] is False


def test_payment_ledger_owner_does_not_expand_to_other_payments_of_same_invoice():
    clean = manual_public()
    invoice = add_invoice(clean, 1)
    for number in (1, 2):
        pid = f"PAY-{number:08d}"
        clean["payments"].append({"payment_id": pid, "doc_id": f"DOC-{number + 10:08d}",
                                  "timestamp": "2026-09-20T12:00:00+05:00", "currency": "KZT",
                                  "amount_minor": 500, "invoice_refs": [{"invoice_id": invoice["invoice_id"],
                                                                          "amount_minor": 500}]})
        for account, debit, credit in (("1010", 500, 0), ("1200", 0, 500)):
            clean["ledger"].append({"row_id": f"L-{len(clean['ledger']) + 1:08d}",
                                     "timestamp": "2026-09-20T12:00:00+05:00", "account": account,
                                     "debit_minor": debit, "credit_minor": credit, "currency": "KZT",
                                     "entity_id": invoice["entity_id"], "payment_id": pid,
                                     "invoice_id": invoice["invoice_id"] if account == "1200" else None})
    public = deepcopy(clean)
    for row in public["ledger"]:
        if row["payment_id"]:
            row["timestamp"] = "2026-09-21T12:00:00+05:00"
    item = issue("payment_allocation")
    item["affected_ids"] = [invoice["invoice_id"], "PAY-00000001"]
    truth = truth_for(item)
    report = check_consistency(public, truth, provenance_for(clean, public, truth))
    assert report["valid"] is False
    assert "not covered by truth" in report["errors"][0]
