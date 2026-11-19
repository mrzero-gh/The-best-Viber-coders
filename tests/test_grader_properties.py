"""Свойства награды и метаморфные проверки без генератора."""
from copy import deepcopy

from hypothesis import given, strategies as st
import pytest

from grader.evaluate import evaluate
from tests.test_grader_fixtures import EPISODE_ID, TYPES, answer_for, issue, truth_for


@given(st.integers(1, 12), st.integers(0, 100))
def test_reward_properties(count, steps):
    truth = truth_for(*(issue(TYPES[n % 4], n + 1) for n in range(count)))
    oracle = answer_for(truth)
    score = lambda answer, cost=steps: evaluate(answer, truth, EPISODE_ID, cost)["reward"]
    # 1. Полный oracle достигает максимума с данным бюджетом.
    assert score(oracle) == pytest.approx(1 - 0.002 * steps)
    # 2. Удаление TP не улучшает награду.
    missing = deepcopy(oracle)
    missing["findings"].pop()
    assert score(missing) < score(oracle)
    # 3. Добавление FP не улучшает награду.
    extra = deepcopy(oracle)
    false = deepcopy(oracle["findings"][0])
    false["affected_ids"] = ["INV-99999999"]
    extra["findings"].append(false)
    assert score(extra) < score(oracle)
    # 4. Повтор TP не даёт дополнительного зачёта.
    duplicate = deepcopy(oracle)
    duplicate["findings"].append(duplicate["findings"][0])
    assert score(duplicate) == score(extra)
    # 5. Дополнительные действия только уменьшают награду; она в [-1, 1].
    assert -1 <= score(oracle, steps + 1) <= score(oracle) <= 1


@given(st.permutations(tuple(range(4))), st.integers(0, 100), st.integers(0, 10**12))
def test_permutation_and_bijective_id_renaming(order, steps, salt):
    truth = truth_for(*(issue(kind, n + 1) for n, kind in enumerate(TYPES)))
    answer = answer_for(truth)
    answer["findings"][1]["claimed_amount_minor"] += 10
    answer["findings"].append(deepcopy(answer["findings"][0]))
    expected = evaluate(answer, truth, EPISODE_ID, steps)
    truth["issues"] = [truth["issues"][n] for n in order]
    answer["findings"] = [answer["findings"][n] for n in order] + [answer["findings"][-1]]
    rename = lambda value: f"renamed-{salt}-{value}"
    for item in truth["issues"]:
        item["affected_ids"] = list(map(rename, item["affected_ids"]))
        item["evidence_sets"] = [list(map(rename, ids)) for ids in item["evidence_sets"]]
    for item in answer["findings"]:
        item["affected_ids"] = list(map(rename, item["affected_ids"]))
        item["evidence_ids"] = list(map(rename, reversed(item["evidence_ids"])))
    assert evaluate(answer, truth, EPISODE_ID, steps) == expected
