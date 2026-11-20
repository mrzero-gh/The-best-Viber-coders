"""Оракул: идеальный аудитор с эталонной точностью."""
from common.jsonio import canonical_dumps
from policies.base import Policy


class OraclePolicy(Policy):
    """Политика-оракул.

    Если передан эталон (truth) — на первом шаге сразу отправляет канонические
    выводы без лишних шагов исследования (награда = 1.0).
    Если эталон не передан заранее, может быть установлен через set_truth()
    или загружен из файла private/truth.json эпизода.
    """

    def __init__(self, truth: dict | None = None):
        self._initial_truth = truth
        self.truth = truth
        self._submitted = False

    def reset(self) -> None:
        self.truth = self._initial_truth
        self._submitted = False

    def set_truth(self, truth: dict) -> None:
        self.truth = truth

    def act(self, observation: dict) -> dict:
        if self._submitted or observation.get("kind") == "final":
            return {"tool": "submit", "args": {"findings": {"episode_id": "EP-00000000", "findings": []}}}

        manifest = observation.get("manifest") or {}
        episode_id = manifest.get("episode_id")
        if not episode_id and self.truth:
            episode_id = self.truth.get("episode_id")

        findings = []
        if self.truth and "issues" in self.truth:
            for issue in self.truth["issues"]:
                evidence = sorted(issue["evidence_sets"][0]) if issue.get("evidence_sets") else []
                findings.append({
                    "type": issue["type"],
                    "affected_ids": sorted(issue["affected_ids"]),
                    "claimed_amount_minor": int(issue["expected_amount_minor"]),
                    "currency": issue["currency"],
                    "evidence_ids": evidence,
                    "explanation": f"Эталонное подтверждение ошибки {issue.get('issue_id', '')}",
                })

        self._submitted = True
        return {
            "tool": "submit",
            "args": {
                "findings": {
                    "episode_id": episode_id or "EP-00000000",
                    "findings": findings,
                }
            },
        }
