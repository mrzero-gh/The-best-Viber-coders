"""Базовый интерфейс политики."""
from abc import ABC, abstractmethod


class Policy(ABC):
    """Абстрактная политика: по наблюдению выбирает действие."""

    @abstractmethod
    def act(self, observation: dict) -> dict:
        """Возвращает действие по наблюдению."""

    @abstractmethod
    def reset(self) -> None:
        """Сбрасывает внутреннее состояние перед новым эпизодом."""
