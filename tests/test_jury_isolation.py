"""Изоляция политики в собственном запускателе (scripts.jury), внешний отчёт 22.11.

Раньше эталон загружался в процесс, где работала политика, а эпизод лежал на диске с private/:
политика, получившая только наблюдение, могла прочитать эталон из памяти вызывающей функции
или из файла. Теперь политика - отдельный процесс, эпизод генерирует сервер среды и удаляет
папку сразу после загрузки. «Жульническая» политика (tests/probe_policy.py) ищет эталон
везде, куда дотягивается, и должна остаться с наградой −1.
"""
import json

from generator.build import generate_episode
from scripts import jury
from tests.probe_policy import find_truth

GRADER = "grader.evaluate:evaluate"
CFG = jury._scenario_config(2, jury.TEST_SCENARIOS[1])  # одиночные ошибки всех 4 типов
SEED = 7721


def test_probe_finds_truth_when_reachable(tmp_path):
    # контроль: зонд действительно находит эталон и в памяти, и в файле, если они доступны
    ep = generate_episode(SEED, CFG, tmp_path)
    truth = json.loads((ep / "private" / "truth.json").read_text(encoding="utf-8"))
    assert truth["issues"]
    assert find_truth(truth["episode_id"])[1] == "memory"
    episode_id = truth["episode_id"]
    del truth
    import gc
    gc.collect()
    found, where = find_truth(episode_id, roots=[tmp_path])
    assert where == "file" and found["episode_id"] == episode_id


def test_cheating_policy_cannot_reach_truth():
    with jury.PolicyProcess("tests.probe_policy:Probe") as policy:
        result = jury.evaluate_policy_on_episode(policy, SEED, CFG, GRADER)
    assert result["tp"] == 0 and result["fn"] > 0
    assert result["reward"] == -1.0


def test_oracle_gets_truth_only_explicitly():
    truth = jury.oracle_truth(SEED, CFG)
    with jury.PolicyProcess("OraclePolicy") as policy:
        result = jury.evaluate_policy_on_episode(policy, SEED, CFG, GRADER, truth)
    assert result["f1"] == 1.0 and result["reward"] == 1.0
