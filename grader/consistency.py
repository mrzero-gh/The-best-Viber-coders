"""Правильность чистой истории, полнота эталона и провенанс: python -m grader.consistency <эпизод>.

Условия 1–3 блока D жюри. Хэш и баланс (grader.clean_history) подтверждают, что чистая история
восстановлена точно, но не что она верна по правилам учёта и что эталон описывает все изменения.
Здесь это проверяется независимо от генератора, matcher и награды:

1. Провенанс относится к этому эпизоду (`episode_id` манифеста), и его `mutations` описывают ровно
   ошибки эталона: те же `issue_id` и типы, у каждой непустой список изменений.
2. Чистая история верна по правилам контракта: каждый документ проведён по своей дате, по курсу
   даты документа и с распределениями из выписки (`PublicEvidence.assert_clean`); каждое ненулевое
   распределение даёт строку Кт 1200 не меньше 1 минимальной единицы базы (поэтому в изменённой книге
   «по счёту нет строки 1200» однозначно значит «разнесено 0»); дублей счетов нет; курсы не менялись.
3. Каждое отличие изменённого эпизода от чистого покрыто ошибкой эталона своего типа, и каждая
   ошибка эталона опирается на реальное изменение:
   - новый документ - только копия из duplicate_invoice (проведена по правилам или с одной ошибкой
     курса); других дублей в открытых данных нет;
   - сдвиг дат документа или проводок - только period_cutoff, и документ перепроведён по правилам
     своей новой даты (иначе это незаявленная ошибка курса);
   - fx_conversion - документ проведён по одному неверному курсу, распределения и счета учёта те же;
   - payment_allocation - у платежа меняется ровно распределение по заявленному счёту
     (восстанавливается совместно по всем строкам платежа);
   - документ без ошибки совпадает с чистым.

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


def _duplicates(data: PublicEvidence) -> set:
    """Пары счетов-дублей: один контрагент, валюта, сумма и строки, даты не дальше 5 дней."""
    seen, pairs = {}, set()
    for invoice in sorted(data.invoices.values(), key=lambda doc: doc["invoice_id"]):
        key = (invoice["entity_id"], invoice["currency"], invoice["total_minor"],
               canonical_dumps(invoice["line_items"]))
        for other in seen.get(key, []):
            gap = abs((data.local(invoice["issued_at"]).date() - data.local(other["issued_at"]).date()).days)
            if gap <= 5:
                pairs.add((other["invoice_id"], invoice["invoice_id"]))
        seen.setdefault(key, []).append(invoice)
    return pairs


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
    for first, second in sorted(_duplicates(clean)):
        errors.append(f"clean {second}: duplicate of {first}")


def _payment_change(data: PublicEvidence, doc: dict, invoice_ids: set) -> None:
    """У платежа с payment_allocation меняется только распределение по заявленному счёту.

    Распределения по заявленным счетам восстанавливаются совместно по всем строкам платежа
    (`PublicEvidence.allocation_ranges`), остальные равны выписке."""
    declared = data.allocations(doc)
    require(bool(invoice_ids) and invoice_ids <= set(declared), "declared invoice absent from payment references")
    book = data.book(doc)
    fixed = {i: amount for i, amount in declared.items() if i not in invoice_ids}
    recovered = data.allocation_ranges(doc, book, fixed)
    require(all(low == high for i, (low, high) in recovered["ranges"].items() if i in invoice_ids),
            "allocation cannot be uniquely recovered after rounding")
    posted = recovered["assignment"]
    require(book == data.payment_book(doc, posted), "payment postings change more than the declared allocation")
    require(any(posted[i] < declared[i] for i in invoice_ids), "allocation is not reduced")


def _fx_change(data: PublicEvidence, doc: dict) -> None:
    """Ошибка курса: документ проведён по одному неверному курсу, остальное - по правилам.

    Счёт: ровно Дт 1200 / Кт 4000 на одну и ту же сумму, отличную от верной. Платёж: Дт 1010 не по
    курсу даты платежа, остаток 2900 пересчитан тем же курсом, строки 1200 - распределения выписки
    по курсу дат счетов, разница - 7900. Распределения и счета учёта меняться не могут."""
    book = data.book(doc)
    rule = _rule_book(data, doc)
    if "invoice_id" in doc:
        value = book.get(("1200", doc["invoice_id"]), 0)
        require(set(book) == {("1200", doc["invoice_id"]), ("4000", None)} and value > 0
                and book[("4000", None)] == -value, "FX error changes more than the conversion rate")
        require(value != rule.get(("1200", doc["invoice_id"]), 0), "FX error does not change the posted amount")
        return
    sign = 1 if doc["amount_minor"] >= 0 else -1
    cash = book.get(("1010", None), 0)
    require(sign * cash > 0, "FX error changes the cash posting direction")
    require(cash != rule.get(("1010", None), 0), "FX error does not change the posted amount")
    rest = book.get(("2900", None), 0)
    total = abs(doc["amount_minor"])
    remainder = total - sum(data.allocations(doc).values())
    # 1010 = round(total·k), 2900 = round(remainder·k) с одним и тем же неверным курсом k
    require(-sign * rest >= 0 and abs(abs(cash) * remainder - abs(rest) * total) * 2 <= remainder + total,
            "FX error converts the unallocated remainder at another rate")
    expected = {("1010", None): cash, **{key: value for key, value in rule.items() if key[0] == "1200"},
                ("2900", None): rest}
    expected[("7900", None)] = -sum(expected.values())
    require(book == {key: value for key, value in expected.items() if value},
            "FX error changes more than the conversion rate")


def _own_issues(public: PublicEvidence, clean: PublicEvidence, truth: dict, errors: list):
    """Ошибки по документам, к которым они относятся. Счёт в payment_allocation только указывает
    распределение платежа; оригинал дубля не меняется - меняется (появляется) только копия."""
    own, copies, pairs = {}, set(), set()
    for issue in truth["issues"]:
        kind, affected = issue["type"], issue["affected_ids"]
        if kind == "duplicate_invoice":
            fresh = [i for i in affected if i not in clean.invoices]
            if len(fresh) != 1 or len(affected) != 2:
                errors.append(f"{issue['issue_id']}: duplicate needs exactly one invoice absent from clean history")
            copies.update(fresh)
            pairs.add(tuple(sorted(affected)))
        elif kind == "payment_allocation":
            payments = [i for i in affected if i in public.payments or i in clean.payments]
            if len(payments) != 1:
                errors.append(f"{issue['issue_id']}: allocation needs exactly one payment")
            for payment in payments:
                own.setdefault(payment, {}).setdefault(kind, set()).update(i for i in affected if i != payment)
        else:
            if len(affected) != 1:
                errors.append(f"{issue['issue_id']}: {kind} needs exactly one document")
            for identifier in affected:
                own.setdefault(identifier, {}).setdefault(kind, set())
    return own, copies, pairs


def _check_document(public: PublicEvidence, clean: PublicEvidence, name: str, kinds: dict, copies: set) -> None:
    before = clean.invoices.get(name) or clean.payments.get(name)
    after = public.invoices.get(name) or public.payments.get(name)
    require(after is not None, "document removed from public data")
    if before is None:
        require(name in copies, "new document is not a declared duplicate")
        require(set(kinds) <= {"fx_conversion"}, f"new document has issues {sorted(kinds)}")
        if "fx_conversion" in kinds:
            _fx_change(public, after)
        else:
            public.assert_clean(after)
        require(all(row["timestamp"] == public.timestamp(after) for row in public.rows(after)),
                "posting dates differ from document date")
        return
    require(name not in copies, "declared duplicate copy exists in clean history")
    changed = {key for key in set(before) | set(after) if before.get(key) != after.get(key)}
    require(changed <= set(_DOC_TIME), f"document fields changed: {sorted(changed)}")
    require(not changed or "period_cutoff" in kinds, "document date changed without period_cutoff")
    old, new = _postings(clean, before), _postings(public, after)
    if not kinds:
        require(old == new, "postings changed without a declared issue")
        return
    # каждая заявленная ошибка опирается на реальное изменение; правила проверяются всегда,
    # в том числе когда после сдвига даты проводки остались прежними
    require(old != new or bool(changed), "declared issue has no change in public data")
    if "period_cutoff" in kinds:
        require(bool(changed) or {row["timestamp"] for row in public.rows(after)}
                != {row["timestamp"] for row in clean.rows(before)}, "period_cutoff changes no date")
    if "fx_conversion" in kinds:
        require("payment_allocation" not in kinds, "FX and allocation errors on one payment")
        _fx_change(public, after)
    elif "payment_allocation" in kinds:
        _payment_change(public, after, kinds["payment_allocation"])
    else:
        # только period_cutoff: документ перепроведён по своей новой дате (курс этой даты),
        # меняются даты проводок, но не правило пересчёта - иначе это незаявленная ошибка курса
        require(public.book(after) == _rule_book(public, after),
                "postings do not follow policy for the document date (undeclared FX change)")
    if "period_cutoff" not in kinds:
        times = {row["timestamp"] for row in public.rows(after)}
        require(times <= {public.timestamp(after)}, "posting dates changed without period_cutoff")


def _check_changes(public: PublicEvidence, clean: PublicEvidence, truth: dict, errors: list) -> None:
    own, copies, pairs = _own_issues(public, clean, truth, errors)
    require(public.fx == clean.fx, "FX table differs from clean history")
    names = set(clean.invoices) | set(clean.payments) | set(public.invoices) | set(public.payments)
    for name in sorted(names | set(own)):
        try:
            _check_document(public, clean, name, own.get(name, {}), copies)
        except (KeyError, ValueError, TypeError, ArithmeticError) as exc:
            errors.append(f"{name}: {exc}")
    for first, second in sorted(_duplicates(public) - pairs):
        errors.append(f"{second}: undeclared duplicate of {first}")


def _check_provenance(public: dict, truth: dict, provenance: dict, errors: list) -> None:
    """Провенанс относится к этому эпизоду и описывает ровно ошибки эталона."""
    if provenance.get("episode_id") != public["manifest"]["episode_id"]:
        errors.append("provenance episode_id differs from manifest")
    mutations = provenance.get("mutations")
    if not isinstance(mutations, list):
        errors.append("provenance has no mutations list")
        return
    issues = {issue["issue_id"]: issue["type"] for issue in truth["issues"]}
    seen = {}
    for item in mutations:
        if not isinstance(item, dict) or not isinstance(item.get("changes"), list) or not item["changes"]:
            errors.append("provenance mutation without changes")
            continue
        seen.setdefault(str(item.get("issue_id")), []).append(item.get("type"))
    for issue_id, kinds in sorted(seen.items()):
        if issue_id not in issues:
            errors.append(f"provenance mutation {issue_id} is not in truth")
        elif kinds != [issues[issue_id]]:
            errors.append(f"{issue_id}: provenance mutation type differs from truth")
    for issue_id in sorted(set(issues) - set(seen)):
        errors.append(f"{issue_id}: no provenance mutation")


def check(public: dict, truth: dict, provenance: dict | None) -> dict:
    """{"status": "ok"|"failed"|"not_checked", "errors": [...]}"""
    diff = (provenance or {}).get("clean_history_diff")
    if not isinstance(diff, dict):
        return {"status": "not_checked", "reason": "provenance has no clean_history_diff", "errors": []}
    errors = []
    try:
        _check_provenance(public, truth, provenance, errors)
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
