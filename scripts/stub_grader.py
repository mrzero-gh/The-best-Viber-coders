"""Функция-заглушка оценщика по разделу 16 контракта."""


def evaluate(findings_obj, truth: dict, episode_id: str, inspection_steps: int) -> dict:
    """Простая функция-заглушка, возвращающая тестовые P/R/F1 и награду."""
    if not isinstance(findings_obj, dict) or "findings" not in findings_obj:
        return {
            "valid": False,
            "reward": -1.0,
            "error": "invalid structure",
            "metrics": {"tp": 0, "fp": 0, "fn": len(truth.get("issues", [])),
                        "precision": 0.0, "recall": 0.0, "f1": 0.0},
        }

    findings = findings_obj.get("findings", [])
    issues = truth.get("issues", [])

    # Простая оценка совпадения
    # Если выводы пустые и эталон пустой -> F1 = 1.0
    if not findings and not issues:
        f1 = 1.0
        tp = fp = fn = 0
        p = r = 1.0
    elif not findings and issues:
        f1 = 0.0
        tp = fp = 0
        fn = len(issues)
        p = r = 0.0
    elif findings and not issues:
        f1 = 0.0
        tp = 0
        fp = len(findings)
        fn = 0
        p = r = 0.0
    else:
        # Сопоставляем типы и ID
        matched_issues = set()
        tp = 0
        fp = 0
        for f in findings:
            f_type = f.get("type")
            f_aff = set(f.get("affected_ids", []))
            for idx, iss in enumerate(issues):
                if idx in matched_issues:
                    continue
                if iss["type"] == f_type and set(iss["affected_ids"]) == f_aff:
                    matched_issues.add(idx)
                    tp += 1
                    break
            else:
                fp += 1
        fn = len(issues) - len(matched_issues)
        d_p = tp + fp
        p = tp / d_p if d_p else 0.0
        d_r = tp + fn
        r = tp / d_r if d_r else 0.0
        d_f1 = 2 * tp + fp + fn
        f1 = 2 * tp / d_f1 if d_f1 else 0.0

    reward = max(-1.0, min(1.0, 2.0 * f1 - 1.0 - 0.002 * inspection_steps))
    return {
        "valid": True,
        "reward": reward,
        "error": None,
        "metrics": {"tp": tp, "fp": fp, "fn": fn, "precision": p, "recall": r, "f1": f1},
    }
