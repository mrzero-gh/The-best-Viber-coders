"""Проверки мини-эпизода: схемы, баланс проводок, доступность доказательств, детерминизм."""
import json
import tempfile
from collections import defaultdict
from pathlib import Path

import pytest

jsonschema = pytest.importorskip("jsonschema")
pytest.importorskip("referencing")
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from env.episode import load_public, load_truth
from tests.fixtures.build_mini import EPISODE_ID, build

ROOT = Path(__file__).resolve().parent.parent
SCHEMAS = ROOT / "schemas"
EP_DIR = ROOT / "tests" / "fixtures" / "mini" / EPISODE_ID


def _schema(name):
    return json.loads((SCHEMAS / (name + ".schema.json")).read_text(encoding="utf-8"))


def _registry():
    reg = Registry()
    for p in SCHEMAS.glob("*.schema.json"):
        s = json.loads(p.read_text(encoding="utf-8"))
        reg = reg.with_resource(s["$id"], Resource.from_contents(s, default_specification=DRAFT202012))
    return reg


def _validator(schema_name, def_name=None):
    schema = _schema(schema_name)
    if def_name:
        schema = {"$ref": "#/$defs/" + def_name, "$defs": schema["$defs"], "$id": schema["$id"]}
    return jsonschema.Draft202012Validator(schema, registry=_registry())


def test_files_exist():
    for name in ["manifest.json", "ledger.csv", "invoices.jsonl", "payments.jsonl", "fx.csv", "policy.json"]:
        assert (EP_DIR / "public" / name).is_file(), name
    for name in ["truth.json", "provenance.json", "generation.json"]:
        assert (EP_DIR / "private" / name).is_file(), name


def test_records_match_schema():
    ep = load_public(EP_DIR)
    for table, defname in [("ledger", "ledger_row"), ("invoices", "invoice"),
                           ("payments", "payment"), ("fx", "fx_rate")]:
        v = _validator("records", defname)
        for row in ep.table(table):
            v.validate(row)
    _validator("records", "policy").validate(ep.policy)
    _validator("manifest").validate(ep.manifest)
    _validator("truth").validate(load_truth(EP_DIR))


def test_counts_and_ledger_shape():
    ep = load_public(EP_DIR)
    c = ep.manifest["counts"]
    assert (len(ep.ledger), len(ep.invoices), len(ep.payments), len(ep.fx)) == (
        c["ledger_rows"], c["invoices"], c["payments"], c["fx_rates"])
    assert c["documents"] == len(ep.documents)
    for r in ep.ledger:  # ровно одно из debit/credit > 0
        assert (r["debit_minor"] > 0) != (r["credit_minor"] > 0)


def test_double_entry_balance():
    ep = load_public(EP_DIR)
    assert sum(r["debit_minor"] for r in ep.ledger) == sum(r["credit_minor"] for r in ep.ledger)
    groups = defaultdict(int)
    for r in ep.ledger:
        groups[r["payment_id"] or r["invoice_id"]] += r["debit_minor"] - r["credit_minor"]
    assert all(v == 0 for v in groups.values()), dict(groups)


def test_evidence_reachable():
    ep = load_public(EP_DIR)
    truth = load_truth(EP_DIR)
    for issue in truth["issues"]:
        for ev_set in issue["evidence_sets"]:
            for ident in ev_set:
                if ident.startswith("DOC-"):
                    assert ident in ep.documents
                elif ident.startswith("L-"):
                    assert ident in ep.records["ledger"]
                elif ident.startswith("FX:"):
                    assert ident in ep.records["fx"]
                else:
                    pytest.fail("неизвестный ID " + ident)
        for ident in issue["affected_ids"]:
            table = "invoices" if ident.startswith("INV-") else "payments"
            assert ident in ep.records[table]


def test_build_is_deterministic():
    # tempfile вместо tmp_path: не зависит от прав на общий каталог pytest-of-<user>
    with tempfile.TemporaryDirectory() as tmp:
        _check_build(Path(tmp))


def _check_build(tmp_path):
    a = build(tmp_path / "a")
    b = build(tmp_path / "b")
    files_a = sorted(p.relative_to(a) for p in a.rglob("*") if p.is_file())
    files_b = sorted(p.relative_to(b) for p in b.rglob("*") if p.is_file())
    assert files_a == files_b and files_a
    for rel in files_a:
        assert (a / rel).read_bytes() == (b / rel).read_bytes(), rel
        assert (a / rel).read_bytes() == (EP_DIR / rel).read_bytes(), "в репозитории устарел " + str(rel)
