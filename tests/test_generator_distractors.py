"""Генератор, этап 4: чистые компании и отвлекающие случаи (задача #15)."""
from collections import Counter, defaultdict
from datetime import datetime, timedelta

import pytest

from generator.build import generate_episode
from tests.test_generator_clean import _convert, _validator, load

ON2 = {"enabled": True, "count": [2, 2]}
KINDS = ("split_payment", "refund", "legit_recurring", "late_evening_local")
ALL_D = {"config_version": "t-dst", "mode": "train",
         "distractors": {k: ON2 for k in KINDS},
         "errors": {t: ON2 for t in ("duplicate_invoice", "payment_allocation", "fx_conversion", "period_cutoff")}}
CLEAN_D = dict(ALL_D, clean=True)


@pytest.fixture(scope="module", params=[(s, c) for s in range(200, 208) for c in ("all", "clean")],
                ids=lambda p: f"{p[0]}-{p[1]}")
def ep(request, tmp_path_factory):
    seed, kind = request.param
    return load(generate_episode(seed, ALL_D if kind == "all" else CLEAN_D, tmp_path_factory.mktemp("d")))


def _items(d, kind):
    return [x for x in d["truth"]["distractors"] if x["kind"] == kind]


def _docs(d):
    out = {i["invoice_id"]: i for i in d["invoices"]}
    out.update({p["payment_id"]: p for p in d["payments"]})
    return out


def test_every_kind_present_and_schema(ep):
    assert Counter(x["kind"] for x in ep["truth"]["distractors"]) == {k: 2 for k in KINDS}
    assert not list(_validator("https://audit-gym.local/schemas/truth.schema.json").iter_errors(ep["truth"]))
    if ep["generation"]["config"]["clean"]:
        assert ep["truth"]["issues"] == []


def test_distractors_never_overlap_issues(ep):
    dst = {x for item in ep["truth"]["distractors"] for x in item["record_ids"]}
    bad = {x for i in ep["truth"]["issues"] for x in i["affected_ids"]}
    assert not dst & bad
    seen = [x for item in ep["truth"]["distractors"] for x in item["record_ids"]]
    assert len(seen) == len(set(seen)), "отвлекающие случаи не делят документы"


def test_books_balanced(ep):
    groups = defaultdict(int)
    for r in ep["ledger"]:
        groups[r["payment_id"] or r["invoice_id"]] += r["debit_minor"] - r["credit_minor"]
    assert all(v == 0 for v in groups.values())
    assert 200 <= len(ep["ledger"]) <= 2000 and 20 <= len(ep["invoices"]) + len(ep["payments"]) <= 200


def test_split_payment_settles_invoice_exactly(ep):
    docs = _docs(ep)
    for item in _items(ep, "split_payment"):
        (iid,) = [x for x in item["record_ids"] if x.startswith("INV-")]
        pays = [docs[x] for x in item["record_ids"] if x.startswith("PAY-")]
        assert len(pays) == 2 and all(p["amount_minor"] > 0 for p in pays)
        assert sum(r["amount_minor"] for p in pays for r in p["invoice_refs"]) == docs[iid]["total_minor"]
        ar = sum(r["debit_minor"] - r["credit_minor"] for r in ep["ledger"]
                 if r["account"] == "1200" and r["invoice_id"] == iid)
        assert ar == 0, "счёт закрыт без остатка"


def test_refund_reverses_postings(ep):
    docs = _docs(ep)
    for item in _items(ep, "refund"):
        pays = sorted((docs[x] for x in item["record_ids"] if x.startswith("PAY-")), key=lambda p: p["amount_minor"])
        back, orig = pays
        assert back["amount_minor"] < 0 < orig["amount_minor"]
        assert -back["amount_minor"] <= orig["amount_minor"]
        assert datetime.fromisoformat(back["timestamp"]) > datetime.fromisoformat(orig["timestamp"])
        rows = [r for r in ep["ledger"] if r["payment_id"] == back["payment_id"]]
        cash = next(r for r in rows if r["account"] == "1010")
        assert cash["credit_minor"] == _convert(-back["amount_minor"], back["currency"], back["timestamp"][:10], ep)
        assert any(r["account"] == "1200" and r["debit_minor"] > 0 for r in rows)


def test_legit_recurring_differs_by_month_and_service(ep):
    docs = _docs(ep)
    for item in _items(ep, "legit_recurring"):
        a, b = (docs[x] for x in item["record_ids"])
        assert (a["entity_id"], a["currency"], a["total_minor"]) == (b["entity_id"], b["currency"], b["total_minor"])
        months = {datetime.fromisoformat(x["issued_at"]).strftime("%Y-%m") for x in (a, b)}
        assert len(months) == 2
        assert [x["description"] for x in a["line_items"]] != [x["description"] for x in b["line_items"]]


def test_late_evening_is_inside_period(ep):
    docs = _docs(ep)
    end = datetime.fromisoformat(ep["manifest"]["period_end"])
    for item in _items(ep, "late_evening_local"):
        (key,) = item["record_ids"]
        ts = datetime.fromisoformat(docs[key].get("timestamp") or docs[key]["issued_at"])
        assert ts.hour == 23 and end - timedelta(hours=1) <= ts < end
        rows = [r for r in ep["ledger"] if key in (r["payment_id"], r["invoice_id"])]
        assert rows and all(datetime.fromisoformat(r["timestamp"]) < end for r in rows)


def test_only_distractors_are_late_evening(ep):
    late = {x for item in _items(ep, "late_evening_local") for x in item["record_ids"]}
    for doc in ep["invoices"] + ep["payments"]:
        ts = datetime.fromisoformat(doc.get("timestamp") or doc["issued_at"])
        if ts.hour >= 23:
            assert (doc.get("payment_id") or doc["invoice_id"]) in late
