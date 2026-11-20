"""Детерминированное сопоставление 1-к-1 по разделу 10 контракта и соглашениям жюри.

Вход уже проверен evaluate(). Детали match() предназначены только оценщику.

Соглашения (genesis-exe.com/fintech-checks.html, «Точные соглашения matcher»):
- ID сравниваются точно: с учётом регистра и пробелов; повторы внутри списка не важны (множество);
- affected_ids должны совпасть с каноническим набором ошибки;
- evidence_ids должны включать хотя бы один допустимый набор; если указан ID, которого нет
  в открытых данных эпизода (public_ids), вывод не сопоставляется (FP);
- валюта и тип сравниваются точно, сумма - в пределах допуска ошибки;
- сопоставление - максимальное паросочетание; при равной мощности выбирается
  лексикографически наименьшая последовательность пар (issue_id, индекс вывода).
"""


def canonical_ids(ids: list[str]) -> tuple[str, ...]:
    """Множество ID в отсортированном виде. Строки не меняются: регистр и пробелы значимы."""
    return tuple(sorted(set(ids)))


def _edge(finding: dict, issue: dict, public_ids) -> bool:
    """Выполнены ли все условия TP для пары (вывод, ошибка)."""
    evidence = set(finding["evidence_ids"])
    if public_ids is not None and not evidence <= public_ids:
        return False
    return (
        finding["type"] == issue["type"]
        and canonical_ids(finding["affected_ids"]) == canonical_ids(issue["affected_ids"])
        and finding["currency"] == issue["currency"]
        and abs(finding["claimed_amount_minor"] - issue["expected_amount_minor"])
        <= issue["amount_tolerance_minor"]
        and any(set(ids) <= evidence for ids in issue["evidence_sets"])
    )


def _max_matching(issues: list, adj: dict, banned_findings: set) -> int:
    """Мощность максимального паросочетания (алгоритм Куна) для данных ошибок."""
    owner = {}  # индекс вывода -> issue_id

    def augment(iid, seen):
        for f in adj[iid]:
            if f in banned_findings or f in seen:
                continue
            seen.add(f)
            if f not in owner or augment(owner[f], seen):
                owner[f] = iid
                return True
        return False

    return sum(augment(iid, set()) for iid in issues)


def match(findings: list[dict], truth: list[dict], public_ids=None) -> dict:
    """Сопоставляет находки с эталоном; возвращает словарь с TP/FP/FN и деталями.

    public_ids - множество ID открытых записей и документов эпизода (None - не проверять).
    """
    if public_ids is not None:
        public_ids = set(public_ids)
    issues = sorted(truth, key=lambda issue: issue["issue_id"])
    order = [issue["issue_id"] for issue in issues]
    adj = {issue["issue_id"]: [i for i, f in enumerate(findings) if _edge(f, issue, public_ids)]
           for issue in issues}
    best = _max_matching(order, adj, set())

    # Детерминированный выбор среди максимальных: ошибки по issue_id, выводы по индексу;
    # пару фиксируем, только если после неё ещё достижима максимальная мощность.
    used, pairs = set(), []
    for pos, iid in enumerate(order):
        rest = order[pos + 1:]
        for f in adj[iid]:
            if f in used:
                continue
            if len(pairs) + 1 + _max_matching(rest, adj, used | {f}) == best:
                pairs.append((iid, f))
                used.add(f)
                break

    matched_issues = {iid for iid, _ in pairs}
    matches = sorted(({"finding_index": f, "issue_id": iid} for iid, f in pairs),
                     key=lambda m: m["finding_index"])
    unmatched_findings = [i for i in range(len(findings)) if i not in used]
    unmatched_issues = [iid for iid in order if iid not in matched_issues]
    return {
        "tp": len(pairs),
        "fp": len(unmatched_findings),
        "fn": len(unmatched_issues),
        "matches": matches,
        "unmatched_findings": unmatched_findings,
        "unmatched_issues": unmatched_issues,
    }
