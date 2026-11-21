"""The strict certificate consumes main's reversible diff, without a new snapshot file."""
from copy import deepcopy
import json
import subprocess
import sys

import pytest

from grader.certificate import certify, certify_episode
from tests.test_grader_certificate import write_episode
from tests.test_grader_full_certificate import cutoff_case
from tests.test_grader_history_proof import allocation_case, combined_payment_case, duplicate_fx_case


def net_proof(args):
    public, truth, old_provenance, snapshot = deepcopy(args)
    diff = {}
    for table, field in (("ledger", "row_id"), ("invoices", "invoice_id"),
                         ("payments", "payment_id"), ("fx", None)):
        def key(record):
            return record[field] if field else "FX:{date}:{source_currency}:{target_currency}".format(**record)
        before = {key(row): row for row in snapshot["tables"][table]}
        after = {key(row): row for row in public[table]}
        diff[table] = {
            "added": sorted(after.keys() - before.keys()),
            "removed": [before[k] for k in sorted(before.keys() - after.keys())],
            "modified": [{"id": k, "field": f, "before": before[k][f], "after": after[k][f]}
                         for k in sorted(before.keys() & after.keys()) for f in sorted(before[k])
                         if before[k][f] != after[k][f]],
        }
    old_provenance["clean_history_diff"] = diff
    # main's mutation journal is sorted by ID. Chronological full-record replay
    # must not be imposed on this independent net-diff representation.
    old_provenance["mutations"] = sorted(
        [{"issue_id": m["issue_id"], "type": m["type"], "changes": []}
         for m in old_provenance["mutations"]], key=lambda m: m["issue_id"])
    return public, truth, old_provenance


@pytest.mark.parametrize("factory", [allocation_case, duplicate_fx_case, combined_payment_case])
def test_current_diff_gets_full_certificate_without_legacy_snapshot(factory):
    args = net_proof(factory())
    before = deepcopy(args)
    report = certify(*args, require_history=True)
    assert report["valid"] and report["fully_verified"], report
    assert report["clean_history_balance"]["status"] == "ok"
    assert report["truth_consistency"]["valid"]
    assert args == before


@pytest.mark.parametrize("posted,valid", [(3000, True), (3100, False)])
def test_net_diff_does_not_certify_hidden_fx_under_cutoff(posted, valid):
    report = certify(*net_proof(cutoff_case(posted)), require_history=True)
    assert report["clean_history_balance"]["status"] == "ok"
    assert report["valid"] is valid, report
    assert report["fully_verified"] is valid


def test_removing_truth_and_mutations_cannot_hide_injected_errors():
    public, truth, provenance = net_proof(duplicate_fx_case())
    truth["issues"] = []
    provenance["mutations"] = []
    report = certify(public, truth, provenance, require_history=True)
    assert report["clean_history_balance"]["status"] == "ok"
    assert report["valid"] is False and report["fully_verified"] is False
    assert report["truth_consistency"]["valid"] is False


@pytest.mark.parametrize("require_history", [None, True, False])
def test_malformed_provenance_cannot_be_treated_as_missing(require_history):
    public, truth, _ = net_proof(duplicate_fx_case())
    report = certify(public, truth, {}, require_history=require_history)
    assert report["valid"] is False and report["fully_verified"] is False
    assert report["clean_history_balance"]["status"] == "failed"


def test_cli_reads_current_provenance_without_snapshot_file(tmp_path):
    public, truth, provenance = net_proof(duplicate_fx_case())
    write_episode(tmp_path, public, truth)
    (tmp_path / "private" / "provenance.json").write_text(json.dumps(provenance), encoding="utf-8")
    assert not (tmp_path / "private" / "clean_history.json").exists()
    report = certify_episode(tmp_path, require_history=True)
    assert report["fully_verified"], report
    completed = subprocess.run([sys.executable, "-m", "grader.certificate", str(tmp_path)],
                               capture_output=True, text=True, encoding="utf-8")
    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert json.loads(completed.stdout)["fully_verified"]
