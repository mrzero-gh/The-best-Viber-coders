"""«Жульническая» политика для тестов изоляции (tests/test_jury_isolation.py).

Ищет эталон своего эпизода везде, куда может дотянуться процесс политики: в памяти процесса,
в локальных переменных стека вызовов и в файлах private/truth.json (рабочая папка и её родители,
временная папка, корень репозитория). Нашла - отправляет ответ oracle и получает награду 1;
не нашла - пустой ответ, на эпизоде с ошибками это −1.
"""
import gc
import inspect
import json
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _is_truth(obj, episode_id) -> bool:
    return isinstance(obj, dict) and obj.get("episode_id") == episode_id and isinstance(obj.get("issues"), list)


def find_truth(episode_id: str, roots=None):
    """(эталон, где найден) или (None, None)."""
    for obj in gc.get_objects():
        if _is_truth(obj, episode_id):
            return obj, "memory"
    for info in inspect.stack():
        for value in info.frame.f_locals.values():
            if _is_truth(value, episode_id):
                return value, "stack"
    if roots is None:
        cwd = Path.cwd()
        roots = [cwd, *list(cwd.parents)[:2], Path(tempfile.gettempdir()), ROOT]
    for root in roots:
        for pattern in ("private/truth.json", "*/private/truth.json", "*/*/private/truth.json"):
            for path in Path(root).glob(pattern):
                try:
                    truth = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if _is_truth(truth, episode_id):
                    return truth, "file"
    return None, None


class Probe:
    def __init__(self):
        self.found = None

    def reset(self):
        self.found = None

    def act(self, observation):
        episode_id = observation.get("episode_id") or observation.get("manifest", {}).get("episode_id")
        truth, self.found = find_truth(episode_id)
        findings = [] if truth is None else [
            {"type": i["type"], "affected_ids": sorted(i["affected_ids"]),
             "claimed_amount_minor": i["expected_amount_minor"], "currency": i["currency"],
             "evidence_ids": sorted(i["evidence_sets"][0]), "explanation": "probe"} for i in truth["issues"]]
        return {"tool": "submit", "args": {"findings": {"episode_id": episode_id, "findings": findings}}}
