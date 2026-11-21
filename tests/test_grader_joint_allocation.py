"""Joint inverse rounding uses all public postings without consulting truth amounts."""
import json

import pytest

from grader.evidence import PublicEvidence, read_public
from tests.test_grader_certificate import invoice, payment, post, public_data, rate


def example(invoice_rate="0.2000", amount=100, second=False, allocated_total=0):
    public = public_data()
    inv = invoice(public, amount=amount, currency="USD")
    rate(public, "2026-09-15", value=invoice_rate)
    rate(public, "2026-09-20", value="1.0000")
    docs = [inv]
    if second:
        docs.append(invoice(public, number=2, amount=amount, currency="USD", description="Another service"))
    pay = payment(public, inv, amount=amount * len(docs))
    pay["invoice_refs"] = [{"invoice_id": doc["invoice_id"], "amount_minor": amount} for doc in docs]
    remainder = pay["amount_minor"] - allocated_total
    postings = [("1010", pay["amount_minor"]), ("2900", -remainder)]
    if allocated_total:
        postings.append(("7900", -allocated_total))
    post(public, pay, postings)
    return PublicEvidence(public), pay


@pytest.mark.parametrize("base,expected", [(0, (0, 1)), (1, (2, 2)), (2, (3, 5))])
def test_half_even_inverse_endpoints(base, expected):
    data, _ = example(invoice_rate="0.5000")
    assert data.source_amount_bounds(base, "USD", "2026-09-15T12:00:00+05:00") == expected


def test_impossible_inverse_amount_is_rejected():
    data, _ = example(invoice_rate="2.0000")
    with pytest.raises(ValueError, match="no source amount"):
        data.source_amount_bounds(1, "USD", "2026-09-15T12:00:00+05:00")


def test_two_dates_make_missing_allocation_uniquely_recoverable():
    data, pay = example()
    assert data.source_amount_bounds(0, "USD", "2026-09-15T12:00:00+05:00") == (0, 2)
    with pytest.raises(ValueError, match="uniquely recovered"):
        data.source_amount(0, "USD", "2026-09-15T12:00:00+05:00")
    assert data.recover_allocations(pay) == {pay["invoice_refs"][0]["invoice_id"]: 0}


def test_large_inverse_interval_is_solved_without_enumeration():
    data, pay = example(invoice_rate="0.000000001", amount=10**12)
    assert data.source_amount_bounds(0, "USD", "2026-09-15T12:00:00+05:00") == (0, 500_000_000)
    assert data.recover_allocations(pay) == {pay["invoice_refs"][0]["invoice_id"]: 0}


def test_multiple_references_are_resolved_by_the_sum_constraint():
    data, pay = example(second=True, allocated_total=4)
    assert data.recover_allocations(pay) == {ref["invoice_id"]: 2 for ref in pay["invoice_refs"]}


def test_multiple_reference_tradeoff_remains_ambiguous():
    data, pay = example(second=True, allocated_total=2)
    with pytest.raises(ValueError, match="uniquely recovered"):
        data.recover_allocations(pay)


def test_recovery_validates_cash_and_fx_postings_as_well():
    data, pay = example()
    cash = next(row for row in data.rows(pay) if row["account"] == "1010")
    cash["debit_minor"] += 1
    with pytest.raises(ValueError, match="postings are inconsistent"):
        data.recover_allocations(pay)


def test_seed_1038_unique_finding_recovers_but_other_finding_remains_ambiguous(tmp_path):
    from generator.build import generate_episode
    from tests.test_generator_invariants import random_config

    ep = generate_episode(1038, random_config(1038), tmp_path)
    data = PublicEvidence(read_public(ep / "public"))
    truth = json.loads((ep / "private" / "truth.json").read_text(encoding="utf-8"))
    issues = {item["issue_id"]: item for item in truth["issues"]}
    for issue_id, unique in (("ISS-JYTQYKKT", True), ("ISS-QDQCTX76", False)):
        item = issues[issue_id]
        pay = next(data.payments[key] for key in item["affected_ids"] if key in data.payments)
        if unique:
            assert data.recover_allocations(pay) == {ref["invoice_id"]: 0 for ref in pay["invoice_refs"]}
        else:
            with pytest.raises(ValueError, match="uniquely recovered"):
                data.recover_allocations(pay)
