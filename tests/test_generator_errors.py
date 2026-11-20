"""Генератор, этап 2: ошибки duplicate_invoice и payment_allocation (задача #7)."""
from collections import defaultdict
from datetime import datetime

import pytest

from generator.build import generate_episode
from tests.test_generator_clean import _convert, _validator, load

OFF = {"enabled": False}
DUP_ALLOC = {"config_version": "t-err", "mode": "train",
             "errors": {"duplicate_invoice": {"enabled": True, "count": [2, 2]},
                        "payment_allocation": {"enabled": True, "count": [3, 3]},
                        "fx_conversion": OFF, "period_cutoff": OFF}}
SEEDS = list(range(20, 32))


@pytest.fixture(scope="module", params=SEEDS)
def ep(request, tmp_path_factory):
    return load(generate_episode(request.param, DUP_ALLOC, tmp_path_factory.mktemp("e")))


def _by_type(d, kind):
    return [i for i in d["truth"]["issues"] if i["type"] == kind]


def test_counts_and_truth_schema(ep):
    assert len(_by_type(ep, "duplicate_invoice")) == 2
    assert len(_by_type(ep, "payment_allocation")) == 3
    v = _validator("https://audit-gym.local/schemas/truth.schema.json")
    assert not list(v.iter_errors(ep["truth"]))
    keys = [(i["type"], tuple(i["affected_ids"])) for i in ep["truth"]["issues"]]
    assert len(keys) == len(set(keys)), "(type, affected_ids) уникальны"
    assert [i["issue_id"] for i in ep["truth"]["issues"]] == sorted(i["issue_id"] for i in ep["truth"]["issues"])


def test_books_still_balanced(ep):
    groups = defaultdict(int)
    for r in ep["ledger"]:
        groups[r["payment_id"] or r["invoice_id"]] += r["debit_minor"] - r["credit_minor"]
    assert all(v == 0 for v in groups.values())
    assert 200 <= len(ep["ledger"]) <= 2000
    assert ep["manifest"]["counts"]["ledger_rows"] == len(ep["ledger"])


def test_evidence_is_public_and_small(ep):
    public = ({r["row_id"] for r in ep["ledger"]} | {i["doc_id"] for i in ep["invoices"]}
              | {p["doc_id"] for p in ep["payments"]}
              | {f"FX:{x['date']}:{x['source_currency']}:{x['target_currency']}" for x in ep["fx"]})
    public_docs = {i["invoice_id"] for i in ep["invoices"]} | {p["payment_id"] for p in ep["payments"]}
    for issue in ep["truth"]["issues"]:
        assert set(issue["affected_ids"]) <= public_docs
        for s in issue["evidence_sets"]:
            assert set(s) <= public and len(s) <= 10


def test_duplicate_is_provable_from_public(ep):
    inv = {i["invoice_id"]: i for i in ep["invoices"]}
    for issue in _by_type(ep, "duplicate_invoice"):
        a, b = (inv[x] for x in issue["affected_ids"])
        for key in ("entity_id", "currency", "total_minor", "line_items"):
            assert a[key] == b[key]
        gap = abs((datetime.fromisoformat(a["issued_at"]).date() - datetime.fromisoformat(b["issued_at"]).date()).days)
        assert gap <= 5
        assert issue["expected_amount_minor"] == a["total_minor"] and issue["currency"] == a["currency"]
        assert issue["evidence_sets"] == [sorted([a["doc_id"], b["doc_id"]])]
        for x in (a, b):  # оба проведены в книге
            assert any(r["account"] == "1200" and r["invoice_id"] == x["invoice_id"] and r["debit_minor"] > 0
                       and r["payment_id"] is None for r in ep["ledger"])


def test_allocation_is_provable_from_public(ep):
    pays = {p["payment_id"]: p for p in ep["payments"]}
    inv = {i["invoice_id"]: i for i in ep["invoices"]}
    for issue in _by_type(ep, "payment_allocation"):
        pid = next(x for x in issue["affected_ids"] if x.startswith("PAY-"))
        iid = next(x for x in issue["affected_ids"] if x.startswith("INV-"))
        pay = pays[pid]
        declared = sum(r["amount_minor"] for r in pay["invoice_refs"] if r["invoice_id"] == iid)
        allocated = declared - issue["expected_amount_minor"]
        assert 0 <= allocated < declared and issue["currency"] == pay["currency"]
        rows = [r for r in ep["ledger"] if r["payment_id"] == pid]
        ar = sum(r["credit_minor"] for r in rows if r["account"] == "1200")
        assert ar == _convert(allocated, pay["currency"], inv[iid]["issued_at"][:10], ep)
        unalloc = sum(r["credit_minor"] for r in rows if r["account"] == "2900")
        assert unalloc == _convert(issue["expected_amount_minor"], pay["currency"], pay["timestamp"][:10], ep)
        assert sorted([pay["doc_id"]] + [r["row_id"] for r in rows]) == issue["evidence_sets"][0]


def test_unaffected_documents_stay_clean(ep):
    """Платежи вне ошибок разнесены полностью: 2900 только у ошибочных."""
    bad = {x for i in _by_type(ep, "payment_allocation") for x in i["affected_ids"]}
    for r in ep["ledger"]:
        if r["account"] == "2900":
            assert r["payment_id"] in bad


def test_clean_flag_and_max_errors(tmp_path):
    d = load(generate_episode(5, dict(DUP_ALLOC, clean=True), tmp_path / "c"))
    assert d["truth"]["issues"] == []
    d = load(generate_episode(5, dict(DUP_ALLOC, max_errors=3), tmp_path / "m"))
    assert len(d["truth"]["issues"]) == 3


def test_disabled_types_absent(tmp_path):
    cfg = dict(DUP_ALLOC, errors={"duplicate_invoice": OFF, "payment_allocation": {"enabled": True, "count": [2, 2]},
                                  "fx_conversion": OFF, "period_cutoff": OFF})
    d = load(generate_episode(6, cfg, tmp_path / "x"))
    assert {i["type"] for i in d["truth"]["issues"]} == {"payment_allocation"}


def test_deterministic_and_id_independent(tmp_path):
    a = load(generate_episode(9, DUP_ALLOC, tmp_path / "a"))
    b = load(generate_episode(9, DUP_ALLOC, tmp_path / "b"))
    c = load(generate_episode(9, DUP_ALLOC, tmp_path / "c", id_seed=12345))
    assert a["generation"]["episode_sha256"] == b["generation"]["episode_sha256"]
    assert a["truth"] == b["truth"]
    sig = lambda d: sorted((i["type"], i["expected_amount_minor"], i["currency"]) for i in d["truth"]["issues"])
    assert sig(a) == sig(c)
    assert sorted(r["debit_minor"] for r in a["ledger"]) == sorted(r["debit_minor"] for r in c["ledger"])
