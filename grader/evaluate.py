"""Интерфейс оценщика (контракт, раздел 16). Реализует участник C."""


def evaluate(findings_obj, truth: dict, episode_id: str, inspection_steps: int) -> dict:
    """Оценивает ответ агента.

    findings_obj - то, что прислал агент в submit.args.findings, как есть (любой JSON);
    при усечении среда передаёт {"episode_id": <id>, "findings": []}.
    inspection_steps - число шагов до submit (при усечении - все шаги).

    Возвращает {"valid": bool, "reward": float, "error": str | None,
                "metrics": {"tp", "fp", "fn", "precision", "recall", "f1"}}.
    При valid = False reward = -1.
    """
    raise NotImplementedError("grader.evaluate: owner C")
