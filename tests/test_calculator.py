"""Тесты безопасного калькулятора."""
import pytest

from env.calculator import CalcError, evaluate_expression


def test_example():
    assert evaluate_expression("(250000 - 125000) * 470.15 / 100") == "587687.5"


def test_division_inexact():
    assert evaluate_expression("1/3") == "0." + "3" * 34


def test_unary_minus():
    assert evaluate_expression("-5 + 2") == "-3"
    assert evaluate_expression("--5") == "5"
    assert evaluate_expression("-0") == "0"


def test_leading_dot_and_integers():
    assert evaluate_expression(".5 + 12") == "12.5"
    assert evaluate_expression("100") == "100"


@pytest.mark.parametrize("expr", [
    "1/0",
    "__import__('os')",
    "1e5",
    "2*(3",
    "1 2",
    "2)",
    "",
    "   ",
    "1 +",
    "1..2",
    "x",
    "2**3",
    "1" * 201,
    "(" * 51 + "1" + ")" * 51,
])
def test_errors(expr):
    with pytest.raises(CalcError):
        evaluate_expression(expr)


def test_depth_50_ok():
    assert evaluate_expression("(" * 50 + "1" + ")" * 50) == "1"


def test_length_200_ok():
    assert evaluate_expression("1" * 200)  # длина ровно 200 допустима
