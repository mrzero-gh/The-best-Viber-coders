"""20 эпизодов репозитория пересобираются из своего generation.json побайтно.

Эпизоды собраны на Windows; в Docker (Linux, другая версия Python) этот тест подтверждает,
что генератор не зависит от платформы: public/ и private/ совпадают байт в байт.
"""
import json
from pathlib import Path

import pytest

from generator.build import generate_episode

ROOT = Path(__file__).resolve().parent.parent
EPISODES = sorted(p for split in ("train", "dev") for p in (ROOT / "episodes" / split).glob("EP-*") if p.is_dir())


def test_ten_train_and_ten_dev():
    assert [p.parent.name for p in EPISODES].count("train") == 10
    assert [p.parent.name for p in EPISODES].count("dev") == 10


@pytest.mark.parametrize("episode", EPISODES, ids=lambda p: p.name)
def test_episode_regenerates_byte_identical(tmp_path, episode):
    gen = json.loads((episode / "private" / "generation.json").read_text(encoding="utf-8"))
    out = generate_episode(gen["seed"], gen["config"], tmp_path, id_seed=gen.get("id_seed"))
    assert out.name == episode.name
    files = sorted(p.relative_to(out) for p in out.rglob("*") if p.is_file())
    assert files, "генератор ничего не записал"
    for name in files:
        assert (episode / name).read_bytes() == (out / name).read_bytes(), name
