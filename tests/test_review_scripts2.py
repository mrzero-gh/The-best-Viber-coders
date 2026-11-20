"""Находки ревью скриптов plot_complexity, demo и replay (20.11).

1. без matplotlib plot_complexity завершается с кодом 1, а не делает вид, что график обновлён;
2. график пишется туда, куда сказали (--out), demo не трогает закоммиченный PNG;
3. --seeds 0 - ошибка аргумента, мёртвого флага --real-grader нет;
4. replay папки без траектории - ошибка, а не другой эпизод; --list уважает --no-color.
"""
import sys
from pathlib import Path

import pytest

from generator.build import generate_episode
from scripts import plot_complexity, replay

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def one_level(monkeypatch):
    monkeypatch.setattr(plot_complexity, "COMPLEXITY_LEVELS", plot_complexity.COMPLEXITY_LEVELS[1:2])


def test_missing_matplotlib_returns_error(tmp_path, monkeypatch, one_level, capsys):
    monkeypatch.setitem(sys.modules, "matplotlib", None)  # import matplotlib -> ImportError
    out = tmp_path / "curve.png"
    assert plot_complexity.build_complexity_curve(seeds_per_level=1, out_img=out) == 1
    assert not out.exists()
    assert "requirements-plot.txt" in capsys.readouterr().out


def test_curve_goes_to_out_path(tmp_path, one_level):
    pytest.importorskip("matplotlib")
    committed = ROOT / "docs" / "complexity_curve.png"
    before = committed.read_bytes()
    out = tmp_path / "sub" / "curve.png"
    assert plot_complexity.build_complexity_curve(seeds_per_level=1, out_img=out) == 0
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert committed.read_bytes() == before


@pytest.mark.parametrize("argv", [["--seeds", "0"], ["--real-grader"]])
def test_plot_complexity_rejects_bad_args(argv):
    with pytest.raises(SystemExit) as e:
        plot_complexity.main(argv)
    assert e.value.code == 2


def test_demo_uses_temp_graph_and_correct_texts():
    src = (ROOT / "scripts" / "demo.py").read_text(encoding="utf-8")
    assert '"--out"' in src
    assert 'ROOT / "docs" / "complexity_curve.png"' not in src
    assert "5 базовых политик" in src and "4 базовых политик" not in src
    assert "Роль D" not in src


def test_replay_dir_without_trajectory_is_error(tmp_path, capsys):
    ep = generate_episode(3, {}, tmp_path)
    assert not (ep / "trajectories.jsonl").exists()
    assert replay.replay_episode(source=str(ep), summary_only=True, use_color=False) == 1
    assert ep.name in capsys.readouterr().out


def test_replay_dir_with_trajectory_works():
    ep = ROOT / "episodes" / "train" / "EP-0HJPAE81"
    assert replay.replay_episode(source=str(ep), summary_only=True, use_color=False) == 0


def test_replay_list_without_color(capsys):
    replay.list_episodes(None, use_color=False)
    out = capsys.readouterr().out
    assert "EP-0HJPAE81" in out
    assert "\x1b[" not in out
