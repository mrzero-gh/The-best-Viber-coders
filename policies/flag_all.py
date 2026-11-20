"""Политика 'Пометить всё' (Flag All baseline)."""
from policies.base import Policy


class FlagAllPolicy(Policy):
    """Политика 'пометить всё': без разбора объявляет документы ошибочными.

    Используется для проверки устойчивости сопоставителя и награды к FP:
    выдаёт до MAX_FINDINGS (30) выводов, приводя к высокому числу ложных
    срабатываний и отрицательной награде (около -1).
    """

    def __init__(self):
        self._step = 0
        self._episode_id = None
        self._submitted = False

    def reset(self) -> None:
        self._step = 0
        self._episode_id = None
        self._submitted = False

    def act(self, observation: dict) -> dict:
        self._step += 1
        kind = observation.get("kind")

        if kind == "initial":
            self._episode_id = observation.get("manifest", {}).get("episode_id", "EP-00000000")
            # Сначала запросим список счетов, чтобы взять реальные ID
            return {"tool": "list_records", "args": {"table": "invoices", "limit": 30}}

        if kind == "result" and observation.get("tool") == "list_records" and not self._submitted:
            records = observation.get("result", {}).get("records", [])
            findings = []
            for r in records[:30]:
                inv_id = r.get("invoice_id")
                doc_id = r.get("doc_id", "DOC-00000000")
                curr = r.get("currency", "KZT")
                amt = r.get("total_minor", 1000)
                findings.append({
                    "type": "duplicate_invoice",
                    "affected_ids": [inv_id],
                    "claimed_amount_minor": int(amt),
                    "currency": curr,
                    "evidence_ids": [doc_id],
                    "explanation": f"Помечено всё: подозрение на дубль {inv_id}",
                })
            if not findings:
                # Если счетов нет, сформируем фиктивные выводы
                for idx in range(1, 10):
                    findings.append({
                        "type": "period_cutoff",
                        "affected_ids": [f"INV-FLAG{idx:04d}"],
                        "claimed_amount_minor": 1000,
                        "currency": "KZT",
                        "evidence_ids": [f"DOC-FLAG{idx:04d}"],
                        "explanation": "Фиктивный вывод для проверки детектора FP",
                    })

            self._submitted = True
            return {
                "tool": "submit",
                "args": {
                    "findings": {
                        "episode_id": self._episode_id or "EP-00000000",
                        "findings": findings[:30],
                    }
                },
            }

        self._submitted = True
        return {
            "tool": "submit",
            "args": {
                "findings": {
                    "episode_id": self._episode_id or "EP-00000000",
                    "findings": [],
                }
            },
        }
