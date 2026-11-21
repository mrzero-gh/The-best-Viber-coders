"""«Жульническая» политика для тестов изоляции (tests/test_jury_isolation.py).

Ищет эталон своего эпизода везде, куда может дотянуться процесс политики: в памяти процесса,
в локальных переменных стека вызовов, в файлах private/truth.json (рабочая папка и её родители,
временная папка, корень репозитория и сохранённые эпизоды episodes/*/*), через импорт оценщика
и через дочерний процесс. Нашла - отправляет ответ oracle и получает награду 1; не нашла -
пустой ответ, на эпизоде с ошибками это −1. Что заблокировано, записывает в blocked.
"""
import gc
import inspect
import json
import sys
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_PATTERNS = ("private/truth.json", "*/private/truth.json", "*/*/private/truth.json", "*/*/*/private/truth.json")


def _is_truth(obj, episode_id) -> bool:
    return isinstance(obj, dict) and obj.get("episode_id") == episode_id and isinstance(obj.get("issues"), list)


def find_truth(episode_id: str, roots=None, blocked=None):
    """(эталон, где найден) или (None, None). blocked - список заблокированных попыток."""
    blocked = [] if blocked is None else blocked
    for obj in gc.get_objects():
        if _is_truth(obj, episode_id):
            return obj, "memory"
    for info in inspect.stack():
        for value in info.frame.f_locals.values():
            if _is_truth(value, episode_id):
                return value, "stack"
    if roots is None:
        cwd = Path.cwd()
        temp = Path(os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp")
        roots = [cwd, *list(cwd.parents)[:2], temp, ROOT]
    for root in roots:
        for pattern in _PATTERNS:
            for path in Path(root).glob(pattern):
                try:
                    truth = json.loads(path.read_text(encoding="utf-8"))
                except OSError as exc:
                    blocked.append(f"open: {type(exc).__name__}")
                    continue
                except ValueError:
                    continue
                if _is_truth(truth, episode_id):
                    return truth, "file"
    try:
        __import__("grader.evidence")
        return None, "grader imported"  # сам импорт оценщика - уже нарушение изоляции
    except ImportError as exc:
        blocked.append(f"import: {exc}")
    try:
        __import__("subprocess").run([sys.executable, "-c", "print(1)"], capture_output=True, check=False)
        return None, "subprocess started"
    except (ImportError, OSError, RuntimeError) as exc:
        blocked.append(f"subprocess: {type(exc).__name__}")
    return None, None


class Probe:
    def __init__(self):
        self.found = None
        self.blocked = []

    def reset(self):
        self.found = None
        self.blocked = []

    def act(self, observation):
        episode_id = observation.get("episode_id") or observation.get("manifest", {}).get("episode_id")
        truth, self.found = find_truth(episode_id, blocked=self.blocked)
        findings = [] if truth is None else [
            {"type": i["type"], "affected_ids": sorted(i["affected_ids"]),
             "claimed_amount_minor": i["expected_amount_minor"], "currency": i["currency"],
             "evidence_ids": sorted(i["evidence_sets"][0]), "explanation": "probe"} for i in truth["issues"]]
        if self.found is not None and truth is None:  # нарушение без эталона: тоже сигнал тесту
            findings = [{"type": "breach:" + self.found, "affected_ids": [], "claimed_amount_minor": 0,
                         "currency": "XXX", "evidence_ids": [], "explanation": "probe"}]
        print("probe blocked:", self.blocked, file=sys.stderr)
        return {"tool": "submit", "args": {"findings": {"episode_id": episode_id, "findings": findings}}}
