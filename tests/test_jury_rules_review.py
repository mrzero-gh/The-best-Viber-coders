"""Ревью по правилам жюри (fintech-checks.txt, 21.11): фикстуры жюри с собственными ID, τ из policy,
одинаковые ID в разных таблицах, вариант payment_allocation, config адаптера в формате reset."""
import json
import shutil
from pathlib import Path

import pytest

import judge_adapter as ja
from env.client import AuditEnv
from env.episode import apply_policy_tolerance, load_truth
from generator.build import generate_episode
from generator.config import ConfigError
from grader.evaluate import evaluate

ROOT = Path(__file__).resolve().parent.parent
MINI = ROOT / "tests" / "fixtures" / "mini" / "EP-8K2M4Q7X"


def _rw(path, fn):
    data = json.loads(path.read_text(encoding="utf-8"))
    data = fn(data) or data
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _answer(truth, amount_delta=0, affected=None):
    i = truth["issues"][0]
    return {"episode_id": truth["episode_id"], "findings": [
        {"type": i["type"], "affected_ids": affected or list(i["affected_ids"]),
         "claimed_amount_minor": i["expected_amount_minor"] + amount_delta, "currency": i["currency"],
         "evidence_ids": list(i["evidence_sets"][0]), "explanation": "x"}]}


@pytest.fixture
def fixture_ep(tmp_path, monkeypatch):
    """Копия мини-эпизода; журнал score_episode - во временной папке."""
    monkeypatch.setenv("AUDIT_GYM_RUNS_DIR", str(tmp_path / "runs"))
    ep = tmp_path / "ep"
    shutil.copytree(MINI, ep)
    return ep


# ---------- 1. ID жюри произвольного вида (блок 3 собирается вручную: A, R1, D1)

def test_hand_written_fixture_ids_are_accepted(fixture_ep):
    def rename(d):
        d["episode_id"] = "fixture-C"
    _rw(fixture_ep / "public" / "manifest.json", rename)

    def truth(d):
        rename(d)
        d["issues"][0]["issue_id"] = "A"
    _rw(fixture_ep / "private" / "truth.json", truth)
    t = load_truth(fixture_ep)
    assert ja.score_episode(str(fixture_ep), _answer(t), 0)["tp"] == 1
    # и через среду: reset + submit без EPISODE_INVALID
    with AuditEnv(episode=str(fixture_ep), trust_episode=True, runs_dir=fixture_ep.parent / "r") as env:
        env.reset(0, {"mode": "train"})
        obs, reward, term, *_ = env.step({"tool": "submit", "args": {"findings": _answer(t)}})
    assert obs["status"] == "submitted" and term and reward > 0


# ---------- 8. Одинаковые строки ID в разных таблицах после смены ID жюри

def test_same_id_string_in_two_tables_is_a_set():
    truth = {"schema_version": "1.0", "episode_id": "E", "distractors": [], "issues": [
        {"issue_id": "A", "type": "payment_allocation", "affected_ids": ["X1", "X1"],
         "expected_amount_minor": 100, "currency": "KZT", "amount_tolerance_minor": 0,
         "evidence_sets": [["D1"]], "scenario": None}]}
    res = evaluate(_answer(truth, affected=["X1"]), truth, "E", 0)
    assert res["valid"] and res["metrics"]["tp"] == 1


# ---------- 2. Допуск суммы: по типам и с учётом опубликованного в policy

def test_generated_tolerance_by_type_and_published(tmp_path):
    ep = generate_episode(3, {}, tmp_path)
    policy = json.loads((ep / "public" / "policy.json").read_text(encoding="utf-8"))
    assert policy["amount_tolerance_minor_by_type"] == {
        "duplicate_invoice": 0, "payment_allocation": 1, "fx_conversion": 1, "period_cutoff": 0}
    truth = json.loads((ep / "private" / "truth.json").read_text(encoding="utf-8"))
    for i in truth["issues"]:
        assert i["amount_tolerance_minor"] == policy["amount_tolerance_minor_by_type"][i["type"]]


def test_policy_tolerance_zero_makes_off_by_one_fp(fixture_ep):
    """Тест 8 и M07 жюри: τ=0 опубликован в policy, сумма на 1 больше -> FP, даже если в truth 1."""
    _rw(fixture_ep / "public" / "policy.json", lambda d: d.update(amount_tolerance_minor=0))
    t = load_truth(fixture_ep)
    assert t["issues"][0]["amount_tolerance_minor"] == 0
    r = ja.score_episode(str(fixture_ep), _answer(t, amount_delta=1), 0)
    assert (r["tp"], r["fp"], r["fn"], r["return"]) == (0, 1, 1, -1.0)


def test_m08_published_tolerance_one_is_inclusive(fixture_ep):
    t = load_truth(fixture_ep)  # в truth и policy мини-эпизода τ=1
    assert ja.score_episode(str(fixture_ep), _answer(t, amount_delta=1), 0)["tp"] == 1
    assert ja.score_episode(str(fixture_ep), _answer(t, amount_delta=2), 0)["tp"] == 0


def test_by_type_cap_and_truth_cap():
    truth = {"issues": [{"type": "fx_conversion", "amount_tolerance_minor": 1},
                        {"type": "duplicate_invoice", "amount_tolerance_minor": 1},
                        {"type": "period_cutoff", "amount_tolerance_minor": 0}]}
    policy = {"amount_tolerance_minor": 1,
              "amount_tolerance_minor_by_type": {"fx_conversion": 1, "duplicate_invoice": 0, "period_cutoff": 5}}
    out = apply_policy_tolerance(truth, policy)
    assert [i["amount_tolerance_minor"] for i in out["issues"]] == [1, 0, 0]
    # bool и отрицательные значения policy не применяются; битый truth не трогается
    same = apply_policy_tolerance({"issues": [{"type": "x", "amount_tolerance_minor": 1}]},
                                  {"amount_tolerance_minor": True, "amount_tolerance_minor_by_type": {"x": -1}})
    assert same["issues"][0]["amount_tolerance_minor"] == 1
    assert apply_policy_tolerance([], policy) == []


# ---------- 4. D09–D11: пропуск и частичное распределение настраиваются отдельно

def _allocations(ep):
    truth = json.loads((ep / "private" / "truth.json").read_text(encoding="utf-8"))
    prov = json.loads((ep / "private" / "provenance.json").read_text(encoding="utf-8"))
    ids = {i["issue_id"] for i in truth["issues"] if i["type"] == "payment_allocation" and i["scenario"] is None}
    return [c["after"] for m in prov["mutations"] if m["issue_id"] in ids
            for c in m["changes"] if str(c["field"]).startswith("allocated_minor")]


@pytest.mark.parametrize("variant", ["missing", "partial"])
def test_payment_allocation_variant(tmp_path, variant):
    cfg = {"errors": {"payment_allocation": {"enabled": True, "count": [2, 2], "variant": variant}}}
    got = []
    for seed in range(3):
        got += _allocations(generate_episode(seed, cfg, tmp_path / str(seed)))
    assert got
    assert all(a == 0 for a in got) if variant == "missing" else all(a > 0 for a in got)


def test_payment_allocation_variant_validated(tmp_path):
    with pytest.raises(ConfigError):
        generate_episode(1, {"errors": {"payment_allocation": {"enabled": True, "variant": "half"}}}, tmp_path)
    with pytest.raises(ConfigError):  # variant есть только у payment_allocation
        generate_episode(1, {"errors": {"fx_conversion": {"enabled": True, "variant": "missing"}}}, tmp_path)


# ---------- 5. generate_episode принимает config в формате reset

def test_generate_episode_accepts_reset_config(tmp_path):
    ja.generate_episode(1, {"mode": "closed", "team_config": {"size": {"ledger_rows": [300, 400]}},
                            "episode_dir": "ignored"}, str(tmp_path))
    man = json.loads((tmp_path / "public" / "manifest.json").read_text(encoding="utf-8"))
    assert 300 <= man["counts"]["ledger_rows"] <= 400
