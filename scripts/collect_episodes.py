"""Сборка эпизодов и запись реальных траекторий в episodes/."""
import argparse
import json
from pathlib import Path

from common.jsonio import canonical_dumps
from env.client import AuditEnv
from generator.build import generate_episode
from policies import RuleBasedPolicy, OraclePolicy

ROOT = Path(__file__).resolve().parent.parent

TRAIN_CONFIG = {
    "config_version": "train-v1",
    "mode": "train",
    "company": {
        "archetype": "importer",
        "base_currency": "KZT",
        "timezone": "+05:00",
        "foreign_currencies": ["USD", "CNY", "EUR", "RUB", "JPY"],
    },
    "size": {
        "ledger_rows": [250, 600],
        "documents": [30, 80],
    },
    "errors": {
        "duplicate_invoice": {"enabled": True, "count": [1, 2]},
        "payment_allocation": {"enabled": True, "count": [1, 2]},
        "fx_conversion": {"enabled": True, "count": [1, 2]},
        "period_cutoff": {"enabled": True, "count": [1, 2]},
    },
    "interactions": {
        "partial_fx_payment_at_cutoff": {"enabled": True, "count": [0, 1]},
        "duplicate_invoice_wrong_fx": {"enabled": True, "count": [0, 1]},
    },
    "distractors": {
        "split_payment": {"enabled": True, "count": [1, 2]},
        "refund": {"enabled": True, "count": [0, 2]},
        "legit_recurring": {"enabled": True, "count": [1, 2]},
        "late_evening_local": {"enabled": True, "count": [1, 2]},
    },
    "clean": False,
    "max_errors": 10,
}

DEV_CONFIG = dict(TRAIN_CONFIG, config_version="dev-v1", mode="eval")


def run_episode_and_record_trajectory(ep_dir: Path, cfg: dict, policy) -> list[dict]:
    trajectory = []
    policy.reset()
    with AuditEnv(episode=str(ep_dir)) as env:
        obs, info = env.reset(seed=0, config=cfg)
        ep_id = obs["manifest"]["episode_id"]

        # Шаг 0 (сброс)
        step_idx = 0
        trajectory.append({
            "episode_id": ep_id,
            "step": step_idx,
            "action": None,
            "observation": obs,
            "reward": 0.0,
            "terminated": False,
            "truncated": False,
        })

        terminated = truncated = False
        while not (terminated or truncated):
            action = policy.act(obs)
            obs, reward, terminated, truncated, info = env.step(action)
            step_idx += 1
            trajectory.append({
                "episode_id": ep_id,
                "step": step_idx,
                "action": action,
                "observation": obs,
                "reward": float(reward),
                "terminated": bool(terminated),
                "truncated": bool(truncated),
            })

    return trajectory


def generate_and_collect(train_count: int = 10, dev_count: int = 10, base_seed: int = 1000):
    train_dir = ROOT / "episodes" / "train"
    dev_dir = ROOT / "episodes" / "dev"
    train_dir.mkdir(parents=True, exist_ok=True)
    dev_dir.mkdir(parents=True, exist_ok=True)

    all_trajectories = []
    policy = RuleBasedPolicy(naive=False)

    print(f"Генерация {train_count} train-эпизодов...")
    for idx in range(train_count):
        seed = base_seed + idx
        ep = generate_episode(seed, TRAIN_CONFIG, train_dir)
        traj = run_episode_and_record_trajectory(ep, TRAIN_CONFIG, policy)
        all_trajectories.extend(traj)
        # Записываем в папку эпизода
        traj_file = ep / "trajectories.jsonl"
        with open(traj_file, "w", encoding="utf-8", newline="\n") as f:
            for item in traj:
                f.write(canonical_dumps(item) + "\n")
        print(f"  [train] seed={seed} -> {ep.name} (шагов: {len(traj)-1}, финальная награда: {traj[-1]['reward']:.4f})")

    print(f"Генерация {dev_count} dev-эпизодов (eval-режим)...")
    for idx in range(dev_count):
        seed = base_seed + 1000 + idx
        ep = generate_episode(seed, DEV_CONFIG, dev_dir)
        traj = run_episode_and_record_trajectory(ep, DEV_CONFIG, policy)
        all_trajectories.extend(traj)
        traj_file = ep / "trajectories.jsonl"
        with open(traj_file, "w", encoding="utf-8", newline="\n") as f:
            for item in traj:
                f.write(canonical_dumps(item) + "\n")
        print(f"  [dev] seed={seed} -> {ep.name} (шагов: {len(traj)-1}, eval-награда: {traj[-1]['reward']})")

    # Сводный файл траекторий (сохраняем и в episodes/trajectories.jsonl, и в корень для совместимости)
    master_traj = ROOT / "episodes" / "trajectories.jsonl"
    root_traj = ROOT / "trajectories.jsonl"
    content = "".join(canonical_dumps(item) + "\n" for item in all_trajectories)
    master_traj.write_text(content, encoding="utf-8")
    root_traj.write_text(content, encoding="utf-8")
    print(f"Всего шагов траекторий сохранено в {master_traj} и {root_traj}: {len(all_trajectories)}")


def main():
    parser = argparse.ArgumentParser(description="Сборка эпизодов и траекторий")
    parser.add_argument("--train", type=int, default=10, help="Число train эпизодов")
    parser.add_argument("--dev", type=int, default=10, help="Число dev эпизодов")
    parser.add_argument("--seed", type=int, default=1000, help="Базовый seed")
    args = parser.parse_args()
    generate_and_collect(args.train, args.dev, args.seed)


if __name__ == "__main__":
    main()
