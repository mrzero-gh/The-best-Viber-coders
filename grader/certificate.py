"""Независимый проверщик: python -m grader.certificate <episode>.

Сертификат остаётся в private/. Его результат не меняет награду агенту.
"""
import argparse
import itertools
import json
from pathlib import Path

from common.jsonio import canonical_dumps
from grader import clean_history, consistency
from grader.evidence import PublicEvidence, read_public, require
from grader.validation import validate_truth


def _choices(data: PublicEvidence, affected: list[str]) -> list[list[dict]]:
    """Варианты прочтения affected_ids: invoice_id и payment_id - разные пространства, и одна строка
    может быть ID и счёта, и платежа. Доказательство принимается, если его даёт хотя бы один вариант
    и все доказавшие варианты дают одну сумму."""
    options = [data.doc_options(identifier) for identifier in sorted(affected)]
    require(all(options), "affected IDs must be invoice/payment IDs")
    return [list(combo) for combo in itertools.islice(itertools.product(*options), 64)]


def _prove(data: PublicEvidence, kind: str, affected: list[str], evidence: set[str]) -> tuple[int, str]:
    require(all(identifier in data.ids for identifier in evidence), "evidence record is not public")
    results, errors = set(), []
    for docs in _choices(data, affected):
        try:
            results.add(_prove_docs(data, kind, docs, evidence))
        except (KeyError, ValueError, TypeError, ArithmeticError) as exc:
            errors.append(str(exc))
    require(bool(results), errors[0] if errors else "no proof")
    require(len(results) == 1, "affected IDs are ambiguous across invoice and payment IDs")
    return results.pop()


def _prove_docs(data: PublicEvidence, kind: str, docs: list[dict], evidence: set[str]) -> tuple[int, str]:
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
        # нет строки 1200 по счёту - по нему ничего не разнесено: по контракту каждое ненулевое
        # распределение даёт строку Кт 1200 не меньше 1 minor базы (grader.consistency проверяет это
        # на чистой истории, а у изменённого платежа - что сменилось только это распределение);
        # есть строка - разнесённая сумма восстанавливается обратным пересчётом совместно по всем
        # строкам платежа (1200 по каждому счёту и остаток 2900) и должна быть однозначной
        recovered = data.allocation_ranges(payment, book)
        low, high = recovered["ranges"][invoice["invoice_id"]]
        require(low == high, "allocation cannot be uniquely recovered after rounding")
        require(data.payment_book(payment, recovered["assignment"]) == book,
                "unallocated remainder or FX postings are inconsistent")
        amount = declared[invoice["invoice_id"]] - low
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


def _distractor_options(data: PublicEvidence, identifier: str) -> list[dict]:
    """Документ по ID отвлекающего случая: invoice_id, payment_id, doc_id или row_id (пространства разные)."""
    options = data.doc_options(identifier)
    if identifier in data.documents:
        options.append(data.documents[identifier])
    row = data.ledger.get(identifier)
    if row is not None:
        options.append(data.payments[row["payment_id"]] if row["payment_id"] is not None
                       else data.invoices[row["invoice_id"]])
    unique = {doc["doc_id"]: doc for doc in options}
    require(bool(unique), "distractor reference is not a document or ledger row")
    return list(unique.values())


def _distractor(data: PublicEvidence, item: dict) -> None:
    """Отвлекающий случай подтверждён, если его подтверждает хотя бы одно прочтение ID."""
    options = [_distractor_options(data, identifier) for identifier in item["record_ids"]]
    errors = []
    for combo in itertools.islice(itertools.product(*options), 64):
        try:
            _distractor_docs(data, item, {doc["doc_id"]: doc for doc in combo})
            return
        except (KeyError, ValueError, TypeError, ArithmeticError) as exc:
            errors.append(str(exc))
    raise ValueError(errors[0])


def _distractor_docs(data: PublicEvidence, item: dict, docs: dict) -> None:
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


def certify(public: dict, truth: dict, provenance: dict | None = None, require_history: bool = True) -> dict:
    """Проверяет все допустимые evidence_sets, не используя суммы truth в пересчёте.

    По умолчанию valid требует доказанной чистой истории (хэш, баланс, правила учёта, провенанс):
    без provenance или без журнала эпизод не сертифицирован. require_history=False - только для
    ручных мини-эпизодов в тестах, где журнала нет; тогда не допускается лишь status "failed"."""
    validate_truth(truth, public["manifest"]["episode_id"])
    report = {"schema_version": "1.0", "episode_id": truth["episode_id"], "valid": False,
              "issues": [], "distractors": [], "public_errors": [],
              "clean_history_balance": (clean_history.check(public, provenance) if provenance is not None else
                                        {"status": "not_checked", "reason": "provenance not provided"}),
              "clean_history_rules": (consistency.check(public, truth, provenance) if provenance is not None else
                                      {"status": "not_checked", "reason": "provenance not provided", "errors": []})}
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
    history = (report["clean_history_balance"]["status"], report["clean_history_rules"]["status"])
    history_ok = all(status == "ok" for status in history) if require_history else "failed" not in history
    report["valid"] = (not report["public_errors"] and history_ok and
                       all(item["valid"] for item in report["issues"] + report["distractors"]))
    report["summary"] = {"issues_confirmed": sum(item["valid"] for item in report["issues"]),
                         "issues_total": len(truth["issues"]),
                         "distractors_confirmed": sum(item["valid"] for item in report["distractors"]),
                         "distractors_total": len(truth["distractors"])}
    return report


def certify_episode(episode_dir: str | Path, require_history: bool = True) -> dict:
    """Читает public/*, private/truth.json и private/provenance.json. Provenance не принимается на веру:
    чистая история восстанавливается по открытым данным и сверяется с clean_history_sha256."""
    episode = Path(episode_dir)
    public = read_public(episode / "public")
    truth = json.loads((episode / "private" / "truth.json").read_text(encoding="utf-8"))
    prov_path = episode / "private" / "provenance.json"
    provenance = json.loads(prov_path.read_text(encoding="utf-8")) if prov_path.exists() else None
    report = certify(public, truth, provenance, require_history)
    with (episode / "private" / "certificate.json").open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(canonical_dumps(report) + "\n")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("episode", type=Path)
    parser.add_argument("--no-history", action="store_true",
                        help="ручной эпизод без provenance: не требовать доказательства чистой истории")
    args = parser.parse_args()
    try:
        report = certify_episode(args.episode, require_history=not args.no_history)
    except (OSError, ValueError, KeyError, TypeError, ArithmeticError) as exc:
        parser.exit(2, f"Certificate input error: {exc}\n")
    print(canonical_dumps(report))
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
