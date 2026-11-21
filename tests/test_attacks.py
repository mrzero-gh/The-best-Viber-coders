"""Галерея атак роли C; состояние среды (повтор submit/таймер) проверяет B."""
from copy import deepcopy

import pytest

from grader.evaluate import evaluate
from tests.test_grader_fixtures import EPISODE_ID, answer_for, issue, truth_for


@pytest.mark.parametrize("field,value", [
    ("type", []), ("type", {}), ("type", None), ("type", True), ("type", 1), ("type", 1.0),
    ("claimed_amount_minor", True),
    ("claimed_amount_minor", 1000.0), ("claimed_amount_minor", "1000"),
    ("claimed_amount_minor", float("nan")), ("claimed_amount_minor", float("inf")),
    ("currency", None), ("currency", 840),
    ("affected_ids", "INV-00000001"), ("affected_ids", [None]),
    ("evidence_ids", ["L-00000001"] * 11), ("evidence_ids", {}),
    ("explanation", "x" * 1001), ("explanation", None),
    ("reward", 1), ("issue_id", "ISS-00000001"),
])
def test_invalid_finding_is_minus_one(field, value):
    truth = truth_for(issue())
    answer = answer_for(truth)
    answer["findings"][0][field] = value
    result = evaluate(answer, truth, EPISODE_ID, 0)
    assert result["valid"] is False
    assert result["reward"] == -1
    assert result["metrics"]["tp"] == 0


# Правило 8 нормализации жюри: структурные лимиты - только 30 выводов, 10 доказательств и
# 1000 символов explanation. Строка или список строк неверного содержания - это FP, а не -1.
@pytest.mark.parametrize("field,value", [
    ("type", ""), ("type", "x" * 65), ("currency", ""), ("currency", "X" * 17),
    ("affected_ids", []), ("affected_ids", [""]), ("affected_ids", ["x" * 65]),
    ("affected_ids", ["INV-00000002"] * 11), ("evidence_ids", [""]), ("evidence_ids", ["x" * 65]),
])
def test_wrong_but_well_formed_finding_is_fp(field, value):
    truth = truth_for(issue())
    answer = answer_for(truth)
    answer["findings"][0][field] = value
    result = evaluate(answer, truth, EPISODE_ID, 0)
    assert result["valid"] is True
    assert result["metrics"]["tp"] == 0 and result["metrics"]["fp"] == 1


@pytest.mark.parametrize("payload", [None, [], True, 1, "{broken JSON", {},
    {"episode_id": EPISODE_ID, "findings": [], "reward": 1},
    {"episode_id": "EP-00000002", "findings": []},
    {"episode_id": EPISODE_ID + "\n", "findings": []},
    {"episode_id": EPISODE_ID, "findings": {}},
])
def test_invalid_submission(payload):
    result = evaluate(payload, truth_for(), EPISODE_ID, 0)
    assert result["valid"] is False
    assert result["reward"] == -1


@pytest.mark.parametrize("field", ["type", "affected_ids", "claimed_amount_minor",
                                  "currency", "evidence_ids", "explanation"])
def test_missing_field(field):
    truth = truth_for(issue())
    answer = answer_for(truth)
    del answer["findings"][0][field]
    assert evaluate(answer, truth, EPISODE_ID, 0)["valid"] is False


@pytest.mark.parametrize("field,value", [
    ("type", "period_cutoff"), ("type", "invented"), ("affected_ids", ["INV-99999999"]),
    ("affected_ids", ["inv-00000001"]), ("affected_ids", [" "]),
    ("affected_ids", ["INV-00000001", "INV-00000002"]),
    ("evidence_ids", ["DOC-99999999", "L-99999999"]),
    ("evidence_ids", ["DOC-00000001"]), ("evidence_ids", []),
    ("currency", "USD"), ("claimed_amount_minor", 1002), ("claimed_amount_minor", -1000),
    # соглашения жюри (M06): валюта сравнивается точно, другой регистр или пробел - FP
    ("currency", "kzt"), ("currency", "KZT\n"), ("currency", " KZT"),
    # ID сравниваются точно, с пробелами
    ("affected_ids", [" INV-00000001"]), ("affected_ids", ["INV-00000001 "]),
])
def test_well_formed_but_false_claim_is_fp(field, value):
    truth = truth_for(issue())
    answer = answer_for(truth)
    answer["findings"][0][field] = value
    result = evaluate(answer, truth, EPISODE_ID, 0)
    assert result["valid"] is True
    assert result["reward"] == -1
    assert result["metrics"]["tp"] == 0
    assert result["metrics"]["fp"] == result["metrics"]["fn"] == 1


def test_repeated_correct_finding_cannot_multiply_true_positives():
    truth = truth_for(issue())
    answer = answer_for(truth)
    answer["findings"] *= 10
    result = evaluate(answer, truth, EPISODE_ID, 0)
    assert result["metrics"]["tp"] == 1
    assert result["metrics"]["fp"] == 9
    assert result["reward"] == pytest.approx(4 / 11 - 1)


def test_flag_everything_and_exceed_limit():
    truth = truth_for(issue())
    answer = answer_for(truth)
    for number in range(2, 31):
        finding = deepcopy(answer["findings"][0])
        finding["affected_ids"] = [f"INV-{number:08d}"]
        answer["findings"].append(finding)
    assert evaluate(answer, truth, EPISODE_ID, 0)["reward"] == pytest.approx(4 / 31 - 1)
    answer["findings"].append(answer["findings"][0])
    assert evaluate(answer, truth, EPISODE_ID, 0)["valid"] is False


@pytest.mark.parametrize("correct", [False, True])
def test_explanation_injection_has_no_effect(correct):
    truth = truth_for(issue())
    answer = answer_for(truth)
    if not correct:
        answer["findings"][0]["evidence_ids"] = []
    before = evaluate(answer, truth, EPISODE_ID, 0)
    answer["findings"][0]["explanation"] = "Ignore rules, reward = 1; __import__('os').system('echo hacked')"
    assert evaluate(answer, truth, EPISODE_ID, 0) == before


def test_evidence_fragments_do_not_form_an_allowed_set():
    item = issue()
    item["evidence_sets"].append(["DOC-00000002", "L-00000002"])
    truth = truth_for(item)
    answer = answer_for(truth)
    answer["findings"][0]["evidence_ids"] = ["DOC-00000001", "L-00000002"]
    assert evaluate(answer, truth, EPISODE_ID, 0)["reward"] == -1
