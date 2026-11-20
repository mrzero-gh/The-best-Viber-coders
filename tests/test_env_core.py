"""Тесты ядра среды AuditCore (с фейковым оценщиком)."""
import json
from pathlib import Path

import pytest

jsonschema = pytest.importorskip("jsonschema")

from env.core import MAX_STEPS, AuditCore
from env.episode import load_public, load_truth

ROOT = Path(__file__).resolve().parent.parent
EP_DIR = ROOT / "tests" / "fixtures" / "mini" / "EP-8K2M4Q7X"
OBS_SCHEMA = json.loads((ROOT / "schemas" / "observation.schema.json").read_text(encoding="utf-8"))
_VALIDATOR = jsonschema.Draft202012Validator(OBS_SCHEMA)


class FakeGrader:
    """Запоминает аргументы вызова и возвращает фиксированный ответ."""

    def __init__(self, valid=True):
        self.calls = []
        self.valid = valid

    def __call__(self, findings_obj, truth, episode_id, inspection_steps):
        self.calls.append((findings_obj, truth, episode_id, inspection_steps))
        metrics = {"tp": 1, "fp": 0, "fn": 0, "precision": 1.0, "recall": 1.0, "f1": 1.0}
        return {"valid": self.valid, "reward": 0.5 if self.valid else -1.0, "error": None, "metrics": metrics}


class Env:
    """Обёртка: каждый шаг сразу проверяется по observation.schema.json."""

    def __init__(self, mode="train", grader=None, clock=None, **kw):
        self.grader = grader or FakeGrader()
        extra = {"clock": clock} if clock else {}
        self.core = AuditCore(load_public(EP_DIR), load_truth(EP_DIR), mode=mode, grader=self.grader,
                              **extra, **kw)

    def step(self, tool, **args):
        return self.raw({"tool": tool, "args": args})

    def raw(self, action):
        out = self.core.step(action)
        _VALIDATOR.validate(out[0])
        return out


@pytest.fixture
def env():
    return Env()


def _err(out, code):
    obs = out[0]
    assert obs["kind"] == "error" and obs["error"]["code"] == code, obs
    assert len(obs["error"]["message"]) <= 200
    return obs


def test_initial_valid_and_clean():
    e = Env()
    obs = e.core.initial_observation()
    _VALIDATOR.validate(obs)
    text = json.dumps(obs, ensure_ascii=False)
    for bad in ("ISS-", "private", "truth"):
        assert bad not in text
    assert obs["steps_used"] == 0 and obs["steps_remaining"] == 100
    assert obs["tools"] == ["list_records", "get_record", "get_document", "calculator", "submit"]


def test_pagination_ledger(env):
    sizes, cursor = [], None
    while True:
        obs = env.step("list_records", table="ledger", cursor=cursor, limit=5)[0]
        assert obs["kind"] == "result"
        res = obs["result"]
        assert res["total"] == 12
        sizes.append(len(res["records"]))
        cursor = res["next_cursor"]
        if cursor is None:
            break
    assert sizes == [5, 5, 2]
    a = env.step("list_records", table="ledger", cursor="c5", limit=5)[0]["result"]
    b = env.step("list_records", table="ledger", cursor="c5", limit=5)[0]["result"]
    assert a == b and a["next_cursor"] == "c10"


def test_default_limit_and_end_cursor(env):
    res = env.step("list_records", table="fx")[0]["result"]
    assert len(res["records"]) == 50 and res["next_cursor"] == "c50" and res["total"] == 51
    res = env.step("list_records", table="fx", cursor="c51")[0]["result"]
    assert res["records"] == [] and res["next_cursor"] is None


@pytest.mark.parametrize("cursor", ["x", "c-1", "c99", "c01", "", "5", "c", "C5", "c5.0"])
def test_bad_cursor(env, cursor):
    _err(env.step("list_records", table="ledger", cursor=cursor), "INVALID_CURSOR")


@pytest.mark.parametrize("limit", [0, 51, True, "5", 2.5, -1])
def test_bad_limit(env, limit):
    _err(env.step("list_records", table="ledger", limit=limit), "INVALID_ARGS")


def test_invalid_args_variants(env):
    _err(env.step("list_records"), "INVALID_ARGS")
    _err(env.step("list_records", table="ledger", foo=1), "INVALID_ARGS")
    _err(env.step("list_records", table=5), "INVALID_ARGS")
    _err(env.step("list_records", table="ledger", cursor=5), "INVALID_ARGS")
    _err(env.step("get_record", table="ledger"), "INVALID_ARGS")
    _err(env.step("get_record", table="ledger", id=5), "INVALID_ARGS")
    _err(env.step("get_document"), "INVALID_ARGS")
    _err(env.step("calculator", expression=5), "INVALID_ARGS")
    _err(env.step("submit"), "INVALID_ARGS")
    _err(env.step("submit", findings={}, extra=1), "INVALID_ARGS")


def test_invoices_without_line_items(env):
    res = env.step("list_records", table="invoices")[0]["result"]
    assert len(res["records"]) == 3
    assert all("line_items" not in r for r in res["records"])
    rec = env.step("get_record", table="invoices", id="INV-3HZ7QK2D")[0]["result"]["record"]
    assert "line_items" not in rec and rec["total_minor"] == 250000


def test_get_record_all_tables(env):
    cases = [("ledger", "L-7TBC4X1P"), ("invoices", "INV-6TQ4W8NR"), ("payments", "PAY-9F4TX1MB"),
             ("fx", "FX:2026-09-02:USD:KZT")]
    for table, rid in cases:
        res = env.step("get_record", table=table, id=rid)[0]["result"]
        assert res["table"] == table
    assert res["record"]["rate"] == "470.5500"


def test_not_found_and_unknown(env):
    obs = _err(env.step("get_record", table="ledger", id="L-NOPE" + "X" * 100), "NOT_FOUND")
    assert len(obs["error"]["message"]) <= 200
    _err(env.step("get_document", doc_id="DOC-00000000"), "NOT_FOUND")
    _err(env.step("get_record", table="secrets", id="1"), "UNKNOWN_TABLE")
    _err(env.step("list_records", table="truth"), "UNKNOWN_TABLE")
    _err(env.step("hack"), "UNKNOWN_TOOL")


def test_invalid_action(env):
    _err(env.raw("list_records"), "INVALID_ACTION")
    _err(env.raw(None), "INVALID_ACTION")
    _err(env.raw({"tool": "calculator", "args": {}, "extra": 1}), "INVALID_ACTION")
    _err(env.raw({"tool": "calculator"}), "INVALID_ACTION")
    _err(env.raw({"tool": "calculator", "args": []}), "INVALID_ACTION")
    _err(env.raw({"tool": ["x"], "args": {}}), "UNKNOWN_TOOL")


def test_get_document(env):
    inv = env.step("get_document", doc_id="DOC-7C2K9W4M")[0]["result"]["document"]
    assert inv["doc_type"] == "invoice" and len(inv["line_items"]) == 1
    pay = env.step("get_document", doc_id="DOC-5RW2N8CA")[0]["result"]["document"]
    assert pay["doc_type"] == "payment" and pay["payment_id"] == "PAY-9F4TX1MB"


def test_calculator_tool(env):
    res = env.step("calculator", expression="(250000 - 125000) * 470.15 / 100")[0]["result"]
    assert res == {"expression": "(250000 - 125000) * 470.15 / 100", "value": "587687.5"}
    _err(env.step("calculator", expression="1/0"), "CALC_ERROR")


def test_errors_consume_steps(env):
    env.step("hack")
    env.raw("junk")
    out = env.step("get_record", table="ledger", id="nope")
    assert out[0]["steps_used"] == 3 and out[0]["steps_remaining"] == 97
    assert out[1:4] == (0.0, False, False)


def test_submit_train():
    e = Env()
    e.step("calculator", expression="1+1")
    e.step("calculator", expression="2+2")
    findings = {"episode_id": "EP-8K2M4Q7X", "findings": [{"anything": 1}]}
    obs, reward, term, trunc, info = e.step("submit", findings=findings)
    assert obs == {"kind": "final", "status": "submitted", "steps_used": 3, "steps_remaining": 97}
    assert (reward, term, trunc) == (0.5, True, False)
    assert info["episode_id"] == "EP-8K2M4Q7X" and info["mode"] == "train"
    assert info["metrics"]["f1"] == 1.0
    f, truth, ep_id, steps = e.grader.calls[0]
    assert f == findings and ep_id == "EP-8K2M4Q7X" and steps == 2
    assert truth["issues"][0]["issue_id"] == "ISS-6M3KQ8WD"
    assert e.core.grader_result["reward"] == 0.5


def test_submit_eval():
    e = Env(mode="eval")
    obs, reward, term, trunc, info = e.step("submit", findings={"findings": []})
    assert obs["status"] == "submitted" and reward == 0.0 and term and not trunc
    assert "metrics" not in info and info == {"episode_id": "EP-8K2M4Q7X", "mode": "eval"}
    assert e.core.grader_result["valid"] is True


def test_submit_invalid_findings():
    e = Env(grader=FakeGrader(valid=False))
    obs, reward, term, trunc, info = e.step("submit", findings="мусор")
    assert obs["status"] == "invalid_findings" and reward == -1.0 and term and not trunc
    assert e.grader.calls[0][0] == "мусор"


def test_after_finish():
    e = Env()
    e.step("submit", findings={})
    out = e.step("calculator", expression="1")
    _err(out, "EPISODE_FINISHED")
    assert out[0]["steps_used"] == 1
    assert out[1:4] == (0.0, True, False)
    assert len(e.grader.calls) == 1


def test_step_limit():
    e = Env()
    for i in range(MAX_STEPS - 1):
        obs, reward, term, trunc, _ = e.step("calculator", expression="1+1")
        assert obs["kind"] == "result" and not term and not trunc
    obs, reward, term, trunc, info = e.step("calculator", expression="1+1")
    assert obs == {"kind": "final", "status": "step_limit", "steps_used": 100, "steps_remaining": 0}
    assert (term, trunc, reward) == (False, True, 0.5)
    f, _, ep_id, steps = e.grader.calls[0]
    assert f == {"episode_id": ep_id, "findings": []} and steps == 100
    _err(e.step("calculator", expression="1"), "EPISODE_FINISHED")


def test_submit_on_last_step():
    e = Env()
    for _ in range(MAX_STEPS - 1):
        e.step("calculator", expression="1")
    obs, _, term, trunc, _ = e.step("submit", findings={"findings": []})
    assert obs["status"] == "submitted" and term and not trunc
    assert e.grader.calls[0][3] == 99


def test_time_limit():
    t = [0.0]
    e = Env(clock=lambda: t[0])
    e.step("calculator", expression="1")
    t[0] = 61.0
    obs, reward, term, trunc, info = e.step("calculator", expression="1")
    assert obs == {"kind": "final", "status": "time_limit", "steps_used": 1, "steps_remaining": 99}
    assert trunc and not term
    f, _, ep_id, steps = e.grader.calls[0]
    assert f == {"episode_id": ep_id, "findings": []} and steps == 1


def test_time_exactly_limit_ok():
    t = [0.0]
    e = Env(clock=lambda: t[0])
    t[0] = 60.0
    assert e.step("calculator", expression="1")[0]["kind"] == "result"


def test_records_are_copies(env):
    rec = env.step("get_record", table="ledger", id="L-7TBC4X1P")[0]["result"]["record"]
    rec["account"] = "HACKED"
    page = env.step("list_records", table="ledger")[0]["result"]["records"]
    page[0]["account"] = "HACKED"
    doc = env.step("get_document", doc_id="DOC-7C2K9W4M")[0]["result"]["document"]
    doc["line_items"].clear()
    assert env.step("get_record", table="ledger", id="L-7TBC4X1P")[0]["result"]["record"]["account"] == "2900"
    assert all(r["account"] != "HACKED" for r in env.step("list_records", table="ledger")[0]["result"]["records"])
    assert len(env.step("get_document", doc_id="DOC-7C2K9W4M")[0]["result"]["document"]["line_items"]) == 1
    init = env.core.initial_observation()
    init["manifest"]["episode_id"] = "X"
    assert env.core.initial_observation()["manifest"]["episode_id"] == "EP-8K2M4Q7X"
