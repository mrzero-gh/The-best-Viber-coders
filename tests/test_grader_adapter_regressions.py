"""Matcher regressions through the public jury scoring entry point.

M10/M11 use the graphs from fintech-checks.txt, with evidence backed by real
public documents. These checks deliberately do not call evaluate() or match()
directly: truth and finding validation must admit the official examples too.
"""
import json
import shutil
from itertools import permutations
from pathlib import Path

import pytest

import judge_adapter as adapter


FIXTURE = Path(__file__).parent / "fixtures" / "mini" / "EP-8K2M4Q7X"
EPISODE_ID = "EP-8K2M4Q7X"
AFFECTED = ["INV-3HZ7QK2D", "INV-6TQ4W8NR"]
DOC_A, DOC_B = "DOC-7C2K9W4M", "DOC-2PM8K5XA"
ISSUE_A, ISSUE_B = "ISS-0000000A", "ISS-0000000B"
METRICS = ("tp", "fp", "fn", "precision", "recall", "f1")


@pytest.fixture
def episode_dir(tmp_path, monkeypatch):
    """Reuse only the small public fixture; each test supplies its own truth."""
    episode = tmp_path / "episode"
    shutil.copytree(FIXTURE / "public", episode / "public")
    (episode / "private").mkdir()
    monkeypatch.setenv("AUDIT_GYM_RUNS_DIR", str(tmp_path / "runs"))
    return episode


def issue(issue_id=ISSUE_A, evidence=(DOC_A,)):
    return {
        "issue_id": issue_id,
        "type": "payment_allocation",
        "affected_ids": AFFECTED[:],
        "expected_amount_minor": 10000,
        "currency": "KZT",
        "amount_tolerance_minor": 0,
        "evidence_sets": [list(evidence)],
        "scenario": None,
    }


def finding(evidence=(DOC_A,), kind="payment_allocation", explanation="proof"):
    return {
        "type": kind,
        "affected_ids": AFFECTED[:],
        "claimed_amount_minor": 10000,
        "currency": "KZT",
        "evidence_ids": list(evidence),
        "explanation": explanation,
    }


def write_truth(episode_dir, issues):
    truth = {
        "schema_version": "1.0",
        "episode_id": EPISODE_ID,
        "issues": list(issues),
        "distractors": [],
    }
    (episode_dir / "private" / "truth.json").write_text(
        json.dumps(truth), encoding="utf-8"
    )


def score(episode_dir, issues, findings):
    write_truth(episode_dir, issues)
    return adapter.score_episode(
        str(episode_dir),
        {"episode_id": EPISODE_ID, "findings": list(findings)},
        inspection_steps=10,
    )


def assert_score(result, tp, fp, fn, precision, recall, f1, reward):
    assert set(result) == set(METRICS) | {"return"}
    assert (result["tp"], result["fp"], result["fn"]) == (tp, fp, fn)
    for key, expected in (
        ("precision", precision), ("recall", recall), ("f1", f1), ("return", reward)
    ):
        assert result[key] == pytest.approx(expected, abs=1e-9)


def test_fixture_evidence_is_reachable_through_public_tools(episode_dir):
    write_truth(episode_dir, [issue()])
    with adapter.make_env() as env:
        env.reset(0, {"mode": "closed", "episode_dir": str(episode_dir)})
        for invoice_id, doc_id in zip(AFFECTED, (DOC_A, DOC_B)):
            obs, *_ = env.step({
                "name": "get_record", "args": {"table": "invoices", "id": invoice_id}
            })
            assert obs["error"] is None
            assert obs["result"]["doc_id"] == doc_id
            obs, *_ = env.step({"name": "get_document", "args": {"doc_id": doc_id}})
            assert obs["error"] is None
            assert obs["result"]["invoice_id"] == invoice_id


def test_m10_adapter_accepts_graph_requiring_maximum_matching(episode_dir):
    """F0 matches A/B, F1 only A: a greedy F0->A would lose one TP."""
    issues = [issue(ISSUE_A, (DOC_A,)), issue(ISSUE_B, (DOC_B,))]
    findings = [finding((DOC_A, DOC_B)), finding((DOC_A,))]
    for truth_order in permutations(issues):
        for finding_order in permutations(findings):
            result = score(episode_dir, truth_order, finding_order)
            assert_score(result, 2, 0, 0, 1.0, 1.0, 1.0, 0.98)


def test_m11_adapter_preserves_metrics_with_ambiguous_matches(episode_dir):
    """Both findings match A/B; order cannot change TP or reward.

    The public score intentionally exposes no private matching pairs. Their
    exact lexicographic tie break is covered by the matcher unit tests.
    """
    issues = [issue(ISSUE_A), issue(ISSUE_B)]
    findings = [finding(explanation="F0"), finding(explanation="F1")]
    for truth_order in permutations(issues):
        for finding_order in permutations(findings):
            result = score(episode_dir, truth_order, finding_order)
            assert_score(result, 2, 0, 0, 1.0, 1.0, 1.0, 0.98)


@pytest.mark.parametrize("kind", ["unknown_error", "Payment_allocation", "payment_allocation "])
def test_unknown_string_type_is_fp_without_invalidating_correct_finding(episode_dir, kind):
    correct, unknown = finding(), finding(kind=kind)
    for findings in ([correct, unknown], [unknown, correct]):
        result = score(episode_dir, [issue()], findings)
        assert_score(result, 1, 1, 0, 0.5, 1.0, 2 / 3, 47 / 150)


def test_unknown_string_type_alone_has_numeric_metrics(episode_dir):
    result = score(episode_dir, [issue()], [finding(kind="unknown_error")])
    assert_score(result, 0, 1, 1, 0.0, 0.0, 0.0, -1.0)


@pytest.mark.parametrize("kind", [None, True, 1, 1.0, [], {}])
def test_nonstring_type_invalidates_entire_submit(episode_dir, kind):
    result = score(episode_dir, [issue()], [finding(), finding(kind=kind)])
    assert result == {**dict.fromkeys(METRICS), "return": -1.0}


def test_duplicate_issue_id_remains_invalid_truth(episode_dir):
    a, b = issue(ISSUE_A), issue(ISSUE_A, (DOC_B,))
    b["type"] = "fx_conversion"  # Distinct canonical keys must not hide duplicate IDs.
    with pytest.raises(ValueError, match="duplicate"):
        score(episode_dir, [a, b], [finding()])
