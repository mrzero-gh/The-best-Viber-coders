"""Independent consistency of truth with the generator's reversible net diff.

This is not a second matcher. It checks which documents actually changed,
immutable reference data, the issue/mutation correspondence, and all observable
duplicate pairs. Certificate checks the final monetary deviations separately.
The hash commits four tables; it contains no historical manifest/policy snapshot.
"""
import json
from itertools import combinations

from grader.clean_history import TABLES, digest, reconstruct
from grader.evidence import PublicEvidence, require


_KINDS = {"duplicate_invoice", "payment_allocation", "fx_conversion", "period_cutoff"}
_IDS = {"ledger": "row_id", "invoices": "invoice_id", "payments": "payment_id"}


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def _owner(table, row):
    if table == "ledger":
        return row["payment_id"] if row["payment_id"] is not None else row["invoice_id"]
    return row[_IDS[table]]


def _duplicate_pairs(data):
    groups = {}
    for doc in data.invoices.values():
        book = data.book(doc)
        if book.get(("1200", doc["invoice_id"]), 0) <= 0 or book.get(("4000", None), 0) >= 0:
            continue
        signature = _json([doc[key] for key in ("entity_id", "currency", "total_minor", "line_items")])
        groups.setdefault(signature, []).append(doc)
    pairs = set()
    for documents in groups.values():
        for left, right in combinations(documents, 2):
            gap = abs((data.local(left["issued_at"]).date() - data.local(right["issued_at"]).date()).days)
            if gap <= 5:
                pairs.add(frozenset((left["invoice_id"], right["invoice_id"])))
    return pairs


def require_duplicate_coverage(data: PublicEvidence, issues: list[dict]) -> int:
    """Require complete final duplicate coverage for either history format."""
    actual_pairs = _duplicate_pairs(data)
    declared_pairs = {frozenset(item["affected_ids"]) for item in issues
                      if item["type"] == "duplicate_invoice"}
    require(actual_pairs == declared_pairs, "final duplicate pairs differ from declared affected sets")
    return len(actual_pairs)


def _issues(truth, data):
    require(type(truth["issues"]) is list, "truth issues must be a list")
    result = {}
    documents = set(data.invoices) | set(data.payments)
    for item in truth["issues"]:
        require(type(item) is dict, "truth issue must be an object")
        identifier = item["issue_id"]
        require(type(identifier) is str and bool(identifier) and identifier not in result,
                "invalid or duplicate truth issue ID")
        require(item["type"] in _KINDS, "unknown truth issue type")
        affected = item["affected_ids"]
        require(type(affected) is list and bool(affected) and
                all(type(key) is str for key in affected), "invalid affected IDs")
        require(len(set(affected)) == len(affected) and set(affected) <= documents,
                "affected IDs must identify distinct public documents")
        result[identifier] = item
    return result


def _mutations(provenance, issues):
    require(type(provenance["mutations"]) is list, "mutations must be a list")
    seen = set()
    for mutation in provenance["mutations"]:
        require(type(mutation) is dict, "mutation must be an object")
        identifier = mutation["issue_id"]
        require(type(identifier) is str and identifier in issues and identifier not in seen,
                "mutation issue ID is absent from truth or repeated")
        require(mutation["type"] == issues[identifier]["type"], "mutation type differs from truth")
        require(type(mutation["changes"]) is list, "mutation changes must be a list")
        for change in mutation["changes"]:
            require(type(change) is dict and
                    {"table", "record_id", "field", "before", "after"} <= change.keys(),
                    "malformed mutation change")
            require(change["table"] in TABLES, "mutation cannot change policy or other reference metadata")
            # rate_used describes a wrong applied rate, not a modification of fx.csv.
            if change["table"] == "fx":
                require(mutation["type"] == "fx_conversion" and change["field"] == "rate_used",
                        "mutation cannot change the FX reference table")
        seen.add(identifier)
    require(seen == set(issues), "truth issue has no provenance mutation")
    return len(seen)


def _document_changes(clean, final, issues):
    kinds = {}
    for item in issues.values():
        for identifier in item["affected_ids"]:
            kinds.setdefault(identifier, set()).add(item["type"])
    changed, ledger_changed, added_invoices, dated = set(), set(), set(), set()
    for table in _IDS:
        before = {row[_IDS[table]]: row for row in clean[table]}
        after = {row[_IDS[table]]: row for row in final[table]}
        for key in set(before) | set(after):
            old, new = before.get(key), after.get(key)
            if _json(old) == _json(new):
                continue
            owners = {_owner(table, row) for row in (old, new) if row is not None}
            require(owners <= kinds.keys(), "changed document is not covered by truth")
            changed |= owners
            if table == "ledger":
                ledger_changed |= owners
                continue
            require(new is not None, "error injection cannot remove source documents")
            if old is None:
                require(table == "invoices" and "duplicate_invoice" in kinds[key],
                        "only a declared duplicate may add a source document")
                added_invoices.add(key)
                continue
            fields = old.keys() ^ new.keys()
            fields |= {field for field in old.keys() & new.keys()
                       if _json(old[field]) != _json(new[field])}
            allowed = {"issued_at", "due_at"} if table == "invoices" else {"timestamp"}
            require("period_cutoff" in kinds[key] and fields <= allowed,
                    "source document changed outside declared cutoff dates")
            timestamp = "issued_at" if table == "invoices" else "timestamp"
            if timestamp in fields:
                dated.add(key)
    for item in issues.values():
        affected, kind = set(item["affected_ids"]), item["type"]
        require(bool(affected & changed), "truth issue has no changed document")
        if kind == "duplicate_invoice":
            require(len(affected) == 2 and len(affected & added_invoices) == 1,
                    "duplicate issue must pair one original and one added invoice")
        elif kind == "period_cutoff":
            require(len(affected) == 1 and affected <= dated,
                    "cutoff issue has no actual source date change")
        elif kind == "fx_conversion":
            require(len(affected) == 1 and affected <= ledger_changed,
                    "FX issue has no changed document postings")
        else:
            payment_ids = affected & {row["payment_id"] for row in final["payments"]}
            invoice_ids = affected & {row["invoice_id"] for row in final["invoices"]}
            require(len(payment_ids) == len(invoice_ids) == 1 and len(affected) == 2 and
                    payment_ids <= ledger_changed, "allocation issue has no changed payment postings")
    return changed, set(kinds)


def check_consistency(public: dict, truth: dict, provenance: dict) -> dict:
    """Check net-diff coverage without importing generator or trusting mutation prose.

    Missing/malformed proof is invalid. No input is changed. The current public
    policy must be supported; historical policy identity is not provable from
    the existing four-table hash and is not claimed by this report.
    """
    report = {"valid": False, "errors": [], "changed_documents": [], "covered_documents": [],
              "mutations_checked": 0, "duplicate_pairs_checked": 0,
              "historical_policy_verified": False}
    try:
        require(all(type(value) is dict for value in (public, truth, provenance)),
                "public, truth and provenance must be objects")
        episode_id = public["manifest"]["episode_id"]
        require(truth["episode_id"] == provenance["episode_id"] == episode_id, "episode ID mismatch")
        diff = provenance["clean_history_diff"]
        require(type(diff) is dict and set(diff) == set(TABLES),
                "clean history diff must contain exactly four tables, not policy or other metadata")
        clean, errors = reconstruct(public, diff)
        require(not errors, "inconsistent clean history diff: " + "; ".join(errors))
        require(digest(clean) == provenance["clean_history_sha256"], "clean history hash mismatch")
        data = PublicEvidence(public)
        PublicEvidence({**public, **clean})  # Validate reconstructed IDs, links and current policy too.
        # No error type modifies the reference rates; rate_used is merely descriptive.
        final_fx = {f"{row['date']}:{row['source_currency']}:{row['target_currency']}": row
                    for row in public["fx"]}
        clean_fx = {f"{row['date']}:{row['source_currency']}:{row['target_currency']}": row
                    for row in clean["fx"]}
        require(_json(final_fx) == _json(clean_fx), "FX reference table changed during error injection")
        issues = _issues(truth, data)
        report["mutations_checked"] = _mutations(provenance, issues)
        changed, covered = _document_changes(clean, public, issues)
        report.update(changed_documents=sorted(changed), covered_documents=sorted(covered))
        report["duplicate_pairs_checked"] = require_duplicate_coverage(data, truth["issues"])
        report["valid"] = True
    except (KeyError, ValueError, TypeError, ArithmeticError, AttributeError, OverflowError) as exc:
        report["errors"].append(str(exc))
    return report
