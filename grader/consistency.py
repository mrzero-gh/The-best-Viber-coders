"""Правильность чистой истории и полнота эталона: python -m grader.consistency <эпизод>.

Условия 1–3 блока D жюри. Хэш и баланс (grader.clean_history) подтверждают, что чистая история
восстановлена точно, но не что она верна по правилам учёта и что эталон описывает все изменения.
Здесь это проверяется независимо от генератора, matcher и награды:

1. Чистая история верна по правилам контракта: каждый документ проведён по своей дате, по курсу
   даты документа и с распределениями из выписки (`PublicEvidence.assert_clean`); каждое ненулевое
   распределение даёт строку Кт 1200 не меньше 1 минимальной единицы базы (поэтому в изменённой книге
   «по счёту нет строки 1200» однозначно значит «разнесено 0»); дублей счетов нет; курсы не менялись.
2. Каждое отличие изменённого эпизода от чистого покрыто ошибкой эталона своего типа:
   новый документ - только копия из duplicate_invoice; сдвиг дат документа или проводок - только
   period_cutoff; другие суммы проводок - только fx_conversion или payment_allocation (у платежа
   меняется ровно распределение по заявленному счёту). Документ без ошибки совпадает с чистым.

Файлы эпизода не меняются; результат входит в сертификат (`grader.certificate`).
"""
import argparse
import json
from pathlib import Path

from common.jsonio import canonical_dumps
from grader import clean_history
from grader.evidence import PublicEvidence, read_public, require

_DOC_TIME = ("issued_at", "due_at", "timestamp")


def _doc_id(doc: dict) -> str:
    return doc["invoice_id"] if "invoice_id" in doc else doc["payment_id"]


def _postings(data: PublicEvidence, doc: dict) -> list:
    return sorted((r["account"], r["invoice_id"] or "", r["debit_minor"], r["credit_minor"], r["timestamp"])
                  for r in data.rows(doc))


def _rule_book(data: PublicEvidence, doc: dict) -> dict:
    """Проводки документа по правилам policy: курс даты документа, распределения из выписки."""
    if "payment_id" in doc:
        return data.payment_book(doc, data.allocations(doc))
    value = data.convert(doc["total_minor"], doc["currency"], doc["issued_at"])
    return {key: amount for key, amount in {("1200", doc["invoice_id"]): value, ("4000", None): -value}.items()
            if amount}


def _check_clean(clean: PublicEvidence, errors: list) -> None:
    for doc in clean.documents.values():
        name = _doc_id(doc)
        try:
            clean.assert_clean(doc)
            require(all(row["timestamp"] == clean.timestamp(doc) for row in clean.rows(doc)),
                    "posting date differs from document date")
            if "payment_id" in doc:
                for invoice_id, amount in clean.allocations(doc).items():
                    invoice = clean.invoices[invoice_id]
                    require(amount == 0 or clean.convert(amount, doc["currency"], invoice["issued_at"]) != 0,
                            "allocation is too small to be posted to 1200")
        except (KeyError, ValueError, TypeError, ArithmeticError) as exc:
            errors.append(f"clean {name}: {exc}")
    seen = {}
    for invoice in clean.invoices.values():
        key = (invoice["entity_id"], invoice["currency"], invoice["total_minor"],
               canonical_dumps(invoice["line_items"]))
        for other in seen.get(key, []):
            gap = abs((clean.local(invoice["issued_at"]).date() - clean.local(other["issued_at"]).date()).days)
            if gap <= 5:
                errors.append(f"clean {invoice['invoice_id']}: duplicate of {other['invoice_id']}")
        seen.setdefault(key, []).append(invoice)


def _payment_change(data: PublicEvidence, doc: dict, invoice_ids: set, errors: list) -> None:
    """У платежа с payment_allocation меняется только распределение по заявленному счёту."""
    declared = data.allocations(doc)
    book = data.book(doc)
    posted = {}
    for invoice_id, amount in declared.items():
        line = book.get(("1200", invoice_id))
        if invoice_id in invoice_ids:
            posted[invoice_id] = 0 if line is None else data.source_amount(
                abs(line), doc["currency"], data.invoices[invoice_id]["issued_at"])
        else:
            posted[invoice_id] = amount
    require(book == data.payment_book(doc, posted), "payment postings change more than the declared allocation")
    require(any(posted[i] < declared[i] for i in invoice_ids if i in declared), "allocation is not reduced")


def _check_changes(public: PublicEvidence, clean: PublicEvidence, truth: dict, errors: list) -> None:
    types, copies = {}, set()
    for issue in truth["issues"]:
        for identifier in issue["affected_ids"]:
            types.setdefault(identifier, set()).add(issue["type"])
        if issue["type"] == "duplicate_invoice":
            fresh = [i for i in issue["affected_ids"] if i not in clean.invoices]
            if len(fresh) != 1:
                errors.append(f"{issue['issue_id']}: duplicate needs exactly one invoice absent from clean history")
            copies.update(fresh)
    require(public.fx == clean.fx, "FX table differs from clean history")
    names = set(clean.invoices) | set(clean.payments) | set(public.invoices) | set(public.payments)
    for name in sorted(names):
        before = clean.invoices.get(name) or clean.payments.get(name)
        after = public.invoices.get(name) or public.payments.get(name)
        kinds = types.get(name, set())
        try:
            require(after is not None, "document removed from public data")
            if before is None:
                require(name in copies, "new document is not a declared duplicate")
                if "fx_conversion" not in kinds:
                    public.assert_clean(after)
                continue
            changed = {key for key in set(before) | set(after) if before.get(key) != after.get(key)}
            require(changed <= set(_DOC_TIME), f"document fields changed: {sorted(changed)}")
            require(not changed or "period_cutoff" in kinds, "document date changed without period_cutoff")
            old, new = _postings(clean, before), _postings(public, after)
            if old == new:
                continue
            require(bool(kinds), "postings changed without a declared issue")
            if "fx_conversion" in kinds:
                pass  # расхождение курса доказывает _prove по строкам-доказательствам
            elif "payment_allocation" in kinds:
                invoices = {i for issue in truth["issues"] if issue["type"] == "payment_allocation"
                            and name in issue["affected_ids"] for i in issue["affected_ids"] if i != name}
                _payment_change(public, after, invoices, errors)
            else:
                # только period_cutoff: документ перепроведён по своей новой дате (курс этой даты),
                # меняются даты проводок, но не правило пересчёта - иначе это незаявленная ошибка курса
                require(public.book(after) == _rule_book(public, after),
                        "postings do not follow policy for the document date (undeclared FX change)")
            if "period_cutoff" not in kinds:
                times = {row["timestamp"] for row in public.rows(after)}
                require(times <= {public.timestamp(after)}, "posting dates changed without period_cutoff")
        except (KeyError, ValueError, TypeError, ArithmeticError) as exc:
            errors.append(f"{name}: {exc}")


def check(public: dict, truth: dict, provenance: dict | None) -> dict:
    """{"status": "ok"|"failed"|"not_checked", "errors": [...]}"""
    diff = (provenance or {}).get("clean_history_diff")
    if not isinstance(diff, dict):
        return {"status": "not_checked", "reason": "provenance has no clean_history_diff", "errors": []}
    errors = []
    try:
        tables, journal = clean_history.reconstruct(public, diff)
        errors += journal
        clean = PublicEvidence({"manifest": public["manifest"], "policy": public["policy"], **tables})
        current = PublicEvidence(public)
        _check_clean(clean, errors)
        _check_changes(current, clean, truth, errors)
    except (KeyError, ValueError, TypeError, ArithmeticError) as exc:
        errors.append(str(exc))
    return {"status": "failed" if errors else "ok", "errors": errors}


def check_episode(episode_dir) -> dict:
    episode = Path(episode_dir)
    truth = json.loads((episode / "private" / "truth.json").read_text(encoding="utf-8"))
    prov = episode / "private" / "provenance.json"
    provenance = json.loads(prov.read_text(encoding="utf-8")) if prov.exists() else None
    return check(read_public(episode / "public"), truth, provenance)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("episode", type=Path)
    args = parser.parse_args()
    report = check_episode(args.episode)
    print(canonical_dumps(report))
    return 0 if report["status"] != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
