"""Тесты метрик и награды."""
import pytest

from grader.reward import f1_score, precision, recall, terminal_reward


def test_f1_edge_cases():
    assert f1_score(0, 0, 0) == 1.0
    assert f1_score(0, 3, 0) == 0.0
    assert f1_score(0, 0, 4) == 0.0
    assert f1_score(2, 1, 1) == pytest.approx(2 / 3)


def test_reward_clip():
    assert terminal_reward(0.0, 1000) == -1.0
    assert terminal_reward(1.0, 0) == 1.0
    assert terminal_reward(1.0, 100) == pytest.approx(0.8)


def test_step_penalty():
    assert terminal_reward(0.5, 10) == pytest.approx(-0.02)


def test_precision_recall_zero_denominator():
    assert precision(0, 0) == 0.0
    assert recall(0, 0) == 0.0
    assert precision(3, 1) == 0.75
    assert recall(3, 1) == 0.75
