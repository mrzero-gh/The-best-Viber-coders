"""Адаптер жюри (judge_adapter.py) по определениям проверок FinTech.

Номера C01-C20 - контрактные тесты жюри; ниже их воспроизведение на нашем эпизоде.
Плюс score_episode (блок 3) и подача изменённой копии эпизода (блок 4, парные проверки).
"""
import json
import random
import shutil
import tempfile
from pathlib import Path

import pytest

import judge_adapter as ja
from env.client import AuditEnv, EnvError
from env.core import MAX_STEPS

SEED = 1


@pytest.fixture(scope="module")
def episode():
    """Эпизод, сгенерированный через generate_episode адаптера, во временном каталоге «жюри»."""
    d = tempfile.mkdtemp(prefix="judge-ep-")
    ep = Path(d) / "episode"
    ja.generate_episode(SEED, {}, str(ep))
    yield ep
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def env(monkeypatch):
    runs = Path(tempfile.mkdtemp(prefix="judge-runs-"))
    monkeypatch.setenv("AUDIT_GYM_RUNS_DIR", str(runs))
    e = ja.make_env()
    yield e
    e.close()
    shutil.rmtree(runs, ignore_errors=True)


def closed(ep):
    return {"mode": "closed", "episode_dir": str(ep), "team_config": {}}


def truth(ep):
    return json.loads((Path(ep) / "private" / "truth.json").read_text(encoding="utf-8"))


def oracle_findings(ep):
    t = truth(ep)
    return {"episode_id": t["episode_id"], "findings": [
        {"type": i["type"], "affected_ids": list(i["affected_ids"]),
         "claimed_amount_minor": i["expected_amount_minor"], "currency": i["currency"],
         "evidence_ids": sorted(i["evidence_sets"][0]), "explanation": "oracle"}
        for i in t["issues"]]}


def act(name, **args):
    return {"name": name, "args": args}


def walk(env, table):
    """Проходит таблицу по курсорам (первый cursor = null)."""
    rows, cursor, steps = [], None, 0
    while True:
        obs, *_ = env.step(act("list_records", table=table, cursor=cursor, limit=50))
        steps += 1
        assert obs["error"] is None
        page = obs["result"]["records"]
        assert len(page) <= 50
        rows += page
        cursor = obs["result"]["next_cursor"]
        if cursor is None:
            return rows, steps


# ---------------------------------------------------------------- генерация

def test_generate_episode_layout_and_determinism(episode):
    """generate_episode пишет public/ и private/ прямо в output_dir и воспроизводим по seed."""
    pub = {p.name for p in (episode / "public").iterdir()}
    assert pub == {"manifest.json", "ledger.csv", "invoices.jsonl", "payments.jsonl", "fx.csv", "policy.json"}
    assert {"truth.json", "provenance.json"} <= {p.name for p in (episode / "private").iterdir()}
    assert [p.name for p in episode.iterdir() if p.name.startswith(".gen-")] == []
    with tempfile.TemporaryDirectory() as d:
        again = Path(d) / "ep"
        ja.generate_episode(SEED, {"mode": "closed"}, str(again))
        for name in pub:
            assert (again / "public" / name).read_bytes() == (episode / "public" / name).read_bytes()


# ---------------------------------------------------------------- C01-C04

def test_c01_c02_reset_pair_and_first_observation(env, episode):
    obs, info = env.reset(0, closed(episode))
    json.dumps([obs, info])  # JSON-совместимы
    assert set(obs) == {"task", "manifest", "policy", "remaining_steps"}
    assert obs["remaining_steps"] == MAX_STEPS and isinstance(obs["task"], str)
    assert info == {"episode_id": obs["manifest"]["episode_id"], "mode": "closed"}
    text = json.dumps([obs, info], ensure_ascii=False)
    assert "seed" not in text and "issue_id" not in text and "evidence_sets" not in text


def test_c03_manifest_matches_data(env, episode):
    obs, _ = env.reset(0, closed(episode))
    m = obs["manifest"]
    for key in ("schema_version", "episode_id", "base_currency", "period_start", "period_end",
                "timezone", "counts", "rounding"):
        assert key in m
    ledger, _ = walk(env, "ledger")
    invoices, _ = walk(env, "invoices")
    payments, _ = walk(env, "payments")
    fx, _ = walk(env, "fx")
    assert m["counts"]["ledger_rows"] == len(ledger)
    assert m["counts"]["invoices"] == len(invoices)
    assert m["counts"]["payments"] == len(payments)
    assert m["counts"]["fx_rates"] == len(fx)


def test_c04_step_types(env, episode):
    env.reset(0, closed(episode))
    obs, reward, term, trunc, info = env.step(act("list_records", table="ledger", cursor=None, limit=50))
    json.dumps([obs, reward, info])
    assert type(term) is bool and type(trunc) is bool and isinstance(reward, float)
    assert set(obs) == {"result", "error", "remaining_steps"} and obs["remaining_steps"] == MAX_STEPS - 1


# ---------------------------------------------------------------- C05-C08

def test_c05_c06_pagination_complete_and_stable(env, episode):
    env.reset(0, closed(episode))
    rows, steps = walk(env, "ledger")
    ids = [r["row_id"] for r in rows]
    assert len(ids) == len(set(ids)) and len(ids) > 100 and steps >= 3
    a, *_ = env.step(act("list_records", table="ledger", cursor="c50", limit=50))
    b, *_ = env.step(act("list_records", table="ledger", cursor="c50", limit=50))
    assert a["result"] == b["result"] and a["remaining_steps"] == b["remaining_steps"] + 1


def test_c07_c08_get_record_and_document(env, episode):
    env.reset(0, closed(episode))
    first, *_ = env.step(act("list_records", table="ledger", cursor=None, limit=1))
    row = first["result"]["records"][0]
    got, *_ = env.step(act("get_record", table="ledger", id=row["row_id"]))
    assert got["error"] is None and got["result"] == row
    inv, *_ = env.step(act("list_records", table="invoices", cursor=None, limit=1))
    doc_id = inv["result"]["records"][0]["doc_id"]
    doc, *_ = env.step(act("get_document", doc_id=doc_id))
    assert doc["error"] is None and doc["result"]["doc_id"] == doc_id


# ---------------------------------------------------------------- C09-C14

def test_c09_c10_calculator(env, episode):
    env.reset(0, closed(episode))
    ok, *_ = env.step(act("calculator", expression="0.1+0.2"))
    assert ok == {"result": "0.3", "error": None, "remaining_steps": MAX_STEPS - 1}
    for expr in ("open('/etc/passwd').read()", "__import__('os').system('id')", "1; import os"):
        bad, *_ = env.step(act("calculator", expression=expr))
        assert bad["result"] is None and bad["error"]["code"] in ("CALC_ERROR", "INVALID_ARGS")
        assert "root" not in json.dumps(bad)


def test_c11_c12_c13_c14_errors_cost_a_step(env, episode):
    env.reset(0, closed(episode))
    u = "L-ZZZZZZZZ"
    o1, *_ = env.step(act("get_record", table="ledger", id=u))
    o2, *_ = env.step(act("get_document", doc_id="DOC-ZZZZZZZZ"))
    o3, *_ = env.step(act("delete_everything"))
    o4, *_ = env.step({"tool": None})
    o5, *_ = env.step(act("list_records", table="ledger", cursor=None, limit=1))
    assert o1["error"]["code"] == "NOT_FOUND" and o2["error"]["code"] == "NOT_FOUND"
    assert o3["error"]["code"] == "UNKNOWN_TOOL" and o4["error"]["code"] == "INVALID_ACTION"
    assert [o["remaining_steps"] for o in (o1, o2, o3, o4, o5)] == [99, 98, 97, 96, 95]
    assert o5["error"] is None and len(o5["result"]["records"]) == 1


# ---------------------------------------------------------------- C15-C17, C19

def test_c15_submit_is_final(env, episode):
    obs, info = env.reset(0, closed(episode))
    fin = oracle_findings(episode)
    o, r, term, trunc, i = env.step(act("submit", findings=fin))
    assert o == {"result": {"accepted": True}, "error": None, "remaining_steps": MAX_STEPS - 1}
    assert term and not trunc and r == 0.0 and i == info
    for a in (act("submit", findings={"episode_id": fin["episode_id"], "findings": []}),
              act("list_records", table="ledger", cursor=None, limit=1)):
        o, r, term, trunc, _ = env.step(a)
        assert o["error"]["code"] == "EPISODE_FINISHED" and r == 0.0 and term


def test_c16_submit_on_action_100(env, episode):
    env.reset(0, closed(episode))
    for _ in range(MAX_STEPS - 1):
        o, r, term, trunc, _ = env.step(act("calculator", expression="1+1"))
        assert not (term or trunc)
    o, r, term, trunc, _ = env.step(act("submit", findings=oracle_findings(episode)))
    assert term and not trunc and o["result"] == {"accepted": True} and o["remaining_steps"] == 0


def test_c17_100_actions_without_submit(env, episode):
    env.reset(0, closed(episode))
    for i in range(MAX_STEPS):
        o, r, term, trunc, _ = env.step(act("calculator", expression="1+1"))
    assert trunc and not term and o["error"]["code"] == "STEP_LIMIT" and r == 0.0
    o, *_ = env.step(act("submit", findings=oracle_findings(episode)))
    assert o["error"]["code"] == "EPISODE_FINISHED"


def test_c19_closed_mode_hides_reward(env, episode):
    _, info = env.reset(0, closed(episode))
    rewards, infos = [], []
    for _ in range(3):
        _, r, _, _, i = env.step(act("list_records", table="ledger", cursor=None, limit=5))
        rewards.append(r)
        infos.append(i)
    o, r, term, _, i = env.step(act("submit", findings=oracle_findings(episode)))
    assert term and rewards + [r] == [0.0] * 4
    assert all(x == {"episode_id": info["episode_id"], "mode": "closed"} for x in infos + [i])


def test_train_mode_generates_and_rewards(env):
    """Открытый режим без episode_dir: генератор команды с seed и team_config; награда в конце."""
    obs, info = env.reset(5, {"mode": "train", "team_config": {"max_errors": 2}})
    assert info["mode"] == "train"
    fin = {"episode_id": info["episode_id"], "findings": []}
    o, r, term, _, i = env.step(act("submit", findings=fin))
    assert term and o["result"] == {"accepted": True} and r == -1.0
    assert set(i) == {"episode_id", "mode"}  # ответы оценщика не в info


def test_invalid_submit_is_bounded_error(env, episode):
    _, info = env.reset(0, closed(episode))
    bad = {"episode_id": info["episode_id"], "findings": [{"type": "x"}]}
    o, r, term, _, _ = env.step(act("submit", findings=bad))
    assert term and o["result"] is None and o["error"]["code"] == "INVALID_FINDINGS" and r == 0.0


# ---------------------------------------------------------------- C20, повтор, изоляция

def test_c20_no_truth_in_policy_channel(env, episode):
    obs, info = env.reset(0, closed(episode))
    seen = [obs, info]
    for table in ja.TABLES:
        rows, _ = walk(env, table)
        seen.append(rows)
    for a in (act("get_record", table="ledger", id="../private/truth.json"),
              act("get_document", doc_id="../private/truth.json"),
              act("calculator", expression="open('private/truth.json')")):
        seen.append(env.step(a)[0])
    seen.append(env.step(act("submit", findings=oracle_findings(episode))))
    text = json.dumps(seen, ensure_ascii=False)
    t = truth(episode)
    for issue in t["issues"]:
        assert issue["issue_id"] not in text
    for word in ("evidence_sets", "expected_amount_minor", "provenance", "tp", "f1"):
        assert '"%s"' % word not in text


def test_repeat_reset_restarts_same_episode(env, episode):
    o1, i1 = env.reset(0, closed(episode))
    a, *_ = env.step(act("list_records", table="ledger", cursor=None, limit=10))
    o2, i2 = env.reset(0, closed(episode))
    assert (o1, i1) == (o2, i2) and o2["remaining_steps"] == MAX_STEPS
    b, *_ = env.step(act("list_records", table="ledger", cursor=None, limit=10))
    assert a == b


def test_step_before_reset(env):
    with pytest.raises(EnvError) as e:
        env.step(act("list_records", table="ledger"))
    assert e.value.code == "NO_EPISODE"


def test_bad_mode_rejected(env, episode):
    with pytest.raises(ValueError):
        env.reset(0, {"mode": "god", "episode_dir": str(episode)})


# ---------------------------------------------------------------- score_episode (блок 3)

def test_score_oracle_and_empty(episode):
    fin = oracle_findings(episode)
    n = len(fin["findings"])
    s = ja.score_episode(str(episode), fin, 10)
    assert set(s) == {"tp", "fp", "fn", "precision", "recall", "f1", "return"}
    assert (s["tp"], s["fp"], s["fn"], s["f1"]) == (n, 0, 0, 1.0)
    assert s["return"] == pytest.approx(0.98, abs=1e-9)
    empty = ja.score_episode(str(episode), {"episode_id": fin["episode_id"], "findings": []}, 10)
    assert (empty["tp"], empty["fn"], empty["return"]) == (0, n, -1.0)


def test_score_invalid_structure(episode):
    fin = oracle_findings(episode)
    f = fin["findings"][0]
    cases = [
        {"episode_id": fin["episode_id"], "findings": [f] * 31},                    # 31 вывод
        {"episode_id": fin["episode_id"], "findings": [dict(f, evidence_ids=["E%d" % i for i in range(11)])]},
        {"episode_id": fin["episode_id"], "findings": [dict(f, explanation="a" * 1001)]},
        {"episode_id": fin["episode_id"], "findings": [dict(f, claimed_amount_minor=True)]},
        {"episode_id": fin["episode_id"], "findings": [dict(f, claimed_amount_minor="10000")]},
    ]
    for c in cases:
        s = ja.score_episode(str(episode), c, 10)
        assert s["return"] == -1.0 and s["tp"] is None and s["f1"] is None


# ---------------------------------------------------------------- парные проверки (блок 4)

def _shuffled_copy(ep, dest, seed=0):
    """Копия эпизода с переставленными строками ledger.csv (как делает жюри)."""
    shutil.copytree(ep, dest)
    path = dest / "public" / "ledger.csv"
    text = path.read_text(encoding="utf-8")
    header, *rows = text.splitlines()
    random.Random(seed).shuffle(rows)
    path.write_text("\n".join([header] + rows) + "\n", encoding="utf-8", newline="\n")
    return dest


def test_pair_permuted_copy_loads_and_scores_same(env, episode):
    with tempfile.TemporaryDirectory() as d:
        copy = _shuffled_copy(episode, Path(d) / "perm")
        # без режима доверия наш сервер считает такую копию повреждённой
        with AuditEnv(episode=copy, runs_dir=Path(d) / "runs", run_id="x") as strict:
            with pytest.raises(EnvError) as e:
                strict.reset(0, {"mode": "eval"})
            assert e.value.code == "EPISODE_INVALID"
        # через адаптер жюри копия грузится как есть
        o_src, _ = env.reset(0, closed(episode))
        src_rows, _ = walk(env, "ledger")
        o_cp, _ = env.reset(0, closed(copy))
        cp_rows, _ = walk(env, "ledger")
        assert o_src["manifest"] == o_cp["manifest"]
        assert sorted(src_rows, key=lambda r: r["row_id"]) == sorted(cp_rows, key=lambda r: r["row_id"])
        assert [r["row_id"] for r in src_rows] != [r["row_id"] for r in cp_rows]
        fin = oracle_findings(episode)
        assert ja.score_episode(str(episode), fin, 7) == ja.score_episode(str(copy), fin, 7)


def test_pair_source_replay_is_exact(env, episode):
    """Повтор на исходном эпизоде точно воспроизводит переходы."""
    def run():
        out = [env.reset(0, closed(episode))]
        for a in (act("list_records", table="payments", cursor=None, limit=3),
                  act("calculator", expression="2*3"), act("get_record", table="fx", id="nope"),
                  act("submit", findings=oracle_findings(episode))):
            out.append(env.step(a))
        return out
    assert run() == run()
