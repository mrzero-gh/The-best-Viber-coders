"""Ядро среды Audit Gym: инструменты, шаги, завершение эпизода (контракт, разделы 5-7)."""
import copy
import re
import time

from env.calculator import CalcError, evaluate_expression
from env.episode import Episode

MAX_STEPS = 100
PAGE_LIMIT = 50
TIME_LIMIT_S = 60.0
TOOLS = ["list_records", "get_record", "get_document", "calculator", "submit"]
TABLES = ["ledger", "invoices", "payments", "fx"]

_CURSOR_RE = re.compile(r"c(0|[1-9][0-9]{0,11})")

# Фиксированные шаблоны сообщений об ошибках (без путей, имён файлов и данных эталона)
_MSG = {
    "INVALID_ACTION": "Действие должно быть объектом с ключами tool и args (args - объект).",
    "UNKNOWN_TOOL": "Неизвестный инструмент.",
    "UNKNOWN_TABLE": "Неизвестная таблица. Допустимы: ledger, invoices, payments, fx.",
    "INVALID_ARGS": "Неверные аргументы инструмента.",
    "INVALID_CURSOR": "Неверный курсор.",
    "EPISODE_FINISHED": "Эпизод уже завершён.",
}

_TASK = ("Вы аудитор. Изучите книгу проводок, счета, платежи и курсы валют эпизода инструментами "
         "list_records, get_record, get_document и calculator, найдите ошибки учёта и отправьте "
         "их через submit в формате findings.")


def _default_grader(*args, **kwargs):
    """Оценщик по умолчанию; импорт внутри, чтобы тесты могли подставить свой."""
    from grader.evaluate import evaluate
    return evaluate(*args, **kwargs)


def _strip_lines(row: dict) -> dict:
    """Копия счёта без line_items (для list_records / get_record)."""
    return {k: copy.deepcopy(v) for k, v in row.items() if k != "line_items"}


class AuditCore:
    """Состояние одного эпизода. Наружу отдаёт только глубокие копии данных."""

    def __init__(self, episode: Episode, truth: dict, mode: str = "train", grader=None,
                 clock=time.monotonic, time_limit_s: float = TIME_LIMIT_S):
        self.episode = episode
        self._truth = truth
        self.mode = mode
        self._grader = grader if grader is not None else _default_grader
        self._clock = clock
        self._time_limit_s = time_limit_s
        self._start = clock()
        self.steps_used = 0
        self.finished = False
        self.grader_result = None
        self.inspection_steps = None  # выставляется в _finish (для журнала оценщика)

    # ---------- служебное ----------
    @property
    def episode_id(self) -> str:
        return self.episode.manifest["episode_id"]

    def _info(self) -> dict:
        return {"episode_id": self.episode_id, "mode": self.mode}

    def _remaining(self) -> int:
        return MAX_STEPS - self.steps_used

    def _error(self, code: str, tool, message: str = None) -> dict:
        """Наблюдение kind=error."""
        return {"kind": "error", "tool": tool, "error": {"code": code, "message": message or _MSG[code]},
                "steps_used": self.steps_used, "steps_remaining": self._remaining()}

    def _result(self, tool: str, result: dict) -> dict:
        return {"kind": "result", "tool": tool, "result": result,
                "steps_used": self.steps_used, "steps_remaining": self._remaining()}

    # ---------- публичный интерфейс ----------
    def initial_observation(self) -> dict:
        """Первое наблюдение после reset."""
        return {"kind": "initial", "task": _TASK,
                "manifest": copy.deepcopy(self.episode.manifest),
                "policy": copy.deepcopy(self.episode.policy),
                "tools": list(TOOLS), "tables": list(TABLES),
                "steps_used": 0, "steps_remaining": MAX_STEPS}

    def step(self, action):
        """Один шаг: (observation, reward, terminated, truncated, info)."""
        if self.finished:
            return self._error("EPISODE_FINISHED", None), 0.0, *self._flags(), self._info()

        if self._clock() - self._start > self._time_limit_s:
            return self._finish("time_limit", None, self.steps_used)

        self.steps_used += 1

        if (not isinstance(action, dict) or set(action.keys()) != {"tool", "args"}
                or not isinstance(action["args"], dict)):
            return self._after(self._error("INVALID_ACTION", None))
        tool, args = action["tool"], action["args"]
        if not isinstance(tool, str) or tool not in TOOLS:
            return self._after(self._error("UNKNOWN_TOOL", None))

        if tool == "submit":
            if set(args.keys()) != {"findings"}:
                return self._after(self._error("INVALID_ARGS", tool))
            return self._finish("submitted", args["findings"], self.steps_used - 1)

        obs = getattr(self, "_tool_" + tool)(args)
        return self._after(obs)

    # ---------- завершение ----------
    def _flags(self):
        """Прежние флаги завершённого эпизода (terminated, truncated)."""
        return self._terminated, self._truncated

    _terminated = False
    _truncated = False

    def _after(self, obs: dict):
        """Обычный (не submit) шаг: проверка лимита шагов."""
        if self.steps_used >= MAX_STEPS:
            return self._finish("step_limit", None, self.steps_used)
        return obs, 0.0, False, False, self._info()

    def _finish(self, status: str, findings, inspection_steps: int):
        """Завершает эпизод и вызывает оценщика."""
        truncated = status in ("step_limit", "time_limit")
        if truncated:
            findings_obj = {"episode_id": self.episode_id, "findings": []}
        else:
            findings_obj = findings
        res = self._grader(findings_obj, self._truth, self.episode_id, inspection_steps)
        self.grader_result = res
        self.inspection_steps = inspection_steps
        if status == "submitted" and not res.get("valid", True):
            status = "invalid_findings"
        self.finished = True
        self._terminated = not truncated
        self._truncated = truncated
        obs = {"kind": "final", "status": status, "steps_used": self.steps_used,
               "steps_remaining": self._remaining()}
        info = self._info()
        reward = 0.0
        if self.mode == "train":
            reward = float(res["reward"])
            info["metrics"] = copy.deepcopy(res.get("metrics"))
        return obs, reward, self._terminated, self._truncated, info

    # ---------- проверка аргументов ----------
    @staticmethod
    def _keys_ok(args: dict, required: set, optional: set = frozenset()) -> bool:
        keys = set(args.keys())
        return required <= keys <= (required | optional)

    # ---------- инструменты ----------
    def _tool_list_records(self, args: dict) -> dict:
        t = "list_records"
        if not self._keys_ok(args, {"table"}, {"cursor", "limit"}):
            return self._error("INVALID_ARGS", t)
        table = args["table"]
        cursor = args.get("cursor")
        limit = args.get("limit", PAGE_LIMIT)
        if not isinstance(table, str) or not (cursor is None or isinstance(cursor, str)):
            return self._error("INVALID_ARGS", t)
        if not isinstance(limit, int) or isinstance(limit, bool):
            return self._error("INVALID_ARGS", t)
        if table not in TABLES:
            return self._error("UNKNOWN_TABLE", t)
        if not 1 <= limit <= PAGE_LIMIT:
            return self._error("INVALID_ARGS", t)
        rows = self.episode.table(table)
        total = len(rows)
        offset = 0
        if cursor is not None:
            if not _CURSOR_RE.fullmatch(cursor) or int(cursor[1:]) > total:
                return self._error("INVALID_CURSOR", t)
            offset = int(cursor[1:])
        end = min(offset + limit, total)
        page = rows[offset:end]
        if table == "invoices":
            page = [_strip_lines(r) for r in page]
        else:
            page = [copy.deepcopy(r) for r in page]
        return self._result(t, {"table": table, "records": page,
                                "next_cursor": f"c{end}" if end < total else None, "total": total})

    def _tool_get_record(self, args: dict) -> dict:
        t = "get_record"
        if not self._keys_ok(args, {"table", "id"}):
            return self._error("INVALID_ARGS", t)
        table, rid = args["table"], args["id"]
        if not isinstance(table, str) or not isinstance(rid, str):
            return self._error("INVALID_ARGS", t)
        if table not in TABLES:
            return self._error("UNKNOWN_TABLE", t)
        row = self.episode.records[table].get(rid)
        if row is None:
            return self._error("NOT_FOUND", t, "Запись не найдена: " + rid[:64])
        row = _strip_lines(row) if table == "invoices" else copy.deepcopy(row)
        return self._result(t, {"table": table, "record": row})

    def _tool_get_document(self, args: dict) -> dict:
        t = "get_document"
        if not self._keys_ok(args, {"doc_id"}) or not isinstance(args["doc_id"], str):
            return self._error("INVALID_ARGS", t)
        doc_id = args["doc_id"]
        found = self.episode.documents.get(doc_id)
        if found is None:
            return self._error("NOT_FOUND", t, "Документ не найден: " + doc_id[:64])
        doc_type, row = found
        doc = copy.deepcopy(row)
        doc["doc_type"] = doc_type
        return self._result(t, {"document": doc})

    def _tool_calculator(self, args: dict) -> dict:
        t = "calculator"
        if not self._keys_ok(args, {"expression"}) or not isinstance(args["expression"], str):
            return self._error("INVALID_ARGS", t)
        expr = args["expression"]
        try:
            value = evaluate_expression(expr)
        except CalcError as e:
            return self._error("CALC_ERROR", t, e.message[:200])
        return self._result(t, {"expression": expr, "value": value})
