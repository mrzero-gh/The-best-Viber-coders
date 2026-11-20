"""Блок 3 жюри: 20 тестов matcher и награды и открытые примеры M01-M12.

Источник: genesis-exe.com/fintech-checks.html, разделы «Блок 3» и «Точные соглашения matcher».
Проверяется наш оценщик grader.evaluate.evaluate (его же вызывает judge_adapter.score_episode).

Известные расхождения с правилами жюри помечены xfail(strict=True): когда оценщик исправят,
тест начнёт проходить, pytest сообщит XPASS как ошибку, и метку нужно будет снять.
"""
import copy

import pytest

from grader.evaluate import evaluate
from grader.matcher import match

EP = "EP-00000000"
T, T2 = "payment_allocation", "fx_conversion"
TYPES = ("duplicate_invoice", "payment_allocation", "fx_conversion", "period_cutoff")
R1, R2, R3, R9 = "INV-R1", "INV-R2", "INV-R3", "INV-R9"
D1, D2, D3, U = "DOC-D1", "DOC-D2", "DOC-D3", "DOC-UNKNOWN"
STEPS = 10  # inspection_steps по умолчанию в unit-тестах жюри
EPS = 1e-9  # допустимая абсолютная погрешность вещественного результата

PENDING_EVIDENCE = pytest.mark.xfail(
    strict=True, reason="C: несуществующий evidence_id должен давать FP; evaluate не знает публичных ID")
PENDING_CURRENCY = pytest.mark.xfail(
    strict=True, reason="C: валюта с другим регистром/пробелом - FP, а не ошибка структуры (pattern в схеме)")
PENDING_STRIP = pytest.mark.xfail(
    strict=True, reason="C: ID сравниваются точно, с пробелами; matcher делает strip()")
PENDING_MAXMATCH = pytest.mark.xfail(
    strict=True, reason="matcher жадный; в наших эпизодах ключ (type, affected_ids) уникален, "
                        "поэтому граф M10 не возникает, но на уровне match() результат 1, а не 2")


def issue(iid="ISS-0000000A", type_=T, affected=(R1,), amount=10000, currency="KZT", tol=0,
          evidence=((D1, D2),)):
    return {"issue_id": iid, "type": type_, "affected_ids": list(affected),
            "expected_amount_minor": amount, "currency": currency, "amount_tolerance_minor": tol,
            "evidence_sets": [list(e) for e in evidence], "scenario": None}


def truth(*issues):
    return {"schema_version": "1.0", "episode_id": EP, "issues": list(issues), "distractors": []}


def finding(type_=T, affected=(R1,), amount=10000, currency="KZT", evidence=(D1, D2), explanation="ok"):
    return {"type": type_, "affected_ids": list(affected), "claimed_amount_minor": amount,
            "currency": currency, "evidence_ids": list(evidence), "explanation": explanation}


def run(findings, tr, steps=STEPS):
    res = evaluate({"episode_id": EP, "findings": findings}, tr, EP, steps)
    return res


def assert_metrics(res, tp, fp, fn, reward, p=None, r=None, f1=None):
    assert res["valid"] is True, res["error"]
    m = res["metrics"]
    assert (m["tp"], m["fp"], m["fn"]) == (tp, fp, fn)
    if p is not None:
        assert abs(m["precision"] - p) <= EPS
    if r is not None:
        assert abs(m["recall"] - r) <= EPS
    if f1 is not None:
        assert abs(m["f1"] - f1) <= EPS
    assert abs(res["reward"] - reward) <= EPS


def assert_invalid(res):
    assert res["valid"] is False and res["reward"] == -1.0 and res["error"]


A = issue()
B = issue(iid="ISS-0000000B", type_=T2, affected=(R2,), amount=5000, evidence=((D3,),))
F = finding()
FB = finding(type_=T2, affected=(R2,), amount=5000, evidence=(D3,))


# ------------------------------------------------------------------ тесты 1-20

def test_01_one_correct():
    assert_metrics(run([F], truth(A)), 1, 0, 0, 0.98, p=1, r=1, f1=1)


def test_02_empty_findings():
    assert_metrics(run([], truth(A)), 0, 0, 1, -1.0, p=0, r=0, f1=0)


def test_03_empty_truth_empty_findings():
    assert_metrics(run([], truth()), 0, 0, 0, 0.98, p=0, r=0, f1=1)


def test_04_empty_truth_false_positive():
    assert_metrics(run([F], truth()), 0, 1, 0, -1.0, f1=0)


def test_05_duplicate_correct():
    res = run([F, F], truth(A))
    assert_metrics(res, 1, 1, 0, 47 / 150, p=0.5, r=1, f1=2 / 3)
    assert res["reward"] <= run([F], truth(A))["reward"]


@pytest.mark.parametrize("n,bad", [
    (6, finding(type_=T2)),                      # неправильный тип
    (7, finding(affected=(R9,))),                # другая существующая, не затронутая запись
    (8, finding(amount=10001)),                  # сумма вне допуска 0
    (9, finding(currency="USD")),                # другая валюта
    (10, finding(evidence=(D1,))),               # неполный набор доказательств
    (11, finding(evidence=(D1, U))),             # отсутствующий evidence_id вместо D2
])
def test_06_11_wrong_field(n, bad):
    assert_metrics(run([bad], truth(A)), 0, 1, 1, -1.0, f1=0)


def test_12_mass_flagging():
    """5 записей x 4 типа = 20 выводов, ровно один совпадает с A."""
    records = (R1, R2, R3, "INV-R4", "INV-R5")
    findings = [finding(type_=t, affected=(r,)) for r in records for t in TYPES]
    assert sum(f == F for f in findings) == 1
    assert_metrics(run(findings, truth(A)), 1, 19, 0, -871 / 1050, p=1 / 20, r=1, f1=2 / 21)


def test_13_two_issues_one_found():
    assert_metrics(run([F], truth(A, B)), 1, 0, 1, 47 / 150, p=1, r=0.5, f1=2 / 3)


def test_14_two_issues_one_found_one_false():
    assert_metrics(run([F, finding(affected=(R9,))], truth(A, B)), 1, 1, 1, -0.02, p=0.5, r=0.5, f1=0.5)


@pytest.mark.parametrize("steps,reward", [(0, 1.0), (50, 0.90), (100, 0.80)])
def test_15_17_step_cost(steps, reward):
    assert_metrics(run([F], truth(A), steps=steps), 1, 0, 0, reward, f1=1)


def test_16_difference_with_test_1():
    assert abs(run([F], truth(A), 10)["reward"] - run([F], truth(A), 50)["reward"] - 0.08) <= EPS


def test_18_31_findings():
    assert_invalid(run([F] * 31, truth(A)))


def test_19_11_evidence_ids():
    assert_invalid(run([finding(evidence=[D1, D2] + ["DOC-E%d" % i for i in range(9)])], truth(A)))


def test_20_explanation_1001_ascii():
    assert_invalid(run([finding(explanation="a" * 1001)], truth(A)))


def test_limits_are_inclusive():
    """Ровно 30 выводов, 10 доказательств и 1000 символов - ещё допустимая структура."""
    assert run([F] * 30, truth(A))["valid"] is True
    assert run([finding(evidence=[D1, D2] + ["DOC-E%d" % i for i in range(8)])], truth(A))["valid"] is True
    assert run([finding(explanation="я" * 1000)], truth(A))["valid"] is True  # code points, не байты


# ------------------------------------------------------------------ M01-M12

AM = issue(affected=(R1, R2))
FM = finding(affected=(R1, R2))


def test_m01_duplicate_affected_ids_is_a_set():
    assert_metrics(run([finding(affected=(R2, R1, R1))], truth(AM)), 1, 0, 0, 0.98)


def test_m02_extra_affected_record():
    assert_metrics(run([finding(affected=(R1, R2, R3))], truth(AM)), 0, 1, 1, -1.0)


def test_m03_extra_existing_evidence_ok():
    assert_metrics(run([finding(affected=(R1, R2), evidence=(D2, D1, D3))], truth(AM)), 1, 0, 0, 0.98)


@PENDING_EVIDENCE
def test_m04_unknown_evidence_id():
    assert_metrics(run([finding(affected=(R1, R2), evidence=(D1, D2, U))], truth(AM)), 0, 1, 1, -1.0)


def test_m05_eleven_evidence_with_repeats():
    assert_invalid(run([finding(affected=(R1, R2), evidence=[D2] + [D1] * 10)], truth(AM)))


@PENDING_CURRENCY
@pytest.mark.parametrize("cur", ["kzt", "KZT ", " KZT"])
def test_m06_currency_exact(cur):
    assert_metrics(run([finding(affected=(R1, R2), currency=cur)], truth(AM)), 0, 1, 1, -1.0)


def test_m07_amount_off_by_one():
    assert_metrics(run([finding(affected=(R1, R2), amount=10001)], truth(AM)), 0, 1, 1, -1.0)


def test_m08_tolerance_inclusive():
    t1 = truth(issue(affected=(R1, R2), tol=1))
    assert_metrics(run([finding(affected=(R1, R2), amount=10001)], t1), 1, 0, 0, 0.98)
    assert_metrics(run([finding(affected=(R1, R2), amount=9999)], t1), 1, 0, 0, 0.98)
    assert_metrics(run([finding(affected=(R1, R2), amount=10002)], t1), 0, 1, 1, -1.0)


@pytest.mark.parametrize("amount", ["10000", True, 10000.0])
def test_m09_amount_type(amount):
    assert_invalid(run([finding(affected=(R1, R2), amount=amount)], truth(AM)))


def _edge_issue(iid, evidence):
    return issue(iid=iid, affected=(R1, R2), evidence=evidence)


@PENDING_MAXMATCH
def test_m10_maximum_matching():
    """F0 подходит A и B, F1 - только A: нужно F1->A, F0->B, TP=2 (жадный F0->A даёт 1)."""
    a = _edge_issue("ISS-0000000A", ((D1,),))
    b = _edge_issue("ISS-0000000B", ((D2,),))
    f0 = finding(affected=(R1, R2), evidence=(D1, D2))
    f1 = finding(affected=(R1, R2), evidence=(D1,))
    assert match([f0, f1], [a, b])["tp"] == 2


def test_m10_graph_cannot_occur_in_our_truth():
    """Почему жадный порядок у нас эквивалентен максимальному: такой truth отвергается."""
    a = _edge_issue("ISS-0000000A", ((D1,),))
    b = _edge_issue("ISS-0000000B", ((D2,),))
    with pytest.raises(ValueError):
        run([], truth(a, b))


def test_m11_deterministic_tie_break():
    a = _edge_issue("ISS-0000000A", ((D1,),))
    b = _edge_issue("ISS-0000000B", ((D1,),))
    f0 = f1 = finding(affected=(R1, R2), evidence=(D1,))
    res = match([f0, f1], [b, a])
    assert res["tp"] == 2
    assert res["matches"] == [{"finding_index": 0, "issue_id": "ISS-0000000A"},
                              {"finding_index": 1, "issue_id": "ISS-0000000B"}]
    assert match([f1, f0], [a, b])["tp"] == 2


def test_m12_identical_correct_findings():
    res = run([FM, copy.deepcopy(FM)], truth(AM))
    assert_metrics(res, 1, 1, 0, 47 / 150)
    assert res["metrics"] == run([F, F], truth(A))["metrics"]  # тот же вектор, что в тесте 5


# ------------------------------------------------------------------ нормализация ID

@PENDING_STRIP
def test_ids_compared_exactly_with_spaces():
    assert_metrics(run([finding(affected=(" " + R1,))], truth(A)), 0, 1, 1, -1.0)


def test_ids_case_sensitive():
    assert_metrics(run([finding(affected=(R1.lower(),))], truth(A)), 0, 1, 1, -1.0)


def test_findings_order_does_not_change_score():
    fs = [finding(affected=(R9,)), F, FB, F]
    a = run(fs, truth(A, B))
    b = run(list(reversed(fs)), truth(A, B))
    assert a["metrics"] == b["metrics"] and a["reward"] == b["reward"]
