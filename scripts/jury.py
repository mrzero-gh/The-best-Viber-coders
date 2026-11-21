"""Скрипт самопроверки методикой жюри: python -m scripts.jury."""
import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

from env.client import AuditEnv
from generator.build import generate_episode
from policies import FlagAllPolicy, OraclePolicy, RandomPolicy, RuleBasedPolicy, SimpleRulePolicy

ROOT = Path(__file__).resolve().parent.parent

TEST_SCENARIOS = [
    {
        "name": "Чистая компания с отвлекающими случаями",
        "clean": True,
        "distractors": {"split_payment": {"enabled": True, "count": [1, 2]},
                        "refund": {"enabled": True, "count": [1, 1]},
                        "legit_recurring": {"enabled": True, "count": [1, 1]},
                        "late_evening_local": {"enabled": True, "count": [1, 1]}},
        "errors": {t: {"enabled": False} for t in ["duplicate_invoice", "payment_allocation", "fx_conversion", "period_cutoff"]},
        "interactions": {s: {"enabled": False} for s in ["partial_fx_payment_at_cutoff", "duplicate_invoice_wrong_fx"]},
    },
    {
        "name": "Одиночные ошибки 4 типов",
        "clean": False,
        "distractors": {"split_payment": {"enabled": True, "count": [1, 1]},
                        "refund": {"enabled": False},
                        "legit_recurring": {"enabled": True, "count": [1, 1]},
                        "late_evening_local": {"enabled": True, "count": [1, 1]}},
        "errors": {t: {"enabled": True, "count": [1, 1]} for t in ["duplicate_invoice", "payment_allocation", "fx_conversion", "period_cutoff"]},
        "interactions": {s: {"enabled": False} for s in ["partial_fx_payment_at_cutoff", "duplicate_invoice_wrong_fx"]},
    },
    {
        "name": "Совмещённые сценарии (2 взаимодействия)",
        "clean": False,
        "distractors": {"split_payment": {"enabled": True, "count": [1, 1]},
                        "refund": {"enabled": True, "count": [1, 1]},
                        "legit_recurring": {"enabled": False},
                        "late_evening_local": {"enabled": True, "count": [1, 1]}},
        "errors": {t: {"enabled": False} for t in ["duplicate_invoice", "payment_allocation", "fx_conversion", "period_cutoff"]},
        "interactions": {"partial_fx_payment_at_cutoff": {"enabled": True, "count": [1, 1]},
                        "duplicate_invoice_wrong_fx": {"enabled": True, "count": [1, 1]}},
    },
    {
        "name": "Полный комплекс (все типы + взаимодействия + отвлекающие)",
        "clean": False,
        "distractors": {"split_payment": {"enabled": True, "count": [1, 2]},
                        "refund": {"enabled": True, "count": [1, 1]},
                        "legit_recurring": {"enabled": True, "count": [1, 2]},
                        "late_evening_local": {"enabled": True, "count": [1, 1]}},
        "errors": {t: {"enabled": True, "count": [1, 2]} for t in ["duplicate_invoice", "payment_allocation", "fx_conversion", "period_cutoff"]},
        "interactions": {"partial_fx_payment_at_cutoff": {"enabled": True, "count": [1, 1]},
                        "duplicate_invoice_wrong_fx": {"enabled": True, "count": [1, 1]}},
    },
]


def evaluate_policy_on_episode(policy, episode_path: Path, grader_str: str) -> dict:
    t0 = time.monotonic()
    truth = json.loads((episode_path / "private" / "truth.json").read_text(encoding="utf-8"))
    policy.reset()
    if isinstance(policy, OraclePolicy):
        policy.set_truth(truth)

    steps = 0
    with AuditEnv(episode=str(episode_path), grader=grader_str) as env:
        obs, info = env.reset(seed=0, config={"mode": "train"})
        terminated = truncated = False
        while not (terminated or truncated):
            action = policy.act(obs)
            obs, reward, terminated, truncated, info = env.step(action)
            steps += 1
            if terminated or truncated:
                # В режиме train metrics приходят в info
                metrics = info.get("metrics") or {}
                elapsed = time.monotonic() - t0
                return {
                    "reward": reward,
                    "steps": steps,
                    "time_s": elapsed,
                    "p": metrics.get("precision", 0.0),
                    "r": metrics.get("recall", 0.0),
                    "f1": metrics.get("f1", 0.0),
                    "tp": metrics.get("tp", 0),
                    "fp": metrics.get("fp", 0),
                    "fn": metrics.get("fn", 0),
                }

    elapsed = time.monotonic() - t0
    return {"reward": -1.0, "steps": steps, "time_s": elapsed, "p": 0.0, "r": 0.0, "f1": 0.0, "tp": 0, "fp": 0, "fn": 0}


def _check_reproducibility(tpath: Path, grader_str: str):
    """Тот же seed и config дают побайтно те же public-файлы, а повторный прогон SmartAuditor -
    тот же итог. Возвращает (ok, пояснение)."""
    cfg = _scenario_config(1, TEST_SCENARIOS[1])
    eps = [generate_episode(7790, cfg, tpath / f"repro-{k}") for k in (1, 2)]
    shas = [json.loads((e / "private" / "generation.json").read_text(encoding="utf-8"))["episode_sha256"]
            for e in eps]
    keys = ("reward", "steps", "tp", "fp", "fn", "f1")
    runs = [evaluate_policy_on_episode(RuleBasedPolicy(naive=False), eps[0], grader_str) for _ in (1, 2)]
    same_files = shas[0] == shas[1]
    same_run = all(runs[0][k] == runs[1][k] for k in keys)
    if same_files and same_run:
        return True, "Повторная генерация seed 7790 и повторный прогон SmartAuditor совпали"
    return False, ("Не воспроизводится: " + ", ".join(
        x for x, bad in (("файлы эпизода", not same_files), ("итог прогона", not same_run)) if bad))


def _scenario_config(scen_idx: int, scen: dict) -> dict:
    return {
        "config_version": f"jury-scen-{scen_idx}",
        "mode": "train",
        "clean": scen["clean"],
        "company": {"archetype": "importer", "base_currency": "KZT", "timezone": "+05:00",
                    "foreign_currencies": ["USD", "CNY", "EUR", "RUB", "JPY"]},
        "size": {"ledger_rows": [200, 450], "documents": [25, 60]},
        "errors": scen["errors"],
        "interactions": scen["interactions"],
        "distractors": scen["distractors"],
        "max_errors": 10,
    }


def run_jury_evaluation(grader_str: str = "grader.evaluate:evaluate", seeds_per_scenario: int = 2):
    print("=================================================================")
    print("      AUDIT GYM — ЭКЗАМЕНАЦИОННЫЙ ПРОГОН МЕТОДИКОЙ ЖЮРИ         ")
    print("=================================================================")
    print(f"Используемый оценщик: {grader_str}\n")

    policies = {
        "OraclePolicy": OraclePolicy(),
        "SmartAuditor": RuleBasedPolicy(naive=False),
        "SimpleRule": SimpleRulePolicy(),
        "FlagAllPolicy": FlagAllPolicy(),
        "RandomPolicy": RandomPolicy(seed=42),
    }

    results = {name: {"reward": [], "f1": [], "p": [], "r": [], "steps": [], "time_s": []} for name in policies}
    per_episode = []  # (номер сценария, чистый ли эпизод, политика, результат)

    with tempfile.TemporaryDirectory() as td:
        tpath = Path(td)
        total_episodes = 0

        for scen_idx, scen in enumerate(TEST_SCENARIOS, 1):
            cfg = _scenario_config(scen_idx, scen)

            print(f"Сценарий {scen_idx}/4: {scen['name']}")
            for s_offset in range(seeds_per_scenario):
                seed = 7700 + scen_idx * 10 + s_offset
                ep = generate_episode(seed, cfg, tpath)
                total_episodes += 1

                for pol_name, policy in policies.items():
                    res = evaluate_policy_on_episode(policy, ep, grader_str)
                    per_episode.append((scen_idx, scen["clean"], pol_name, res))
                    results[pol_name]["reward"].append(res["reward"])
                    results[pol_name]["f1"].append(res["f1"])
                    results[pol_name]["p"].append(res["p"])
                    results[pol_name]["r"].append(res["r"])
                    results[pol_name]["steps"].append(res["steps"])
                    results[pol_name]["time_s"].append(res["time_s"])

        repro_ok, repro_note = _check_reproducibility(tpath, grader_str)

    print("\n" + "-" * 88)
    print(f"{'Политика':<18} | {'Precision':<10} | {'Recall':<8} | {'F1':<8} | {'Reward':<10} | {'Шаги':<6} | {'Время (с)':<9}")
    print("-" * 88)
    for pol_name, data in results.items():
        avg_p = sum(data["p"]) / len(data["p"])
        avg_r = sum(data["r"]) / len(data["r"])
        avg_f1 = sum(data["f1"]) / len(data["f1"])
        avg_rew = sum(data["reward"]) / len(data["reward"])
        avg_st = sum(data["steps"]) / len(data["steps"])
        avg_time = sum(data["time_s"]) / len(data["time_s"])
        print(f"{pol_name:<18} | {avg_p:<10.3f} | {avg_r:<8.3f} | {avg_f1:<8.3f} | {avg_rew:<10.3f} | {avg_st:<6.1f} | {avg_time:<9.3f}")
    print("-" * 88)
    print("Диагностическое примечание:")
    print("  * В чистых эпизодах при пустом эталоне и пустых выводах F1=1.000, а Precision и Recall")
    print("    равны 0.000 по правилу контракта (0/0 = 0.0), поэтому средние P и R ниже F1.")
    oracle_err = [r for _, clean, name, r in per_episode if name == "OraclePolicy" and not clean]
    perfect = sum(1 for r in oracle_err if r["p"] == r["r"] == r["f1"] == 1.0)
    print(f"  * Эпизоды с ошибками: Oracle получил P=R=F1=1.000 на {perfect} из {len(oracle_err)}.\n")

    # -----------------------------------------------------------------
    # САМОПРОВЕРКА: проходят ли ключевые свойства среды на этом прогоне.
    # Это не оценка жюри и не баллы, а быстрый индикатор «ничего не сломалось».
    # -----------------------------------------------------------------
    print("\n=================================================================")
    print("              САМОПРОВЕРКА ПО РЕЗУЛЬТАТАМ ПРОГОНА")
    print("=================================================================")

    # 1. Разрешимость эталона: Oracle находит всё
    oracle_avg_f1 = sum(results["OraclePolicy"]["f1"]) / len(results["OraclePolicy"]["f1"])
    data_ok = oracle_avg_f1 == 1.0
    note_data = f"Oracle F1 = {oracle_avg_f1:.3f}"

    # 2. Лимит шагов соблюдён (полный контракт и изоляцию проверяют тесты: scripts.check)
    steps_ok = all(s <= 100 for s in results["OraclePolicy"]["steps"] + results["SmartAuditor"]["steps"])
    note_steps = "шаги Oracle и SmartAuditor <= 100" if steps_ok else "превышен лимит шагов"

    # 3. Атака «пометить всё» не окупается
    flagall_avg_rew = sum(results["FlagAllPolicy"]["reward"]) / len(results["FlagAllPolicy"]["reward"])
    reward_ok = flagall_avg_rew <= -0.99
    note_reward = f"награда FlagAll = {flagall_avg_rew:.3f}"

    # 4. Воспроизводимость: тот же seed -> те же файлы, тот же прогон -> тот же итог
    note_repro = repro_note

    # 5. Время эпизода в пределах 60 с
    max_ep_time = max(max(data["time_s"]) for data in results.values())
    time_ok = max_ep_time < 60.0
    note_time = f"макс. время эпизода {max_ep_time:.3f} с (лимит 60 с)"

    checks = [
        ("Разрешимость эталона", data_ok, note_data),
        ("Лимит шагов", steps_ok, note_steps),
        ("Атака «пометить всё»", reward_ok, note_reward),
        ("Воспроизводимость", repro_ok, note_repro),
        ("Время эпизода", time_ok, note_time),
    ]
    for name, ok, note in checks:
        print(f"  {'OK  ' if ok else 'FAIL'} {name:<22} | {note}")
    print("=" * 65)
    passed = sum(1 for _, ok, _ in checks if ok)
    print(f"САМОПРОВЕРКА: {passed}/{len(checks)} пройдено. Полная проверка: python -m scripts.check")
    print("=================================================================\n")
    return 0 if passed == len(checks) else 1


def main():
    parser = argparse.ArgumentParser(description="Запуск проверки методикой жюри")
    parser.add_argument("--grader", default="grader.evaluate:evaluate",
                        help="Путь к функции оценщика module:func (по умолчанию боевой оценщик grader.evaluate:evaluate)")
    parser.add_argument("--seeds", type=int, default=2, help="Число seed на сценарий (не меньше 1)")
    args = parser.parse_args()
    if args.seeds < 1:
        parser.error("--seeds: нужно целое число не меньше 1")

    return run_jury_evaluation(args.grader, args.seeds)


if __name__ == "__main__":
    sys.exit(main())
