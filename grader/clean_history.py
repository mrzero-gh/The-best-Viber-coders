"""Независимая проверка исходной (чистой) истории по открытым данным и provenance.

Условие 1 блока D жюри: до внесения ошибки книги согласованы, а provenance вместе с открытыми
документами позволяет это проверить. Проверка не использует генератор:
1. из открытых таблиц убираются строки clean_history_diff.*.added, возвращаются .removed,
   у modified ставится before вместо after;
2. sha256 восстановленных таблиц сравнивается с provenance.clean_history_sha256;
3. в восстановленной книге сумма дебета равна сумме кредита и сходится каждый документ;
4. журнал согласован с открытыми данными: after совпадает с текущим значением, added есть,
   removed нет.

Запуск: python -m grader.clean_history <эпизод>
"""
import argparse
import copy
import hashlib
import json
from datetime import datetime
from pathlib import Path

from common.jsonio import canonical_dumps
from grader.evidence import read_public

TABLES = ("ledger", "invoices", "payments", "fx")
_ID = {"ledger": "row_id", "invoices": "invoice_id", "payments": "payment_id"}
_TIME = {"ledger": "timestamp", "invoices": "issued_at", "payments": "timestamp"}


def _key(table: str, row: dict) -> str:
    if table == "fx":
        return f"FX:{row['date']}:{row['source_currency']}:{row['target_currency']}"
    return row[_ID[table]]


def _sort(table: str, rows: list) -> list:
    """Тот же порядок, что в открытых файлах: (момент времени, ID); курсы - (дата, валюты)."""
    if table == "fx":
        return sorted(rows, key=lambda r: (r["date"], r["source_currency"], r["target_currency"]))
    return sorted(rows, key=lambda r: (datetime.fromisoformat(r[_TIME[table]]), r[_ID[table]]))


def digest(tables: dict) -> str:
    blob = canonical_dumps({name: tables[name] for name in TABLES})
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def reconstruct(public: dict, diff: dict) -> tuple[dict, list]:
    """Восстанавливает чистые таблицы. Возвращает (таблицы, ошибки согласованности журнала)."""
    errors, tables = [], {}
    for name in TABLES:
        part = diff.get(name, {"removed": [], "added": [], "modified": []})
        rows = {_key(name, r): r for r in copy.deepcopy(public[name])}
        for key in part["added"]:
            if key not in rows:
                errors.append(f"{name}: added {key} отсутствует в открытых данных")
            rows.pop(key, None)
        for change in part["modified"]:
            row = rows.get(change["id"])
            if row is None or row.get(change["field"]) != change["after"]:
                errors.append(f"{name}: {change['id']}.{change['field']} не совпадает с after")
                continue
            row[change["field"]] = change["before"]
        for row in part["removed"]:
            key = _key(name, row)
            if key in rows:
                errors.append(f"{name}: removed {key} всё ещё есть в открытых данных")
            rows[key] = copy.deepcopy(row)
        tables[name] = _sort(name, list(rows.values()))
    return tables, errors


def _balance(ledger: list) -> tuple[int, list]:
    total = sum(r["debit_minor"] - r["credit_minor"] for r in ledger)
    per_doc = {}
    for r in ledger:
        # проводки платежа относятся к платежу (в них есть и invoice_id), остальные - к счёту
        # ключ с пространством: invoice_id и payment_id могут совпасть как строки
        doc = ("payment", r["payment_id"]) if r["payment_id"] else ("invoice", r["invoice_id"])
        per_doc[doc] = per_doc.get(doc, 0) + r["debit_minor"] - r["credit_minor"]
    return total, sorted(str(d[1]) for d, v in per_doc.items() if v != 0)


def check(public: dict, provenance: dict) -> dict:
    """{"status": "ok"|"failed"|"not_checked", ...}. not_checked - в provenance нет журнала."""
    diff, expected = provenance.get("clean_history_diff"), provenance.get("clean_history_sha256")
    if not isinstance(diff, dict) or not expected:
        return {"status": "not_checked", "reason": "provenance has no clean_history_diff"}
    try:
        tables, errors = reconstruct(public, diff)
        total, unbalanced = _balance(tables["ledger"])
        got = digest(tables)
    except (KeyError, TypeError, ValueError) as exc:
        return {"status": "failed", "errors": [f"bad clean_history_diff: {exc}"]}
    report = {"sha256_matches": got == expected, "ledger_balance_minor": total,
              "unbalanced_documents": unbalanced, "errors": errors,
              "changes": {name: {k: len(diff[name][k]) for k in ("removed", "added", "modified")}
                          for name in TABLES if name in diff}}
    ok = report["sha256_matches"] and total == 0 and not unbalanced and not errors
    return {"status": "ok" if ok else "failed", **report}


def check_episode(episode_dir) -> dict:
    episode = Path(episode_dir)
    provenance = json.loads((episode / "private" / "provenance.json").read_text(encoding="utf-8"))
    return check(read_public(episode / "public"), provenance)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("episode", type=Path)
    args = parser.parse_args()
    report = check_episode(args.episode)
    print(canonical_dumps(report))
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
