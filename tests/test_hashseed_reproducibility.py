"""Воспроизводимость между процессами: результат не зависит от PYTHONHASHSEED.

Порядок обхода set в Python зависит от случайной соли хэшей процесса. Найдено на сборке из
чистого клона (#28): RuleBasedPolicy запрашивала документы в порядке set, и траектории
отличались от запуска к запуску. Жюри требует точного повтора записей, переходов и наград.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_SCRIPT = r"""
import json, sys, tempfile
from pathlib import Path
from env.core import AuditCore
from env.episode import load_public, load_truth
from generator.build import generate_episode
from policies import POLICIES

cfg = {"errors": {"duplicate_invoice": {"enabled": True, "count": [2, 2]}},
       "distractors": {"legit_recurring": {"enabled": True, "count": [2, 2]}}}
out = {}
with tempfile.TemporaryDirectory() as d:
    ep = generate_episode(int(sys.argv[1]), cfg, Path(d))
    for name in ("rule_based", "simple_rule", "random", "flag_all"):
        core = AuditCore(load_public(ep), load_truth(ep), mode="train")
        policy = POLICIES[name]()
        policy.reset()
        obs, steps, done = core.initial_observation(), [], False
        while not done:
            action = policy.act(obs)
            obs, reward, term, trunc, info = core.step(action)
            steps.append([action, reward])
            done = term or trunc
        out[name] = steps
print(json.dumps(out, sort_keys=True, ensure_ascii=False))
"""


def _run(seed: int, hashseed: str) -> dict:
    env = dict(os.environ, PYTHONHASHSEED=hashseed, PYTHONIOENCODING="utf-8")
    res = subprocess.run([sys.executable, "-c", _SCRIPT, str(seed)], cwd=str(ROOT), env=env,
                         capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert res.returncode == 0, res.stderr[-2000:]
    return json.loads(res.stdout.strip().splitlines()[-1])


def test_policies_do_not_depend_on_hash_seed():
    for seed in (3, 11):
        runs = [_run(seed, h) for h in ("1", "2", "12345")]
        assert runs[0] == runs[1] == runs[2]
        # эпизод действительно проверяет ветку с дубликатами (запросы get_document)
        tools = [a["tool"] for a, _ in runs[0]["rule_based"]]
        assert tools.count("get_document") >= 2
