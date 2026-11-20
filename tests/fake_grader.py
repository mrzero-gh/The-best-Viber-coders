"""Фейковый оценщик для тестов сервера (подключается через --grader tests.fake_grader:evaluate)."""


def evaluate(findings_obj, truth, episode_id, inspection_steps, **kwargs):
    # Лишний вывод в stdout: сервер не должен из-за него ломать протокол
    print("noise")
    if isinstance(findings_obj, dict) and isinstance(findings_obj.get("findings"), list):
        return {"valid": True, "reward": 0.5, "error": None,
                "metrics": {"tp": 1, "fp": 0, "fn": 0, "precision": 1.0, "recall": 1.0, "f1": 1.0}}
    return {"valid": False, "reward": -1.0, "error": "bad structure", "metrics": None}


def evaluate_fd_noise(findings_obj, truth, episode_id, inspection_steps, **kwargs):
    """Пишет мусор прямо в дескриптор 1, мимо sys.stdout (как C-расширение или дочерний процесс)."""
    import os
    os.write(1, b"raw fd1 noise" + bytes([10]))
    return evaluate(findings_obj, truth, episode_id, inspection_steps)
