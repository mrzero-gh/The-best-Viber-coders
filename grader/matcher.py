"""Детерминированное сопоставление 1-к-1 по разделу 10 контракта.

Вход уже проверен evaluate(). Детали match() предназначены только оценщику.
"""


def canonical_ids(ids: list[str]) -> tuple[str, ...]:
    """Пробелы и повторы незначимы; регистр ID сохраняется."""
    return tuple(sorted({value.strip() for value in ids}))


def match(findings: list[dict], truth: list[dict]) -> dict:
    """Сопоставляет находки с эталоном; возвращает словарь с TP/FP/FN и деталями."""
    issues = sorted(truth, key=lambda issue: issue["issue_id"])
    used = set()
    matches = []
    unmatched_findings = []
    for index, finding in enumerate(findings):
        affected = canonical_ids(finding["affected_ids"])
        evidence = set(canonical_ids(finding["evidence_ids"]))
        for issue in issues:
            if issue["issue_id"] in used:
                continue
            if (
                finding["type"] == issue["type"]
                and affected == canonical_ids(issue["affected_ids"])
                and finding["currency"] == issue["currency"]
                and abs(finding["claimed_amount_minor"] - issue["expected_amount_minor"])
                <= issue["amount_tolerance_minor"]
                and any(set(canonical_ids(ids)) <= evidence for ids in issue["evidence_sets"])
            ):
                used.add(issue["issue_id"])
                matches.append({"finding_index": index, "issue_id": issue["issue_id"]})
                break
        else:
            unmatched_findings.append(index)
    unmatched_issues = [issue["issue_id"] for issue in issues if issue["issue_id"] not in used]
    return {
        "tp": len(matches),
        "fp": len(unmatched_findings),
        "fn": len(unmatched_issues),
        "matches": matches,
        "unmatched_findings": unmatched_findings,
        "unmatched_issues": unmatched_issues,
    }
