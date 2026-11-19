"""Клиент среды в процессе агента (заглушка)."""


class AuditEnv:
    """Клиент в процессе агента: общается с env.server через subprocess и JSON."""

    def reset(self, seed: int, config: dict) -> tuple[dict, dict]:
        """Начинает эпизод; возвращает (observation, info)."""
        raise NotImplementedError("AuditEnv.reset: owner B")

    def step(self, action: dict) -> tuple[dict, float, bool, bool, dict]:
        """Один шаг; возвращает (observation, reward, terminated, truncated, info)."""
        raise NotImplementedError("AuditEnv.step: owner B")

    def close(self) -> None:
        """Завершает серверный процесс."""
        raise NotImplementedError("AuditEnv.close: owner B")
