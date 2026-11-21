"""Независимая проверка исходной (чистой) истории по открытым данным и provenance.

Условие 1 блока D жюри: до внесения ошибки книги согласованы, а provenance вместе с открытыми
документами позволяет это проверить. Проверка не использует генератор:
1. из открытых таблиц убираются строки clean_history_diff.*.added, возвращаются .removed,
   у modified ставится before вместо after;
2. sha256 восстановленных таблиц сравнивается с provenance.clean_history_sha256;
3. восстановленные документы, проводки и ссылки согласованы с policy; исходных дублей нет;
4. журнал согласован с открытыми данными: after совпадает с текущим значением, added есть,
   removed нет.

Запуск: python -m grader.clean_history <эпизод>
"""
import argparse
import copy
import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
import re

from common.jsonio import canonical_dumps
from grader.evidence import PublicEvidence, read_public, require

TABLES = ("ledger", "invoices", "payments", "fx")
_ID = {"ledger": "row_id", "invoices": "invoice_id", "payments": "payment_id"}
_TIME = {"ledger": "timestamp", "invoices": "issued_at", "payments": "timestamp"}
_FIELDS = {
    "ledger": {"row_id", "timestamp", "account", "debit_minor", "credit_minor", "currency",
               "entity_id", "invoice_id", "payment_id"},
    "invoices": {"invoice_id", "doc_id", "entity_id", "issued_at", "due_at", "currency", "total_minor", "line_items"},
    "payments": {"payment_id", "doc_id", "timestamp", "currency", "amount_minor", "invoice_refs"},
    "fx": {"date", "source_currency", "target_currency", "rate"},
}
_BAD_INPUT = (KeyError, IndexError, TypeError, ValueError, ArithmeticError, AttributeError, RecursionError)


def _json(value) -> str:
    """Exact JSON equality: unlike Python, true, 1 and 1.0 are different values."""
    def validate(item):
        if type(item) is dict:
            require(all(type(key) is str for key in item), "JSON object keys must be strings")
            for child in item.values():
                validate(child)
        elif type(item) is list:
            for child in item:
                validate(child)
        else:
            require(item is None or type(item) in (str, int, float, bool), "value is not JSON")
    validate(value)
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _string(value, label):
    require(type(value) is str and bool(value), f"{label}: expected nonempty string")


def _timestamp(value):
    _string(value, "timestamp")
    instant = datetime.fromisoformat(value)
    require(instant.tzinfo is not None, "timestamp lacks timezone")
    return instant


def _currency(value):
    require(type(value) is str and re.fullmatch(r"[A-Z]{3}", value) is not None, "invalid currency code")


def _record(table, row):
    require(type(row) is dict and _FIELDS[table] <= row.keys(), f"{table}: incomplete record")
    _json(row)
    if table == "fx":
        _string(row["date"], "FX date")
        require(re.fullmatch(r"\d{4}-\d{2}-\d{2}", row["date"]) is not None, "invalid FX date")
        date.fromisoformat(row["date"])
        _currency(row["source_currency"])
        _currency(row["target_currency"])
        require(type(row["rate"]) is str and re.fullmatch(r"[0-9]+\.[0-9]+", row["rate"]) is not None,
                "FX rate must be a decimal string")
        require(Decimal(row["rate"]) > 0, "FX rate must be positive")
        return
    _string(row[_ID[table]], "record ID")
    _currency(row["currency"])
    _timestamp(row[_TIME[table]])
    if table == "ledger":
        _string(row["account"], "account")
        for key in ("entity_id", "invoice_id", "payment_id"):
            if row[key] is not None:
                _string(row[key], key)
        require(all(type(row[key]) is int and row[key] >= 0 for key in ("debit_minor", "credit_minor")),
                "ledger amounts must be nonnegative integers")
        return
    _string(row["doc_id"], "doc_id")
    if table == "invoices":
        _string(row["entity_id"], "entity_id")
        _timestamp(row["due_at"])
        require(type(row["total_minor"]) is int and row["total_minor"] >= 0, "invalid invoice total")
        require(type(row["line_items"]) is list and bool(row["line_items"]), "invalid invoice line_items")
        for line in row["line_items"]:
            require(type(line) is dict and {"description", "quantity", "unit_price_minor", "amount_minor"} <= line.keys(),
                    "incomplete line item")
            _string(line["description"], "line item description")
            require(all(type(line[key]) is int for key in ("quantity", "unit_price_minor", "amount_minor")),
                    "line item amounts must be integers")
    else:
        require(type(row["amount_minor"]) is int, "payment amount must be an integer")
        require(type(row["invoice_refs"]) is list, "invoice_refs must be a list")
        for ref in row["invoice_refs"]:
            require(type(ref) is dict and {"invoice_id", "amount_minor"} <= ref.keys(), "incomplete invoice reference")
            _string(ref["invoice_id"], "invoice reference ID")
            require(type(ref["amount_minor"]) is int and ref["amount_minor"] >= 0, "invalid allocation amount")


def _key(table: str, row: dict) -> str:
    if table == "fx":
        return f"FX:{row['date']}:{row['source_currency']}:{row['target_currency']}"
    return row[_ID[table]]


def _sort(table: str, rows: list) -> list:
    """Тот же порядок, что в открытых файлах: (момент времени, ID); курсы - (дата, валюты)."""
    if table == "fx":
        return sorted(rows, key=lambda r: (r["date"], r["source_currency"], r["target_currency"]))
    return sorted(rows, key=lambda r: (_timestamp(r[_TIME[table]]), r[_ID[table]]))


def digest(tables: dict) -> str:
    require(type(tables) is dict and set(tables) == set(TABLES), "history must contain exactly four tables")
    blob = _json({name: _sort(name, tables[name]) for name in TABLES})
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def reconstruct(public: dict, diff: dict) -> tuple[dict, list]:
    """Восстанавливает чистые таблицы без изменения входа; errors проверяют до использования tables.

    Это разность двух состояний, поэтому повторные и пересекающиеся операции недопустимы.
    Отсутствующее поле не эквивалентно явному null. ID изменяют через removed/added.
    """
    tables = {}
    try:
        require(type(public) is dict and type(diff) is dict and set(diff) == set(TABLES),
                "clean_history_diff must contain exactly four tables")
        for name in TABLES:
            part = diff[name]
            require(type(part) is dict and set(part) == {"removed", "added", "modified"}, "invalid diff section")
            require(all(type(part[key]) is list for key in part), "diff operations must be lists")
            require(type(public[name]) is list, "public table must be a list")
            rows = {}
            for row in public[name]:
                _record(name, row)
                key = _key(name, row)
                require(key not in rows, f"{name}: duplicate public record ID")
                rows[key] = copy.deepcopy(row)
            added, removed, modified = set(), {}, set()
            for key in part["added"]:
                _string(key, "added ID")
                require(key not in added and key in rows, f"{name}: repeated or absent added ID")
                added.add(key)
            for row in part["removed"]:
                _record(name, row)
                key = _key(name, row)
                require(key not in removed and key not in rows and key not in added,
                        f"{name}: repeated or overlapping removed ID")
                removed[key] = copy.deepcopy(row)
            for change in part["modified"]:
                require(type(change) is dict and set(change) == {"id", "field", "before", "after"},
                        "invalid modified operation")
                key, field = change["id"], change["field"]
                _string(key, "modified ID")
                _string(field, "modified field")
                require(key in rows and key not in added and key not in removed, "modified ID is absent or overlaps another operation")
                require((key, field) not in modified, "repeated modified field")
                identity = {"date", "source_currency", "target_currency"} if name == "fx" else {_ID[name]}
                require(field not in identity, "record identity cannot be modified in place")
                row = rows[key]
                require(field in row and _json(row[field]) == _json(change["after"]), "modified after differs from public value")
                require(_json(change["before"]) != _json(change["after"]), "modified operation has no effect")
                row[field] = copy.deepcopy(change["before"])
                modified.add((key, field))
            for key in added:
                del rows[key]
            rows.update(removed)
            for row in rows.values():
                _record(name, row)
            tables[name] = _sort(name, list(rows.values()))
    except _BAD_INPUT as exc:
        return {}, [f"bad clean_history_diff: {exc}"]
    return tables, []


def _balance(ledger: list) -> tuple[int, list]:
    total = sum(r["debit_minor"] - r["credit_minor"] for r in ledger)
    per_doc = {}
    for r in ledger:
        # проводки платежа относятся к платежу (в них есть и invoice_id), остальные - к счёту
        doc = r["payment_id"] or r["invoice_id"]
        per_doc[doc] = per_doc.get(doc, 0) + r["debit_minor"] - r["credit_minor"]
    return total, sorted(str(d) for d, v in per_doc.items() if v != 0)


def _data(public):
    """Validate types and record ownership before doing accounting arithmetic."""
    manifest, policy = public["manifest"], public["policy"]
    require(type(manifest) is dict and type(policy) is dict, "manifest and policy must be objects")
    _json(manifest)
    _json(policy)
    _string(manifest["episode_id"], "episode_id")
    require(manifest["schema_version"] == policy["schema_version"] == "1.0", "unsupported schema version")
    _currency(manifest["base_currency"])
    require(type(manifest["timezone"]) is str and re.fullmatch(r"[+-][0-9]{2}:[0-9]{2}", manifest["timezone"]) is not None,
            "invalid manifest timezone")
    require(type(manifest["currency_exponents"]) is dict, "currency_exponents must be an object")
    for currency in manifest["currency_exponents"]:
        _currency(currency)
    require(policy["period"]["start_inclusive"] is True and policy["period"]["end_exclusive"] is True,
            "period flags must be booleans")
    tolerance = policy["amount_tolerance_minor"]
    require(type(tolerance) is int and tolerance >= 0, "amount tolerance must be a nonnegative integer")
    if "amount_tolerance_minor_by_type" in policy:
        tolerances = policy["amount_tolerance_minor_by_type"]
        require(type(tolerances) is dict and all(type(value) is int and value >= 0 for value in tolerances.values()),
                "invalid per-type amount tolerances")
    for table in TABLES:
        require(type(public[table]) is list, "table must be a list")
        for row in public[table]:
            _record(table, row)
    data = PublicEvidence(public)
    for row in data.ledger.values():
        if row["payment_id"] is not None:
            payment = data.payments[row["payment_id"]]
            refs = payment["invoice_refs"]
            entity = data.invoices[refs[0]["invoice_id"]]["entity_id"] if refs else None
            if row["account"] == "1200":
                require(row["invoice_id"] in data.allocations(payment), "receivable row references another payment's invoice")
            else:
                require(row["invoice_id"] is None, "non-receivable payment row has an invoice link")
        else:
            entity = data.invoices[row["invoice_id"]]["entity_id"]
        require(row["entity_id"] == entity, "ledger entity differs from the document owner")
    return data


def _no_duplicates(data):
    """Use the same duplicate definition as certificate, preserving line_items order."""
    groups = {}
    for doc in data.invoices.values():
        book = data.book(doc)
        if book.get(("1200", doc["invoice_id"]), 0) <= 0 or book.get(("4000", None), 0) >= 0:
            continue
        key = _json([doc[field] for field in ("entity_id", "currency", "total_minor", "line_items")])
        groups.setdefault(key, []).append(data.local(doc["issued_at"]).date())
    for dates in groups.values():
        dates.sort()
        require(all((right - left).days > 5 for left, right in zip(dates, dates[1:])),
                "clean history already contains duplicate invoices")


def check(public: dict, provenance: dict | None) -> dict:
    """ok requires a complete, valid proof; missing diff is not_checked, malformed proof failed."""
    report = {"status": "failed", "sha256_matches": False, "ledger_balance_minor": None,
              "unbalanced_documents": [], "errors": [], "changes": {}, "documents_checked": 0}
    if provenance is None:
        return {"status": "not_checked", "reason": "provenance not provided"}
    try:
        require(type(provenance) is dict, "provenance must be an object")
        if "clean_history_diff" not in provenance:
            return {"status": "not_checked", "reason": "provenance has no clean_history_diff"}
        require(type(public) is dict, "public must be an object")
        episode_id = public["manifest"]["episode_id"]
        _string(episode_id, "episode_id")
        require(type(provenance["episode_id"]) is str and provenance["episode_id"] == episode_id,
                "provenance episode ID mismatch")
        diff, expected = provenance["clean_history_diff"], provenance["clean_history_sha256"]
        require(type(expected) is str and re.fullmatch(r"[0-9a-f]{64}", expected) is not None,
                "invalid clean history hash")
        tables, errors = reconstruct(public, diff)
        if errors:
            report["errors"] = errors
            return report
        total, unbalanced = _balance(tables["ledger"])
        report.update(sha256_matches=digest(tables) == expected, ledger_balance_minor=total,
                      unbalanced_documents=unbalanced,
                      changes={name: {key: len(diff[name][key]) for key in ("removed", "added", "modified")}
                               for name in TABLES})
        require(report["sha256_matches"], "clean history hash mismatch")
        _data(public)  # malformed final records cannot be hidden by removing them in the diff
        clean = _data({**public, **tables})
        require(total == 0 and not unbalanced, "clean ledger is not balanced by document")
        for doc in clean.documents.values():
            clean.assert_clean(doc)
            report["documents_checked"] += 1
        _no_duplicates(clean)
        report["status"] = "ok"
    except _BAD_INPUT as exc:
        report["errors"].append(str(exc))
    return report


def check_episode(episode_dir) -> dict:
    episode = Path(episode_dir)
    provenance = json.loads((episode / "private" / "provenance.json").read_text(encoding="utf-8"))
    return check(read_public(episode / "public"), provenance)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("episode", type=Path)
    args = parser.parse_args()
    try:
        report = check_episode(args.episode)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        parser.exit(2, f"Clean history input error: {exc}\n")
    print(canonical_dumps(report))
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
