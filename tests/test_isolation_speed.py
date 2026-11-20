"""Задача #24: изоляция, контракт и скорость среды через настоящий процесс env.server.

- эпизод на 2000+ строк книги проводок проходит целиком намного быстрее 60 с
  (в `python -m scripts.check --docker` тот же тест идёт при 2 CPU / 4 ГБ);
- агент не может изменить записи эпизода и состояние оценщика;
- граничные случаи протокола и завершения эпизода.
"""
import copy
import json
import os
import tempfile
import time
from pathlib import Path

import pytest

from env.client import AuditEnv, EnvError
from env.core import MAX_STEPS, PAGE_LIMIT, TABLES

ROOT = Path(__file__).resolve().parent.parent
EP_DIR = ROOT / "tests" / "fixtures" / "mini" / "EP-8K2M4Q7X"
GRADER = "tests.fake_grader:evaluate"
FINDINGS = {"episode_id": "EP-8K2M4Q7X", "findings": []}
LIST = {"tool": "list_records", "args": {"table": "ledger"}}
SUBMIT = {"tool": "submit", "args": {"findings": FINDINGS}}

BUDGET_S = 60.0  # лимит кейса на эпизод


@pytest.fixture
def tmp():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


def _log(runs_dir, run_id="r1"):
    p = Path(runs_dir) / run_id / "grader_log.jsonl"
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()]


def _fixture_env(tmp, **kw):
    opts = dict(episode=EP_DIR, grader=GRADER, run_id="r1", runs_dir=tmp / "runs")
    opts.update(kw)
    return AuditEnv(**opts)


def _big_config(mode="train"):
    """Конфиг генератора с книгой проводок на 2000 строк (максимум по кейсу)."""
    from generator.config import DEFAULT_CONFIG
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["size"] = {"ledger_rows": [2000, 2000], "documents": [200, 200]}
    cfg["mode"] = mode
    return cfg


# ---------------------------------------------------------------- скорость

def _read_everything(env):
    """Читает все таблицы постранично; возвращает (число шагов, totals по таблицам)."""
    steps, totals = 0, {}
    for table in TABLES:
        cursor = None
        while True:
            args = {"table": table, "limit": PAGE_LIMIT}
            if cursor:
                args["cursor"] = cursor
            obs, _, term, trunc, _ = env.step({"tool": "list_records", "args": args})
            steps += 1
            assert obs["kind"] == "result" and not (term or trunc)
            totals[table] = obs["result"]["total"]
            cursor = obs["result"]["next_cursor"]
            if cursor is None:
                break
    return steps, totals


def test_2000_rows_full_read_within_budget(tmp):
    """Генерация + проверка отпечатка + чтение всех таблиц + submit укладываются в лимит."""
    t0 = time.monotonic()
    with AuditEnv(run_id="r1", runs_dir=tmp / "runs") as env:
        obs, info = env.reset(7, _big_config())
        steps, totals = _read_everything(env)
        assert totals["ledger"] >= 2000
        assert steps < MAX_STEPS
        fin = {"episode_id": info["episode_id"], "findings": []}
        obs, _, term, _, _ = env.step({"tool": "submit", "args": {"findings": fin}})
        assert term and obs["status"] == "submitted"
    elapsed = time.monotonic() - t0
    # реально ~0.5 с; запас в 4 раза от лимита кейса на медленные машины и CI
    assert elapsed < BUDGET_S / 4, "эпизод 2000 строк: %.1f с" % elapsed
    rows = _log(tmp / "runs")
    assert len(rows) == 1 and rows[0]["valid"] is True


def test_2000_rows_rule_based_policy_within_budget(tmp):
    """Эвристическая политика решает эпизод на 2000 строк за лимит шагов и времени."""
    from policies import RuleBasedPolicy
    policy = RuleBasedPolicy()
    policy.reset()
    t0 = time.monotonic()
    with AuditEnv(run_id="r1", runs_dir=tmp / "runs") as env:
        obs, _ = env.reset(11, _big_config())
        term = trunc = False
        while not (term or trunc):
            obs, reward, term, trunc, info = env.step(policy.act(obs))
    elapsed = time.monotonic() - t0
    assert term and not trunc, "политика не успела отправить findings: %s" % obs.get("status")
    assert elapsed < BUDGET_S / 4, "эпизод 2000 строк: %.1f с" % elapsed
    row = _log(tmp / "runs")[0]
    assert row["status"] == "submitted" and row["valid"] is True
    assert row["steps_used"] <= MAX_STEPS


def test_generated_truth_removed_from_disk(tmp, monkeypatch):
    """Сгенерированный эпизод (с private/) удаляется с диска сразу после загрузки."""
    work = tmp / "child-tmp"
    work.mkdir()
    for var in ("TMPDIR", "TEMP", "TMP"):
        monkeypatch.setenv(var, str(work))  # временная папка дочернего процесса
    with AuditEnv(run_id="r1", runs_dir=tmp / "runs") as env:
        env.reset(3, {"mode": "train"})
        assert list(work.glob("auditgym-*")) == []
        env.step(LIST)
        assert list(work.glob("auditgym-*")) == []
    assert not list(work.rglob("truth*.json"))


# ------------------------------------------- агент не меняет записи и оценщик

def test_client_mutation_does_not_reach_server(tmp):
    """Изменение полученных данных на стороне агента не влияет на ответы среды."""
    with _fixture_env(tmp) as env:
        obs0, _ = env.reset(1, {"mode": "train"})
        first, *_ = env.step(LIST)
        snapshot = copy.deepcopy(first["result"]["records"])
        for r in first["result"]["records"]:
            for k in list(r):
                r[k] = "HACKED"
        obs0["manifest"]["episode_id"] = "EP-HACKED0"
        again, *_ = env.step(LIST)
        assert again["result"]["records"] == snapshot
        rid = snapshot[0]["row_id"]
        one, *_ = env.step({"tool": "get_record", "args": {"table": "ledger", "id": rid}})
        assert one["kind"] == "result" and one["result"]["record"] == snapshot[0]


@pytest.mark.parametrize("tool", ["update_record", "delete_record", "insert_record", "set_reward",
                                  "grade", "evaluate", "write_file", "__init__", "_finish",
                                  "_tool_list_records", "episode", "truth"])
def test_write_like_tools_are_unknown(tmp, tool):
    """Инструментов записи нет; внутренние имена ядра тоже недоступны."""
    with _fixture_env(tmp) as env:
        env.reset(1, {"mode": "train"})
        before, *_ = env.step(LIST)
        obs, reward, term, trunc, _ = env.step({"tool": tool, "args": {"table": "ledger", "id": "x"}})
        assert obs["kind"] == "error" and obs["error"]["code"] == "UNKNOWN_TOOL"
        assert (reward, term, trunc) == (0.0, False, False)
        after, *_ = env.step(LIST)
        assert after["result"]["records"] == before["result"]["records"]


@pytest.mark.parametrize("action", [
    {"tool": "submit", "args": {"findings": FINDINGS}, "reward": 1.0},
    {"tool": "list_records", "args": {"table": "ledger"}, "mode": "train"},
    {"tool": "list_records", "args": {"table": "ledger", "write": True}},
    {"tool": "get_record", "args": {"table": "ledger", "id": "x", "set": {"amount": 0}}},
])
def test_extra_fields_rejected(tmp, action):
    """Лишние поля в действии не проходят: ни награду, ни режим, ни запись передать нельзя."""
    with _fixture_env(tmp) as env:
        env.reset(1, {"mode": "train"})
        obs, reward, term, trunc, _ = env.step(action)
        assert obs["kind"] == "error" and obs["error"]["code"] in ("INVALID_ACTION", "INVALID_ARGS")
        assert (reward, term, trunc) == (0.0, False, False)
    assert _log(tmp / "runs") == []


@pytest.mark.parametrize("doc_id", ["../private/truth.json", "private/truth.json", "..\\private\\truth.json",
                                    "/etc/passwd", "C:\\Windows\\win.ini", "truth", ""])
def test_document_paths_not_followed(tmp, doc_id):
    """get_document ищет только по ID в памяти: пути к файлам не открываются."""
    with _fixture_env(tmp) as env:
        env.reset(1, {"mode": "train"})
        obs, *_ = env.step({"tool": "get_document", "args": {"doc_id": doc_id}})
        assert obs["kind"] == "error" and obs["error"]["code"] == "NOT_FOUND"
        assert "issues" not in json.dumps(obs, ensure_ascii=False)


def test_second_submit_cannot_rewrite_grade(tmp):
    """После submit оценка зафиксирована: повторный submit и шаги не меняют журнал."""
    with _fixture_env(tmp) as env:
        env.reset(1, {"mode": "train"})
        obs, reward, term, _, _ = env.step(SUBMIT)
        assert term
        for action in (SUBMIT, LIST, {"tool": "submit", "args": {"findings": {"x": 1}}}):
            o, r, t, tr, _ = env.step(action)
            assert o["error"]["code"] == "EPISODE_FINISHED" and r == 0.0 and t
    rows = _log(tmp / "runs")
    assert len(rows) == 1 and rows[0]["reward"] == reward


def test_config_cannot_redirect_episode_grader_or_log(tmp):
    """Ключи config не подменяют эпизод, оценщик, run_id и папку журнала."""
    evil = tmp / "evil"
    cfg = {"mode": "train", "episode": str(evil), "grader": "tests.fake_grader:evaluate_fd_noise",
           "run_id": "evil", "runs_dir": str(evil), "truth": {"issues": []}, "reward": 1.0}
    with _fixture_env(tmp) as env:
        obs, info = env.reset(1, cfg)
        assert info["episode_id"] == "EP-8K2M4Q7X"
        env.step(SUBMIT)
    assert not evil.exists()
    assert not (tmp / "runs" / "evil").exists()
    assert len(_log(tmp / "runs")) == 1


@pytest.mark.parametrize("msg", [
    {"cmd": "grade"}, {"cmd": "set_reward", "reward": 1.0}, {"cmd": "load", "path": "x"},
    {"cmd": "step", "action": LIST, "reward": 1.0}, {"cmd": "reset", "seed": 1, "config": {"mode": "train"},
                                                     "episode": "x"},
    {"cmd": "close", "force": True},
])
def test_unknown_protocol_commands(tmp, msg):
    """Посторонние команды и лишние поля протокола дают BAD_REQUEST, сервер продолжает работать."""
    with _fixture_env(tmp) as env:
        env.reset(1, {"mode": "train"})
        with pytest.raises(EnvError) as e:
            env._call(msg)
        assert e.value.code == "BAD_REQUEST"
        obs, *_ = env.step(LIST)
        assert obs["kind"] == "result" and obs["steps_used"] == 1


# ---------------------------------------------------- завершение и сбросы

def test_step_limit_through_server(tmp):
    """Лимит шагов: на шаге 100 эпизод обрезается, в журнале одна строка step_limit."""
    with _fixture_env(tmp) as env:
        env.reset(1, {"mode": "train"})
        for i in range(1, MAX_STEPS + 1):
            obs, reward, term, trunc, _ = env.step(LIST)
            if i < MAX_STEPS:
                assert not (term or trunc)
        assert trunc and not term and obs["status"] == "step_limit"
        o, *_ = env.step(SUBMIT)
        assert o["error"]["code"] == "EPISODE_FINISHED"
    rows = _log(tmp / "runs")
    assert len(rows) == 1 and rows[0]["status"] == "step_limit" and rows[0]["steps_used"] == MAX_STEPS


def test_reset_mid_episode_starts_clean(tmp):
    """reset посреди эпизода: счётчики с нуля, брошенный эпизод не попадает в журнал."""
    with _fixture_env(tmp) as env:
        env.reset(1, {"mode": "train"})
        for _ in range(5):
            env.step(LIST)
        obs, _ = env.reset(2, {"mode": "eval"})
        assert obs["steps_used"] == 0 and obs["steps_remaining"] == MAX_STEPS
        assert _log(tmp / "runs") == []
        o, reward, term, _, info = env.step(SUBMIT)
        assert o["steps_used"] == 1 and reward == 0.0 and set(info) == {"episode_id", "mode"}
    rows = _log(tmp / "runs")
    assert len(rows) == 1 and rows[0]["mode"] == "eval" and rows[0]["steps_used"] == 1


def test_two_generated_episodes_share_no_state(tmp):
    """Два reset подряд в одном процессе дают те же данные, что и свежие процессы."""
    def first_page(env, seed):
        env.reset(seed, {"mode": "train"})
        obs, *_ = env.step(LIST)
        return obs["result"]

    with AuditEnv(run_id="a", runs_dir=tmp / "runs") as env:
        a1 = first_page(env, 5)
        b1 = first_page(env, 6)
        a2 = first_page(env, 5)
    with AuditEnv(run_id="b", runs_dir=tmp / "runs") as env:
        b_fresh = first_page(env, 6)
    assert a1 == a2
    assert b1 == b_fresh
    assert a1 != b1


def test_server_survives_bad_input_burst(tmp):
    """Поток мусорных действий не роняет сервер и честно тратит шаги."""
    bad = [None, 1, "x", [], {}, {"tool": 1, "args": {}}, {"tool": "list_records"},
           {"tool": "list_records", "args": []}, {"tool": "list_records", "args": {"table": "ledger", "limit": 10**9}},
           {"tool": "list_records", "args": {"table": "ledger", "cursor": "c-1"}},
           {"tool": "calculator", "args": {"op": "__import__('os')"}},
           {"tool": "get_record", "args": {"table": "../private", "id": "x"}}]
    with _fixture_env(tmp) as env:
        env.reset(1, {"mode": "train"})
        for i, a in enumerate(bad, 1):
            obs, reward, term, trunc, _ = env.step(a)
            assert obs["kind"] == "error" and obs["steps_used"] == i
            assert (reward, term, trunc) == (0.0, False, False)
        obs, *_ = env.step(SUBMIT)
        assert obs["kind"] == "final" and obs["steps_used"] == len(bad) + 1
