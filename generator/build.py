"""Построение эпизодов (заглушка)."""
from pathlib import Path


def generate_episode(seed: int, config: dict, out_dir: "Path") -> "Path":
    """Генерирует эпизод в out_dir/<episode_id>/public и /private, возвращает путь эпизода."""
    raise NotImplementedError("generate_episode: owner A")
