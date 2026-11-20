"""Тесты терминального просмотрщика траекторий scripts.replay."""
import io
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.replay import (
    Theme,
    draw_panel,
    draw_table,
    find_episode_path,
    format_money,
    list_episodes,
    load_ground_truth,
    load_trajectories_from_source,
    replay_episode,
    strip_ansi,
    visible_len,
)

ROOT = Path(__file__).resolve().parent.parent


def test_strip_ansi_and_visible_len():
    colored = "\033[31m\033[1mОшибка!\033[0m"
    assert strip_ansi(colored) == "Ошибка!"
    assert visible_len(colored) == 7


def test_format_money():
    exponents = {"KZT": 2, "USD": 2, "JPY": 0, "RUB": 2}
    assert format_money(1057272, "JPY", exponents) == "1 057 272 JPY"
    assert format_money(84208900, "KZT", exponents) == "842 089,00 KZT"
    assert format_money(-4798308, "RUB", exponents) == "−47 983,08 RUB"
    assert format_money(500, "USD", exponents) == "5,00 USD"


def test_draw_panel():
    th = Theme(False)
    panel = draw_panel("Заголовок", ["Строка 1", "Длинная строка 2"], width=40, theme=th)
    assert "Заголовок" in panel
    assert "Строка 1" in panel
    assert "Длинная строка 2" in panel
    lines = panel.splitlines()
    assert len(lines) >= 4
    # Верхняя и нижняя границы начинаются с рамки
    assert lines[0].startswith("┌")
    assert lines[-1].startswith("└")


def test_draw_table():
    th = Theme(False)
    headers = ["Колонка A", "Колонка B"]
    rows = [["1", "Тест A"], ["2", "Тест B"]]
    tbl = draw_table(headers, rows, alignments=["center", "left"], theme=th)
    assert "Колонка A" in tbl
    assert "Тест B" in tbl
    lines = tbl.splitlines()
    assert len(lines) == 6  # top, hdr, mid, row1, row2, bot = 6 lines


def test_load_trajectories_from_source():
    episodes, ep_dir = load_trajectories_from_source("trajectories.jsonl")
    assert len(episodes) > 0
    assert "EP-0HJPAE81" in episodes
    steps = episodes["EP-0HJPAE81"]
    assert len(steps) == 25
    assert steps[0]["step"] == 0
    assert steps[-1]["step"] == 24


def test_find_episode_path_and_ground_truth():
    p = find_episode_path("EP-0HJPAE81")
    assert p is not None
    assert p.is_dir()
    truth = load_ground_truth(p)
    assert truth is not None
    assert "issues" in truth
    assert len(truth["issues"]) == 9


def test_replay_episode_summary():
    code = replay_episode(
        source="trajectories.jsonl",
        episode_id="EP-0HJPAE81",
        summary_only=True,
        use_color=False,
    )
    assert code == 0


def test_replay_episode_step_filter():
    code = replay_episode(
        source="trajectories.jsonl",
        episode_id="EP-0HJPAE81",
        step_filter=16,
        use_color=False,
    )
    assert code == 0


def test_replay_episode_nonexistent():
    code = replay_episode(
        source="trajectories.jsonl",
        episode_id="EP-NONEXISTENT",
        use_color=False,
    )
    assert code != 0


def test_cli_help():
    res = subprocess.run([sys.executable, "-m", "scripts.replay", "--help"], capture_output=True, text=True)
    assert res.returncode == 0
    assert "Audit Gym Replay" in res.stdout


def test_cli_list():
    res = subprocess.run([sys.executable, "-m", "scripts.replay", "--list"], capture_output=True, text=True)
    assert res.returncode == 0
    assert "EP-0HJPAE81" in res.stdout
    assert "Доступные эпизоды" in res.stdout


def test_cli_summary_no_color():
    res = subprocess.run(
        [sys.executable, "-m", "scripts.replay", "trajectories.jsonl", "--episode", "EP-0HJPAE81", "--summary", "--no-color"],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0
    assert "АКТ I: ВХОД В КОНТЕКСТ АУДИТА" in res.stdout
    assert "АКТ II: ОФИЦИАЛЬНЫЙ АУДИТОРСКИЙ ОТЧЁТ" in res.stdout
    assert "ЭКЗАМЕНАЦИОННАЯ КАРТОЧКА ОЦЕНКИ" in res.stdout
    assert "True Positives" in res.stdout


def test_cli_dir_argument():
    ep_dir = ROOT / "episodes" / "train" / "EP-0HJPAE81"
    res = subprocess.run(
        [sys.executable, "-m", "scripts.replay", str(ep_dir), "--summary", "--no-color"],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0
    assert "EP-0HJPAE81" in res.stdout
