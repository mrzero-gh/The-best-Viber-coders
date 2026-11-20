"""Случайная политика (Random baseline)."""
import random
from policies.base import Policy


class RandomPolicy(Policy):
    """Случайная политика: выполняет случайные допустимые действия или отправляет пустой submit."""

    def __init__(self, seed: int = 42, max_explore_steps: int = 3):
        self._seed = seed
        self._rng = random.Random(seed)
        self._max_explore_steps = max_explore_steps
        self._step = 0
        self._episode_id = "EP-00000000"

    def reset(self) -> None:
        self._rng = random.Random(self._seed)
        self._step = 0
        self._episode_id = "EP-00000000"

    def act(self, observation: dict) -> dict:
        self._step += 1
        kind = observation.get("kind")
        if kind == "initial":
            self._episode_id = observation.get("manifest", {}).get("episode_id", "EP-00000000")

        # Если превысили число шагов исследования или мало шагов осталось — submit
        remaining = observation.get("steps_remaining", 100)
        if self._step >= self._max_explore_steps or remaining <= 2:
            return {
                "tool": "submit",
                "args": {
                    "findings": {
                        "episode_id": self._episode_id,
                        "findings": [],
                    }
                },
            }

        choice = self._rng.choice(["list_records", "calculator"])
        if choice == "calculator":
            return {"tool": "calculator", "args": {"expression": f"{self._rng.randint(1, 100)} + {self._rng.randint(1, 50)}"}}
        else:
            table = self._rng.choice(["ledger", "invoices", "payments", "fx"])
            return {"tool": "list_records", "args": {"table": table, "limit": 10}}
