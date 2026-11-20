"""Парные и метаморфные тесты для проверки инвариантов политик и воспроизводимости.

Запуск:
    python -m scripts.test_metamorphic
или через pytest:
    pytest tests/test_metamorphic.py
"""
import json
import sys
import tempfile
from pathlib import Path

from env.client import AuditEnv
from generator.build import generate_episode
from grader.evaluate import evaluate
from policies import OraclePolicy, RuleBasedPolicy, FlagAllPolicy

CLEAN_WITH_DISTRACTORS = {
    "config_version": "meta-clean",
    "mode": "train",
    "clean": True,
    "distractors": {
        "split_payment": {"enabled": True, "count": [1, 2]},
        "refund": {"enabled": True, "count": [1, 2]},
        "legit_recurring": {"enabled": True, "count": [1, 2]},
        "late_evening_local": {"enabled": True, "count": [1, 2]},
    },
    "errors": {
        "duplicate_invoice": {"enabled": False},
        "payment_allocation": {"enabled": False},
        "fx_conversion": {"enabled": False},
        "period_cutoff": {"enabled": False},
    },
    "interactions": {
        "partial_fx_payment_at_cutoff": {"enabled": False},
        "duplicate_invoice_wrong_fx": {"enabled": False},
    },
    "max_errors": 12,
}

ERRORS_WITH_DISTRACTORS = {
    "config_version": "meta-errors",
    "mode": "train",
    "clean": False,
    "distractors": {
        "split_payment": {"enabled": True, "count": [1, 1]},
        "refund": {"enabled": True, "count": [1, 1]},
        "legit_recurring": {"enabled": True, "count": [1, 1]},
        "late_evening_local": {"enabled": True, "count": [1, 1]},
    },
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
    "max_errors": 12,
}


def run_policy(policy, episode_path: Path):
    policy.reset()
    actions = []
    with AuditEnv(episode=str(episode_path)) as env:
        obs, info = env.reset(seed=0, config={"mode": "train"})
        terminated = truncated = False
        while not (terminated or truncated):
            action = policy.act(obs)
            actions.append(action)
            obs, reward, terminated, truncated, info = env.step(action)
    return actions, reward


def test_determinism_fingerprint(tmp_path: Path):
    """Инвариант 1: Тот же seed -> тот же sha256 отпечаток эпизода."""
    for seed in [701, 702, 703]:
        dir_a = tmp_path / f"a_{seed}"
        dir_b = tmp_path / f"b_{seed}"
        ep_a = generate_episode(seed, ERRORS_WITH_DISTRACTORS, dir_a)
        ep_b = generate_episode(seed, ERRORS_WITH_DISTRACTORS, dir_b)

        gen_a = json.loads((ep_a / "private" / "generation.json").read_text(encoding="utf-8"))
        gen_b = json.loads((ep_b / "private" / "generation.json").read_text(encoding="utf-8"))

        assert gen_a["episode_sha256"] == gen_b["episode_sha256"], f"Хэш не сошёлся для seed {seed}"
        assert gen_a["public_sha256"] == gen_b["public_sha256"]


def test_paired_id_permutation(tmp_path: Path):
    """Инвариант 2 (Парный тест): Смена id_seed сохраняет структуру ошибок."""
    seed = 801
    ep_orig = generate_episode(seed, ERRORS_WITH_DISTRACTORS, tmp_path / "orig")
    ep_perm = generate_episode(seed, ERRORS_WITH_DISTRACTORS, tmp_path / "perm", id_seed=seed + 9999)

    truth_orig = json.loads((ep_orig / "private" / "truth.json").read_text(encoding="utf-8"))
    truth_perm = json.loads((ep_perm / "private" / "truth.json").read_text(encoding="utf-8"))

    # Состав ошибок (тип, сумма, валюта) идентичен независимо от смены ID
    sig_orig = sorted((i["type"], i["expected_amount_minor"], i["currency"]) for i in truth_orig["issues"])
    sig_perm = sorted((i["type"], i["expected_amount_minor"], i["currency"]) for i in truth_perm["issues"])
    assert sig_orig == sig_perm, "Структура ошибок изменилась при смене id_seed"

    # RuleBasedPolicy находит то же число ошибок одинаковых типов
    policy = RuleBasedPolicy(naive=False)
    actions_orig, _ = run_policy(policy, ep_orig)
    findings_orig = actions_orig[-1]["args"]["findings"]
    eval_orig = evaluate(findings_orig, truth_orig, ep_orig.name, len(actions_orig) - 1)

    actions_perm, _ = run_policy(policy, ep_perm)
    findings_perm = actions_perm[-1]["args"]["findings"]
    eval_perm = evaluate(findings_perm, truth_perm, ep_perm.name, len(actions_perm) - 1)

    assert eval_orig["metrics"]["tp"] == eval_perm["metrics"]["tp"], "Число TP разошлось при смене ID"
    assert eval_orig["metrics"]["f1"] == eval_perm["metrics"]["f1"], "F1 разошёлся при смене ID"


def test_clean_and_distractors_invariants(tmp_path: Path):
    """Инвариант 3: На чистых данных со всеми 4 отвлекающими случаями FP = 0."""
    for seed in [901, 902, 903]:
        ep = generate_episode(seed, CLEAN_WITH_DISTRACTORS, tmp_path / f"clean_{seed}")
        truth = json.loads((ep / "private" / "truth.json").read_text(encoding="utf-8"))
        assert len(truth["distractors"]) >= 4

        policy = RuleBasedPolicy(naive=False)
        actions, _ = run_policy(policy, ep)
        findings = actions[-1]["args"]["findings"]["findings"]
        assert len(findings) == 0, f"Ложные срабатывания на чистых отвлекающих данных: {findings}"

        eval_res = evaluate({"episode_id": ep.name, "findings": findings}, truth, ep.name, len(actions) - 1)
        assert eval_res["metrics"]["fp"] == 0
        assert eval_res["metrics"]["f1"] == 1.0


def test_policy_run_determinism(tmp_path: Path):
    """Инвариант 4: Повторный прогон политики на одном эпизоде абсолютно детерминирован."""
    ep = generate_episode(950, ERRORS_WITH_DISTRACTORS, tmp_path / "det")
    policy = RuleBasedPolicy(naive=False)

    actions1, rew1 = run_policy(policy, ep)
    actions2, rew2 = run_policy(policy, ep)

    assert len(actions1) == len(actions2)
    assert actions1 == actions2, "Действия политики различаются между повторными прогонами"
    assert rew1 == rew2


def test_canary_leak_check(tmp_path: Path):
    """Инвариант 5: Закрытые метки-канарейки (issue_id) не утекают в наблюдения."""
    ep = generate_episode(960, ERRORS_WITH_DISTRACTORS, tmp_path / "canary")
    truth = json.loads((ep / "private" / "truth.json").read_text(encoding="utf-8"))
    canary_issue_ids = {i["issue_id"] for i in truth["issues"]}

    policy = RuleBasedPolicy(naive=False)
    with AuditEnv(episode=str(ep)) as env:
        obs, _ = env.reset(seed=0, config={"mode": "train"})
        for canary in canary_issue_ids:
            assert canary not in json.dumps(obs), f"Утечка канарейки {canary} в reset!"
        terminated = truncated = False
        while not (terminated or truncated):
            action = policy.act(obs)
            obs, _, terminated, truncated, _ = env.step(action)
            obs_json = json.dumps(obs)
            for canary in canary_issue_ids:
                assert canary not in obs_json, f"Утечка канарейки {canary} в наблюдении!"


def main():
    print("=== Запуск парных и метаморфных тестов ===")
    with tempfile.TemporaryDirectory() as td:
        tpath = Path(td)
        print("1. Тест детерминизма и отпечатка (fingerprint)...", end=" ", flush=True)
        test_determinism_fingerprint(tpath)
        print("OK")

        print("2. Парный тест (смена ID / id_seed)...", end=" ", flush=True)
        test_paired_id_permutation(tpath)
        print("OK")

        print("3. Тест инварианта чистых данных и отвлекающих случаев...", end=" ", flush=True)
        test_clean_and_distractors_invariants(tpath)
        print("OK")

        print("4. Тест воспроизводимости траектории политики...", end=" ", flush=True)
        test_policy_run_determinism(tpath)
        print("OK")

        print("5. Тест отсутствия утечек канареек (canary audit)...", end=" ", flush=True)
        test_canary_leak_check(tpath)
        print("OK")

    print("\n>>> ВСЕ ПАРНЫЕ И МЕТАМОРФНЫЕ ТЕСТЫ ПРОЙДЕНЫ УСПЕШНО! (Критерий «Воспроизводимость»)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
