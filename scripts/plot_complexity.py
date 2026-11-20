"""Построение графика сложности: python -m scripts.plot_complexity [--seeds N] [--out путь.png].

Нужен matplotlib: pip install -r requirements-plot.txt. Без него скрипт печатает таблицу
и завершается с кодом 1 (график не обновлён)."""
import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from env.client import AuditEnv
from generator.build import generate_episode
from policies import FlagAllPolicy, OraclePolicy, RandomPolicy, RuleBasedPolicy

ROOT = Path(__file__).resolve().parent.parent

COMPLEXITY_LEVELS = [
    {
        "level": "1. Чистая",
        "clean": True,
        "errors": {t: {"enabled": False} for t in ["duplicate_invoice", "payment_allocation", "fx_conversion", "period_cutoff"]},
        "interactions": {s: {"enabled": False} for s in ["partial_fx_payment_at_cutoff", "duplicate_invoice_wrong_fx"]},
        "distractors": {k: {"enabled": False} for k in ["split_payment", "refund", "legit_recurring", "late_evening_local"]},
    },
    {
        "level": "2. Простая",
        "clean": False,
        "errors": {
            "duplicate_invoice": {"enabled": True, "count": [1, 1]},
            "payment_allocation": {"enabled": False},
            "fx_conversion": {"enabled": False},
            "period_cutoff": {"enabled": False},
        },
        "interactions": {s: {"enabled": False} for s in ["partial_fx_payment_at_cutoff", "duplicate_invoice_wrong_fx"]},
        "distractors": {k: {"enabled": False} for k in ["split_payment", "refund", "legit_recurring", "late_evening_local"]},
    },
    {
        "level": "3. Средняя",
        "clean": False,
        "errors": {
            "duplicate_invoice": {"enabled": True, "count": [1, 1]},
            "payment_allocation": {"enabled": True, "count": [1, 1]},
            "fx_conversion": {"enabled": False},
            "period_cutoff": {"enabled": False},
        },
        "interactions": {s: {"enabled": False} for s in ["partial_fx_payment_at_cutoff", "duplicate_invoice_wrong_fx"]},
        "distractors": {
            "split_payment": {"enabled": True, "count": [1, 1]},
            "refund": {"enabled": True, "count": [1, 1]},
            "legit_recurring": {"enabled": False},
            "late_evening_local": {"enabled": False},
        },
    },
    {
        "level": "4. Сложная",
        "clean": False,
        "errors": {t: {"enabled": True, "count": [1, 1]} for t in ["duplicate_invoice", "payment_allocation", "fx_conversion", "period_cutoff"]},
        "interactions": {s: {"enabled": False} for s in ["partial_fx_payment_at_cutoff", "duplicate_invoice_wrong_fx"]},
        "distractors": {k: {"enabled": True, "count": [1, 1]} for k in ["split_payment", "refund", "legit_recurring", "late_evening_local"]},
    },
    {
        "level": "5. Эксперт",
        "clean": False,
        "errors": {t: {"enabled": True, "count": [1, 2]} for t in ["duplicate_invoice", "payment_allocation", "fx_conversion", "period_cutoff"]},
        "interactions": {s: {"enabled": True, "count": [1, 1]} for s in ["partial_fx_payment_at_cutoff", "duplicate_invoice_wrong_fx"]},
        "distractors": {k: {"enabled": True, "count": [1, 2]} for k in ["split_payment", "refund", "legit_recurring", "late_evening_local"]},
    },
]


def eval_policy(policy, ep_path: Path, grader_str: str) -> dict:
    truth = json.loads((ep_path / "private" / "truth.json").read_text(encoding="utf-8"))
    policy.reset()
    if isinstance(policy, OraclePolicy):
        policy.set_truth(truth)

    with AuditEnv(episode=str(ep_path), grader=grader_str) as env:
        obs, info = env.reset(seed=0, config={"mode": "train"})
        terminated = truncated = False
        while not (terminated or truncated):
            action = policy.act(obs)
            obs, reward, terminated, truncated, info = env.step(action)
            if terminated or truncated:
                metrics = info.get("metrics") or {}
                return {
                    "f1": metrics.get("f1", 0.0),
                    "reward": reward,
                    "p": metrics.get("precision", 0.0),
                    "r": metrics.get("recall", 0.0),
                }
    return {"f1": 0.0, "reward": -1.0, "p": 0.0, "r": 0.0}


DEFAULT_OUT = ROOT / "docs" / "complexity_curve.png"


def build_complexity_curve(grader_str: str = "grader.evaluate:evaluate", seeds_per_level: int = 2,
                           out_img: Path = DEFAULT_OUT) -> int:
    """Таблица F1 по уровням и график в out_img. 0 - график сохранён, 1 - нет."""
    print(f"Построение кривой сложности с оценщиком: {grader_str}")
    policies = {
        "Oracle": OraclePolicy(),
        "Smart Auditor": RuleBasedPolicy(naive=False),
        "Naive Auditor": RuleBasedPolicy(naive=True),
        "Flag All": FlagAllPolicy(),
        "Random": RandomPolicy(seed=42),
    }

    levels = [item["level"] for item in COMPLEXITY_LEVELS]
    results_f1 = {name: [] for name in policies}
    results_rew = {name: [] for name in policies}

    with tempfile.TemporaryDirectory() as td:
        tpath = Path(td)

        for lvl_idx, lvl in enumerate(COMPLEXITY_LEVELS, 1):
            cfg = {
                "config_version": f"curve-lvl-{lvl_idx}",
                "mode": "train",
                "clean": lvl["clean"],
                "company": {"archetype": "importer", "base_currency": "KZT", "timezone": "+05:00",
                            "foreign_currencies": ["USD", "CNY", "EUR", "RUB", "JPY"]},
                "size": {"ledger_rows": [200, 450], "documents": [25, 60]},
                "errors": lvl["errors"],
                "interactions": lvl["interactions"],
                "distractors": lvl["distractors"],
                "max_errors": 10,
            }

            lvl_f1 = {name: [] for name in policies}
            lvl_rew = {name: [] for name in policies}

            for s_idx in range(seeds_per_level):
                seed = 8800 + lvl_idx * 10 + s_idx
                ep = generate_episode(seed, cfg, tpath)

                for pol_name, pol in policies.items():
                    res = eval_policy(pol, ep, grader_str)
                    lvl_f1[pol_name].append(res["f1"])
                    lvl_rew[pol_name].append(res["reward"])

            for pol_name in policies:
                results_f1[pol_name].append(sum(lvl_f1[pol_name]) / len(lvl_f1[pol_name]))
                results_rew[pol_name].append(sum(lvl_rew[pol_name]) / len(lvl_rew[pol_name]))

    print("\nТаблица зависимости F1 от сложности:")
    header = f"{'Уровень сложности':<16} | " + " | ".join(f"{p:<14}" for p in policies)
    print("-" * len(header))
    print(header)
    print("-" * len(header))
    for i, lvl in enumerate(levels):
        row = f"{lvl:<16} | " + " | ".join(f"{results_f1[p][i]:<14.3f}" for p in policies)
        print(row)
    print("-" * len(header))

    # Рисуем график
    out_img = Path(out_img)
    try:
        import matplotlib
    except ImportError:
        print("\nОшибка: нет matplotlib, график не обновлён. Установите: pip install -r requirements-plot.txt")
        return 1
    out_img.parent.mkdir(parents=True, exist_ok=True)
    try:
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        plt.figure(figsize=(10, 6), dpi=150)
        colors = {
            "Oracle": "#2ca02c",         # green
            "Smart Auditor": "#1f77b4",   # blue
            "Naive Auditor": "#ff7f0e",   # orange
            "Flag All": "#d62728",        # red
            "Random": "#7f7f7f",          # gray
        }
        styles = {
            "Oracle": "o-",
            "Smart Auditor": "s--",
            "Naive Auditor": "^-.",
            "Flag All": "x:",
            "Random": "v:",
        }

        for pol_name in policies:
            plt.plot(levels, results_f1[pol_name], styles[pol_name],
                     color=colors[pol_name], label=pol_name, linewidth=2, markersize=8)

        plt.title("Зависимость качества (F1-score) политик от сложности окружения", fontsize=14, pad=12)
        plt.xlabel("Уровень сложности окружения", fontsize=12)
        plt.ylabel("Метрика F1-score", fontsize=12)
        plt.ylim(-0.05, 1.05)
        plt.grid(True, linestyle="--", alpha=0.6)
        plt.legend(loc="lower left", fontsize=10)
        plt.tight_layout()
        plt.savefig(out_img)
        plt.close()
        print(f"\nГрафик успешно сохранён в: {out_img}")
    except Exception as e:
        print(f"Ошибка: не удалось сохранить изображение графика: {e}")
        return 1

    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Построение графика сложности")
    parser.add_argument("--grader", default="grader.evaluate:evaluate",
                        help="Функция оценщика (по умолчанию боевой оценщик grader.evaluate:evaluate)")
    parser.add_argument("--seeds", type=int, default=1, help="Число seed на уровень (не меньше 1)")
    parser.add_argument("--out", default=str(DEFAULT_OUT),
                        help="Куда сохранить PNG (по умолчанию docs/complexity_curve.png)")
    args = parser.parse_args(argv)
    if args.seeds < 1:
        parser.error("--seeds: нужно целое число не меньше 1")
    return build_complexity_curve(args.grader, args.seeds, Path(args.out))


if __name__ == "__main__":
    sys.exit(main())
