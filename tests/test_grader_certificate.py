"""Независимые бухгалтерские примеры: расчёты заданы вручную, без генератора."""
from copy import deepcopy
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys

import pytest

from grader.certificate import certify, certify_episode
from grader.evidence import PublicEvidence
from tests.test_grader_fixtures import EPISODE_ID, issue, truth_for

EXAMPLES = Path(__file__).resolve().parents[1] / "schemas" / "examples"


def public_data():
    return {
        "manifest": json.loads((EXAMPLES / "manifest.example.json").read_text(encoding="utf-8")),
        "policy": json.loads((EXAMPLES / "records.policy.json").read_text(encoding="utf-8")),
        "invoices": [], "payments": [], "ledger": [], "fx": [],
    }


def invoice(public, number=1, amount=1000, currency="KZT", timestamp="2026-09-15T12:00:00+05:00",
            description="Услуги за сентябрь"):
    doc = {"invoice_id": f"INV-{number:08d}", "doc_id": f"DOC-{10000000 + number:08d}",
           "entity_id": "E-00000001", "issued_at": timestamp, "due_at": timestamp,
           "currency": currency, "total_minor": amount,
           "line_items": [{"description": description, "quantity": 1,
                           "unit_price_minor": amount, "amount_minor": amount}]}
    public["invoices"].append(doc)
    return doc


def payment(public, inv, number=1, amount=1000, timestamp="2026-09-20T12:00:00+05:00"):
    doc = {"payment_id": f"PAY-{number:08d}", "doc_id": f"DOC-{20000000 + number:08d}",
           "timestamp": timestamp, "currency": inv["currency"], "amount_minor": amount,
           "invoice_refs": [{"invoice_id": inv["invoice_id"], "amount_minor": abs(amount)}]}
    public["payments"].append(doc)
    return doc


def post(public, doc, postings, timestamp=None):
    rows = []
    for account, signed_amount in postings:
        row = {"row_id": f"L-{len(public['ledger']) + 1:08d}",
               "timestamp": timestamp or doc.get("issued_at", doc.get("timestamp")),
               "account": account, "debit_minor": max(signed_amount, 0),
               "credit_minor": max(-signed_amount, 0), "currency": "KZT", "entity_id": "E-00000001",
               "invoice_id": doc.get("invoice_id"), "payment_id": doc.get("payment_id")}
        if "payment_id" in doc and account == "1200":
            row["invoice_id"] = doc["invoice_refs"][0]["invoice_id"]
        public["ledger"].append(row)
        rows.append(row)
    return rows


def rate(public, date, currency="USD", value="2.5000"):
    public["fx"].append({"date": date, "source_currency": currency, "target_currency": "KZT", "rate": value})
    return f"FX:{date}:{currency}:KZT"


def declared(kind, affected, amount, evidence, currency="KZT", number=1):
    item = issue(kind, number, amount)
    item.update({"affected_ids": affected, "currency": currency, "evidence_sets": [evidence]})
    return item


def combined_duplicate():
    public = public_data()
    original = invoice(public, currency="USD")
    duplicate = invoice(public, number=2, currency="USD", timestamp="2026-09-19T12:00:00+05:00")
    rate(public, "2026-09-15")
    fx = rate(public, "2026-09-19")
    post(public, original, [("1200", 2500), ("4000", -2500)])
    rows = post(public, duplicate, [("1200", 2600), ("4000", -2600)])
    duplicate_issue = declared("duplicate_invoice", [original["invoice_id"], duplicate["invoice_id"]],
                               1000, [original["doc_id"], duplicate["doc_id"]], "USD")
    fx_issue = declared("fx_conversion", [duplicate["invoice_id"]], 100,
                       [duplicate["doc_id"], rows[0]["row_id"], fx], number=2)
    for item in (duplicate_issue, fx_issue):
        item["scenario"] = "duplicate_invoice_wrong_fx"
    return public, truth_for(duplicate_issue, fx_issue)


def combined_payment():
    public = public_data()
    inv = invoice(public, currency="USD")
    pay = payment(public, inv, timestamp="2026-10-01T00:00:00+05:00")
    rate(public, "2026-09-15", value="2.0000")
    rate(public, "2026-10-01")
    post(public, inv, [("1200", 2000), ("4000", -2000)])
    rows = post(public, pay, [("1010", 2500), ("1200", -800), ("2900", -1500), ("7900", -200)],
                timestamp="2026-09-30T23:59:59+05:00")
    allocation = declared("payment_allocation", [inv["invoice_id"], pay["payment_id"]], 600,
                          [pay["doc_id"], *(row["row_id"] for row in rows)], "USD")
    cutoff = declared("period_cutoff", [pay["payment_id"]], 1000, [pay["doc_id"], rows[0]["row_id"]],
                      "USD", number=2)
    for item in (allocation, cutoff):
        item["scenario"] = "partial_fx_payment_at_cutoff"
    return public, truth_for(allocation, cutoff)


def distractor_case(kind):
    public = public_data()
    inv = invoice(public)
    post(public, inv, [("1200", 1000), ("4000", -1000)])
    if kind == "split_payment":
        first = payment(public, inv, amount=400)
        second = payment(public, inv, number=2, amount=600)
        post(public, first, [("1010", 400), ("1200", -400)])
        post(public, second, [("1010", 600), ("1200", -600)])
        ids = [first["doc_id"], second["doc_id"]]
    elif kind == "refund":
        original = payment(public, inv)
        refund = payment(public, inv, number=2, amount=-250, timestamp="2026-09-21T12:00:00+05:00")
        post(public, original, [("1010", 1000), ("1200", -1000)])
        post(public, refund, [("1010", -250), ("1200", 250)])
        ids = [refund["doc_id"]]
    elif kind == "legit_recurring":
        previous = invoice(public, number=2, timestamp="2026-08-15T12:00:00+05:00",
                           description="Услуги за август")
        post(public, previous, [("1200", 1000), ("4000", -1000)])
        ids = [inv["doc_id"], previous["doc_id"]]
    else:
        late = payment(public, inv, timestamp="2026-09-30T23:40:00+05:00")
        post(public, late, [("1010", 1000), ("1200", -1000)])
        ids = [late["doc_id"]]
    truth = truth_for()
    truth["distractors"] = [{"distractor_id": "DST-00000001", "kind": kind, "record_ids": ids}]
    return public, truth


@pytest.mark.parametrize("factory", [combined_duplicate, combined_payment])
def test_combined_scenarios_recompute_both_errors(factory):
    public, truth = factory()
    before = deepcopy((public, truth))
    report = certify(public, truth)
    assert report["valid"], report
    assert report["fully_verified"] is False
    assert report["summary"]["issues_confirmed"] == 2
    assert report["ledger_balance_minor"] == 0
    assert report["clean_history_balance"]["status"] == "not_checked"
    assert (public, truth) == before


@pytest.mark.parametrize("kind", ["split_payment", "refund", "legit_recurring", "late_evening_local"])
def test_legal_distractors_and_tampered_books(kind):
    public, truth = distractor_case(kind)
    assert certify(public, truth)["valid"]
    # Сохраняем баланс всей книги, но искажаем проводки отвлекающего документа.
    public["ledger"][-1]["credit_minor"] += 1
    public["ledger"][-2]["debit_minor"] += 1
    report = certify(public, truth)
    assert report["valid"] is False


@pytest.mark.parametrize("factory", [combined_duplicate, combined_payment])
def test_certificate_cannot_trust_private_amount_or_forged_alternative(factory):
    public, truth = factory()
    for index in range(2):
        wrong = deepcopy(truth)
        wrong["issues"][index]["expected_amount_minor"] += 1
        assert certify(public, wrong)["issues"][index]["valid"] is False
        wrong = deepcopy(truth)
        wrong["issues"][index]["evidence_sets"].append(["DOC-99999999"])
        assert certify(public, wrong)["valid"] is False


def test_certificate_requires_all_payment_rows():
    public, truth = combined_payment()
    truth["issues"][0]["evidence_sets"][0].pop()
    report = certify(public, truth)
    assert report["issues"][0]["valid"] is False


def test_wholly_unallocated_payment_is_provable():
    public, truth = combined_payment()
    pay = public["payments"][0]
    public["ledger"] = [row for row in public["ledger"] if row["payment_id"] is None]
    rows = post(public, pay, [("1010", 2500), ("2900", -2500)], timestamp="2026-09-30T23:59:59+05:00")
    truth["issues"] = [declared("payment_allocation", [public["invoices"][0]["invoice_id"], pay["payment_id"]],
                                1000, [pay["doc_id"], *(row["row_id"] for row in rows)], "USD")]
    assert certify(public, truth)["valid"]


def test_payment_fx_conversion_uses_cash_and_document_date():
    public = public_data()
    inv = invoice(public, currency="USD")
    pay = payment(public, inv)
    rate(public, "2026-09-15", value="2.0000")
    fx = rate(public, "2026-09-20")
    post(public, inv, [("1200", 2000), ("4000", -2000)])
    rows = post(public, pay, [("1010", 2600), ("1200", -2000), ("7900", -600)])
    truth = truth_for(declared("fx_conversion", [pay["payment_id"]], 100,
                              [pay["doc_id"], rows[0]["row_id"], fx]))
    assert certify(public, truth)["valid"]
    truth["issues"][0]["evidence_sets"][0][-1] = "FX:2026-09-15:USD:KZT"
    assert certify(public, truth)["valid"] is False


@pytest.mark.parametrize("defect", ["not_duplicate", "missing_posting", "doc_alias", "unrelated_row", "correct_fx"])
def test_unproved_issue_cannot_receive_a_certificate(defect):
    public, truth = combined_duplicate()
    if defect == "not_duplicate":
        public["invoices"][1]["line_items"][0]["description"] = "Другой период услуги"
    elif defect == "missing_posting":
        public["ledger"] = public["ledger"][2:]
    elif defect == "doc_alias":
        truth["issues"][0]["affected_ids"][0] = public["invoices"][0]["doc_id"]
    elif defect == "unrelated_row":
        truth["issues"][1]["evidence_sets"][0][1] = public["ledger"][0]["row_id"]
    else:
        public["ledger"][2]["debit_minor"] = 2500
        public["ledger"][3]["credit_minor"] = 2500
    assert certify(public, truth)["valid"] is False


def test_public_policy_and_duplicate_ids_are_checked():
    public, truth = combined_duplicate()
    public["policy"]["rounding"]["mode"] = "ROUND_HALF_UP"
    assert certify(public, truth)["public_errors"]
    public, truth = combined_duplicate()
    public["invoices"].append(deepcopy(public["invoices"][0]))
    assert certify(public, truth)["public_errors"]


@pytest.mark.parametrize("rate_value,amount,expected", [("1.005", 1, 100), ("1.015", 1, 102),
                                                       ("1.005", -1, -100), ("1.015", -1, -102)])
def test_jpy_exponents_and_half_even(rate_value, amount, expected):
    public = public_data()
    rate(public, "2026-09-15", "JPY", rate_value)
    data = PublicEvidence(public)
    assert data.convert(amount, "JPY", "2026-09-15T12:00:00+05:00") == expected


@pytest.mark.parametrize("source,base", [(0, 0), (1, 2), (2, 4), (10**30, 2 * 10**30)])
def test_unique_inverse_conversion(source, base):
    public = public_data()
    rate(public, "2026-09-15", value="2.0000")
    data = PublicEvidence(public)
    assert data.source_amount(base, "USD", "2026-09-15T12:00:00+05:00") == source


def test_ambiguous_inverse_is_not_a_false_certificate():
    public = public_data()
    rate(public, "2026-09-15", value="0.1000")
    with pytest.raises(ValueError, match="uniquely recovered"):
        PublicEvidence(public).source_amount(10, "USD", "2026-09-15T12:00:00+05:00")


@pytest.mark.parametrize("timestamp,valid", [
    ("2026-10-01T00:00:00+05:00", True), ("2026-09-30T19:00:00+00:00", True),
    ("2026-09-30T23:59:59+05:00", False), ("2026-09-01T00:00:00+05:00", False),
    ("2026-08-31T23:59:59+05:00", True),
])
def test_local_period_boundaries(timestamp, valid):
    public, truth = combined_payment()
    public["payments"][0]["timestamp"] = timestamp
    truth["issues"] = [truth["issues"][1]]
    assert certify(public, truth)["valid"] is valid


def test_permutation_timezone_and_ids_preserve_certificate():
    public, truth = combined_payment()
    before = certify(public, truth)
    for name in ("invoices", "payments", "ledger", "fx"):
        public[name].reverse()
        for row in public[name]:
            for field in ("issued_at", "due_at", "timestamp"):
                if field in row:
                    row[field] = datetime.fromisoformat(row[field]).astimezone(timezone.utc).isoformat()
    truth["issues"].reverse()
    assert certify(public, truth) == before
    replacements = {}
    for row in [*public["invoices"], *public["payments"], *public["ledger"]]:
        for key in ("invoice_id", "payment_id", "doc_id", "row_id"):
            if row.get(key):
                old = row[key]
                prefix, number = old.rsplit("-", 1)
                replacements[old] = f"{prefix}-{int(number) + 100:08d}"

    def rename(value):
        if isinstance(value, dict):
            return {key: rename(item) for key, item in value.items()}
        if isinstance(value, list):
            return [rename(item) for item in value]
        return replacements.get(value, value) if isinstance(value, str) else value

    assert certify(rename(public), rename(truth)) == rename(before)


def write_episode(path, public, truth):
    (path / "public").mkdir(parents=True)
    (path / "private").mkdir()
    for name in ("manifest", "policy"):
        (path / "public" / f"{name}.json").write_text(json.dumps(public[name]), encoding="utf-8")
    (path / "private" / "truth.json").write_text(json.dumps(truth), encoding="utf-8")
    for name in ("invoices", "payments"):
        (path / "public" / f"{name}.jsonl").write_text(
            "".join(json.dumps(doc) + "\n" for doc in public[name]), encoding="utf-8")
    for name in ("ledger", "fx"):
        with (path / "public" / f"{name}.csv").open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(public[name][0]))
            writer.writeheader()
            writer.writerows(public[name])


def test_episode_cli_writes_only_private_certificate_and_returns_failure(tmp_path):
    public, truth = combined_payment()
    write_episode(tmp_path, public, truth)
    before = {path.name: path.read_bytes() for path in (tmp_path / "public").iterdir()}
    report = certify_episode(tmp_path)
    assert report["valid"]
    certificate_path = tmp_path / "private" / "certificate.json"
    assert b"\r" not in certificate_path.read_bytes()
    assert not (tmp_path / "public" / "certificate.json").exists()
    assert {path.name: path.read_bytes() for path in (tmp_path / "public").iterdir()} == before
    for valid in (True, False):
        if not valid:
            truth["issues"][0]["expected_amount_minor"] += 1
            (tmp_path / "private" / "truth.json").write_text(json.dumps(truth), encoding="utf-8")
        completed = subprocess.run([sys.executable, "-m", "grader.certificate", str(tmp_path), "--public-only"],
                                   capture_output=True, text=True, check=False)
        assert completed.returncode == (0 if valid else 1), completed.stderr
        assert json.loads(completed.stdout)["valid"] is valid


def test_full_certificate_refuses_missing_clean_history(tmp_path):
    public, truth = combined_payment()
    write_episode(tmp_path, public, truth)
    report = certify_episode(tmp_path, require_history=True)
    assert report["valid"] is False
    assert report["fully_verified"] is False
    assert report["clean_history_balance"]["status"] == "not_checked"
    assert report["summary"]["issues_confirmed"] == 2
    completed = subprocess.run([sys.executable, "-m", "grader.certificate", str(tmp_path)],
                               capture_output=True, text=True, encoding="utf-8", check=False)
    assert completed.returncode == 1, completed.stderr
    assert json.loads(completed.stdout)["history_verification_required"] is True


def test_clean_ledger_balance_is_checked_without_claiming_pre_mutation_balance():
    public, truth = distractor_case("split_payment")
    truth["distractors"] = []
    assert certify(public, truth)["valid"]
    public["ledger"][0]["debit_minor"] += 1
    report = certify(public, truth)
    assert report["valid"] is False
    assert report["ledger_balance_minor"] == 1
    assert report["unbalanced_documents"] == [public["invoices"][0]["doc_id"]]
