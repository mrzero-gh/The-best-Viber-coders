"""Контракт оценщика, крайние случаи и детерминизм."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from grader.evaluate import evaluate
from grader.matcher import match
from tests.test_grader_fixtures import EPISODE_ID, TYPES, answer_for, issue, truth_for


def score(answer, truth, steps=0):
    return evaluate(answer, truth, EPISODE_ID, steps)


@pytest.mark.parametrize("kind", TYPES)
def test_oracle_each_type(kind):
    truth = truth_for(issue(kind))
    assert score(answer_for(truth), truth) == {
        "valid": True, "reward": 1.0, "error": None,
        "metrics": {"tp": 1, "fp": 0, "fn": 0, "precision": 1.0, "recall": 1.0, "f1": 1.0},
    }


@pytest.mark.parametrize("kind1,kind2", [
    ("duplicate_invoice", "fx_conversion"), ("payment_allocation", "period_cutoff"),
])
def test_combined_issues_on_same_document(kind1, kind2):
    first, second = issue(kind1, 1), issue(kind2, 2)
    second["affected_ids"] = first["affected_ids"][:]
    truth = truth_for(first, second)
    assert score(answer_for(truth), truth)["metrics"]["tp"] == 2


def test_clean_and_empty_cases():
    clean, dirty = truth_for(), truth_for(issue())
    assert score(answer_for(clean), clean)["metrics"] == {
        "tp": 0, "fp": 0, "fn": 0, "precision": 0.0, "recall": 0.0, "f1": 1.0,
    }
    assert score(answer_for(clean), clean, 100)["reward"] == pytest.approx(0.8)
    assert score(answer_for(clean), dirty)["reward"] == -1
    assert score(answer_for(dirty), clean)["reward"] == -1


@pytest.mark.parametrize("offset,matched", [(-2, False), (-1, True), (0, True), (1, True), (2, False)])
def test_tolerance_boundaries(offset, matched):
    truth = truth_for(issue(amount=10**30))
    answer = answer_for(truth)
    answer["findings"][0]["claimed_amount_minor"] += offset
    assert score(answer, truth)["metrics"]["tp"] == int(matched)


def test_normalization_and_alternative_evidence_do_not_mutate_inputs():
    item = issue()
    item["affected_ids"].append("PAY-00000001")
    item["evidence_sets"].append(["DOC-00000002", "L-00000002"])
    truth = truth_for(item)
    answer = answer_for(truth)
    answer["findings"][0]["affected_ids"] = [" PAY-00000001 ", "INV-00000001", "INV-00000001"]
    answer["findings"][0]["evidence_ids"] = [" DOC-00000002 ", "L-00000002", "L-00000002", "EXTRA"]
    before = deepcopy((answer, truth))
    assert score(answer, truth)["reward"] == 1
    assert (answer, truth) == before


def test_one_to_one_and_stable_issue_order():
    # Прямой matcher детерминирован даже на неоднозначном входе; evaluate
    # отдельно запрещает такие эталоны согласно инварианту генератора.
    first, second = issue(number=1), issue(number=2)
    second["affected_ids"] = first["affected_ids"][:]
    second["evidence_sets"] = deepcopy(first["evidence_sets"])
    findings = answer_for(truth_for(first))["findings"] * 3
    result = match(findings, [second, first])
    assert result == {
        "tp": 2, "fp": 1, "fn": 0,
        "matches": [{"finding_index": 0, "issue_id": first["issue_id"]},
                    {"finding_index": 1, "issue_id": second["issue_id"]}],
        "unmatched_findings": [2], "unmatched_issues": [],
    }


@pytest.mark.parametrize("steps", [-1, True, 1.0, "1", None])
def test_bad_server_step_count_is_not_an_agent_penalty(steps):
    truth = truth_for()
    with pytest.raises(ValueError, match="inspection_steps"):
        score(answer_for(truth), truth, steps)


@pytest.mark.parametrize("defect", ["episode", "duplicate_key", "duplicate_id", "float", "empty_evidence"])
def test_bad_truth_fails_on_server(defect):
    truth = truth_for(issue())
    if defect == "episode":
        truth["episode_id"] = "EP-00000002"
    elif defect in ("duplicate_key", "duplicate_id"):
        extra = issue(number=2)
        key = "affected_ids" if defect == "duplicate_key" else "issue_id"
        extra[key] = truth["issues"][0][key]
        truth["issues"].append(extra)
    elif defect == "float":
        truth["issues"][0]["expected_amount_minor"] = 1000.0
    else:
        truth["issues"][0]["evidence_sets"] = [[]]
    with pytest.raises(ValueError, match="truth"):
        score({"episode_id": EPISODE_ID, "findings": []}, truth)


def test_shared_schema_examples_and_private_output():
    examples = Path(__file__).resolve().parents[1] / "schemas" / "examples"
    truth = json.loads((examples / "truth.example.json").read_text(encoding="utf-8"))
    for path in sorted(examples.glob("findings.*.json")):
        answer = json.loads(path.read_text(encoding="utf-8"))
        result = score(answer, truth)
        assert result["valid"] is (".invalid." not in path.name), path.name
        serialized = json.dumps(result)
        assert all(item["issue_id"] not in serialized for item in truth["issues"])
        assert "evidence_sets" not in serialized


def test_raw_limits_are_checked_before_deduplication():
    truth = truth_for(issue())
    answer = answer_for(truth)
    answer["findings"][0]["affected_ids"] *= 11
    assert score(answer, truth)["valid"] is False


def test_exact_schema_limits_are_accepted():
    truth = truth_for(issue())
    answer = answer_for(truth)
    finding = answer["findings"][0]
    finding["affected_ids"] *= 10
    finding["evidence_ids"] *= 5
    finding["explanation"] = "я" * 1000
    answer["findings"] *= 30
    result = score(answer, truth)
    assert result["valid"] is True
    assert result["metrics"] == {"tp": 1, "fp": 29, "fn": 0,
                                  "precision": 1 / 30, "recall": 1, "f1": 2 / 31}
