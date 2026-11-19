"""Точка входа для env.server; интерфейс из раздела 16 контракта."""
from grader.matcher import match
from grader.reward import f1_score, precision, recall, terminal_reward
from grader.validation import findings_error, validate_truth


def evaluate(findings_obj, truth: dict, episode_id: str, inspection_steps: int) -> dict:
    """Оценивает любой JSON-ответ агента, не изменяя входы.

    Результат не содержит issue_id, доказательств или сумм эталона. В eval
    сервер сохраняет его у себя; в train отдаёт только reward и metrics.
    Некорректный эталон/счётчик шагов вызывает ValueError на стороне сервера.
    """
    if type(inspection_steps) is not int or inspection_steps < 0:
        raise ValueError("inspection_steps must be a non-negative integer")
    validate_truth(truth, episode_id)
    error = findings_error(findings_obj, episode_id)
    if error:
        return {
            "valid": False, "reward": -1.0, "error": error,
            "metrics": {"tp": 0, "fp": 0, "fn": len(truth["issues"]),
                        "precision": 0.0, "recall": 0.0, "f1": 0.0},
        }
    matched = match(findings_obj["findings"], truth["issues"])
    tp, fp, fn = matched["tp"], matched["fp"], matched["fn"]
    f1 = f1_score(tp, fp, fn)
    return {
        "valid": True, "reward": terminal_reward(f1, inspection_steps), "error": None,
        "metrics": {"tp": tp, "fp": fp, "fn": fn, "precision": precision(tp, fp),
                    "recall": recall(tp, fn), "f1": f1},
    }
