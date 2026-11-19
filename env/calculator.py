"""Безопасный калькулятор: токенизатор + рекурсивный спуск над Decimal. Без eval/ast/compile."""
import decimal
from decimal import Decimal

MAX_LEN = 200
MAX_DEPTH = 50
PRECISION = 34


class CalcError(Exception):
    """Ошибка вычисления; message безопасно показывать агенту."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _tokenize(expr: str) -> list:
    """Разбивает строку на токены: ('num', Decimal) или ('op', символ)."""
    tokens = []
    i, n = 0, len(expr)
    while i < n:
        c = expr[i]
        if c == " ":
            i += 1
        elif c in "+-*/()":
            tokens.append(("op", c))
            i += 1
        elif c.isdigit() and c.isascii() or c == ".":
            j = i
            digits = 0
            dots = 0
            while j < n and (expr[j] == "." or (expr[j].isascii() and expr[j].isdigit())):
                if expr[j] == ".":
                    dots += 1
                else:
                    digits += 1
                j += 1
            if dots > 1 or digits == 0:
                raise CalcError("Некорректное число в выражении.")
            tokens.append(("num", Decimal(expr[i:j])))
            i = j
        else:
            raise CalcError("Недопустимый символ в выражении.")
    return tokens


class _Parser:
    """expr := term (('+'|'-') term)*; term := unary (('*'|'/') unary)*;
    unary := ('+'|'-') unary | atom; atom := число | '(' expr ')'."""

    def __init__(self, tokens: list):
        self.t = tokens
        self.pos = 0
        self.depth = 0

    def _peek(self):
        return self.t[self.pos] if self.pos < len(self.t) else (None, None)

    def _enter(self):
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise CalcError("Слишком глубокая вложенность.")

    def _leave(self):
        self.depth -= 1

    def parse(self) -> Decimal:
        value = self._expr()
        if self.pos != len(self.t):
            raise CalcError("Лишние символы в выражении.")
        return value

    def _expr(self) -> Decimal:
        value = self._term()
        while self._peek() in (("op", "+"), ("op", "-")):
            op = self.t[self.pos][1]
            self.pos += 1
            rhs = self._term()
            value = value + rhs if op == "+" else value - rhs
        return value

    def _term(self) -> Decimal:
        value = self._unary()
        while self._peek() in (("op", "*"), ("op", "/")):
            op = self.t[self.pos][1]
            self.pos += 1
            rhs = self._unary()
            if op == "*":
                value = value * rhs
            else:
                if rhs == 0:
                    raise CalcError("Деление на ноль.")
                value = value / rhs
        return value

    def _unary(self) -> Decimal:
        kind, val = self._peek()
        if kind == "op" and val in "+-":
            self.pos += 1
            v = self._unary()
            return -v if val == "-" else v
        return self._atom()

    def _atom(self) -> Decimal:
        kind, val = self._peek()
        if kind == "num":
            self.pos += 1
            return val
        if kind == "op" and val == "(":
            self.pos += 1
            self._enter()  # глубина считается по вложенным скобкам
            v = self._expr()
            if self._peek() != ("op", ")"):
                raise CalcError("Не закрыта скобка.")
            self.pos += 1
            self._leave()
            return v
        raise CalcError("Ожидалось число или скобка.")


def evaluate_expression(expr: str) -> str:
    """Вычисляет выражение, возвращает десятичную строку без экспоненты."""
    if not isinstance(expr, str) or not expr.strip():
        raise CalcError("Пустое выражение.")
    if len(expr) > MAX_LEN:
        raise CalcError("Выражение длиннее 200 символов.")
    tokens = _tokenize(expr)
    with decimal.localcontext() as ctx:
        ctx.prec = PRECISION
        ctx.traps[decimal.Overflow] = True
        ctx.traps[decimal.InvalidOperation] = True
        ctx.traps[decimal.DivisionByZero] = True
        try:
            value = _Parser(tokens).parse()
            value = value.normalize()
            text = format(value, "f")
        except (decimal.DecimalException, OverflowError, ArithmeticError):
            raise CalcError("Ошибка вычисления или переполнение.") from None
    if text in ("-0", "0.0"):
        text = "0"
    return text
