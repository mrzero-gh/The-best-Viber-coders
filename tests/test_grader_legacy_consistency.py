"""Final duplicate completeness must also hold for the legacy record journal."""
from copy import deepcopy

import pytest

from grader.certificate import certify
from grader.history_proof import verify_history
from tests.test_grader_certificate import declared, invoice, post, public_data
from tests.test_grader_fixtures import truth_for
from tests.test_grader_history_proof import change, digest, journal, mutation, snapshot_of


def cutoff_with_nearby_invoice(description):
    clean = public_data()
    original = invoice(clean, number=1, timestamp="2026-08-29T12:00:00+05:00")
    moved = invoice(clean, number=2, timestamp="2026-09-20T12:00:00+05:00", description=description)
    post(clean, original, [("1200", 1000), ("4000", -1000)])
    post(clean, moved, [("1200", 1000), ("4000", -1000)])
    snapshot = snapshot_of(clean)
    public = deepcopy(clean)
    public["invoices"][1].update(issued_at="2026-08-30T12:00:00+05:00",
                                  due_at="2026-09-30T12:00:00+05:00")
    item = declared("period_cutoff", [moved["invoice_id"]], 1000,
                    [moved["doc_id"], public["ledger"][2]["row_id"]])
    changes = [change("invoices", clean["invoices"][1], public["invoices"][1])]
    provenance = journal(public, snapshot, [mutation(item, changes)])
    return public, truth_for(item), provenance, snapshot


@pytest.mark.parametrize("require_history", [None, True, False])
@pytest.mark.parametrize("description,valid", [
    ("Услуги за сентябрь", False),  # Same contents as the first invoice: a new hidden duplicate.
    ("Отдельная услуга", True),
])
def test_legacy_cutoff_cannot_hide_an_undeclared_final_duplicate(description, valid, require_history):
    public, truth, provenance, snapshot = cutoff_with_nearby_invoice(description)
    before = deepcopy((public, truth, provenance, snapshot))
    assert "clean_history_diff" not in provenance
    assert provenance["clean_history_sha256"] == digest(snapshot)
    # The snapshot, its hash and the ordered replay are all valid. Rejection
    # must come from the additional unreported error in the final documents.
    history = verify_history(public, truth, provenance, snapshot)
    assert history["status"] == "verified" and history["valid"] is True, history
    report = certify(public, truth, provenance, clean_history=snapshot, require_history=require_history)
    assert report["clean_history_balance"]["status"] == "verified", report
    assert report["issues"][0]["valid"] is True  # The cutoff itself is supported by evidence.
    assert report["valid"] is valid, report
    assert report["fully_verified"] is valid, report
    if not valid:
        assert "final duplicate pairs differ from declared affected sets" in report["public_errors"]
    assert (public, truth, provenance, snapshot) == before
