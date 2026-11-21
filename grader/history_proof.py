"""Independent replay of a closed clean snapshot and a complete mutation journal.

``private/clean_history.json`` is ``{schema_version: "1.0", episode_id, tables}``,
where tables contains ledger, invoices, payments and fx immediately before error
injection. Manifest and policy are immutable and come from the public episode.

Each provenance mutation adds ``record_changes``: ordered full-record changes
``{table, record_id, before: object | null, after: object | null}``. Both mutations
and their changes are chronological. The legacy ``changes`` descriptions are not
used as a substitute for missing records. Final issue amounts/evidence are checked
separately by certificate; this module proves the actual before/after history.
"""
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import re

from common.jsonio import canonical_dumps
from grader.evidence import PublicEvidence, require


_TABLES = ("ledger", "invoices", "payments", "fx")
_IDS = {"ledger": "row_id", "invoices": "invoice_id", "payments": "payment_id"}
_FIELDS = {
    "ledger": {"row_id", "timestamp", "account", "debit_minor", "credit_minor", "currency",
               "entity_id", "invoice_id", "payment_id"},
    "invoices": {"invoice_id", "doc_id", "entity_id", "issued_at", "due_at", "currency",
                 "total_minor", "line_items"},
    "payments": {"payment_id", "doc_id", "timestamp", "currency", "amount_minor", "invoice_refs"},
    "fx": {"date", "source_currency", "target_currency", "rate"},
}
_KINDS = {"duplicate_invoice", "payment_allocation", "fx_conversion", "period_cutoff"}


def _json(value) -> str:
    # JSON equality must not confuse True, 1 and 1.0, or accept NaN/Infinity.
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _moment(value):
    require(type(value) is str, "timestamp must be a string")
    parsed = datetime.fromisoformat(value)
    require(parsed.tzinfo is not None, "timestamp lacks timezone")
    return parsed


def _record_id(table, record):
    require(type(record) is dict and _FIELDS[table] <= record.keys(), "incomplete history record")
    if table == "fx":
        return "FX:{date}:{source_currency}:{target_currency}".format(**record)
    identifier = record[_IDS[table]]
    require(type(identifier) is str and bool(identifier), "invalid history record ID")
    if table == "invoices":
        _moment(record["due_at"])
    return identifier


def _ordered(tables):
    require(type(tables) is dict and set(tables) == set(_TABLES), "history must contain exactly four tables")
    for table in _TABLES:
        require(type(tables[table]) is list, "history table must be a list")
        for record in tables[table]:
            _record_id(table, record)
    return {
        "ledger": sorted(tables["ledger"], key=lambda r: (_moment(r["timestamp"]), r["row_id"])),
        "invoices": sorted(tables["invoices"], key=lambda r: (_moment(r["issued_at"]), r["invoice_id"])),
        "payments": sorted(tables["payments"], key=lambda r: (_moment(r["timestamp"]), r["payment_id"])),
        "fx": sorted(tables["fx"], key=lambda r: (r["date"], r["source_currency"], r["target_currency"])),
    }


def _public(public, tables):
    return {"manifest": public["manifest"], "policy": public["policy"], **tables}


def _owner(table, record):
    if table == "ledger":
        # A payment's receivable row belongs to the payment, not any other payment
        # sharing the same invoice. Do not widen the mutation's scope through refs.
        return record["payment_id"] if record["payment_id"] is not None else record["invoice_id"]
    return record[_IDS[table]]


def _scope(kind, affected, table, before, after):
    require(table != "fx", "FX reference table must remain unchanged during error injection")
    owners = {_owner(table, record) for record in (before, after) if record is not None}
    require(owners <= affected, "mutation changes a document outside affected_ids")
    if table == "invoices":
        if kind == "duplicate_invoice":
            require(before is None and after is not None, "duplicate injection must create its invoice")
        else:
            require(kind == "period_cutoff" and before is not None and after is not None,
                    "this error type cannot change an invoice document")
            changed = before.keys() ^ after.keys() | {
                key for key in before.keys() & after.keys() if _json(before[key]) != _json(after[key])}
            require(changed <= {"issued_at", "due_at"}, "cutoff changed invoice fields other than dates")
    elif table == "payments":
        require(kind == "period_cutoff" and before is not None and after is not None,
                "this error type cannot change a payment document")
        changed = before.keys() ^ after.keys() | {
            key for key in before.keys() & after.keys() if _json(before[key]) != _json(after[key])}
        require(changed <= {"timestamp"}, "cutoff changed payment fields other than its timestamp")
    return owners


def _signature(state, identifier):
    return {table: {key: row for key, row in state[table].items() if _owner(table, row) == identifier}
            for table in _IDS}


def _assert_no_clean_duplicates(data):
    """Apply the same documented duplicate definition as the final certificate."""
    dates = {}
    for doc in data.invoices.values():
        book = data.book(doc)
        if book.get(("1200", doc["invoice_id"]), 0) <= 0 or book.get(("4000", None), 0) >= 0:
            continue
        key = _json([doc[field] for field in ("entity_id", "currency", "total_minor", "line_items")])
        dates.setdefault(key, []).append(data.local(doc["issued_at"]).date())
    for group in dates.values():
        ordered = sorted(group)
        require(all((right - left).days > 5 for left, right in zip(ordered, ordered[1:])),
                "clean history already contains duplicate invoices")


def verify_history(public, truth, provenance=None, clean_history=None) -> dict:
    """Return verified/failed/not_checked; never infer missing original records.

    No inputs are modified. A missing snapshot or provenance is not_checked;
    supplied but malformed/incomplete proof is failed. A verified history is not
    a replacement for certificate's independent proof of each final finding.
    """
    report = {"status": "not_checked", "valid": None, "errors": [],
              "clean_history_sha256": None, "clean_ledger_balance_minor": None,
              "clean_documents_checked": 0, "mutations_checked": 0, "record_changes_checked": 0}
    # Missing evidence must not conceal a separately supplied malformed object.
    # Legacy provenance may lack record_changes; require it only for full replay.
    try:
        if provenance is not None:
            require(type(provenance) is dict, "provenance must be an object")
            require(type(provenance["episode_id"]) is str and type(provenance["clean_history_sha256"]) is str
                    and type(provenance["mutations"]) is list, "invalid provenance shape")
            require(all(type(item) is dict for item in provenance["mutations"]), "mutation must be an object")
        if clean_history is not None:
            require(type(clean_history) is dict, "clean history must be an object")
            require(clean_history["schema_version"] == "1.0" and type(clean_history["episode_id"]) is str,
                    "invalid clean history schema or episode ID")
            _ordered(clean_history["tables"])
    except (KeyError, ValueError, TypeError, ArithmeticError, AttributeError) as exc:
        report.update(status="failed", valid=False)
        report["errors"].append(str(exc))
        return report
    if provenance is None or clean_history is None:
        missing = [name for name, value in (("provenance", provenance), ("clean_history", clean_history))
                   if value is None]
        report["reason"] = "missing closed proof: " + ", ".join(missing)
        return report
    report.update(status="failed", valid=False)
    try:
        require(type(public) is dict and type(truth) is dict, "public and truth must be objects")
        require(type(provenance) is dict and type(clean_history) is dict, "closed proof must be objects")
        episode_id = public["manifest"]["episode_id"]
        require(all(obj["episode_id"] == episode_id for obj in (truth, provenance, clean_history)),
                "history episode ID mismatch")
        require(clean_history["schema_version"] == "1.0", "unsupported clean history schema")
        tables = deepcopy(_ordered(clean_history["tables"]))
        _json(tables)
        digest = hashlib.sha256(canonical_dumps(tables).encode("utf-8")).hexdigest()
        report["clean_history_sha256"] = digest
        expected = provenance["clean_history_sha256"]
        require(type(expected) is str and re.fullmatch(r"[0-9a-f]{64}", expected) is not None,
                "invalid clean history hash")
        require(digest == expected, "clean history hash mismatch")

        clean = PublicEvidence(_public(public, tables))
        for doc in clean.documents.values():
            clean.assert_clean(doc)
            require(clean.balance(clean.rows(doc)) == 0, "clean document is not balanced")
            report["clean_documents_checked"] += 1
        _assert_no_clean_duplicates(clean)
        report["clean_ledger_balance_minor"] = clean.balance(list(clean.ledger.values()))
        require(report["clean_ledger_balance_minor"] == 0, "clean ledger is not balanced")

        state = {table: {_record_id(table, row): row for row in tables[table]} for table in _TABLES}
        initial = deepcopy(state)
        require(type(truth["issues"]) is list and type(provenance["mutations"]) is list,
                "issues and mutations must be lists")
        issues = {}
        for item in truth["issues"]:
            require(type(item) is dict and type(item["issue_id"]) is str, "invalid truth issue")
            require(item["issue_id"] not in issues, "duplicate truth issue ID")
            require(item["type"] in _KINDS and type(item["affected_ids"]) is list,
                    "invalid issue type or affected IDs")
            require(bool(item["affected_ids"]) and all(type(key) is str and key for key in item["affected_ids"]),
                    "invalid affected IDs")
            issues[item["issue_id"]] = item
        seen, touched = set(), {}
        for mutation in provenance["mutations"]:
            require(type(mutation) is dict, "mutation must be an object")
            iid = mutation["issue_id"]
            require(iid in issues and iid not in seen, "mutation issue ID is absent or repeated")
            item = issues[iid]
            require(mutation["type"] == item["type"], "mutation type differs from truth")
            require(type(mutation.get("record_changes")) is list, "mutation lacks complete record_changes")
            affected, owners = set(item["affected_ids"]), set()
            for change in mutation["record_changes"]:
                require(type(change) is dict and set(change) == {"table", "record_id", "before", "after"},
                        "record change must contain table, record_id, before and after")
                table, identifier = change["table"], change["record_id"]
                require(table in _TABLES and type(identifier) is str, "invalid changed table or record ID")
                before, after = change["before"], change["after"]
                require(before is not None or after is not None, "empty record change")
                for record in (before, after):
                    if record is not None:
                        require(_record_id(table, record) == identifier, "changed record ID mismatch")
                require(_json(before) != _json(after), "record change has no effect")
                owners |= _scope(item["type"], affected, table, before, after)
                require(_json(state[table].get(identifier)) == _json(before), "record before differs from replay state")
                if after is None:
                    del state[table][identifier]
                else:
                    state[table][identifier] = deepcopy(after)
                report["record_changes_checked"] += 1
            # Reject malformed transient records/links too, not only the final state.
            PublicEvidence(_public(public, {table: list(rows.values()) for table, rows in state.items()}))
            seen.add(iid)
            touched[iid] = owners
            report["mutations_checked"] += 1
        require(seen == set(issues), "truth issue has no mutation")

        final_tables = _ordered({table: public[table] for table in _TABLES})
        PublicEvidence(_public(public, final_tables))
        replayed = _ordered({table: list(rows.values()) for table, rows in state.items()})
        require(_json(replayed) == _json(final_tables), "replayed tables differ from final public tables")
        for iid, item in issues.items():
            affected = set(item["affected_ids"])
            require(any(_json(_signature(initial, key)) != _json(_signature(state, key)) for key in affected),
                    "issue has no actual change between clean and final state")
            if not touched[iid]:
                scenario = item.get("scenario")
                require(type(scenario) is str and bool(scenario) and any(
                    other != iid and issues[other].get("scenario") == scenario and bool(owners & affected)
                    for other, owners in touched.items()),
                    "empty mutation is not backed by a changed document in the same combined scenario")
        report.update(status="verified", valid=True)
    except (KeyError, ValueError, TypeError, ArithmeticError, AttributeError, OverflowError) as exc:
        report["errors"].append(str(exc))
    return report
