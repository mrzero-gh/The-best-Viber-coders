"""Тесты всех 4 политик на реальном генераторе и среде."""
import json
from pathlib import Path
import pytest

from env.client import AuditEnv
from generator.build import generate_episode
from grader.evaluate import evaluate
from policies import FlagAllPolicy, OraclePolicy, RandomPolicy, RuleBasedPolicy

ALL_ERRORS_CFG = {
    "config_version": "test-all",
    "mode": "train",
    "errors": {
        "duplicate_invoice": {"enabled": True, "count": [1, 2]},
        "payment_allocation": {"enabled": True, "count": [1, 2]},
        "fx_conversion": {"enabled": True, "count": [1, 2]},
        "period_cutoff": {"enabled": True, "count": [1, 2]},
    },
    "interactions": {
        "partial_fx_payment_at_cutoff": {"enabled": True, "count": [1, 1]},
        "duplicate_invoice_wrong_fx": {"enabled": True, "count": [1, 1]},
    },
    "distractors": {
        "split_payment": {"enabled": True, "count": [1, 2]},
        "refund": {"enabled": True, "count": [1, 2]},
        "legit_recurring": {"enabled": True, "count": [1, 2]},
        "late_evening_local": {"enabled": True, "count": [1, 2]},
    },
    "clean": False,
    "max_errors": 12,
}

CLEAN_CFG = dict(ALL_ERRORS_CFG, clean=True)


def _run_policy_in_env(policy, seed: int, cfg: dict, episode_path: Path | None = None):
    with AuditEnv(episode=str(episode_path) if episode_path else None) as env:
        obs, info = env.reset(seed, cfg)
        policy.reset()
        history = [(None, obs, 0.0, False, False)]
        terminated = truncated = False
        while not (terminated or truncated):
            action = policy.act(obs)
            obs, reward, terminated, truncated, info = env.step(action)
            history.append((action, obs, reward, terminated, truncated))
        return history, info, reward


def test_oracle_policy_clean(tmp_path):
    ep = generate_episode(101, CLEAN_CFG, tmp_path)
    truth = json.loads((ep / "private" / "truth.json").read_text(encoding="utf-8"))
    policy = OraclePolicy(truth=truth)
    history, info, reward = _run_policy_in_env(policy, 101, CLEAN_CFG, ep)
    assert reward == 1.0
    final_action = history[-1][0]
    assert final_action["tool"] == "submit"
    assert final_action["args"]["findings"]["findings"] == []


def test_oracle_policy_all_errors(tmp_path):
    ep = generate_episode(102, ALL_ERRORS_CFG, tmp_path)
    truth = json.loads((ep / "private" / "truth.json").read_text(encoding="utf-8"))
    provenance = json.loads((ep / "private" / "provenance.json").read_text(encoding="utf-8"))
    assert len(truth["issues"]) > 0
    assert len(provenance["mutations"]) > 0

    policy = OraclePolicy(truth=truth)
    history, info, reward = _run_policy_in_env(policy, 102, ALL_ERRORS_CFG, ep)
    assert reward == 1.0
    eval_res = evaluate(history[-1][0]["args"]["findings"], truth, ep.name, 0)
    assert eval_res["valid"] is True
    assert eval_res["metrics"]["f1"] == 1.0
    assert eval_res["metrics"]["fn"] == 0
    assert eval_res["metrics"]["fp"] == 0


def test_rule_based_policy_on_clean(tmp_path):
    ep = generate_episode(103, CLEAN_CFG, tmp_path)
    truth = json.loads((ep / "private" / "truth.json").read_text(encoding="utf-8"))
    policy = RuleBasedPolicy(naive=False)
    history, info, reward = _run_policy_in_env(policy, 103, CLEAN_CFG, ep)
    submit_action = history[-1][0]
    assert submit_action["tool"] == "submit"
    findings = submit_action["args"]["findings"]["findings"]
    assert len(findings) == 0
    eval_res = evaluate(submit_action["args"]["findings"], truth, ep.name, len(history) - 2)
    assert eval_res["valid"] is True
    assert eval_res["metrics"]["f1"] == 1.0


def test_rule_based_policy_detects_errors(tmp_path):
    ep = generate_episode(104, ALL_ERRORS_CFG, tmp_path)
    truth = json.loads((ep / "private" / "truth.json").read_text(encoding="utf-8"))

    policy = RuleBasedPolicy(naive=False)
    history, info, reward = _run_policy_in_env(policy, 104, ALL_ERRORS_CFG, ep)
    submit_action = history[-1][0]
    assert submit_action["tool"] == "submit"

    eval_res = evaluate(submit_action["args"]["findings"], truth, ep.name, len(history) - 2)
    assert eval_res["valid"] is True
    assert eval_res["metrics"]["tp"] > 0
    assert eval_res["metrics"]["precision"] >= 0.7


@pytest.mark.parametrize("error_type", [
    "duplicate_invoice",
    "payment_allocation",
    "fx_conversion",
    "period_cutoff",
])
def test_rule_based_detects_individual_error_types(tmp_path, error_type):
    cfg = {
        "config_version": f"test-{error_type}",
        "mode": "train",
        "errors": {
            t: ({"enabled": True, "count": [2, 2]} if t == error_type else {"enabled": False})
            for t in ["duplicate_invoice", "payment_allocation", "fx_conversion", "period_cutoff"]
        },
        "interactions": {
            "partial_fx_payment_at_cutoff": {"enabled": False},
            "duplicate_invoice_wrong_fx": {"enabled": False},
        },
        "distractors": {
            "split_payment": {"enabled": True, "count": [1, 1]},
            "refund": {"enabled": True, "count": [1, 1]},
            "legit_recurring": {"enabled": True, "count": [1, 1]},
            "late_evening_local": {"enabled": True, "count": [1, 1]},
        },
        "clean": False,
        "max_errors": 12,
    }
    ep = generate_episode(300 + hash(error_type) % 50, cfg, tmp_path)
    truth = json.loads((ep / "private" / "truth.json").read_text(encoding="utf-8"))
    assert any(i["type"] == error_type for i in truth["issues"])

    policy = RuleBasedPolicy(naive=False)
    history, info, reward = _run_policy_in_env(policy, 301, cfg, ep)
    findings = history[-1][0]["args"]["findings"]
    eval_res = evaluate(findings, truth, ep.name, len(history) - 2)
    assert eval_res["valid"] is True
    # Проверяем, что для заданного типа ошибки есть TP
    found_types = {f["type"] for f in findings["findings"]}
    assert error_type in found_types
    assert eval_res["metrics"]["tp"] >= 1


@pytest.mark.parametrize("scenario", [
    "partial_fx_payment_at_cutoff",
    "duplicate_invoice_wrong_fx",
])
def test_rule_based_detects_combined_scenarios(tmp_path, scenario):
    cfg = {
        "config_version": f"test-{scenario}",
        "mode": "train",
        "errors": {
            t: {"enabled": False}
            for t in ["duplicate_invoice", "payment_allocation", "fx_conversion", "period_cutoff"]
        },
        "interactions": {
            s: ({"enabled": True, "count": [1, 1]} if s == scenario else {"enabled": False})
            for s in ["partial_fx_payment_at_cutoff", "duplicate_invoice_wrong_fx"]
        },
        "distractors": {
            "split_payment": {"enabled": True, "count": [1, 1]},
            "refund": {"enabled": True, "count": [1, 1]},
            "legit_recurring": {"enabled": True, "count": [1, 1]},
            "late_evening_local": {"enabled": True, "count": [1, 1]},
        },
        "clean": False,
        "max_errors": 12,
    }
    ep = generate_episode(400 + hash(scenario) % 50, cfg, tmp_path)
    truth = json.loads((ep / "private" / "truth.json").read_text(encoding="utf-8"))
    assert len(truth["issues"]) == 2  # Совмещённый сценарий порождает ровно 2 ошибки

    policy = RuleBasedPolicy(naive=False)
    history, info, reward = _run_policy_in_env(policy, 401, cfg, ep)
    findings = history[-1][0]["args"]["findings"]
    eval_res = evaluate(findings, truth, ep.name, len(history) - 2)
    assert eval_res["valid"] is True
    assert eval_res["metrics"]["tp"] >= 1


def test_flag_all_policy_fails_gracefully(tmp_path):
    ep = generate_episode(105, ALL_ERRORS_CFG, tmp_path)
    truth = json.loads((ep / "private" / "truth.json").read_text(encoding="utf-8"))
    policy = FlagAllPolicy()
    history, info, reward = _run_policy_in_env(policy, 105, ALL_ERRORS_CFG, ep)
    submit_action = history[-1][0]
    assert submit_action["tool"] == "submit"
    findings = submit_action["args"]["findings"]["findings"]
    assert len(findings) > 0
    eval_res = evaluate(submit_action["args"]["findings"], truth, ep.name, len(history) - 2)
    assert eval_res["valid"] is True
    assert eval_res["reward"] < 0.0


def test_random_policy(tmp_path):
    ep = generate_episode(106, ALL_ERRORS_CFG, tmp_path)
    policy = RandomPolicy(seed=42, max_explore_steps=3)
    history, info, reward = _run_policy_in_env(policy, 106, ALL_ERRORS_CFG, ep)
    assert history[-1][3] is True  # terminated
    submit_action = history[-1][0]
    assert submit_action["tool"] == "submit"
