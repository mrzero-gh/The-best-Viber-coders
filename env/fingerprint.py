"""Отпечаток эпизода: sha256 public-файлов и сверка с private/generation.json.

Формула (docs/contract.md, раздел 15):
  public_sha256 = {имя файла: sha256 hex байтов файла} для 6 public-файлов;
  episode_sha256 = sha256(canonical_dumps(public_sha256).encode("utf-8")).hexdigest().
"""
import hashlib
import json
from pathlib import Path

from common.jsonio import canonical_dumps

PUBLIC_FILES = ("manifest.json", "ledger.csv", "invoices.jsonl", "payments.jsonl", "fx.csv", "policy.json")


class EpisodeIntegrityError(Exception):
    """Эпизод повреждён или не соответствует generation.json. Сообщение без путей."""


def public_hashes(episode_dir) -> dict:
    """sha256 (hex) байтов каждого public-файла. Нет файла -> EpisodeIntegrityError."""
    pub = Path(episode_dir) / "public"
    out = {}
    for name in PUBLIC_FILES:
        try:
            data = (pub / name).read_bytes()
        except OSError:
            raise EpisodeIntegrityError("Отсутствует или нечитаем public-файл: " + name)
        out[name] = hashlib.sha256(data).hexdigest()
    return out


def episode_fingerprint(hashes: dict) -> str:
    """Отпечаток эпизода по словарю хэшей public-файлов."""
    return hashlib.sha256(canonical_dumps(hashes).encode("utf-8")).hexdigest()


def verify_episode(episode_dir) -> str:
    """Пересчитывает хэши и сверяет с private/generation.json; возвращает episode_sha256."""
    try:
        gen = json.loads((Path(episode_dir) / "private" / "generation.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise EpisodeIntegrityError("generation.json отсутствует или повреждён.")
    if not isinstance(gen, dict):
        raise EpisodeIntegrityError("generation.json повреждён.")
    expected, fp = gen.get("public_sha256"), gen.get("episode_sha256")
    if not isinstance(expected, dict) or not isinstance(fp, str):
        raise EpisodeIntegrityError("В generation.json нет public_sha256 или episode_sha256.")
    actual = public_hashes(episode_dir)
    if actual != expected:
        raise EpisodeIntegrityError("Хэши public-файлов не совпадают с generation.json.")
    computed = episode_fingerprint(actual)
    if computed != fp:
        raise EpisodeIntegrityError("episode_sha256 не совпадает с хэшами public-файлов.")
    return computed
