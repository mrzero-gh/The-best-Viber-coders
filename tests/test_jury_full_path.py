"""Соглашения жюри через полный путь judge_adapter.score_episode -> grader.evaluate (21.11).

1. Неизвестный строковый type - FP, а не невалидный submit (правило нормализации 8).
2. M10/M11: эталон с двумя ошибками на одной паре (type, affected_ids) принимается,
   паросочетание максимальное и детерминированное.
3. То же для submit через среду (env.server) в режиме train.
"""
import json
from pathlib import Path

import pytest

import judge_adapter as ja


@pytest.fixture(scope="module")
def episode(tmp_path_factory):
    ep = tmp_path_factory.mktemp("full") / "ep"
    ja.generate_episode(5, {}, str(ep))
    truth = json.loads((ep / "private" / "truth.json").read_text(encoding="utf-8"))
    # ошибка с набором доказательств хотя бы из двух ID: из них строим графы M10/M11
    issue = next(i for i in truth["issues"] if len(i["evidence_sets"][0]) >= 2)
    return ep, truth, issue


def _finding(issue, evidence, type_=None):
    return {"type": type_ or issue["type"], "affected_ids": issue["affected_ids"],
            "claimed_amount_minor": issue["expected_amount_minor"], "currency": issue["currency"],
            "evidence_ids": sorted(evidence), "explanation": "x"}


def _with_truth(tmp_path, episode, issues):
    """Копия эпизода с подменённым эталоном (открытые данные те же)."""
    import shutil
    src, truth, _ = episode
    dst = tmp_path / "ep"
    shutil.copytree(src, dst)
    t = dict(truth, issues=issues)
    (dst / "private" / "truth.json").write_text(json.dumps(t), encoding="utf-8")
    return dst, t["episode_id"]


def _metrics(res):
    return res["tp"], res["fp"], res["fn"]


def test_unknown_type_is_fp_via_score_episode(episode):
    ep, truth, issue = episode
    ok = _finding(issue, issue["evidence_sets"][0])
    res = ja.score_episode(str(ep), {"episode_id": truth["episode_id"],
                                     "findings": [_finding(issue, issue["evidence_sets"][0], "mystery_error"), ok]}, 0)
    assert res["return"] != -1.0 and res["tp"] == 1 and res["fp"] == 1


def test_m10_via_score_episode(tmp_path, episode):
    _, _, issue = episode
    d1, d2 = sorted(issue["evidence_sets"][0])[:2]
    a = dict(issue, issue_id="ISS-0000000A", evidence_sets=[[d1]])
    b = dict(issue, issue_id="ISS-0000000B", evidence_sets=[[d2]])
    ep, eid = _with_truth(tmp_path, episode, [a, b])
    f0 = _finding(issue, [d1, d2])  # подходит A и B
    f1 = _finding(issue, [d1])      # подходит только A
    res = ja.score_episode(str(ep), {"episode_id": eid, "findings": [f0, f1]}, 0)
    assert _metrics(res) == (2, 0, 0) and res["return"] == 1.0


def test_m11_via_score_episode(tmp_path, episode):
    _, _, issue = episode
    d1 = sorted(issue["evidence_sets"][0])[0]
    a = dict(issue, issue_id="ISS-0000000A", evidence_sets=[[d1]])
    b = dict(issue, issue_id="ISS-0000000B", evidence_sets=[[d1]])
    ep, eid = _with_truth(tmp_path, episode, [b, a])
    f = _finding(issue, [d1])
    res = ja.score_episode(str(ep), {"episode_id": eid, "findings": [f, dict(f)]}, 0)
    assert _metrics(res) == (2, 0, 0)


def test_unknown_type_is_fp_via_env_submit(episode):
    ep, truth, issue = episode
    correct = [_finding(i, i["evidence_sets"][0]) for i in truth["issues"]]
    findings = {"episode_id": truth["episode_id"],
                "findings": correct + [_finding(issue, issue["evidence_sets"][0], "mystery_error")]}
    with ja.make_env() as env:
        env.reset(0, {"mode": "train", "episode_dir": str(ep)})
        obs, reward, terminated, truncated, _ = env.step({"name": "submit", "args": {"findings": findings}})
    assert terminated and not truncated
    assert obs["error"] is None  # не INVALID_FINDINGS
    n = len(correct)
    assert reward == pytest.approx(2 * (2 * n / (2 * n + 1)) - 1)  # TP=n, FP=1, FN=0, шагов 0
