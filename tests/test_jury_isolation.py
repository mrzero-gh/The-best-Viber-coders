"""Изоляция политики в собственном запускателе (scripts.jury), внешний отчёт 22.11.

Раньше эталон загружался в процесс, где работала политика, а эпизод лежал на диске с private/:
политика, получившая только наблюдение, могла прочитать эталон из памяти вызывающей функции
или из файла. Теперь политика - отдельный процесс, эпизод генерирует сервер среды и удаляет
папку сразу после загрузки. «Жульническая» политика (tests/probe_policy.py) ищет эталон
везде, куда дотягивается, и должна остаться с наградой −1.
"""
import json
from pathlib import Path

from generator.build import generate_episode
from scripts import jury
from tests.probe_policy import find_truth

ROOT = Path(__file__).resolve().parent.parent
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


def test_cheating_policy_cannot_read_saved_episode():
    # внешний отчёт 22.11 (второй): у сохранённого эпизода truth лежит на диске, и отдельный процесс
    # политики мог его прочитать. Теперь процесс политики не открывает закрытые файлы, не
    # импортирует оценщик и не запускает дочерние процессы
    ep = ROOT / "episodes" / "dev" / "EP-4A84A8A4"
    episode_id = json.loads((ep / "public" / "manifest.json").read_text(encoding="utf-8"))["episode_id"]
    found, where = find_truth(episode_id, roots=[ROOT])  # контроль: без ограничений файл находится
    assert where in ("file", "memory") and found["issues"]
    del found
    with jury.PolicyProcess("tests.probe_policy:Probe") as policy:
        result = jury.evaluate_policy_on_episode(policy, 0, {}, GRADER, episode=str(ep))
    assert result["tp"] == 0 and result["fn"] > 0
    assert result["fp"] == 0  # нет и сигнала «импорт оценщика или дочерний процесс удался»
    assert result["reward"] == -1.0


def test_policy_guard_blocks_secrets_writes_and_processes(tmp_path):
    import subprocess
    import sys
    secret = tmp_path / "private" / "truth.json"
    secret.parent.mkdir()
    secret.write_text("{}", encoding="utf-8")
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "from scripts.policy_worker import install_guard\n"
        "install_guard()\n"
        "def tries(f):\n"
        "    try:\n"
        "        f(); return 'allowed'\n"
        "    except (OSError, ImportError) as e:\n"
        "        return 'blocked'\n"
        "print(tries(lambda: open(%r).read()))\n"
        "print(tries(lambda: open(%r, 'w').write('x')))\n"
        "print(tries(lambda: __import__('grader.matcher')))\n"
        "print(tries(lambda: __import__('subprocess')))\n"
        "print(tries(lambda: __import__('ctypes')))\n"
        "print(tries(lambda: __import__('socket')))\n"
        "print(tries(lambda: open(%r).read()))\n"
    ) % (str(ROOT), str(secret), str(tmp_path / "new.txt"), str(ROOT / "README.md"))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, encoding="utf-8",
                         check=True).stdout.split()
    assert out == ["blocked"] * 6 + ["allowed"]  # открытые файлы политике читать можно


def test_oracle_gets_truth_only_explicitly():
    truth = jury.oracle_truth(SEED, CFG)
    with jury.PolicyProcess("OraclePolicy") as policy:
        result = jury.evaluate_policy_on_episode(policy, SEED, CFG, GRADER, truth)
    assert result["f1"] == 1.0 and result["reward"] == 1.0
