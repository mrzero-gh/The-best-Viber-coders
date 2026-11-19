"""Метрики и итоговая награда."""


def precision(tp: int, fp: int) -> float:
    """Точность TP/(TP+FP); 0.0 при нулевом знаменателе."""
    d = tp + fp
    return tp / d if d else 0.0


def recall(tp: int, fn: int) -> float:
    """Полнота TP/(TP+FN); 0.0 при нулевом знаменателе."""
    d = tp + fn
    return tp / d if d else 0.0


def f1_score(tp: int, fp: int, fn: int) -> float:
    """F1 = 2TP/(2TP+FP+FN); если TP=FP=FN=0, то 1.0 (нечего находить и ничего не найдено)."""
    d = 2 * tp + fp + fn
    if d == 0:
        return 1.0
    return 2 * tp / d


def terminal_reward(f1: float, inspection_steps: int) -> float:
    """Награда = clip(2*f1 - 1 - 0.002*inspection_steps, -1, 1)."""
    r = 2 * f1 - 1 - 0.002 * inspection_steps
    return max(-1.0, min(1.0, r))
