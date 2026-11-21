"""Независимый проверщик: python -m grader.certificate <episode>.

Сертификат остаётся в private/. Его результат не меняет награду агенту.
"""
import argparse
import json
import re
from pathlib import Path

from common.jsonio import canonical_dumps
from grader import clean_history as history_checks
from grader.evidence import PublicEvidence, read_public, require
from grader.history_proof import verify_history
from grader.truth_consistency import check_consistency, require_duplicate_coverage
from grader.validation import validate_truth


def _prove(data: PublicEvidence, kind: str, affected: list[str], evidence: set[str]) -> tuple[int, str]:
    require(all(identifier in data.records for identifier in evidence), "evidence record is not public")
    docs = [data.records[identifier] for identifier in sorted(affected)]
    require(all("doc_id" in doc and identifier in (doc.get("invoice_id"), doc.get("payment_id"))
                for identifier, doc in zip(sorted(affected), docs)), "affected IDs must be invoice/payment IDs")
    if kind == "duplicate_invoice":
        require(len(docs) == 2 and all("invoice_id" in doc and "doc_id" in doc for doc in docs),
                "duplicate needs two invoice IDs")
        left, right = docs
        require({left["doc_id"], right["doc_id"]} <= evidence, "missing invoice documents")
        require(all(left[key] == right[key] for key in ("entity_id", "currency", "total_minor", "line_items")),
                "invoices are not duplicates")
        gap = abs((data.local(left["issued_at"]).date() - data.local(right["issued_at"]).date()).days)
        require(gap <= 5, "duplicate dates exceed five days")
        for doc in docs:
            book = data.book(doc)
            require(book.get(("1200", doc["invoice_id"]), 0) > 0 and book.get(("4000", None), 0) < 0,
                    "invoice was not posted twice")
        return right["total_minor"], right["currency"]
    if kind == "payment_allocation":
        invoices = [doc for doc in docs if "invoice_id" in doc and "doc_id" in doc]
        payments = [doc for doc in docs if "payment_id" in doc and "doc_id" in doc]
        require(len(docs) == 2 and len(invoices) == len(payments) == 1, "allocation needs invoice and payment")
        invoice, payment = invoices[0], payments[0]
        rows = data.rows(payment)
        require({payment["doc_id"], *(row["row_id"] for row in rows)} <= evidence and bool(rows),
                "payment evidence must include every ledger row")
        require(payment["amount_minor"] > 0, "allocation issue requires an incoming payment")
        declared = data.allocations(payment)
        require(invoice["invoice_id"] in declared, "invoice absent from payment references")
        book = data.book(payment)
        # Отсутствие 1200 означает ноль в базовой валюте, но не обязательно
        # ноль исходной суммы: ненулевое разнесение могло округлиться до нуля.
        posted = data.recover_allocations(payment)
        amount = declared[invoice["invoice_id"]] - posted[invoice["invoice_id"]]
        require(amount > 0, "no missing payment allocation")
        return amount, payment["currency"]
    require(len(docs) == 1 and "doc_id" in docs[0], "issue needs one document ID")
    doc = docs[0]
    require(doc["doc_id"] in evidence, "missing source document")
    rows = [row for row in data.rows(doc) if row["row_id"] in evidence]
    require(bool(rows), "missing linked ledger row")
    if kind == "period_cutoff":
        require(not data.in_period(data.timestamp(doc)), "document is inside reporting period")
        require(any(data.in_period(row["timestamp"]) for row in rows), "no posting inside reporting period")
        return abs(data.amount(doc)), doc["currency"]
    require(kind == "fx_conversion", "unknown issue type")
    require(data.fx_id(doc["currency"], data.timestamp(doc)) in evidence, "missing correct-date FX record")
    expected = data.convert(data.amount(doc), doc["currency"], data.timestamp(doc))
    candidates = []
    for row in rows:
        if "invoice_id" in doc and row["account"] in ("1200", "4000"):
            posted = row["debit_minor"] - row["credit_minor"]
            if row["account"] == "4000":
                posted = -posted
        elif "payment_id" in doc and row["account"] == "1010":
            posted = row["debit_minor"] - row["credit_minor"]
        else:
            continue
        # Один evidence-row должен представлять сумму документа, а не часть.
        account_rows = [other for other in data.rows(doc) if other["account"] == row["account"]]
        require(len(account_rows) == 1, "FX proof splits the document amount across rows")
        candidates.append(abs(posted - expected))
    require(bool(candidates) and all(amount > 0 for amount in candidates), "no FX discrepancy on evidenced rows")
    require(len(set(candidates)) == 1, "FX evidence rows disagree")
    return candidates[0], data.base


def _distractor(data: PublicEvidence, item: dict) -> None:
    docs = {}
    for identifier in item["record_ids"]:
        record = data.records[identifier]
        if "doc_id" in record:
            doc = record
        elif "row_id" in record:
            doc = (data.payments[record["payment_id"]] if record["payment_id"] is not None
                   else data.invoices[record["invoice_id"]])
        else:
            raise ValueError("distractor reference is not a document or ledger row")
        docs[doc["doc_id"]] = doc
    for doc in docs.values():
        data.assert_clean(doc)
    invoices = [doc for doc in docs.values() if "invoice_id" in doc]
    payments = [doc for doc in docs.values() if "payment_id" in doc]
    kind = item["kind"]
    if kind == "split_payment":
        positive = [doc for doc in payments if doc["amount_minor"] > 0]
        require(len(positive) >= 2, "split payment needs two incoming payments")
        common = set(data.allocations(positive[0]))
        for doc in positive[1:]:
            common &= set(data.allocations(doc))
        require(any(sum(data.allocations(doc)[identifier] for doc in positive)
                    == data.invoices[identifier]["total_minor"] for identifier in common),
                "split payments do not settle one invoice")
        for identifier in sorted(common):
            data.assert_clean(data.invoices[identifier])
    elif kind == "refund":
        refunds = [doc for doc in payments if doc["amount_minor"] < 0]
        require(bool(refunds), "refund is absent")
        for refund in refunds:
            refs = data.allocations(refund)
            require(bool(refs), "refund lacks invoice references")
            originals = [doc for doc in data.payments.values() if doc["amount_minor"] > 0
                         and doc["currency"] == refund["currency"]
                         and data.local(doc["timestamp"]) <= data.local(refund["timestamp"])
                         and set(refs) <= set(data.allocations(doc))]
            originals = [doc for doc in originals if all(data.allocations(doc)[key] >= amount
                                                         for key, amount in refs.items())]
            require(bool(originals), "refund has no covering original payment")
            # Нет явной refund_of ссылки в контракте: подтверждаем покрытие, не причинную связь.
            for original in originals:
                data.assert_clean(original)
            for identifier in sorted(refs):
                paid = sum(data.allocations(doc).get(identifier, 0) for doc in data.payments.values()
                           if doc["amount_minor"] > 0 and doc["currency"] == refund["currency"]
                           and data.local(doc["timestamp"]) <= data.local(refund["timestamp"]))
                returned = sum(data.allocations(doc).get(identifier, 0) for doc in data.payments.values()
                               if doc["amount_minor"] < 0 and doc["currency"] == refund["currency"]
                               and data.local(doc["timestamp"]) <= data.local(refund["timestamp"]))
                require(returned <= paid, "refunds exceed original payments")
    elif kind == "legit_recurring":
        require(len(invoices) >= 2, "recurring case needs two invoices")
        require(len({(doc["entity_id"], doc["currency"], doc["total_minor"]) for doc in invoices}) == 1,
                "recurring invoices differ in counterparty, currency or total")
        months = {(data.local(doc["issued_at"]).year, data.local(doc["issued_at"]).month) for doc in invoices}
        descriptions = {tuple(line["description"] for line in doc["line_items"]) for doc in invoices}
        require(len(months) == len(invoices) and len(descriptions) == len(invoices),
                "recurring invoices lack distinct months and service descriptions")
    elif kind == "late_evening_local":
        require(any(data.in_period(data.timestamp(doc)) and data.local(data.timestamp(doc)).hour >= 23
                    for doc in docs.values()), "no late-evening document inside local period")
    else:
        raise ValueError("unknown distractor kind")


def _verify_final_books(data: PublicEvidence, truth: dict) -> None:
    """Полный сертификат разрешает только заявленные и доказанные отклонения.

    Баланс и точный replay сами по себе не исключают лишнюю ошибку в том же
    документе: например, неверный курс внутри мутации period_cutoff.
    """
    require_duplicate_coverage(data, truth["issues"])
    by_document = {}
    for item in truth["issues"]:
        for identifier in item["affected_ids"]:
            by_document.setdefault(identifier, []).append(item)
    for doc in data.documents.values():
        identifier = doc.get("payment_id", doc.get("invoice_id"))
        issues = by_document.get(identifier, [])
        kinds = {item["type"] for item in issues}
        book = data.book(doc)
        if "invoice_id" in doc:
            value = (book.get(("1200", identifier), 0) if "fx_conversion" in kinds else
                     data.convert(doc["total_minor"], doc["currency"], doc["issued_at"]))
            require(value >= 0, "invoice has negative receivable")
            expected = {("1200", identifier): value, ("4000", None): -value}
            entities = {doc["entity_id"]}
        else:
            allocations = data.allocations(doc)
            recovered = data.recover_allocations(doc) if "payment_allocation" in kinds else {}
            for item in issues:
                if item["type"] != "payment_allocation":
                    continue
                for invoice_id in set(item["affected_ids"]) - {identifier}:
                    require(invoice_id in allocations, "allocation issue has an unrelated invoice")
                    allocations[invoice_id] = recovered[invoice_id]
            expected = data.payment_book(doc, allocations)
            if "fx_conversion" in kinds:
                # A nonzero unallocated remainder needs proof of the applied
                # rate, which the current truth contract does not contain.
                require(sum(allocations.values()) == abs(doc["amount_minor"]),
                        "FX payment with unallocated remainder needs applied-rate proof")
                cash = book.get(("1010", None), 0)
                require(cash * doc["amount_minor"] > 0, "FX payment changed cash direction")
                expected[("1010", None)] = cash
                expected.pop(("7900", None), None)
                expected[("7900", None)] = -sum(expected.values())
            entities = {data.invoices[ref["invoice_id"]]["entity_id"] for ref in doc["invoice_refs"]} or {None}
        expected = {key: value for key, value in expected.items() if value}
        require(book == expected, "final document has postings outside the declared error types")
        require(all(row["entity_id"] in entities for row in data.rows(doc)),
                "ledger entity differs from its source document")
        if "period_cutoff" not in kinds:
            require(all(data.in_period(row["timestamp"]) == data.in_period(data.timestamp(doc))
                        for row in data.rows(doc)), "undeclared period discrepancy in final document")


def _history_report(public, truth, provenance, snapshot):
    """Основной формат main — обратимый net diff; снимок ver3 также поддержан."""
    legacy = verify_history(public, truth, provenance, snapshot) if snapshot is not None else None
    if provenance is None:
        return legacy or {"status": "not_checked", "valid": None, "reason": "provenance not provided"}
    if not isinstance(provenance, dict):
        return {"status": "failed", "valid": False, "errors": ["provenance must be an object"]}
    if (type(provenance.get("episode_id")) is not str or
            provenance["episode_id"] != public["manifest"]["episode_id"] or
            type(provenance.get("clean_history_sha256")) is not str or
            re.fullmatch(r"[0-9a-f]{64}", provenance["clean_history_sha256"]) is None or
            type(provenance.get("mutations")) is not list or
            any(type(item) is not dict for item in provenance["mutations"])):
        return {"status": "failed", "valid": False, "errors": ["malformed provenance metadata"]}
    if "clean_history_diff" not in provenance and legacy is not None:
        return legacy
    report = history_checks.check(public, provenance)
    report["valid"] = {"ok": True, "failed": False, "not_checked": None}[report["status"]]
    if legacy is not None and legacy["valid"] is not True:
        report.update(status="failed", valid=False, legacy_proof=legacy)
    return report


def certify(public: dict, truth: dict, provenance=None, *, clean_history=None,
            require_history: bool | None = None) -> dict:
    """Независимо проверяет evidence и, при наличии входов A, исходную историю.

    API без закрытого журнала проверяет только публичные доказательства.
    Если журнал передан, по умолчанию требуется полная проверка; явный False
    разрешает частичный режим, но не игнорирует повреждённое доказательство.
    Для обязательной проверки истории задайте require_history=True; fully_verified
    никогда не равен True без проверенной исходной истории и соответствия truth.
    """
    if require_history is None:
        require_history = provenance is not None or clean_history is not None
    validate_truth(truth, public["manifest"]["episode_id"])
    report = {"schema_version": "1.0", "episode_id": truth["episode_id"], "valid": False,
              "issues": [], "distractors": [], "public_errors": [],
              "history_verification_required": require_history, "fully_verified": False,
              "clean_history_balance": _history_report(public, truth, provenance, clean_history)}
    try:
        data = PublicEvidence(public)
    except (KeyError, ValueError, TypeError, ArithmeticError) as exc:
        report["public_errors"].append(str(exc))
        return report
    report["ledger_balance_minor"] = data.balance(list(data.ledger.values()))
    unbalanced = sorted(doc["doc_id"] for doc in data.documents.values() if data.balance(data.rows(doc)) != 0)
    report["unbalanced_documents"] = unbalanced
    if report["ledger_balance_minor"] != 0 or unbalanced:
        report["public_errors"].append("public ledger is not balanced by document")
    for item in sorted(truth["issues"], key=lambda item: item["issue_id"]):
        checks = []
        for ids in item["evidence_sets"]:
            check = {"evidence_ids": sorted(ids), "valid": False}
            try:
                amount, currency = _prove(data, item["type"], item["affected_ids"], set(ids))
                check.update({"computed_amount_minor": amount, "currency": currency})
                require(currency == item["currency"] and amount == item["expected_amount_minor"],
                        "recomputed amount/currency differs from truth")
                check["valid"] = True
            except (KeyError, ValueError, TypeError, ArithmeticError) as exc:
                check["error"] = str(exc)
            checks.append(check)
        report["issues"].append({"issue_id": item["issue_id"], "type": item["type"],
                                 "valid": all(check["valid"] for check in checks), "evidence_sets": checks})
    for item in sorted(truth["distractors"], key=lambda item: item["distractor_id"]):
        result = {"distractor_id": item["distractor_id"], "kind": item["kind"], "valid": False}
        try:
            _distractor(data, item)
            result["valid"] = True
        except (KeyError, ValueError, TypeError, ArithmeticError) as exc:
            result["error"] = str(exc)
        report["distractors"].append(result)
    if report["clean_history_balance"]["valid"] is True:
        try:
            _verify_final_books(data, truth)
        except (KeyError, ValueError, TypeError, ArithmeticError) as exc:
            report["public_errors"].append(str(exc))
        if isinstance(provenance, dict) and "clean_history_diff" in provenance:
            consistency = check_consistency(public, truth, provenance)
            report["truth_consistency"] = consistency
            if not consistency["valid"]:
                report["public_errors"].extend(consistency["errors"])
    evidence_valid = (not report["public_errors"] and
                      all(item["valid"] for item in report["issues"] + report["distractors"]))
    history_valid = report["clean_history_balance"]["valid"]
    report["fully_verified"] = evidence_valid and history_valid is True
    report["valid"] = (evidence_valid and history_valid is not False and
                       (not require_history or history_valid is True))
    report["summary"] = {"issues_confirmed": sum(item["valid"] for item in report["issues"]),
                         "issues_total": len(truth["issues"]),
                         "distractors_confirmed": sum(item["valid"] for item in report["distractors"]),
                         "distractors_total": len(truth["distractors"])}
    return report


def certify_episode(episode_dir: str | Path, *, require_history: bool | None = None) -> dict:
    """Читает закрытые доказательства, но проверяет их, а не доверяет журналу."""
    episode = Path(episode_dir)
    public = read_public(episode / "public")
    truth = json.loads((episode / "private" / "truth.json").read_text(encoding="utf-8"))
    history_inputs = {}
    for name in ("provenance", "clean_history"):
        path = episode / "private" / f"{name}.json"
        if path.exists():
            history_inputs[name] = json.loads(path.read_text(encoding="utf-8"))
    report = certify(public, truth, require_history=require_history, **history_inputs)
    with (episode / "private" / "certificate.json").open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(canonical_dumps(report) + "\n")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("episode", type=Path)
    parser.add_argument("--public-only", action="store_true",
                        help="разрешить отсутствие исходной истории; предоставленный журнал всё равно проверяется")
    args = parser.parse_args()
    try:
        report = certify_episode(args.episode, require_history=not args.public_only)
    except (OSError, ValueError, KeyError, TypeError, ArithmeticError) as exc:
        parser.exit(2, f"Certificate input error: {exc}\n")
    print(canonical_dumps(report))
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
